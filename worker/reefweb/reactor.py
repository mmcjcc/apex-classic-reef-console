"""The calcium reactor at a glance (site.yaml reactor): per day, how long the CO2 ran (in the night window of its
program and outside it), how often it switched, and the effluent pH by day and by night; the alkalinity, calcium and
magnesium tests with their change per day; and whether calcium and alkalinity moved together.

A calcium reactor dissolves its media. Pure calcium carbonate (aragonite) gives about 7.15 ppm of calcium with every
dKH of alkalinity (1 dKH = 0.357 meq/L; 20.04 mg of calcium per meq); media with magnesium carbonate in it (dolomite,
magnesium media) gives alkalinity with less calcium, and magnesium instead. Corals and precipitation take both away in
that same ratio, so they leave calcium - 7.15 x alkalinity unchanged. So between two pairs of tests (alkalinity and
calcium within a day of each other), calcium should change by 7.15 times the alkalinity change, within the kits'
errors. When it does not: the media is not pure calcium carbonate (magnesium rising says so), or something else adds
one of them (an alkalinity buffer, a calcium supplement, a salt mix of another balance, denitrification adding
alkalinity), or a test is off. (Seen here: alkalinity very high with calcium near 300 ppm, which ended when the
media was renewed.)

Days are the house's civil days; the night window is the program's, on the Apex's clock (its timezone setting)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from reef.reactor import days
from reef.site import Site

from . import queries as q

CA_PER_DKH = 7.15
KIT_ERROR = {"alkalinity": 0.3, "calcium": 20.0}  # when a test has none: typical hobby kits (dKH, ppm)
PAIR_WINDOW = 86400  # a calcium test within a day of an alkalinity test pairs with it


def tests(db: sqlite3.Connection, since: float) -> dict[str, list[dict[str, Any]]]:
    """Alkalinity, calcium and magnesium tests since a time, oldest first, each with its change per day since the one
    before."""
    out: dict[str, list[dict[str, Any]]] = {}
    for parameter in ("alkalinity", "calcium", "magnesium"):
        rows = db.execute(
            "SELECT measured_at, value, unit, kit, kit_error FROM water_tests WHERE deleted_at IS NULL AND parameter = ? "
            "AND measured_at >= ? ORDER BY measured_at",
            (parameter, since),
        ).fetchall()
        items, previous = [], None
        for ts, value, unit, kit, error in rows:
            item = {"ts": ts, "value": value, "unit": unit, "kit": kit, "kit_error": error, "per_day": None}
            if previous is not None and ts > previous[0]:
                item["per_day"] = round((value - previous[1]) / ((ts - previous[0]) / 86400), 3)
            items.append(item)
            previous = (ts, value)
        out[parameter] = items
    return out


def balance(series: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Between consecutive alkalinity tests that each have a calcium test within a day: the alkalinity change, the
    calcium change, the calcium change expected from it, and whether they agree within the kits' errors."""
    alk, ca = series.get("alkalinity", []), series.get("calcium", [])

    def near(ts: float) -> dict[str, Any] | None:
        close = [c for c in ca if abs(c["ts"] - ts) <= PAIR_WINDOW]
        return min(close, key=lambda c: abs(c["ts"] - ts)) if close else None

    out = []
    for a1, a2 in zip(alk, alk[1:], strict=False):
        c1, c2 = near(a1["ts"]), near(a2["ts"])
        if c1 is None or c2 is None or c1 is c2:
            continue
        d_alk, d_ca = a2["value"] - a1["value"], c2["value"] - c1["value"]
        expected = CA_PER_DKH * d_alk
        e_alk = (a1["kit_error"] or KIT_ERROR["alkalinity"]) + (a2["kit_error"] or KIT_ERROR["alkalinity"])
        e_ca = (c1["kit_error"] or KIT_ERROR["calcium"]) + (c2["kit_error"] or KIT_ERROR["calcium"])
        margin = e_ca + CA_PER_DKH * e_alk
        verdict = "in step" if abs(d_ca - expected) <= margin else ("calcium behind" if d_ca < expected else "calcium ahead")
        out.append({"from": a1["ts"], "to": a2["ts"], "alk_change": round(d_alk, 2), "ca_change": round(d_ca, 1),
                    "ca_expected": round(expected, 1), "margin": round(margin, 1), "verdict": verdict})  # fmt: skip
    return out


def reactor(db: sqlite3.Connection, site: Site, days_back: int, now: float) -> dict[str, Any]:
    if site.reactor is None:
        return {"configured": False}
    since = now - days_back * 86400
    series = tests(db, since - 7 * 86400)  # a test a little before the range gives the first change per day
    note = None
    if not series["calcium"]:
        note = "No calcium test yet: test calcium alongside alkalinity to see whether they move together (7.15 ppm per dKH)."
    elif not balance(series):
        note = "Calcium and alkalinity tests need to be taken within a day of each other, twice, to compare their changes."
    local = datetime.fromtimestamp(since, UTC).astimezone(ZoneInfo(site.timezone))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "configured": True,
        "co2": site.reactor.co2,
        "effluent": site.reactor.effluent,
        "night_apex": list(site.reactor.night_apex) if site.reactor.night_apex else None,
        "ca_per_dkh": CA_PER_DKH,
        "days": days(db, site, start.timestamp(), now),
        "tests": series,
        "balance": balance(series),
        "note": note,
        "since": start.timestamp(),
        "drip": next((f for f in q.kv(db, "analysis", {}).get("findings", []) if f.get("kind") == "drip"), None),
    }
