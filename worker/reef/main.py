"""The reef worker: polls the Apex once a minute, keeps the history, judges the alert conditions and publishes them
for Prometheus (port 9106). Start: python -m reef.main

Settings come from the environment: REEF_SITE_FILE (config/site.yaml), REEF_DATA_DIR (the SQLite file and its
nightly copies), REEF_SECRET_FILE (the Apex sign-in: a CSV file, "ip, username, password"), or instead REEF_APEX_USER and
REEF_APEX_PASSWORD with the address from the site file, REEF_METRICS_PORT."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from prometheus_client import REGISTRY, start_http_server

from .analysis import WINDOW_DAYS, analyse
from .apex import ApexClient, ApexError, Credential, Status, module_of, read_credential
from .conditions import Engine, module_check
from .metrics import Metrics
from .programs import diff, summary
from .reactor import band_changed_at, drip_finding
from .reactor import days as reactor_days
from .report import build as build_report
from .report import due_end
from .site import Site, load_site
from .store import Store
from .weather import SERIAL as WEATHER_SERIAL
from .weather import fetch as fetch_weather

log = logging.getLogger("reef")
KEEP_BACKUPS = 14
PROGRAM_EVERY_SECONDS = 86400
CLOCK_STEP_SECONDS = 300  # a controller clock this far from the worker's is reported (the history uses the worker's)
FAST_STATUS_SECONDS = 2.0  # the unit's own log is asked for only while status answers come faster than this
LOG_PAUSE_SECONDS = 12 * 3600  # after an error or a slow answer: no more log requests tonight
COVERED_STEPS = 140  # of 144 ten-minute steps: a day the polls cover needs nothing from the unit's log
ANALYSIS_EVERY_SECONDS = 3600
OUTLOG_EVERY_SECONDS = 3600  # today's outlet changes, once an hour
OUTLOG_SPACING_SECONDS = 60  # past days, in the quiet hours


def read_outlets(path: Path, now: float) -> list[tuple[float, str, str, bool, str]]:
    """Every outlet switching over the analysis window, (ts, name, type, energized, did): the unit's outlet log (only
    the outputs it is set to log, exact times) and the switchings the minute polls saw (every output, since polling
    began), the same switching once."""
    since = int(now - WINDOW_DAYS * 86400)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        logged = db.execute(
            "SELECT l.ts, o.name, o.type, l.energized, o.did FROM outlet_log l JOIN outlets o ON o.id = l.outlet_id "
            "WHERE l.ts >= ? ORDER BY l.ts",
            (since,),
        ).fetchall()
        polled = db.execute(
            "SELECT x.ts, o.name, o.type, x.energized, o.did FROM outlet_states x JOIN outlets o ON o.id = x.outlet_id "
            "ORDER BY o.name, x.ts"
        ).fetchall()
    finally:
        db.close()
    out = [(float(ts), str(name), str(kind), bool(on), str(did)) for ts, name, kind, on, did in logged]
    seen: dict[tuple[str, bool], list[float]] = {}
    for ts, name, _, on, _ in out:
        seen.setdefault((name, on), []).append(ts)
    previous: dict[str, bool] = {}
    for ts, name, kind, on, did in polled:
        before = previous.get(name)
        previous[name] = bool(on)
        if before is None or before == bool(on) or ts < since:
            continue  # the first state polled is not a switching; nor is a change of mode alone
        if any(abs(t - ts) <= 120 for t in seen.get((name, bool(on)), [])):
            continue  # the outlet log has it, with its exact time
        out.append((float(ts), str(name), str(kind), bool(on), str(did)))
    return sorted(out)


def read_tests(path: Path, now: float) -> list[tuple[float, str, float]]:
    """The manual water tests over the analysis window, (measured_at, parameter, value); reef-web writes them."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        rows = db.execute(
            "SELECT measured_at, parameter, value FROM water_tests WHERE deleted_at IS NULL AND measured_at >= ?",
            (now - WINDOW_DAYS * 86400,),
        ).fetchall()
        return [(float(ts), str(parameter), float(value)) for ts, parameter, value in rows]
    finally:
        db.close()


