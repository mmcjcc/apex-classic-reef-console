"""The calcium reactor's CO2 output per day (shared by the Reactor page and the worker's analysis), and the warning
that its effluent drip is slowing.

A reactor fed through a small needle valve slowly loses flow: it gets too slow, or stops, after a while. With less
water through the reactor, less CO2 keeps the effluent in its pH band, so the CO2's minutes a day fall while the
band is unchanged: the last 3 full days against the 3 before, 30 % less or more, each of the last 3 below the
earlier average, counted outside the program's night window (whose own band is set apart, CO2Night) and only since
the program's effluent band lines last changed. Shown and mailed once; never paged."""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .site import Site

DRIP_DROP = 0.30  # the CO2's daytime minutes falling this much, 3 days against the 3 before
DRIP_MIN_MINUTES = 30.0  # below this a day's CO2 time says too little


class Offsets:
    """The Apex's timezone setting over time (the unit seen here follows daylight time: -3.00 became -4.00 when US
    daylight time ended in 2026), from the
    worker's record of it (kv apex_offsets: [[since, hours], ...]); else the latest poll's for all times; else UTC."""

    def __init__(self, db: sqlite3.Connection) -> None:
        row = db.execute("SELECT value FROM kv WHERE key = 'apex_offsets'").fetchone()
        changes = [(float(since), float(hours)) for since, hours in json.loads(row[0])] if row else []
        if not changes:
            row = db.execute("SELECT value FROM kv WHERE key = 'latest'").fetchone()
            try:
                changes = [(0.0, float(json.loads(row[0]).get("timezone") or 0) if row else 0.0)]
            except ValueError:
                changes = [(0.0, 0.0)]
        self.starts, self.hours = [since for since, _ in changes], [hours for _, hours in changes]

    def at(self, ts: float) -> float:
        return self.hours[max(bisect_right(self.starts, ts) - 1, 0)]


def _in_night(ts: float, night: tuple[str, str] | None, offsets: Offsets) -> bool:
    if night is None:
        return False
    apex = datetime.fromtimestamp(ts + offsets.at(ts) * 3600, UTC)
    minute = apex.hour * 60 + apex.minute
    start, end = (int(t[:2]) * 60 + int(t[3:]) for t in night)
    return start <= minute < end if start < end else minute >= start or minute < end


def days(db: sqlite3.Connection, site: Site, since: float, now: float) -> list[dict[str, Any]]:
    """Per civil day: CO2 minutes (all, in the night window), switchings, effluent pH mean by day and by night."""
    r = site.reactor
    if r is None:
        return []
    zone, offsets = ZoneInfo(site.timezone), Offsets(db)

    def day_of(ts: float) -> str:
        return datetime.fromtimestamp(ts, UTC).astimezone(zone).strftime("%Y-%m-%d")

    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"co2_minutes": 0.0, "co2_night_minutes": 0.0, "switchings": 0,
                                                          "ph_day": [], "ph_night": []})  # fmt: skip
    states = db.execute(
        "SELECT x.ts, x.energized FROM outlet_states x JOIN outlets o ON o.id = x.outlet_id WHERE o.name = ? ORDER BY x.ts",
        (r.co2,),
    ).fetchall()
    previous = None
    for (t0, on), (t1, _) in zip(states, [*states[1:], (now, None)], strict=False):
        changed, previous = previous is not None and bool(on) != previous, bool(on)
        if t1 <= since:
            continue
        t = max(float(t0), since)
        if float(t0) >= since and changed:
            rows[day_of(t0)]["switchings"] += 1
        while on and t < t1:  # a minute at a time: the day and the night window are counted exactly enough
            step = min(60.0, t1 - t)
            row = rows[day_of(t)]
            row["co2_minutes"] += step / 60
            if _in_night(t, r.night_apex, offsets):
                row["co2_night_minutes"] += step / 60
            t += step
    for ts, value in db.execute(
        "SELECT r.ts, r.value FROM readings r JOIN sensors s ON s.id = r.sensor_id WHERE s.name = ? AND r.ts >= ?",
        (r.effluent, int(since)),
    ):
        rows[day_of(ts)]["ph_night" if _in_night(ts, r.night_apex, offsets) else "ph_day"].append(value)
    first_poll = states[0][0] if states else now
    out = []
    for day in sorted(rows):
        row = rows[day]
        mean = lambda xs: round(sum(xs) / len(xs), 2) if xs else None  # noqa: E731
        known = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone).timestamp() + 86400 > first_poll
        out.append({"day": day, "co2_minutes": round(row["co2_minutes"]) if known else None,
                    "co2_night_minutes": round(row["co2_night_minutes"]) if known else None,
                    "switchings": row["switchings"] if known else None,
                    "effluent_day": mean(row["ph_day"]), "effluent_night": mean(row["ph_night"])})  # fmt: skip
    return out


def band_changed_at(db: sqlite3.Connection, site: Site) -> float | None:
    """When the CO2 program's lines on the effluent probe last changed (the daily program copies), or None."""
    r = site.reactor
    if r is None:
        return None
    last, changed = None, None
    for ts, raw in db.execute("SELECT ts, programs FROM program_versions ORDER BY id"):
        lines = sorted(line for line in json.loads(raw).get(r.co2, []) if r.effluent in line)
        if last is not None and lines != last:
            changed = float(ts)
        last = lines
    return changed


def drip_finding(site: Site, day_rows: list[dict[str, Any]], band_since: float | None, now: float) -> dict[str, Any] | None:
    """The "effluent drip slowing?" finding, or None. day_rows: days() up to now (today, incomplete, is left out)."""
    r = site.reactor
    if r is None:
        return None
    zone = ZoneInfo(site.timezone)
    today = datetime.fromtimestamp(now, UTC).astimezone(zone).strftime("%Y-%m-%d")
    since_day = datetime.fromtimestamp(band_since, UTC).astimezone(zone).strftime("%Y-%m-%d") if band_since else ""
    daytime = [(d["day"], d["co2_minutes"] - (d["co2_night_minutes"] or 0)) for d in day_rows
               if d["co2_minutes"] is not None and since_day < d["day"] < today]  # fmt: skip
    if len(daytime) < 6:
        return None
    before = [m for _, m in daytime[-6:-3]]
    lately = [m for _, m in daytime[-3:]]
    mean_before, mean_lately = sum(before) / 3, sum(lately) / 3
    if mean_before < DRIP_MIN_MINUTES or mean_lately > (1 - DRIP_DROP) * mean_before or max(lately) >= mean_before:
        return None
    drop = 1 - mean_lately / mean_before
    first = datetime.strptime(daytime[-3][0], "%Y-%m-%d").replace(tzinfo=zone).timestamp()
    return {
        "kind": "drip", "sensor": r.co2, "start": first, "end": None,
        "summary": f"Effluent drip slowing? The CO2 ran {mean_lately:.0f} min a day outside the night window over the last "
                   f"3 days, against {mean_before:.0f} the 3 days before ({drop:.0%} less) with its pH band unchanged: "
                   "check the reactor's effluent drip",
        "evidence": {"lately": [round(m) for m in lately], "before": [round(m) for m in before], "drop": round(drop, 2),
                     "days": [d for d, _ in daytime[-6:]]},
    }  # fmt: skip
