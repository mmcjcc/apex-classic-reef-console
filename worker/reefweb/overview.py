"""The status page's data, read from the worker's database (read-only): the latest poll, the conditions, 24 hours of
each probe, the AquaBus modules, every output grouped by EnergyBar, the manual overrides with the program lines they
ignore, and the recent events."""

from __future__ import annotations

import sqlite3
from typing import Any

from reef.apex import module_of
from reef.report import energy
from reef.site import Site

from .queries import connect, kv, latest_programs

__all__ = ["build_overview", "connect", "ignored_lines"]

HISTORY_SECONDS = 24 * 3600
BUCKET_SECONDS = 600  # the charts use 10-minute means
STALE_SECONDS = 180  # a poll older than this is not "now"


def history(db: sqlite3.Connection, names: list[str], since: float) -> dict[str, list[list[float]]]:
    if not names:
        return {}
    marks = ",".join("?" for _ in names)
    rows = db.execute(
        f"SELECT s.name, (r.ts / {BUCKET_SECONDS}) * {BUCKET_SECONDS} AS b, AVG(r.value) FROM readings r "
        f"JOIN sensors s ON s.id = r.sensor_id WHERE s.name IN ({marks}) AND r.ts >= ? GROUP BY s.name, b ORDER BY b",
        (*names, int(since)),
    ).fetchall()
    out: dict[str, list[list[float]]] = {name: [] for name in names}
    for name, bucket, value in rows:
        out[name].append([float(bucket), round(float(value), 3)])
    return out


def mode_since(db: sqlite3.Connection, outlet: str, mode: str) -> float | None:
    """When the outlet's current mode began, as far as the history goes back."""
    ids = [r[0] for r in db.execute("SELECT id FROM outlets WHERE name = ?", (outlet,))]
    if not ids:
        return None
    marks = ",".join("?" for _ in ids)
    row = db.execute(
        f"SELECT MIN(ts) FROM outlet_states WHERE outlet_id IN ({marks}) AND ts > COALESCE("
        f"(SELECT MAX(ts) FROM outlet_states WHERE outlet_id IN ({marks}) AND mode != ?), 0)",
        (*ids, *ids, mode),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def history_start(db: sqlite3.Connection) -> float | None:
    row = db.execute("SELECT MIN(first_seen) FROM sensors").fetchone()
    return float(row[0]) if row and row[0] is not None else None


def ignored_lines(lines: list[str]) -> list[str]:
    """The program lines a manual override ignores and that react to something (a probe, a switch, power, feed):
    the ones that matter while the outlet is held. "Fallback" and "Set" are the program's defaults."""
    return [line for line in lines if line.startswith(("If ", "When ", "Defer", "Min Time")) and not line.startswith("If Time")]


def build_overview(db: sqlite3.Connection, site: Site, now: float) -> dict[str, Any]:
    latest: dict[str, Any] | None = kv(db, "latest")
    poll = kv(db, "poll", {}) or {}
    conditions = kv(db, "conditions", []) or []
    latest_version = latest_programs(db)
    programs = latest_version[1] if latest_version else {}
    active = [c for c in conditions if c.get("active")]
    alarmed = {c["subject"] for c in active}
    result: dict[str, Any] = {
        "now": now,
        "poll": {
            "ok": bool(poll.get("ok")),
            "at": poll.get("at"),
            "error": poll.get("error"),
            "last_good": latest.get("received_at") if latest else None,
            "current": bool(latest) and bool(poll.get("ok")) and now - float(latest["received_at"]) < STALE_SECONDS,
        },
        "alerts": sorted(active, key=lambda c: (c["severity"] != "critical", -(c.get("since") or 0))),
        "history_since": history_start(db),
        "polling_since": kv(db, "polling_since"),  # outputs are known from the polls on (the backfill has probes only)
        "energy": {
            "volts": site.extras.volts,
            "price_per_kwh": site.extras.price_per_kwh,
            "bars": energy(db, site, now - 7 * 86400, now),
        },  # fmt: skip
        "external_sensors": [{"name": n, "label": s.label, "unit": s.unit} for n, s in site.sensors.items() if s.external],
        "events": [
            {"ts": ts, "kind": kind, "subject": subject, "detail": detail}
            for ts, kind, subject, detail in db.execute(
                "SELECT ts, kind, subject, detail FROM events ORDER BY ts DESC, id DESC LIMIT 30"
            )
        ],
    }
    if latest is None:
        return result | {"apex": None, "probes": [], "switches": [], "modules": [], "groups": [], "overrides": []}

    result["apex"] = {k: latest.get(k) for k in ("serial", "software", "timezone", "feed_active", "power_failed",
                                                 "power_restored", "latency", "received_at")}  # fmt: skip
    probes = [
        i for i in latest["inputs"]
        if i["type"] not in ("digital", "Amps") and site.sensors.get(i["name"], None) is not None
        and site.sensors[i["name"]].active
    ]  # fmt: skip
    amps = [i for i in latest["inputs"] if i["type"] == "Amps"]
    series = history(db, [p["name"] for p in probes] + [a["name"] for a in amps], now - HISTORY_SECONDS)
    result["probes"] = [
        {
            "name": p["name"],
            "label": site.sensors[p["name"]].label,
            "unit": site.sensors[p["name"]].unit,
            "value": p["value"],
            "alarm": p["name"] in alarmed,
            "history": series.get(p["name"], []),
        }
        for p in probes
    ]
    result["switches"] = [
        {"name": i["name"], "label": site.sensors[i["name"]].label, "closed": bool(i["value"]), "alarm": i["name"] in alarmed}
        for i in latest["inputs"]
        if i["type"] == "digital" and i["name"] in site.sensors and site.sensors[i["name"]].active
    ]
    module_by_address = {m.address: m for m in site.modules}
    result["modules"] = [
        {
            "id": mid,
            **info,
            "alarm": mid in alarmed,
            "history": series.get(module_by_address[info["address"]].current or "", []),
        }
        for mid, info in latest.get("modules", {}).items()
        if info["address"] in module_by_address
    ]
    groups: dict[str, dict[str, Any]] = {}
    for out in latest["outputs"]:
        address = module_of(out["did"])
        module = module_by_address.get(address)
        if module is not None:
            key, title = module.id, f"{module.id} (EnergyBar, AquaBus {address})" if module.kind == "EB8" else module.id
        elif out["type"] == "virtual":
            key, title = "virtual", "Virtual outlets"
        else:
            key, title = "base", "Base unit: variable speed and alarm outputs"
        group = groups.setdefault(key, {"id": key, "title": title, "outputs": []})
        group["outputs"].append(
            {k: out.get(k) for k in ("name", "did", "type", "mode", "health", "intensity", "manual", "energized")}
        )
    order = [m.id for m in site.modules] + ["base", "virtual"]
    result["groups"] = sorted(groups.values(), key=lambda g: order.index(g["id"]) if g["id"] in order else len(order))
    if "virtual" in groups and programs:
        # a virtual outlet matters only if another output's program reads it ("If Outlet Light1_A1 = ON ...")
        groups["virtual"]["unused"] = sum(
            1
            for out in groups["virtual"]["outputs"]
            if not any(out["name"] in line for name, lines in programs.items() if name != out["name"] for line in lines)
        )
    result["overrides"] = [
        {
            "name": out["name"],
            "mode": out["mode"],
            "since": mode_since(db, out["name"], out["mode"]),
            "ignored": ignored_lines(programs.get(out["name"], [])),
            "program_known": out["name"] in programs,
        }
        for out in latest["outputs"]
        if out.get("manual")
    ]
    return result