def read_journal(path: Path, now: float) -> list[tuple[float, str, str]]:
    """The journal over the analysis window, (ts, kind, text); reef-web writes it."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        rows = db.execute(
            "SELECT ts, kind, text FROM journal WHERE deleted_at IS NULL AND ts >= ? ORDER BY ts", (now - WINDOW_DAYS * 86400,)
        ).fetchall()
        return [(float(ts), str(kind), str(text)) for ts, kind, text in rows]
    finally:
        db.close()


def read_history(path: Path, site: Site, now: float) -> dict[str, list[tuple[float, float]]]:
    """Every analysed probe's readings over the analysis window, from its own connection (it runs in a thread)."""
    names = [n for n, s in site.sensors.items() if s.active and s.unit != "switch" and not s.external]
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        history: dict[str, list[tuple[float, float]]] = {}
        for name in names:
            rows = db.execute(
                "SELECT r.ts, r.value FROM readings r JOIN sensors s ON s.id = r.sensor_id WHERE s.name = ? AND r.ts >= ? "
                "ORDER BY r.ts",
                (name, int(now - WINDOW_DAYS * 86400)),
            ).fetchall()
            history[name] = [(float(ts), float(v)) for ts, v in rows]
        return history
    finally:
        db.close()


def reactor_check(path: Path, site: Site, now: float) -> dict[str, Any] | None:
    """The reactor's "effluent drip slowing?" finding over the last two weeks, from its own read connection."""
    if site.reactor is None:
        return None
    start = datetime.fromtimestamp(now - 14 * 86400, ZoneInfo(site.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        return drip_finding(site, reactor_days(db, site, start.timestamp(), now), band_changed_at(db, site), now)
    finally:
        db.close()


def make_report(path: Path, site: Site, end: float, now: float) -> dict[str, Any]:
    """The weekly report, from its own read connection (it runs in a thread)."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        return build_report(db, site, end, now)
    finally:
        db.close()


def finding_key(finding: dict[str, Any]) -> str:
    """The same finding on the next run has the same key (unusual spells are matched by overlap instead): a frozen
    probe by its sensor, a drift or a recurring departure by its direction, a level step by its day."""
    kind, sensor, evidence = finding["kind"], finding["sensor"], finding["evidence"]
    if kind == "drift":
        return f"drift:{sensor}:{'up' if evidence['slope_per_day'] > 0 else 'down'}"
    if kind == "recurring":
        return f"recurring:{sensor}:{'up' if evidence['size'] > 0 else 'down'}"
    if kind in ("frozen", "drip"):
        return f"{kind}:{sensor}"
    if kind == "equipment":
        return f"equipment:{evidence['output']}:{'less' if abs(evidence['lately']) < abs(evidence['before']) else 'more'}"
    if kind == "probe":
        return f"probe:{sensor}:{'above' if sum(evidence['offsets']) > 0 else 'below'}"
    return f"{kind}:{sensor}:{int(finding['start'] or 0) // 86400}"


class Worker:
    def __init__(
        self,
        site: Site,
        store: Store,
        client: ApexClient,
        metrics: Metrics,
        engine: Engine,
        data_dir: Path,
        weather: httpx.AsyncClient | None = None,
    ) -> None:
        self.site, self.store, self.client, self.metrics, self.engine = site, store, client, metrics, engine
        self.weather = weather  # Open-Meteo, when the site has a location
        self.next_weather = 0.0
        self.metrics.report = store.latest_report()
        self.data_dir = data_dir
        self.known = {m.address for m in site.modules} | {"base"}
        self.serial = ""  # the controller's serial, from the latest poll
        self.unit_offset: float | None = None  # the unit's timezone setting, hours (-3.0 in summer, -4.0 in winter)
        self.offset_seen: float | None = None
        self.latency: float | None = None  # the latest status answer's
        self.next_log_request = 0.0
        self.next_outlog_today = 0.0
        self.next_outlog_past = 0.0
        if store.kv_get("polling_since") is None:
            row = store.db.execute("SELECT MIN(ts) FROM readings").fetchone()
            if row and row[0] is not None:
                store.kv_set("polling_since", float(row[0]))
        old = store.kv_get("programs")
        if old is not None:  # before 2026-10-12 the programs were one kv entry: it becomes the first version
            if store.latest_programs() is None:
                serial = (store.kv_get("latest") or {}).get("serial", "")
                store.save_programs(float(store.kv_get("programs_at", 0) or 0), serial, old)
            store.kv_delete("programs")

    def notice(self, status: Status) -> None:
        """One-off facts worth an event: a power failure the Apex reports, a new module, a clock that is off."""
        if status.power_restored and status.power_restored != self.store.kv_get("power_restored"):
            failed = datetime.fromtimestamp(status.power_failed or 0, UTC).isoformat()
            restored = datetime.fromtimestamp(status.power_restored, UTC).isoformat()
            self.store.event(status.received_at, "apex.power", "apex", f"power failed {failed}, restored {restored}")
            self.store.kv_set("power_restored", status.power_restored)
        seen = {module_of(d) for d in [i.did for i in status.inputs.values()] + [o.did for o in status.outputs.values()]}
        for address in sorted(a for a in seen if a.isdigit() and a not in self.known):
            self.store.event(
                status.received_at,
                "module.new",
                address,
                f"a module at AquaBus address {address} reports and is not in site.yaml",
            )
            log.warning("a module at AquaBus address %s reports and is not in site.yaml", address)
            self.known.add(address)
        offset = status.clock_offset()
        if (
            offset is not None
            and abs(offset) > CLOCK_STEP_SECONDS
            and self.store.kv_get("clock_noted") != int(status.received_at // 86400)
        ):
            self.store.event(status.received_at, "apex.clock", "apex", f"the Apex's clock is {offset:+.0f} s off")
            self.store.kv_set("clock_noted", int(status.received_at // 86400))

    def notify(self, status: Status) -> None:
        """One-time notices (site.yaml notices): a switch that came into its state and held it is announced once, as
        an event and a metric Alertmanager mails; it is armed again only after the switch was the other way for a while.
        A switch already in its state when the worker starts is not announced (a restart is not news)."""
        state = self.store.kv_get("notices") or {}
        changed = False
        for n in self.site.notices:
            reading = status.inputs.get(n.sensor)
            if reading is None:
                continue
            inside = reading.closed if n.when == "closed" else not reading.closed
            before = state.get(n.id)
            # a copy: the state is saved only when it differs from what was stored
            mine = dict(before) if before else {"armed": not inside, "since": status.received_at, "inside": inside, "sent": None}
            if mine["inside"] != inside:
                mine["inside"], mine["since"] = inside, status.received_at
            held = status.received_at - mine["since"]
            if inside and mine["armed"] and held >= n.hold:
                mine["armed"], mine["sent"] = False, status.received_at
                self.store.event(status.received_at, "notice", n.id, n.text)
                log.info("notice %s: %s", n.id, n.text)
            elif not inside and not mine["armed"] and held >= n.rearm:
                mine["armed"] = True
            if mine["sent"]:
                self.metrics.notices[n.id] = (mine["sent"], n.text)
            if before != mine:
                state[n.id], changed = mine, True
        if changed:
            self.store.kv_set("notices", state)

    def snapshot(self, status: Status) -> dict[str, Any]:
        """The latest poll as the pages show it (kv "latest")."""
        modules = {}
        for m in self.site.modules:
            connected, why = module_check(m, status)
            amps = status.inputs[m.current].value if m.current and m.current in status.inputs else None
            modules[m.id] = {"kind": m.kind, "address": m.address, "connected": connected, "why": why, "amps": amps}
        return {
            "received_at": status.received_at,
            "latency": status.latency,
            "controller_time": status.controller_time,
            "timezone": status.timezone,
            "serial": status.serial,
            "software": status.software,
            "feed_active": status.feed_active,
            "power_failed": status.power_failed,
            "power_restored": status.power_restored,
            "inputs": [vars(i) for i in status.inputs.values()],
            "outputs": [vars(o) | {"manual": o.manual, "energized": o.energized} for o in status.outputs.values()],
            "modules": modules,
        }

    def condition_states(self) -> list[dict[str, Any]]:
        reported = {c.id for c, _ in self.engine.active()}
        out = []
        for c in self.site.conditions:
            state = self.engine.states[c.id]
            out.append(
                {
                    "id": c.id,
                    "alarm": c.alarm,
                    "severity": c.severity,
                    "subject": c.subject,
                    "text": c.text,
                    "active": c.id in reported,
                    "held": state.active,
                    "since": state.since,
                    "detail": state.detail,
                }
            )
        return out

    async def poll_once(self) -> Status | None:
        try:
            status = await self.client.status()
        except ApexError as exc:
            self.metrics.failed()
            self.store.kv_set("poll", {"ok": False, "at": time.time(), "error": str(exc)})
            log.warning("Apex poll failed: %s", exc)
            return None
        self.metrics.update(status)
        self.serial, self.latency = status.serial, status.latency
        try:
            self.unit_offset = float(status.timezone)
        except ValueError:
            self.unit_offset = None
        self.note_offset(status.received_at)
        if self.store.kv_get("polling_since") is None:
            self.store.kv_set("polling_since", status.received_at)
        self.store.record(status, self.site)
        changes = self.engine.evaluate(status, status.received_at)
        for change in changes:
            kind = "condition.start" if change.active else "condition.end"
            self.store.event(status.received_at, kind, change.condition.id, change.detail)
            log.info("%s %s: %s (%s)", kind, change.condition.id, change.condition.text, change.detail)
        if changes:
            self.store.save_active({cid: s.since or status.received_at for cid, s in self.engine.states.items() if s.active})
        self.notice(status)
        self.store.kv_set("latest", self.snapshot(status))
        self.store.kv_set("conditions", self.condition_states())
        self.notify(status)
        self.store.kv_set("poll", {"ok": True, "at": status.received_at, "error": None})
        return status

    async def refresh_programs(self, now: float) -> None:
        """Once a day: the outputs' programs, for the pages (the lines an override ignores) and their history: a new
        version and an event only when they changed."""
        if now - float(self.store.kv_get("programs_at", 0) or 0) < PROGRAM_EVERY_SECONDS:
            return
        try:
            programs = await self.client.programs()
        except ApexError as exc:
            log.warning("program.xml not read: %s", exc)
            self.store.kv_set("programs_at", now - PROGRAM_EVERY_SECONDS + 3600)  # try again in an hour
            return
        latest = self.store.latest_programs()
        if self.store.save_programs(now, self.serial, programs) and latest is not None:
            self.store.event(now, "apex.program", "apex", "programs changed: " + summary(diff(latest[1], programs)))
        self.store.kv_set("programs_at", now)

    def next_log_day(self, now: float) -> tuple[date, float, float] | None:
        """The oldest of the unit's days (its own calendar) still to fetch: (day, its start and end in UTC)."""
        if self.unit_offset is None:
            return None
        offset = self.unit_offset * 3600
        today = datetime.fromtimestamp(now + offset, UTC).date()
        polling_since = float(self.store.kv_get("polling_since", now) or now)
        for back in range(self.site.datalog.days_back, 0, -1):
            day = today - timedelta(days=back)
            if self.store.log_day(day.isoformat()) is not None:
                continue
            start = datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() - offset
            end = start + 86400
            if end > now - 600 or (end <= polling_since and not self.site.datalog.backfill):
                continue
            if self.store.covered_steps(start, end) >= COVERED_STEPS:
                self.store.save_log_day(day.isoformat(), now, 0, 0, "covered by polls")
                continue
            return day, start, end
        return None

    async def fill_gaps(self, now: float) -> None:
        """At most one day of the unit's own log per call, in the quiet hours, minutes apart, only while it answers
        fast; the first error or slow answer ends tonight's requests (design section 2)."""
        cfg = self.site.datalog
        hour = datetime.fromtimestamp(now, ZoneInfo(self.site.timezone)).hour
        if not cfg.in_hours(hour) or now < self.next_log_request or self.client.waiting() > 0:
            return
        if self.latency is None or self.latency > FAST_STATUS_SECONDS:
            return
        found = self.next_log_day(now)
        if found is None:
            self.report_backfill(now)
            return
        day, _start, end = found
        self.next_log_request = now + cfg.spacing
        try:
            serial, records = await self.client.datalog(day.strftime("%y%m%d"))
        except ApexError as exc:
            self.next_log_request = now + LOG_PAUSE_SECONDS
            self.store.event(now, "history.paused", day.isoformat(), f"the unit's log was not read, no more tonight: {exc}")
            log.warning("datalog for %s not read, pausing until tomorrow night: %s", day, exc)
            return
        inserted = self.store.fill_from_log(serial or self.serial, records, self.site)
        self.store.save_log_day(day.isoformat(), now, len(records), inserted, "ok" if records else "empty")
        if self.client.failures:  # the answer was slow: the breaker is open, and tonight is over
            self.next_log_request = now + LOG_PAUSE_SECONDS
        log.info("datalog %s: %d records, %d readings stored", day, len(records), inserted)
        if inserted and end > float(self.store.kv_get("polling_since", now) or now):
            self.store.event(now, "history.fill", day.isoformat(), f"{inserted} readings from the unit's own log fill a gap")

    def report_backfill(self, now: float) -> None:
        """Once the days before polling are all fetched: one event with the totals."""
        if not self.site.datalog.backfill or self.store.kv_get("backfill_reported"):
            return
        polling_since = float(self.store.kv_get("polling_since", now) or now)
        offset = (self.unit_offset or 0.0) * 3600
        first_polled_day = datetime.fromtimestamp(polling_since + offset, UTC).date().isoformat()
        days, readings, oldest = self.store.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(inserted), 0), MIN(day) FROM datalog_days WHERE inserted > 0 AND day <= ?",
            (first_polled_day,),
        ).fetchone()
        if days:
            detail = f"{readings} readings for {days} days from {oldest}, from the unit's own log"
            self.store.event(now, "history.backfill", "apex", detail)
        self.store.kv_set("backfill_reported", now)

    def note_offset(self, ts: float) -> None:
        """Keep the unit's timezone setting over time (kv apex_offsets, [[since, hours], ...]; the first from the start
        of time), so its own clock can be read for any past moment; a change is an event."""
        if self.unit_offset is None or self.unit_offset == self.offset_seen:
            return
        offsets = self.store.kv_get("apex_offsets", []) or []
        if offsets and offsets[-1][1] != self.unit_offset:
            self.store.event(ts, "apex.timezone", "apex",
                             f"the Apex's timezone setting changed from {offsets[-1][1]:+.2f} to {self.unit_offset:+.2f} "
                             "hours: its time-based programs run by that clock")  # fmt: skip
        if not offsets or offsets[-1][1] != self.unit_offset:
            self.store.kv_set("apex_offsets", [*offsets, [ts if offsets else 0.0, self.unit_offset]])
        self.offset_seen = self.unit_offset

    def unit_day(self, ts: float) -> date | None:
        """The unit's own calendar day of a time (its timezone setting, -3.00 here)."""
        return None if self.unit_offset is None else datetime.fromtimestamp(ts + self.unit_offset * 3600, UTC).date()

    async def refresh_outlog(self, now: float) -> None:
        """The unit's outlet log: today's day once an hour; past days (the month it holds) once each, in the quiet
        hours, a minute apart, oldest first. An error waits an hour."""
        today = self.unit_day(now)
        if today is None or self.client.waiting() > 0 or self.latency is None or self.latency > FAST_STATUS_SECONDS:
            return
        day: date | None = None
        if now >= self.next_outlog_today:
            day, self.next_outlog_today = today, now + OUTLOG_EVERY_SECONDS
        elif now >= self.next_outlog_past and self.site.datalog.in_hours(
            datetime.fromtimestamp(now, ZoneInfo(self.site.timezone)).hour
        ):
            for back in range(self.site.datalog.days_back, 0, -1):
                candidate = today - timedelta(days=back)
                if self.store.outlog_day(candidate.isoformat()) is None:
                    day = candidate
                    break
            self.next_outlog_past = now + OUTLOG_SPACING_SECONDS
        if day is None:
            return
        try:
            serial, changes = await self.client.outlog(day.strftime("%y%m%d"))
        except ApexError as exc:
            self.next_outlog_today = self.next_outlog_past = now + OUTLOG_EVERY_SECONDS
            log.warning("outlog for %s not read: %s", day, exc)
            return
        inserted = self.store.fill_outlog(serial or self.serial, changes)
        if day != today:
            self.store.save_outlog_day(day.isoformat(), now, len(changes))
        if inserted:
            log.info("outlog %s: %d changes, %d new", day, len(changes), inserted)

    async def refresh_analysis(self, now: float) -> None:
        """Once an hour: the analysis of the probe history (reef.analysis), kept for the pages and MCP; a finding not
        seen before becomes an event (shown, never paged)."""
        if now - float(self.store.kv_get("analysis_at", 0) or 0) < ANALYSIS_EVERY_SECONDS:
            return
        self.store.kv_set("analysis_at", now)
        history = await asyncio.to_thread(read_history, self.store.path, self.site, now)
        outlets = await asyncio.to_thread(read_outlets, self.store.path, now)
        journal = await asyncio.to_thread(read_journal, self.store.path, now)
        tests = await asyncio.to_thread(read_tests, self.store.path, now)
        complete = self.store.kv_get("polling_since")
        result = await asyncio.to_thread(analyse, self.site, history, now, outlets, journal, tests, complete)
        drip = await asyncio.to_thread(reactor_check, self.store.path, self.site, now)
        if drip is not None:
            result["findings"].insert(0, drip)
        self.store.kv_set("analysis", result)
        seen = set(self.store.kv_get("analysis_seen", []) or [])
        current = {finding_key(f): f for f in result["findings"] if f["kind"] != "unusual"}
        for key in sorted(set(current) - seen):
            finding = current[key]
            self.store.event(now, f"analysis.{finding['kind']}", finding["sensor"], finding["summary"])
            if finding["kind"] == "drip":  # also mailed once (the ReefNotice alert)
                self.metrics.notices["effluent-drip"] = (now, finding["summary"])
        self.store.kv_set("analysis_seen", sorted(current))
        # unusual spells: announced once, matched by overlap (a spell's start moves as the window slides past it)
        spells = [s for s in self.store.kv_get("analysis_spells", []) or [] if s["end"] >= now - 8 * 86400]
        for finding in (f for f in result["findings"] if f["kind"] == "unusual"):
            if any(
                s["sensor"] == finding["sensor"] and s["start"] < finding["end"] and finding["start"] < s["end"] for s in spells
            ):
                continue
            spells.append({"sensor": finding["sensor"], "start": finding["start"], "end": finding["end"]})
            self.store.event(now, "analysis.unusual", finding["sensor"], finding["summary"])
        self.store.kv_set("analysis_spells", spells)
        log.info("analysis: %d findings over %d sensors", len(result["findings"]), len(result["sensors"]))

    async def refresh_report(self, now: float) -> None:
        """The weekly report on its day and hour (site.yaml report): kept, announced as an event, and published as a
        metric whose headline Alertmanager mails once."""
        end = due_end(now, self.site, self.store.kv_get("report_end"))
        if end is None:
            return
        self.store.kv_set("report_end", end)
        report = await asyncio.to_thread(make_report, self.store.path, self.site, end, now)
        report_id = self.store.save_report(now, report)
        self.store.event(now, "report.weekly", str(report_id), report["headline"])
        self.metrics.report = (now, report["headline"])
        log.info("weekly report %d: %s", report_id, report["headline"])

    async def refresh_weather(self, now: float) -> None:
        """Once an hour, the outdoor temperature (Open-Meteo); the first time, the archive's weeks before."""
        extras = self.site.extras
        name = next((n for n, s in self.site.sensors.items() if s.external), None)
        if self.weather is None or name is None or extras.latitude is None or extras.longitude is None:
            return
        if now < self.next_weather:
            return
        self.next_weather = now + 3600
        backfill = 0 if self.store.kv_get("weather_backfilled") else self.site.datalog.days_back
        try:
            celsius = self.site.sensors[name].unit == "°C"  # the external sensor's unit in the site file
            rows = await fetch_weather(self.weather, extras.latitude, extras.longitude, now, backfill, celsius)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("outdoor temperature not read: %s", str(exc)[:200])
            return
        inserted = self.store.fill_external(WEATHER_SERIAL, "outdoor", name, "Temp", rows)
        if backfill:
            self.store.kv_set("weather_backfilled", now)
            self.next_weather = now  # the last two days next
        if inserted:
            log.info("outdoor temperature: %d hourly values", inserted)

    def nightly_copy(self, now: float) -> None:
        day = datetime.fromtimestamp(now, UTC).strftime("%Y%m%d")
        folder = self.data_dir / "backups"
        target = folder / f"reef-{day}.db"
        if target.exists():
            return
        folder.mkdir(exist_ok=True)
        self.store.backup(target)
        for old in sorted(folder.glob("reef-*.db"))[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
        log.info("copied the database to %s", target.name)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            started = time.time()
            try:
                if await self.poll_once() is not None:
                    await self.refresh_programs(time.time())
                    await self.fill_gaps(time.time())
                    await self.refresh_outlog(time.time())
                    await self.refresh_analysis(time.time())
                    await self.refresh_report(time.time())
                    await self.refresh_weather(time.time())
                self.nightly_copy(time.time())
            except Exception:  # noqa: BLE001 - one bad pass (a full disk, an odd answer) must not end the polling
                log.exception("poll pass failed")
            wait = max(self.site.poll_seconds - (time.time() - started), self.client.waiting(), 1.0)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=wait)


