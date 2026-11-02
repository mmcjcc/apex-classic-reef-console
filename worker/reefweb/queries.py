"""Reads (and the few writes) behind the pages' API and the MCP tools, on the worker's database: program versions
and their differences, water tests, one sensor's or outlet's history, events and the conditions with their limits.

Times are UTC epoch seconds here; the API passes them on as they are (the page shows them in the browser's zone)
and the MCP tools as ISO 8601 in the house's time zone (site.yaml timezone)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reef.programs import Programs, diff, summary
from reef.site import Site
from reef.store import ensure_schema
from reef.water import PARAMETERS, Checked

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400,
          "365d": 365 * 86400}  # fmt: skip
EARLIEST = 946684800.0  # 2000-01-01: a test dated before it is a mistake
FUTURE_SLACK = 300.0  # a test may be dated at most this far ahead of the server's clock


def connect(path: Path, write: bool = False) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=10)
    if write:
        ensure_schema(db)
    else:
        db.execute("PRAGMA query_only = ON")
    return db


def span(range_: str) -> int:
    if range_ not in RANGES:
        raise ValueError(f"range is one of {', '.join(RANGES)}")
    return RANGES[range_]


def kv(db: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row[0])


def iso(ts: float | None, zone: str) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, ZoneInfo(zone)).isoformat(timespec="seconds")


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


# ---- programs ----------------------------------------------------------------------------------------------------
def latest_programs(db: sqlite3.Connection) -> tuple[float, Programs] | None:
    if not _has_table(db, "program_versions"):
        return None
    row = db.execute("SELECT ts, programs FROM program_versions ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else (float(row[0]), json.loads(row[1]))


def program_versions(db: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first: id, ts, outputs, and what changed against the version before (a summary line)."""
    if not _has_table(db, "program_versions"):
        return []
    rows = db.execute("SELECT id, ts, serial, programs FROM program_versions ORDER BY id DESC LIMIT ?", (limit + 1,)).fetchall()
    out = []
    for n, (vid, ts, serial, raw) in enumerate(rows[:limit]):
        programs = json.loads(raw)
        before = json.loads(rows[n + 1][3]) if n + 1 < len(rows) else None  # one row more than shown was read
        changes = diff(before, programs)
        out.append({"id": vid, "ts": ts, "serial": serial, "outputs": len(programs), "first": before is None,
                    "summary": "first version read" if before is None else summary(changes)})  # fmt: skip
    return out


