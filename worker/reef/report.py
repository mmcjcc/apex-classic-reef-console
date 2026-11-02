"""The weekly report: one page on how the tank did, made by the worker once a week (site.yaml report) and on request by
reef-web (a preview of the last 7 days). Everything in it is read from the database: alerts, the analysis' findings,
each probe's week against the week before, the water tests and which are due, energy per EnergyBar, program changes,
the journal, how well the Apex answered, and the outdoor temperature when it is known.

The report is sections of plain lines (the page, MCP and mail show them as they are) and a one-line headline."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .programs import diff, summary
from .site import Site

WEEK = 7 * 86400


def _day(ts: float, zone: str) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone(ZoneInfo(zone)).strftime("%a %b %-d")


def _fmt(value: float, unit: str) -> str:
    digits = 2 if unit == "pH" else 0 if unit == "mV" else 1
    shown = f"{value:.{digits}f}"
    return shown if unit in ("pH", "") else f"{shown} {unit}"


def _kv(db: sqlite3.Connection, key: str) -> Any:
    row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return None if row is None else json.loads(row[0])


def _stats(db: sqlite3.Connection, name: str, start: float, end: float) -> tuple[float, float, float, int] | None:
    row = db.execute(
        "SELECT MIN(r.value), AVG(r.value), MAX(r.value), COUNT(*) FROM readings r JOIN sensors s ON s.id = r.sensor_id "
        "WHERE s.name = ? AND r.ts >= ? AND r.ts < ?",
        (name, int(start), int(end)),
    ).fetchone()
    return None if row is None or not row[3] else (float(row[0]), float(row[1]), float(row[2]), int(row[3]))


def energy(db: sqlite3.Connection, site: Site, start: float, end: float) -> list[dict[str, Any]]:
    """Per EnergyBar: its mean current over the period, kWh per day, and the cost per month at the site's tariff."""
    out = []
    for m in site.modules:
        if not m.current:
            continue
        stats = _stats(db, m.current, start, end)
        if stats is None:
            continue
        kwh_day = stats[1] * site.extras.volts * 24 / 1000
        price = site.extras.price_per_kwh
        out.append({"module": m.id, "amps": round(stats[1], 2), "kwh_per_day": round(kwh_day, 2),
                    "cost_per_month": None if price is None else round(kwh_day * 30.4 * price, 2)})  # fmt: skip
    return out


