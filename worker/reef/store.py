"""History in SQLite (decision D1): one file on the worker's volume.

Sensors and outlets get their own ids, keyed by the controller's serial, the device id and the name, so a renamed
outlet or a replaced controller starts a new row instead of mixing histories. Probe values are stored at every poll;
switches and outlets only when something changes (mode, energized, health). The time axis is the worker's clock.
The programs are kept as dated versions (a new row only when they change). Water tests are written by reef-web;
both processes create the tables they need (ensure_schema), whichever starts first."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .apex import LogRecord, OutletChange, Status
from .programs import Programs, digest
from .site import Site

SCHEMA = """
CREATE TABLE IF NOT EXISTS sensors (
    id INTEGER PRIMARY KEY, serial TEXT NOT NULL, did TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, UNIQUE (serial, did, name));
CREATE TABLE IF NOT EXISTS readings (
    sensor_id INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL NOT NULL, PRIMARY KEY (sensor_id, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS switch_states (
    sensor_id INTEGER NOT NULL, ts INTEGER NOT NULL, closed INTEGER NOT NULL, PRIMARY KEY (sensor_id, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS outlets (
    id INTEGER PRIMARY KEY, serial TEXT NOT NULL, did TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, UNIQUE (serial, did, name));
CREATE TABLE IF NOT EXISTS outlet_states (
    outlet_id INTEGER NOT NULL, ts INTEGER NOT NULL, mode TEXT NOT NULL, manual INTEGER NOT NULL,
    energized INTEGER NOT NULL, health TEXT NOT NULL, intensity INTEGER, PRIMARY KEY (outlet_id, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS conditions_active (id TEXT PRIMARY KEY, since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS program_versions (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, serial TEXT NOT NULL, digest TEXT NOT NULL, programs TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS water_tests (
    id INTEGER PRIMARY KEY, measured_at REAL NOT NULL, parameter TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL,
    entered_value REAL NOT NULL, entered_unit TEXT NOT NULL, kit TEXT NOT NULL, kit_error REAL, note TEXT NOT NULL,
    entered_at REAL NOT NULL, entered_by TEXT NOT NULL, source TEXT NOT NULL, deleted_at REAL, deleted_by TEXT);
CREATE INDEX IF NOT EXISTS water_tests_parameter ON water_tests (parameter, measured_at);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, period_start REAL NOT NULL, period_end REAL NOT NULL, headline TEXT NOT NULL,
    body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, entered_at REAL NOT NULL,
    entered_by TEXT NOT NULL, source TEXT NOT NULL, deleted_at REAL, deleted_by TEXT);
CREATE INDEX IF NOT EXISTS journal_ts ON journal (ts);
CREATE TABLE IF NOT EXISTS outlet_log (
    outlet_id INTEGER NOT NULL, ts INTEGER NOT NULL, energized INTEGER NOT NULL,
    PRIMARY KEY (outlet_id, ts, energized)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS outlog_days (day TEXT PRIMARY KEY, fetched_at REAL NOT NULL, records INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS datalog_days (
    day TEXT PRIMARY KEY, fetched_at REAL NOT NULL, records INTEGER NOT NULL, inserted INTEGER NOT NULL, note TEXT NOT NULL);
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.db = sqlite3.connect(str(path), timeout=30)
        ensure_schema(self.db)
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._sensor_ids: dict[tuple[str, str, str], int] = {}
        self._outlet_ids: dict[tuple[str, str, str], int] = {}
        self._last_switch: dict[int, int] = dict(
            self.db.execute(
                "SELECT sensor_id, closed FROM switch_states s WHERE ts = "
                "(SELECT MAX(ts) FROM switch_states WHERE sensor_id = s.sensor_id)"
            ).fetchall()  # fmt: skip
        )
        self._last_outlet: dict[int, tuple[Any, ...]] = {
            row[0]: tuple(row[1:])
            for row in self.db.execute(
                "SELECT outlet_id, mode, manual, energized, health, intensity FROM outlet_states o WHERE ts = "
                "(SELECT MAX(ts) FROM outlet_states WHERE outlet_id = o.outlet_id)"
            )
        }

    def _id(
        self, table: str, cache: dict[tuple[str, str, str], int], serial: str, did: str, name: str, kind: str, ts: float
    ) -> int:
        key = (serial, did, name)
        if key in cache:
            self.db.execute(f"UPDATE {table} SET last_seen = ? WHERE id = ?", (ts, cache[key]))
            return cache[key]
        row = self.db.execute(f"SELECT id FROM {table} WHERE serial = ? AND did = ? AND name = ?", key).fetchone()
        if row is None:
            cur = self.db.execute(
                f"INSERT INTO {table} (serial, did, name, type, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (serial, did, name, kind, ts, ts),
            )
            new_id = cur.lastrowid
            assert new_id is not None
            cache[key] = new_id
        else:
            cache[key] = int(row[0])
            self.db.execute(f"UPDATE {table} SET last_seen = ? WHERE id = ?", (ts, cache[key]))
        return cache[key]

    def record(self, status: Status, site: Site) -> None:
        ts = int(status.received_at)
        with self.db:
            for reading in status.inputs.values():
                sensor = site.sensors.get(reading.name)
                if sensor is not None and not sensor.active:
                    continue
                sid = self._id("sensors", self._sensor_ids, status.serial, reading.did, reading.name, reading.type, ts)
                if reading.type == "digital":
                    closed = int(reading.closed)
                    if self._last_switch.get(sid) != closed:
                        self.db.execute("INSERT OR REPLACE INTO switch_states VALUES (?, ?, ?)", (sid, ts, closed))
                        self._last_switch[sid] = closed
                else:
                    self.db.execute("INSERT OR IGNORE INTO readings VALUES (?, ?, ?)", (sid, ts, reading.value))
            for out in status.outputs.values():
                oid = self._id("outlets", self._outlet_ids, status.serial, out.did, out.name, out.type, ts)
                state = (out.mode, int(out.manual), int(out.energized), out.health, out.intensity)
                if self._last_outlet.get(oid) != state:
                    self.db.execute("INSERT OR REPLACE INTO outlet_states VALUES (?, ?, ?, ?, ?, ?, ?)", (oid, ts, *state))
                    self._last_outlet[oid] = state

    def fill_from_log(self, serial: str, records: list[LogRecord], site: Site, bucket: int = 600) -> int:
        """Readings from the unit's own log where the polls have none: a logged value is stored only if no reading of
        that sensor lies in its 10-minute step (poll rows win). Returns how many were stored."""
        inserted = 0
        with self.db:
            for record in records:
                ts = int(record.ts)
                for name, (did, kind, value) in record.values.items():
                    sensor = site.sensors.get(name)
                    if sensor is not None and not sensor.active:
                        continue
                    sid = self._log_sensor(serial, did, name, kind, ts)
                    cur = self.db.execute(
                        "INSERT OR IGNORE INTO readings SELECT ?, ?, ? WHERE NOT EXISTS "
                        "(SELECT 1 FROM readings WHERE sensor_id = ? AND ts >= ? AND ts < ?)",
                        (sid, ts, value, sid, ts, ts + bucket),
                    )
                    if cur.rowcount:
                        inserted += 1
                        self.db.execute("UPDATE sensors SET first_seen = MIN(first_seen, ?) WHERE id = ?", (ts, sid))
        return inserted

    def _log_sensor(self, serial: str, did: str, name: str, kind: str, ts: int) -> int:
        """A sensor's id for a logged value: like _id, but it never moves last_seen (fill_from_log moves first_seen)."""
        key = (serial, did, name)
        row = self.db.execute("SELECT id FROM sensors WHERE serial = ? AND did = ? AND name = ?", key).fetchone()
        if row is None:
            cur = self.db.execute(
                "INSERT INTO sensors (serial, did, name, type, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (serial, did, name, kind, ts, ts),
            )
            assert cur.lastrowid is not None
            self._sensor_ids[key] = cur.lastrowid
            return cur.lastrowid
        return int(row[0])

    def fill_outlog(self, serial: str, changes: list[OutletChange]) -> int:
        """The unit's outlet changes (exact times, also the short ones a minute poll misses); a change already stored
        is not stored twice. Returns how many were new."""
        inserted = 0
        with self.db:
            for change in changes:
                key = (serial, change.did, change.name)
                row = self.db.execute("SELECT id FROM outlets WHERE serial = ? AND did = ? AND name = ?", key).fetchone()
                if row is None:
                    cur = self.db.execute(
                        "INSERT INTO outlets (serial, did, name, type, first_seen, last_seen) VALUES (?, ?, ?, '', ?, ?)",
                        (*key, change.ts, change.ts),
                    )
                    outlet_id = cur.lastrowid
                else:
                    outlet_id = row[0]
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO outlet_log VALUES (?, ?, ?)", (outlet_id, int(change.ts), int(change.energized))
                )
                inserted += cur.rowcount
        return inserted

    def outlog_day(self, day: str) -> float | None:
        row = self.db.execute("SELECT fetched_at FROM outlog_days WHERE day = ?", (day,)).fetchone()
        return None if row is None else float(row[0])

    def save_outlog_day(self, day: str, fetched_at: float, records: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO outlog_days VALUES (?, ?, ?)", (day, fetched_at, records))

    def fill_external(self, serial: str, did: str, name: str, kind: str, rows: list[tuple[float, float]]) -> int:
        """Readings from outside the Apex (the outdoor temperature); a time already stored is kept. Returns how many
        were new."""
        inserted = 0
        with self.db:
            for ts, value in rows:
                sid = self._log_sensor(serial, did, name, kind, int(ts))
                cur = self.db.execute("INSERT OR IGNORE INTO readings VALUES (?, ?, ?)", (sid, int(ts), value))
                if cur.rowcount:
                    inserted += 1
                    self.db.execute("UPDATE sensors SET first_seen = MIN(first_seen, ?), last_seen = MAX(last_seen, ?) "
                                    "WHERE id = ?", (int(ts), int(ts), sid))  # fmt: skip
        return inserted

    def save_report(self, ts: float, report: dict[str, Any]) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO reports (ts, period_start, period_end, headline, body) VALUES (?, ?, ?, ?, ?)",
                (ts, report["period_start"], report["period_end"], report["headline"], json.dumps(report, ensure_ascii=False)),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def latest_report(self) -> tuple[float, str] | None:
        row = self.db.execute("SELECT ts, headline FROM reports ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else (float(row[0]), str(row[1]))

    def covered_steps(self, start: float, end: float, bucket: int = 600) -> int:
        """How many 10-minute steps between start and end have at least one reading (of any sensor)."""
        row = self.db.execute(
            f"SELECT COUNT(DISTINCT ts / {bucket}) FROM readings WHERE ts >= ? AND ts < ?", (int(start), int(end))
        ).fetchone()
        return int(row[0])

    def log_day(self, day: str) -> tuple[float, str] | None:
        row = self.db.execute("SELECT fetched_at, note FROM datalog_days WHERE day = ?", (day,)).fetchone()
        return None if row is None else (float(row[0]), str(row[1]))

    def save_log_day(self, day: str, fetched_at: float, records: int, inserted: int, note: str) -> None:
        with self.db:
            row = (day, fetched_at, records, inserted, note)
            self.db.execute("INSERT OR REPLACE INTO datalog_days VALUES (?, ?, ?, ?, ?)", row)

    def event(self, ts: float, kind: str, subject: str, detail: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO events (ts, kind, subject, detail) VALUES (?, ?, ?, ?)", (ts, kind, subject, detail))

    def save_active(self, active: dict[str, float]) -> None:
        with self.db:
            self.db.execute("DELETE FROM conditions_active")
            self.db.executemany("INSERT INTO conditions_active VALUES (?, ?)", list(active.items()))

    def load_active(self) -> dict[str, float]:
        return {str(k): float(v) for k, v in self.db.execute("SELECT id, since FROM conditions_active")}

    def latest_programs(self) -> tuple[int, Programs] | None:
        row = self.db.execute("SELECT id, programs FROM program_versions ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else (int(row[0]), json.loads(row[1]))

    def save_programs(self, ts: float, serial: str, programs: Programs) -> bool:
        """Store the programs as a new version if they differ from the latest one; True if they did."""
        latest = self.latest_programs()
        if latest is not None and digest(latest[1]) == digest(programs):
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO program_versions (ts, serial, digest, programs) VALUES (?, ?, ?, ?)",
                (ts, serial, digest(programs), json.dumps(programs, ensure_ascii=False)),
            )
        return True

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def kv_set(self, key: str, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))

    def kv_delete(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM kv WHERE key = ?", (key,))

    def backup(self, target: Path) -> None:
        """A consistent copy while the worker runs (SQLite's online backup)."""
        dest = sqlite3.connect(str(target))
        try:
            self.db.backup(dest)
        finally:
            dest.close()

    def close(self) -> None:
        self.db.close()
