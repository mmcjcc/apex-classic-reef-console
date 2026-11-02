"""Analysis of the probe history: what each probe is expected to read, and what departs from it (design section 6).

Plain statistics, no model to trust blindly: every finding carries its evidence, and nothing here pages anyone.
For each probe, on 10-minute steps over the last weeks:
- expected value = trend + daily profile. The trend is the median of the 24 hours around each hour (of the readings
  without their daily cycle, so the newest hours are not pulled by it); the daily profile is, for each 10-minute slot
  of the day, the median of (reading - trend) over the window. The slots are counted in
  UTC. The Apex's schedules run on its own clock, which can follow daylight time (the unit seen here does): then they
  move one UTC hour twice a year, and the profile takes a week or two to settle after the change.
- spread = 1.4826 x the median absolute residual (a standard deviation that ignores outliers), never below the probe's
  resolution.
Findings:
- unusual: the reading departs from the expected value by 4 spreads or more, and by at least the unit's smallest
  departure worth mentioning (0.5 °F or 0.3 °C, 0.15 pH, 25 mV, 0.5 ppt: a day-to-day pH or ORP
  swing of 0.1 or 20 mV is ordinary), for 30 minutes or more (last 7 days).
- drift: the daily medians of the last 14 days trend (Theil-Sen slope, most day pairs agreeing) by more than the
  unit's weekly threshold; with the days until an alert limit is reached at that rate, when that is within 30 days.
- step: the daily medians changed level on one day (the best single split) by more than the unit's threshold, in the
  last 14 days (an older step is history; the drift is then judged on the days after it).
- frozen: the raw readings have not changed for 6 hours, where the probe normally moves and has rarely (under 2 % of
  its six-hour blocks) been this still before (not for currents).
- recurring: the daily profile goes beyond the plausible daily swing (3 °F, 0.6 pH, 100 mV, 0.6 ppt): a departure on
  most days at the same time, like a nightly dip. Reported once with on how many days it showed; the profile is
  capped to the plausible swing, so it never becomes "expected", and those slots are left out of the unusual spells.
And a 24-hour forecast band per probe: the current trend plus the daily profile, plus or minus 2 spreads.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .site import Site

STEP = 600  # seconds: the unit logs every 10 minutes, so the analysis does too
SLOTS = 86400 // STEP
MIN_DAYS = 7  # a daily profile needs a week
WINDOW_DAYS = 28
UNUSUAL_Z = 4.0
UNUSUAL_STEPS = 3  # 30 minutes
RECENT_DAYS = 7
FROZEN_SECONDS = 6 * 3600
RARELY_STILL = 0.02  # a probe completely still in this share of its past six-hour blocks or more is not "frozen" when still
# per unit: resolution (the spread's floor), weekly drift and level-step thresholds, and the smallest departure worth
# calling unusual (None: not judged). An EnergyBar's current follows what is switched on (lights with temperature
# cut-offs, feed cycles, overrides), not a daily rhythm: only its level steps are judged.
UNITS: dict[str, tuple[float, float | None, float | None, float | None]] = {
    "°F": (0.1, 1.0, 1.0, 0.5),
    "°C": (0.1, 0.55, 0.55, 0.3),
    "pH": (0.01, 0.1, 0.1, 0.15),
    "mV": (1.0, 40.0, 40.0, 25.0),
    "ppt": (0.1, 0.4, 0.4, 0.5),
    "A": (0.1, None, 0.3, None),
}
DEFAULT_UNIT: tuple[float, float | None, float | None, float | None] = (0.01, None, None, None)
# the largest daily swing a tank plausibly has (the daily profile's high minus low): a profile beyond it holds a
# departure that recurs on most days (a nightly dip), which is reported as such and never becomes "expected"
SWING: dict[str, float] = {"°F": 3.0, "°C": 1.7, "pH": 0.6, "mV": 100.0, "ppt": 0.6}


@dataclass
class Model:
    sensor: str
    label: str
    unit: str
    buckets: dict[int, float]  # bucket start -> mean reading
    trend: dict[int, float]  # bucket start -> trend
    profile: list[float]  # slot -> typical offset from the trend
    spread: float
    days: float  # how much history the model saw
    recurring: set[int] = field(default_factory=set)  # slots where the raw profile went beyond the plausible swing
    raw_profile: list[float] = field(default_factory=list)

    def expected(self, bucket: int) -> float | None:
        t = self.trend.get(bucket)
        return None if t is None else t + self.profile[slot(bucket)]


def slot(bucket: int) -> int:
    return (bucket // STEP) % SLOTS


def to_buckets(rows: list[tuple[float, float]]) -> dict[int, float]:
    """(ts, value) readings -> 10-minute means."""
    sums: dict[int, list[float]] = defaultdict(list)
    for ts, value in rows:
        sums[int(ts) // STEP * STEP].append(value)
    return {b: sum(v) / len(v) for b, v in sorted(sums.items())}


def rolling_trend(buckets: dict[int, float]) -> dict[int, float]:
    """The median of the 24 hours around each hour, interpolated to every bucket (hourly medians keep this fast)."""
    if not buckets:
        return {}
    keys = sorted(buckets)
    first, last = keys[0], keys[-1]
    hours = list(range(first // 3600 * 3600, last + 3600, 3600))
    values = [buckets[k] for k in keys]
    anchors: list[tuple[int, float]] = []
    lo = hi = 0
    for h in hours:
        while lo < len(keys) and keys[lo] < h - 43200:
            lo += 1
        while hi < len(keys) and keys[hi] <= h + 43200:
            hi += 1
        window = values[lo:hi]
        if len(window) >= 36:  # at least 6 hours of data around it
            anchors.append((h, statistics.median(window)))
    trend: dict[int, float] = {}
    if not anchors:
        return trend
    a = 0
    for k in keys:
        while a + 1 < len(anchors) and anchors[a + 1][0] <= k:
            a += 1
        h0, v0 = anchors[a]
        if a + 1 < len(anchors) and k >= h0:
            h1, v1 = anchors[a + 1]
            trend[k] = v0 + (v1 - v0) * (k - h0) / (h1 - h0)
        else:
            trend[k] = v0
    return trend


def daily_profile(buckets: dict[int, float], trend: dict[int, float]) -> list[float]:
    """For each 10-minute slot of the day: the median of (reading - trend)."""
    offsets: list[list[float]] = [[] for _ in range(SLOTS)]
    for b, v in buckets.items():
        if b in trend:
            offsets[slot(b)].append(v - trend[b])
    return [statistics.median(o) if o else 0.0 for o in offsets]


def fit(sensor: str, label: str, unit: str, rows: list[tuple[float, float]]) -> Model | None:
    buckets = to_buckets(rows)
    if len(buckets) < MIN_DAYS * SLOTS * 0.6:
        return None
    # two passes: the first trend is biased where the 24 hours around a time are cut short (the newest 12 hours),
    # because the daily cycle is still in it; the second trend is the median of the readings without the cycle
    trend = rolling_trend(buckets)
    profile = daily_profile(buckets, trend)
    trend = rolling_trend({b: v - profile[slot(b)] for b, v in buckets.items()})
    raw = daily_profile(buckets, trend)
    profile, recurring = list(raw), set()
    swing = SWING.get(unit)
    if swing is not None and max(raw) - min(raw) > swing:
        center = statistics.median(raw)
        recurring = {n for n, v in enumerate(raw) if abs(v - center) > swing / 2}
        profile = [min(max(v, center - swing / 2), center + swing / 2) for v in raw]
    residuals = [v - (trend[b] + profile[slot(b)]) for b, v in buckets.items() if b in trend and slot(b) not in recurring]
    resolution = UNITS.get(unit, DEFAULT_UNIT)[0]
    spread = max(1.4826 * statistics.median([abs(r) for r in residuals]), resolution) if residuals else resolution
    days = (max(buckets) - min(buckets)) / 86400
    return Model(sensor, label, unit, buckets, trend, profile, spread, days, recurring, raw)


def theil_sen(points: list[tuple[float, float]]) -> tuple[float, float]:
    """(slope, agreement): the median of the pairwise slopes, and the share of pairs with that slope's sign."""
    slopes = [(y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(points) for x2, y2 in points[i + 1 :] if x2 != x1]
    if not slopes:
        return 0.0, 0.0
    slope = statistics.median(slopes)
    agree = sum(1 for s in slopes if (s > 0) == (slope > 0) and s != 0) / len(slopes)
    return slope, agree


def daily_medians(model: Model) -> list[tuple[float, float]]:
    """(day number, median) for each day with at least 18 hours of data."""
    days: dict[int, list[float]] = defaultdict(list)
    for b, v in model.buckets.items():
        days[b // 86400].append(v)
    return [(float(d), statistics.median(v)) for d, v in sorted(days.items()) if len(v) >= 108]


def fmt(value: float, unit: str) -> str:
    digits = 2 if unit == "pH" else 0 if unit == "mV" else 1
    shown = f"{value:.{digits}f}"
    return shown if unit in ("pH", "") else f"{shown} {unit}"


def unusual(model: Model, now: float) -> list[dict[str, Any]]:
    """Spells of 30 minutes or more at 4 spreads or more (and the unit's smallest departure) from the expected value,
    in the last 7 days."""
    out: list[dict[str, Any]] = []
    meaningful = UNITS.get(model.unit, DEFAULT_UNIT)[3]
    if meaningful is None:
        return out
    threshold = max(UNUSUAL_Z * model.spread, meaningful)
    run: list[tuple[int, float, float]] = []
    missing = 0

    def close() -> None:
        if len(run) >= UNUSUAL_STEPS:
            peak = max(run, key=lambda r: abs(r[1] - r[2]))
            direction = "high" if peak[1] > peak[2] else "low"
            z = (peak[1] - peak[2]) / model.spread
            out.append({
                "kind": "unusual", "sensor": model.sensor, "start": run[0][0], "end": run[-1][0] + STEP,
                "summary": f"{model.label} unusually {direction}: {fmt(peak[1], model.unit)} where "
                           f"{fmt(peak[2], model.unit)} was expected ({abs(z):.0f} spreads) for "
                           f"{(run[-1][0] + STEP - run[0][0]) // 60} min",
                "evidence": {"peak": peak[1], "expected": round(peak[2], 3), "z": round(z, 1), "spread": model.spread},
            })  # fmt: skip
        run.clear()

    # consecutive steps only: a normal step ends a spell, up to two missing ones (no reading) may bridge it; the slots
    # of a recurring departure are left to the recurring finding
    recent = [b for b in model.buckets if b >= now - RECENT_DAYS * 86400]
    for b in range(min(recent), max(recent) + STEP, STEP) if recent else ():
        value = model.buckets.get(b)
        if value is None:
            missing += 1
            if missing > 2:
                close()
            continue
        missing = 0
        expected = model.expected(b)
        if expected is not None and slot(b) not in model.recurring and abs(value - expected) >= threshold:
            run.append((b, value, expected))
        else:
            close()
    close()
    return out


def recurring(model: Model, zone: str) -> list[dict[str, Any]]:
    """A departure from the plausible day that recurs at the same time on most days (the raw daily profile went beyond
    the unit's plausible swing): reported once, with on how many days it showed, instead of becoming "expected"."""
    if not model.recurring:
        return []
    center = statistics.median(model.raw_profile)
    slots = sorted(model.recurring)
    # the longest run of neighbouring slots (the day wraps around midnight)
    runs: list[list[int]] = []
    for n in slots:
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][-1] == SLOTS - 1:
        runs[0] = runs.pop() + runs[0]
    worst = max(runs, key=len)
    deepest = max(worst, key=lambda n: abs(model.raw_profile[n] - center))
    size = model.raw_profile[deepest] - center
    days: dict[int, list[float]] = defaultdict(list)
    for b, v in model.buckets.items():
        if slot(b) in worst and b in model.trend:
            days[b // 86400].append(v - model.trend[b] - center)
    showing = sum(1 for offsets in days.values() if offsets and abs(statistics.median(offsets)) > SWING[model.unit] / 2
                  and (statistics.median(offsets) > 0) == (size > 0))  # fmt: skip
    last_day = max(model.buckets) // 86400 * 86400
    start = last_day + worst[0] * STEP
    end = last_day + (worst[-1] + 1) * STEP + (86400 if worst[-1] < worst[0] else 0)
    if start > max(model.buckets):  # not come round yet today: the latest occurrence was yesterday
        start, end = start - 86400, end - 86400
    local = ZoneInfo(zone)
    span = (
        f"{datetime.fromtimestamp(start, UTC).astimezone(local):%H:%M}-{datetime.fromtimestamp(end, UTC).astimezone(local):%H:%M}"
    )
    return [{"kind": "recurring", "sensor": model.sensor, "start": start, "end": end,
             "summary": f"{model.label} has a recurring {'rise' if size > 0 else 'dip'} of about {fmt(abs(size), model.unit)} "
                        f"around {span} on {showing} of {len(days)} days; it is not counted as normal, and those times "
                        "are left out of the unusual readings",
             "evidence": {"size": round(size, 3), "days_showing": showing, "days": len(days), "slots": worst}}]  # fmt: skip


def limits_for(site: Site, sensor: str) -> list[tuple[str, str, float]]:
    """(condition id, above|below, limit) of the alert conditions on this sensor."""
    return [(c.id, c.kind, c.limit) for c in site.conditions if c.subject == sensor and c.kind in ("above", "below")
            and c.limit is not None]  # fmt: skip


def drift_and_step(model: Model, site: Site, now: float) -> list[dict[str, Any]]:
    _resolution, weekly, step_limit, _meaningful = UNITS.get(model.unit, DEFAULT_UNIT)
    # level steps are searched in the last 14 days only: two steps over the whole window would hide each other
    medians = [m for m in daily_medians(model) if m[0] * 86400 >= now - 14 * 86400]
    out: list[dict[str, Any]] = []
    step = None
    if step_limit is not None and len(medians) >= 8:
        # the split that best explains the days (the least total distance from each side's median), among those
        # with flat sides and a change beyond the threshold: the median change alone barely depends on the split day
        best = None
        for k in range(3, len(medians) - 2):
            before = [v for _, v in medians[:k]]
            after = [v for _, v in medians[k:]]
            old_level, new_level = statistics.median(before), statistics.median(after)
            change = new_level - old_level
            spread = max(statistics.median([abs(v - old_level) for v in before]),
                         statistics.median([abs(v - new_level) for v in after]))  # fmt: skip
            # a step has flat sides: a steady drift split in two is not one
            sides = max(abs(theil_sen(medians[:k])[0]) * k, abs(theil_sen(medians[k:])[0]) * (len(medians) - k))
            flat = spread < abs(change) / 4 and sides < abs(change) / 3
            cost = sum(abs(v - old_level) for v in before) + sum(abs(v - new_level) for v in after)
            if abs(change) >= step_limit and flat and (best is None or cost < best[4]):
                best = (k, change, old_level, new_level, cost)
        if best is not None and int(medians[best[0]][0]) * 86400 >= now - 14 * 86400:  # older steps are history
            k, change, old, new, _cost = best
            day = int(medians[k][0]) * 86400
            step = {"kind": "step", "sensor": model.sensor, "start": day, "end": None,
                    "summary": f"{model.label} changed level around this day: {fmt(old, model.unit)} before, "
                               f"{fmt(new, model.unit)} after",
                    "evidence": {"before": round(old, 3), "after": round(new, 3), "change": round(change, 3)}}  # fmt: skip
            out.append(step)
    recent = medians
    if weekly is not None and step is None and len(recent) >= 7:
        slope, agree = theil_sen(recent)
        if abs(slope) * 7 >= weekly and agree >= 0.75:
            current = recent[-1][1]
            finding: dict[str, Any] = {
                "kind": "drift", "sensor": model.sensor, "start": int(recent[0][0]) * 86400, "end": None,
                "summary": f"{model.label} {'rising' if slope > 0 else 'falling'} by {fmt(abs(slope) * 7, model.unit)} "
                           f"a week over {len(recent)} days",
                "evidence": {"slope_per_day": round(slope, 4), "agreement": round(agree, 2), "current": round(current, 3),
                             "daily_medians": [round(v, 3) for _, v in recent]},
            }  # fmt: skip
            for cid, kind, limit in limits_for(site, model.sensor):
                if (kind == "above" and slope > 0 and limit > current) or (kind == "below" and slope < 0 and limit < current):
                    days = (limit - current) / slope
                    if 0 < days <= 30:
                        finding["summary"] += (
                            f"; at this rate it reaches {fmt(limit, model.unit)} ({cid}) in about {days:.0f} days"
                        )
                        finding["evidence"]["reaches"] = {"condition": cid, "limit": limit, "days": round(days, 1)}
                        break
            out.append(finding)
    return out


def frozen(model: Model, raw: list[tuple[float, float]], now: float) -> list[dict[str, Any]]:
    """The raw readings of the last 6 hours never changed, where the probe normally moves. Not for currents: a bar
    with only constant loads on reads the same for hours."""
    if UNITS.get(model.unit, DEFAULT_UNIT)[3] is None:
        return []
    recent = [v for ts, v in raw if ts >= now - FROZEN_SECONDS]
    if len(recent) < 30 or len(set(recent)) > 1:
        return []
    # a probe that has sat this still before is steady, not stuck: a heated tank at night can read the same 0.1 °F for
    # hours (the tank's temperature probe, 4 % of its six-hour blocks in 2026-09/10). Only a rare stillness counts.
    values: dict[int, set[float]] = defaultdict(set)
    counts: dict[int, int] = defaultdict(int)
    for ts, v in raw:
        block = int(ts) // FROZEN_SECONDS
        if block < int(now) // FROZEN_SECONDS:
            values[block].add(v)
            counts[block] += 1
    full = [k for k, n in counts.items() if n >= 30]
    if not full or sum(1 for k in full if len(values[k]) == 1) / len(full) >= RARELY_STILL:
        return []
    resolution = UNITS.get(model.unit, DEFAULT_UNIT)[0]
    blocks: dict[int, list[float]] = defaultdict(list)  # six-hour blocks
    for b, v in model.buckets.items():
        blocks[b // FROZEN_SECONDS].append(v)
    ranges = [max(v) - min(v) for v in blocks.values() if len(v) >= 30]
    if not ranges or statistics.median(ranges) < 3 * resolution:
        return []  # this probe is often this still
    return [{"kind": "frozen", "sensor": model.sensor, "start": now - FROZEN_SECONDS, "end": now,
             "summary": f"{model.label} has read exactly {fmt(recent[0], model.unit)} for 6 hours; it normally moves "
                        f"{fmt(statistics.median(ranges), model.unit)} in that time (stuck probe or module?)",
             "evidence": {"value": recent[0], "typical_range": round(statistics.median(ranges), 3)}}]  # fmt: skip


def forecast(model: Model, now: float, hours: int = 24) -> list[list[float]]:
    """[ts, expected, low, high] every 30 minutes for the next hours: the latest trend plus the daily profile."""
    if not model.trend:
        return []
    last = model.trend[max(model.trend)]
    start = int(now) // 1800 * 1800 + 1800
    out = []
    for ts in range(start, start + hours * 3600, 1800):
        expected = last + model.profile[slot(ts // STEP * STEP)]
        out.append([float(ts), round(expected, 3), round(expected - 2 * model.spread, 3), round(expected + 2 * model.spread, 3)])
    return out


def recent_series(model: Model, now: float, days: int = RECENT_DAYS) -> list[list[float | None]]:
    """[ts, reading, expected, low, high] every 30 minutes for the last days: what the page draws. The band is where a
    reading starts to count as unusual."""
    out: list[list[float | None]] = []
    meaningful = UNITS.get(model.unit, DEFAULT_UNIT)[3]
    band = max(UNUSUAL_Z * model.spread, meaningful or 0.0)
    groups: dict[int, list[int]] = defaultdict(list)
    for b in model.buckets:
        if b >= now - days * 86400:
            groups[b // 1800 * 1800].append(b)
    for ts, members in sorted(groups.items()):
        value = sum(model.buckets[b] for b in members) / len(members)
        expected = [e for e in (model.expected(b) for b in members) if e is not None]
        if not expected:
            out.append([float(ts), round(value, 3), None, None, None])
            continue
        e = sum(expected) / len(expected)
        out.append([float(ts), round(value, 3), round(e, 3), round(e - band, 3), round(e + band, 3)])
    return out


# ---- equipment fingerprints: what each output draws when it switches ------------------------------------------------
# A step is measured the same way whatever the readings' resolution (minute polls, or the unit's 10-minute log): the
# last reading in the 11 minutes before the switching against the first one 5 to 15 minutes after it, when a lamp's
# ramp or a pump's start is over. Another switching on the same bar in that span makes the step ambiguous: left out.
BEFORE = 660
SETTLE = (300, 900)
FINGERPRINT_MIN = 0.3  # amps: a change in what an output draws worth mentioning, and at least 30 % of it


def steps(
    outlets: list[tuple], amps: dict[str, list[tuple[float, float]]], bars: dict[str, str]
) -> dict[str, list[tuple[float, float]]]:
    """For each output on an EnergyBar: (ts, amps it drew) at every switching, from the bar's current just before and
    just after it (on: after - before; off: before - after). bars: output name -> its bar's current input."""
    by_bar: dict[str, list[tuple[float, str, bool]]] = defaultdict(list)
    for item in outlets:
        ts, name, _kind, on = item[:4]
        if name in bars:
            by_bar[bars[name]].append((ts, name, on))
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for current, switchings in by_bar.items():
        readings = amps.get(current) or []
        times = [ts for ts, _ in readings]
        for ts, name, on in switchings:
            if any(other != name and ts - BEFORE <= t <= ts + SETTLE[1] for t, other, _ in switchings):
                continue
            i = bisect_left(times, ts - BEFORE)
            j = bisect_left(times, ts + SETTLE[1] + 1)
            before = [v for t, v in readings[i:j] if t < ts]
            after = [v for t, v in readings[i:j] if ts + SETTLE[0] <= t <= ts + SETTLE[1]]
            if before and after:
                drawn = (after[0] - before[-1]) if on else (before[-1] - after[0])
                out[name].append((ts, round(drawn, 2)))
    return out


def equipment(outlets: list[tuple], amps: dict[str, list[tuple[float, float]]], bars: dict[str, str],
              now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:  # fmt: skip
    """(fingerprints, findings): what each output usually draws when it switches, and outputs whose last week differs
    from that by FINGERPRINT_MIN and 30 % (a lamp or a driver failing, a heater element gone, a pump clogging)."""
    prints, findings = [], []
    for name, drawn in sorted(steps(outlets, amps, bars).items()):
        usual = statistics.median(d for _, d in drawn)
        older = [d for ts, d in drawn if ts < now - RECENT_DAYS * 86400]
        recent = [d for ts, d in drawn if ts >= now - RECENT_DAYS * 86400]
        item = {"output": name, "current": bars[name], "draws": round(usual, 2), "switchings": len(drawn)}
        if len(older) >= 3 and len(recent) >= 2:
            before, lately = statistics.median(older), statistics.median(recent)
            item["lately"] = round(lately, 2)
            if abs(lately - before) >= max(FINGERPRINT_MIN, 0.3 * abs(before)) and abs(before) >= FINGERPRINT_MIN:
                less = lately < before
                why = "a lamp, driver or heater element failing?" if less else "a pump working harder, or a new load?"
                first = min(ts for ts, _ in drawn if ts >= now - RECENT_DAYS * 86400)
                findings.append({
                    "kind": "equipment", "sensor": bars[name], "start": first, "end": None,
                    "summary": f"{name} draws {abs(lately):.1f} A when it switches lately, against {abs(before):.1f} A before "
                               f"({len(recent)} times this week): {why}",
                    "evidence": {"output": name, "before": round(before, 2), "lately": round(lately, 2),
                                 "recent": recent[-10:]},
                })  # fmt: skip
        prints.append(item)
    return prints, findings


# ---- the probes against the manual tests ---------------------------------------------------------------------------
PROBE_OFF = {
    "pH": 0.1,
    "ppt": 0.5,
    "°F": 0.5,
    "°C": 0.3,
}  # a probe this far from its tests, the same way at least twice: recalibrate?


def probe_health(site: Site, history: dict[str, list[tuple[float, float]]], tests: list[tuple[float, str, float]],
                 journal: list[tuple[float, str, str]]) -> list[dict[str, Any]]:  # fmt: skip
    """For each probe that a manual test measures too (site.yaml sensors: test): its reading around the newest tests
    (10 minutes either side) minus the test. Two or more of the newest, all off the same way by more than the unit's
    limit: a finding, with the last calibration in the journal."""
    out = []
    calibrations = [ts for ts, kind, _ in journal if kind == "calibration"]
    for name, sensor in site.sensors.items():
        limit = PROBE_OFF.get(sensor.unit)
        if not sensor.test or limit is None or name not in history:
            continue
        rows = history[name]
        times = [ts for ts, _ in rows]
        pairs = []
        for ts, _parameter, value in sorted(t for t in tests if t[1] == sensor.test):
            i, j = bisect_left(times, ts - 600), bisect_left(times, ts + 600)
            near = [v for _, v in rows[i:j]]
            if near:
                pairs.append((ts, value, round(sum(near) / len(near) - value, 3)))
        newest = pairs[-3:]
        if len(newest) < 2:
            continue
        offsets = [d for _, _, d in newest]
        same_way = all(d > 0 for d in offsets) or all(d < 0 for d in offsets)
        if same_way and min(abs(d) for d in offsets) > limit:
            mean = sum(offsets) / len(offsets)
            last_cal = max(calibrations) if calibrations else None
            out.append({
                "kind": "probe", "sensor": name, "start": newest[0][0], "end": None,
                "summary": f"{sensor.label} reads {abs(mean):.2f} {'above' if mean > 0 else 'below'} your last {len(newest)} "
                           f"tests ({', '.join(f'{v:g}' for _, v, _ in newest)}): recalibrate the probe?"
                           + ("" if last_cal else " No calibration in the journal."),
                "evidence": {"offsets": offsets, "tests": [[ts, v] for ts, v, _ in newest], "last_calibration": last_cal},
            })  # fmt: skip
    return out


def meanwhile(finding: dict[str, Any], outlets: list[tuple]) -> None:
    """What the unit switched during a spell (from half an hour before to its end): evidence beside the numbers."""
    start, end = finding["start"] - 1800, (finding["end"] or finding["start"]) + 600
    switched: dict[str, dict[str, Any]] = {}
    for item in outlets:
        ts, name, kind, on = item[:4]
        if start <= ts <= end:
            item = switched.setdefault(name, {"output": name, "type": kind, "on": 0, "off": 0, "first": ts})
            item["on" if on else "off"] += 1
    if not switched:
        return
    finding["evidence"]["outlets"] = sorted(switched.values(), key=lambda i: i["first"])
    alarms = [i["output"] for i in switched.values() if i["type"] == "alert" and i["on"]]
    if alarms:
        finding["summary"] += f"; the Apex's own alarm output {', '.join(alarms)} went on meanwhile"


def logged(finding: dict[str, Any], journal: list[tuple[float, str, str]]) -> None:
    """Journal entries near a finding (12 hours before an unusual spell to 2 hours after it; a day either side of a
    level step): what the person did that may explain it."""
    if finding["kind"] == "unusual":
        start, end = finding["start"] - 12 * 3600, (finding["end"] or finding["start"]) + 7200
    elif finding["kind"] == "step":
        start, end = finding["start"] - 86400, finding["start"] + 2 * 86400
    else:
        return
    near = [{"ts": ts, "kind": kind, "text": text} for ts, kind, text in journal if start <= ts <= end]
    if near:
        finding["evidence"]["journal"] = near
        finding["summary"] += "; journal: " + ", ".join(sorted({n["kind"] for n in near}))


def analyse(
    site: Site,
    history: dict[str, list[tuple[float, float]]],
    now: float,
    outlets: list[tuple] | None = None,
    journal: list[tuple[float, str, str]] | None = None,
    tests: list[tuple[float, str, float]] | None = None,
    switchings_complete_since: float | None = None,
) -> dict[str, Any]:
    """history: sensor name -> its raw readings (ts, value) over the window; outlets: the unit's outlet changes (ts, name,
    type, energized), shown beside each unusual spell; journal: (ts, kind, text) entries, shown beside the findings
    they may explain. The result the page and MCP show."""
    sensors: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for name, rows in history.items():
        meta = site.sensors.get(name)
        label, unit = (meta.label, meta.unit) if meta else (name, "")
        model = fit(name, label, unit, rows)
        if model is None:
            days = len(to_buckets(rows)) / SLOTS
            sensors.append({"sensor": name, "label": label, "unit": unit, "learning": True, "days": round(days, 1)})
            continue
        levels = drift_and_step(model, site, now)
        spells = unusual(model, now)
        for step in (f for f in levels if f["kind"] == "step"):  # the trend catching up with a step is not news
            spells = [f for f in spells if not step["start"] - 86400 <= f["start"] <= step["start"] + 2 * 86400]
        for spell in spells:
            meanwhile(spell, outlets or [])
        found = spells + levels + frozen(model, rows, now) + recurring(model, site.timezone)
        for finding in found:
            logged(finding, journal or [])
        findings += found
        amplitudes = defaultdict(list)
        for b, v in model.buckets.items():
            amplitudes[b // 86400].append(v)
        daily_range = statistics.median([max(v) - min(v) for v in amplitudes.values() if len(v) >= 108] or [0.0])
        sensors.append({
            "sensor": name, "label": label, "unit": unit, "learning": False, "days": round(model.days, 1),
            "spread": round(model.spread, 4), "daily_range": round(daily_range, 3), "findings": len(found),
            "recent": recent_series(model, now), "forecast": forecast(model, now),
        })  # fmt: skip
    # outputs on the EnergyBars (their did starts with the bar's address): what each draws when it switches
    bars = {}
    for item in outlets or []:
        if len(item) > 4:
            module = next((m for m in site.modules if m.current and str(item[4]).split("_")[0] == m.address), None)
            if module is not None and module.current:
                bars[item[1]] = module.current
    # steps only from when every output's switching is known (the polls): the unit's outlet log leaves out the outputs
    # it is not set to log, and one of those switching at the same moment (blueLEDR with PapioLed at 21:01) would be
    # taken for part of the other's step
    complete = [o for o in outlets or [] if switchings_complete_since is not None and o[0] >= switchings_complete_since]
    prints, changed = equipment(complete, history, bars, now)
    findings += changed
    findings += probe_health(site, history, tests or [], journal or [])
    order = {"frozen": 0, "probe": 1, "equipment": 2, "unusual": 3, "drift": 4, "step": 5, "recurring": 6}
    findings.sort(key=lambda f: (order[f["kind"]], -(f["start"] or 0)))
    return {"computed_at": now, "window_days": WINDOW_DAYS, "sensors": sensors, "findings": findings, "equipment": prints}