def program_version(db: sqlite3.Connection, version_id: int) -> dict[str, Any] | None:
    """One version with every program and its differences from the version before."""
    if not _has_table(db, "program_versions"):
        return None
    row = db.execute("SELECT id, ts, serial, programs FROM program_versions WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        return None
    programs = json.loads(row[3])
    prev = db.execute(
        "SELECT id, ts, programs FROM program_versions WHERE id < ? ORDER BY id DESC LIMIT 1", (version_id,)
    ).fetchone()
    before = json.loads(prev[2]) if prev else None
    return {
        "id": row[0],
        "ts": row[1],
        "serial": row[2],
        "previous": {"id": prev[0], "ts": prev[1]} if prev else None,
        "changes": diff(before, programs),
        "programs": programs,
    }


# ---- water tests -------------------------------------------------------------------------------------------------
TEST_COLUMNS = ("id", "measured_at", "parameter", "value", "unit", "entered_value", "entered_unit", "kit", "kit_error",
                "note", "entered_at", "entered_by", "source")  # fmt: skip


def catalogue() -> list[dict[str, Any]]:
    """The parameters the test form offers: id, label, base unit, the units accepted, the plausible range."""
    return [{"id": p.id, "label": p.label, "unit": p.unit, "units": p.all_units(), "plausible": list(p.plausible),
             "decimals": p.decimals} for p in PARAMETERS.values()]  # fmt: skip


def water_tests(
    db: sqlite3.Connection, parameter: str | None = None, since: float | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Tests newest first (deleted ones left out)."""
    if not _has_table(db, "water_tests"):
        return []
    where, args = ["deleted_at IS NULL"], []
    if parameter:
        where.append("parameter = ?")
        args.append(parameter)
    if since is not None:
        where.append("measured_at >= ?")
        args.append(since)
    rows = db.execute(
        f"SELECT {', '.join(TEST_COLUMNS)} FROM water_tests WHERE {' AND '.join(where)} "
        "ORDER BY measured_at DESC, id DESC LIMIT ?",
        (*args, max(1, min(limit, 2000))),
    ).fetchall()
    return [dict(zip(TEST_COLUMNS, row, strict=True)) for row in rows]


def latest_tests(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """The newest test of each parameter, in the catalogue's order, with the one before it for the change."""
    out = []
    for p in PARAMETERS.values():
        rows = water_tests(db, p.id, limit=2)
        if rows:
            out.append(rows[0] | {"label": p.label, "previous": rows[1] if len(rows) > 1 else None})
    return out


def add_water_test(db: sqlite3.Connection, checked: Checked, measured_at: float, kit: str, note: str, who: str, source: str,
                   now: float) -> dict[str, Any]:  # fmt: skip
    if not EARLIEST <= measured_at <= now + FUTURE_SLACK:
        raise ValueError("the test's time is in the future or before 2000: check the date")
    kit, note = kit.strip()[:120], note.strip()[:500]
    with db:
        cur = db.execute(
            "INSERT INTO water_tests (measured_at, parameter, value, unit, entered_value, entered_unit, kit, kit_error, note, "
            "entered_at, entered_by, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (measured_at, checked.parameter.id, checked.value, checked.parameter.unit, checked.entered_value,
             checked.entered_unit, kit, checked.kit_error, note, now, who, source),
        )  # fmt: skip
    row = db.execute(f"SELECT {', '.join(TEST_COLUMNS)} FROM water_tests WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(zip(TEST_COLUMNS, row, strict=True))


def delete_water_test(db: sqlite3.Connection, test_id: int, who: str, now: float) -> bool:
    """Marks a test deleted (it stays in the database with who deleted it and when)."""
    with db:
        cur = db.execute("UPDATE water_tests SET deleted_at = ?, deleted_by = ? WHERE id = ? AND deleted_at IS NULL",
                         (now, who, test_id))  # fmt: skip
    return cur.rowcount == 1


# ---- weekly reports -----------------------------------------------------------------------------------------------
def reports(db: sqlite3.Connection, limit: int = 52) -> list[dict[str, Any]]:
    if not _has_table(db, "reports"):
        return []
    rows = db.execute(
        "SELECT id, ts, period_start, period_end, headline FROM reports ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [{"id": r[0], "ts": r[1], "period_start": r[2], "period_end": r[3], "headline": r[4]} for r in rows]


def report(db: sqlite3.Connection, report_id: int) -> dict[str, Any] | None:
    if not _has_table(db, "reports"):
        return None
    row = db.execute("SELECT id, ts, body FROM reports WHERE id = ?", (report_id,)).fetchone()
    return None if row is None else {"id": row[0], "ts": row[1]} | json.loads(row[2])


# ---- the journal ------------------------------------------------------------------------------------------------
JOURNAL_KINDS = ("water change", "dosing", "calibration", "maintenance", "equipment", "livestock", "feeding", "other")
JOURNAL_COLUMNS = ("id", "ts", "kind", "text", "entered_at", "entered_by", "source")


def journal(db: sqlite3.Connection, since: float | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Journal entries newest first (deleted ones left out)."""
    if not _has_table(db, "journal"):
        return []
    rows = db.execute(
        f"SELECT {', '.join(JOURNAL_COLUMNS)} FROM journal WHERE deleted_at IS NULL AND ts >= ? "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (since or 0, max(1, min(limit, 2000))),
    ).fetchall()
    return [dict(zip(JOURNAL_COLUMNS, row, strict=True)) for row in rows]


def add_journal(db: sqlite3.Connection, ts: float, kind: str, text: str, who: str, source: str, now: float) -> dict[str, Any]:
    kind = kind.strip().lower()
    if kind not in JOURNAL_KINDS:
        raise ValueError(f"kind is one of: {', '.join(JOURNAL_KINDS)}")
    text = text.strip()[:1000]
    if not text:
        raise ValueError("say what was done")
    if not EARLIEST <= ts <= now + FUTURE_SLACK:
        raise ValueError("the entry's time is in the future or before 2000: check the date")
    with db:
        cur = db.execute(
            "INSERT INTO journal (ts, kind, text, entered_at, entered_by, source) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, kind, text, now, who, source),
        )
    row = db.execute(f"SELECT {', '.join(JOURNAL_COLUMNS)} FROM journal WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(zip(JOURNAL_COLUMNS, row, strict=True))


def delete_journal(db: sqlite3.Connection, entry_id: int, who: str, now: float) -> bool:
    with db:
        cur = db.execute("UPDATE journal SET deleted_at = ?, deleted_by = ? WHERE id = ? AND deleted_at IS NULL",
                         (now, who, entry_id))  # fmt: skip
    return cur.rowcount == 1


# ---- history, events, conditions ---------------------------------------------------------------------------------
def sensor_history(db: sqlite3.Connection, site: Site, name: str, since: float, now: float, points: int = 240) -> dict[str, Any]:
    """A probe as [ts, mean, min, max] per step (at most `points` steps), or a switch as its changes."""
    sensor = site.sensors.get(name)
    ids = [r[0] for r in db.execute("SELECT id FROM sensors WHERE name = ?", (name,))]
    if not ids:
        known = sorted(n for n, s in site.sensors.items() if s.active)
        raise ValueError(f"no history for {name!r}; sensors: {', '.join(known)}")
    marks = ",".join("?" for _ in ids)
    out: dict[str, Any] = {"sensor": name, "label": sensor.label if sensor else name, "unit": sensor.unit if sensor else ""}
    if sensor is not None and sensor.unit == "switch":
        rows = db.execute(f"SELECT ts, closed FROM switch_states WHERE sensor_id IN ({marks}) AND ts >= ? ORDER BY ts",
                          (*ids, int(since))).fetchall()  # fmt: skip
        before = db.execute(f"SELECT closed FROM switch_states WHERE sensor_id IN ({marks}) AND ts < ? ORDER BY ts DESC LIMIT 1",
                            (*ids, int(since))).fetchone()  # fmt: skip
        out["state_before"] = None if before is None else ("CLOSED" if before[0] else "OPEN")
        out["changes"] = [[float(ts), "CLOSED" if closed else "OPEN"] for ts, closed in rows]
        return out
    if sensor is not None and sensor.test:
        out["tests"] = beside_probe(db, ids, sensor.test, since)
    step = max(60, int((now - since) / max(points, 1)) // 60 * 60)
    rows = db.execute(
        f"SELECT (ts / {step}) * {step} AS b, AVG(value), MIN(value), MAX(value) FROM readings "
        f"WHERE sensor_id IN ({marks}) AND ts >= ? GROUP BY b ORDER BY b",
        (*ids, int(since)),
    ).fetchall()
    out["step_seconds"] = step
    out["points"] = [[float(b), round(mean, 3), round(low, 3), round(high, 3)] for b, mean, low, high in rows]
    return out


def beside_probe(db: sqlite3.Connection, ids: list[int], parameter: str, since: float) -> list[dict[str, Any]]:
    """The manual tests of the parameter the probe measures, each with the probe's mean reading in the 10 minutes
    around it and the difference (probe - test): how far the probe is off."""
    out = []
    marks = ",".join("?" for _ in ids)
    for test in water_tests(db, parameter, since, 200):
        row = db.execute(
            f"SELECT AVG(value) FROM readings WHERE sensor_id IN ({marks}) AND ts BETWEEN ? AND ?",
            (*ids, int(test["measured_at"]) - 300, int(test["measured_at"]) + 300),
        ).fetchone()
        probe = None if row is None or row[0] is None else round(float(row[0]), 3)
        out.append({"measured_at": test["measured_at"], "value": test["value"], "unit": test["unit"], "kit": test["kit"],
                    "probe": probe, "difference": None if probe is None else round(probe - test["value"], 3)})  # fmt: skip
    return out


def outlet_history(db: sqlite3.Connection, name: str, since: float) -> dict[str, Any]:
    """An output's changes of mode or energized state since a time, with the state it was in before."""
    ids = [r[0] for r in db.execute("SELECT id FROM outlets WHERE name = ?", (name,))]
    if not ids:
        raise ValueError(f"no output named {name!r} in the history")
    marks = ",".join("?" for _ in ids)
    cols = "ts, mode, manual, energized, health, intensity"
    rows = db.execute(f"SELECT {cols} FROM outlet_states WHERE outlet_id IN ({marks}) AND ts >= ? ORDER BY ts",
                      (*ids, int(since))).fetchall()  # fmt: skip
    before = db.execute(f"SELECT {cols} FROM outlet_states WHERE outlet_id IN ({marks}) AND ts < ? ORDER BY ts DESC LIMIT 1",
                        (*ids, int(since))).fetchone()  # fmt: skip

    def state(row: tuple[Any, ...]) -> dict[str, Any]:
        return {"ts": float(row[0]), "mode": row[1], "manual": bool(row[2]), "energized": bool(row[3]), "health": row[4],
                "intensity": row[5]}  # fmt: skip

    return {"output": name, "state_before": state(before) if before else None, "changes": [state(r) for r in rows]}


def events(db: sqlite3.Connection, since: float, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    where, args = ["ts >= ?"], [since]
    if kind:
        where.append("kind LIKE ?")
        args.append(kind.replace("*", "%") + ("" if "*" in kind or "." in kind else ".%"))
    rows = db.execute(
        f"SELECT ts, kind, subject, detail FROM events WHERE {' AND '.join(where)} ORDER BY ts DESC, id DESC LIMIT ?",
        (*args, max(1, min(limit, 500))),
    ).fetchall()
    return [{"ts": ts, "kind": k, "subject": s, "detail": d} for ts, k, s, d in rows]


def conditions(db: sqlite3.Connection, site: Site) -> list[dict[str, Any]]:
    """Every alert condition with its limit and its state as the worker last judged it."""
    states = {c["id"]: c for c in kv(db, "conditions", []) or []}
    out = []
    for c in site.conditions:
        s = states.get(c.id, {})
        out.append({"id": c.id, "alarm": c.alarm, "severity": c.severity, "text": c.text, "kind": c.kind, "subject": c.subject,
                    "limit": c.limit, "clear": c.clear or None, "pending_seconds": c.pending, "active": bool(s.get("active")),
                    "held": bool(s.get("held")), "since": s.get("since"), "detail": s.get("detail") or None})  # fmt: skip
    return out