def build(db: sqlite3.Connection, site: Site, end: float, now: float) -> dict[str, Any]:
    """The report for the 7 days before `end`: {period_start, period_end, headline, sections: [{title, lines}]}."""
    start, zone = end - WEEK, site.timezone
    sections: list[dict[str, Any]] = []
    head: list[str] = []

    # alerts
    texts = {c.id: c.text for c in site.conditions}
    started = db.execute(
        "SELECT subject, COUNT(*) FROM events WHERE kind = 'condition.start' AND ts >= ? AND ts < ? GROUP BY subject "
        "ORDER BY COUNT(*) DESC",
        (start, end),
    ).fetchall()
    lines = [f"{texts.get(cid, cid)}: {n} time{'s' if n > 1 else ''}" for cid, n in started] or ["None."]
    sections.append({"title": "Alerts", "lines": lines})
    count = sum(n for _, n in started)
    head.append(f"{count} alert{'s' if count != 1 else ''}" if count else "no alerts")

    # the analysis
    analysis = _kv(db, "analysis") or {}
    findings = [f for f in analysis.get("findings", []) if (f.get("end") or end) >= start]
    lines = [f["summary"] for f in findings[:10]] or ["Nothing out of the ordinary."]
    if len(findings) > 10:
        lines.append(f"... and {len(findings) - 10} more on the Analysis page.")
    sections.append({"title": "Analysis", "lines": lines})
    if findings:
        head.append(f"{len(findings)} finding{'s' if len(findings) != 1 else ''}")

    # probes: this week against the week before
    lines = []
    for name, sensor in site.sensors.items():
        if not sensor.active or sensor.external or sensor.unit in ("switch", "A"):
            continue
        week, before = _stats(db, name, start, end), _stats(db, name, start - WEEK, start)
        if week is None:
            continue
        line = f"{sensor.label}: {_fmt(week[0], sensor.unit)} to {_fmt(week[2], sensor.unit)}, mean {_fmt(week[1], sensor.unit)}"
        if before is not None:
            line += f" (the week before: {_fmt(before[1], sensor.unit)})"
        lines.append(line)
    sections.append({"title": "Probes", "lines": lines or ["No readings."]})

    # water tests: this week's, and which are due
    rows = db.execute(
        "SELECT measured_at, parameter, value, unit, kit FROM water_tests WHERE deleted_at IS NULL AND measured_at >= ? "
        "AND measured_at < ? ORDER BY measured_at",
        (start, end),
    ).fetchall()
    lines = [
        f"{_day(ts, zone)}: {p} {v:g} {u if u != 'pH' else ''}".rstrip() + (f" ({k})" if k else "") for ts, p, v, u, k in rows
    ]
    due = []
    for parameter, days in site.extras.tests_due.items():
        last = db.execute(
            "SELECT MAX(measured_at) FROM water_tests WHERE deleted_at IS NULL AND parameter = ?", (parameter,)
        ).fetchone()[0]
        if last is None or now - last > days * 86400:
            due.append(parameter if last is None else f"{parameter} (last {_day(last, zone)})")
    lines.append("Due: " + ", ".join(due) + "." if due else "None due.")
    sections.append({"title": "Water tests", "lines": lines if rows else ["None this week.", lines[-1]]})
    if due:
        head.append("test due: " + ", ".join(d.split(" ")[0] for d in due))

    # energy
    bars = energy(db, site, start, end)
    lines = [
        f"{b['module']}: {b['amps']:.2f} A on average, {b['kwh_per_day']:.1f} kWh a day"
        + (f", about ${b['cost_per_month']:.2f} a month" if b["cost_per_month"] is not None else "")
        for b in bars
    ]
    if bars:
        total = sum(b["kwh_per_day"] for b in bars)
        lines.append(f"All bars: {total:.1f} kWh a day" + (f", about ${total * 30.4 * site.extras.price_per_kwh:.2f} a month"
                                                          if site.extras.price_per_kwh is not None else ""))  # fmt: skip
    sections.append({"title": "Energy", "lines": lines or ["No current readings."]})

    # outdoors
    outdoor = next((n for n, s in site.sensors.items() if s.external), None)
    stats = _stats(db, outdoor, start, end) if outdoor else None
    if stats is not None:
        tank = _stats(db, "Tmp", start, end)
        out_unit = site.sensors[outdoor].unit
        tank_unit = site.sensors["Tmp"].unit if "Tmp" in site.sensors else out_unit
        line = f"Outdoors {_fmt(stats[0], out_unit)} to {_fmt(stats[2], out_unit)}"
        if tank is not None:
            line += f"; the tank {_fmt(tank[0], tank_unit)} to {_fmt(tank[2], tank_unit)}"
        sections.append({"title": "Weather", "lines": [line]})

    # programs and the journal
    versions = db.execute(
        "SELECT id, ts, programs FROM program_versions WHERE ts >= ? AND ts < ? ORDER BY id", (start, end)
    ).fetchall()
    lines = []
    for vid, ts, raw in versions:
        prev = db.execute("SELECT programs FROM program_versions WHERE id < ? ORDER BY id DESC LIMIT 1", (vid,)).fetchone()
        if prev is not None:
            lines.append(f"{_day(ts, zone)}: {summary(diff(json.loads(prev[0]), json.loads(raw)))}")
    sections.append({"title": "Apex programs", "lines": lines or ["Unchanged."]})
    if lines:
        head.append("programs changed")
    entries = db.execute(
        "SELECT ts, kind, text FROM journal WHERE deleted_at IS NULL AND ts >= ? AND ts < ? ORDER BY ts", (start, end)
    ).fetchall()
    sections.append({"title": "Journal", "lines": [f"{_day(ts, zone)}: {kind}, {text}" for ts, kind, text in entries]
                     or ["Nothing written."]})  # fmt: skip

    # the Apex itself
    # polls are counted from when polling began (the days before came from the unit's 10-minute log)
    polled_from = max(start, float(_kv(db, "polling_since") or end))
    readings = _stats(db, "Tmp", polled_from, end)
    expected = (end - polled_from) / 60
    lines = []
    if readings is not None and expected >= 60:
        lines.append(f"Answered about {min(100.0, 100 * readings[3] / expected):.0f} % of the minute polls.")
    power = db.execute("SELECT detail FROM events WHERE kind = 'apex.power' AND ts >= ? AND ts < ?", (start, end)).fetchall()
    lines += [f"Power: {d}" for (d,) in power] or ["No power failure reported."]
    sections.append({"title": "The Apex", "lines": lines})

    headline = "; ".join(head) or "a quiet week"
    return {"period_start": start, "period_end": end, "headline": headline[0].upper() + headline[1:], "sections": sections}


def text(report: dict[str, Any], zone: str) -> str:
    """The report as plain text: a title, then each section."""
    lines = [f"Aquarium, the week of {_day(report['period_start'], zone)} to {_day(report['period_end'] - 1, zone)}",
             report["headline"], ""]  # fmt: skip
    for section in report["sections"]:
        lines.append(section["title"])
        lines += [f"  {line}" for line in section["lines"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def due_end(now: float, site: Site, last_end: float | None) -> float | None:
    """The end of the week a report is due for (local midnight of the report day), or None: on the report day, from its
    hour, once per week."""
    local = datetime.fromtimestamp(now, UTC).astimezone(ZoneInfo(site.timezone))
    if local.weekday() != site.extras.report_weekday or local.hour < site.extras.report_hour:
        return None
    end = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return None if last_end is not None and last_end >= end else end