def read_secret(site: Site) -> Credential:
    """The Apex sign-in: REEF_APEX_USER and REEF_APEX_PASSWORD with the site file's address, else the secret file."""
    user, password = os.environ.get("REEF_APEX_USER", ""), os.environ.get("REEF_APEX_PASSWORD", "")
    if user and password:
        if not site.address:
            raise ApexError("REEF_APEX_USER needs apex.address in the site file")
        return Credential(site.address, user, password)
    return read_credential(Path(os.environ.get("REEF_SECRET_FILE", "/run/secrets/apex")).read_text(encoding="utf-8"))


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll otherwise
    site = load_site(Path(os.environ.get("REEF_SITE_FILE", "/app/config/site.yaml")))
    data_dir = Path(os.environ.get("REEF_DATA_DIR", "/data"))
    credential = read_secret(site)
    if site.address and credential.address != site.address:
        log.warning("the secret file names %s, site.yaml %s: using the secret file's", credential.address, site.address)
    store = Store(data_dir / "reef.db")
    engine = Engine(site)
    engine.restore(store.load_active())
    metrics = Metrics(site, engine)
    REGISTRY.register(metrics)  # type: ignore[arg-type]
    start_http_server(int(os.environ.get("REEF_METRICS_PORT", "9106")))
    client = ApexClient(credential)
    has_location = site.extras.latitude is not None and site.extras.longitude is not None
    weather = httpx.AsyncClient(timeout=httpx.Timeout(20.0), headers={"User-Agent": "reef-console"}) if has_location else None
    log.info(
        "reef worker started: polling %s every %.0f s, %d conditions", credential.address, site.poll_seconds, len(site.conditions)
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await Worker(site, store, client, metrics, engine, data_dir, weather).run(stop)
    finally:
        await client.close()
        if weather is not None:
            await weather.aclose()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
