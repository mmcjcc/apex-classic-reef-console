"""Filling the history from the unit's own 10-minute log (datalog.json): the format of firmware 4.53, poll rows win,
and when the worker asks (quiet hours, minutes apart, oldest day first, never again tonight after an error)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reef.apex import ApexError, parse_datalog, parse_outlog, parse_status
from reef.conditions import Engine
from reef.main import LOG_PAUSE_SECONDS, Worker
from reef.metrics import Metrics
from reef.site import Datalog, parse_site
from reef.store import Store

from .test_worker import RAW, SITE, T0, FixedClient

# the first record of 2026-10-11 as the unit served it (datalog.json?sdate=261011&days=0, read 2026-10-12)
FIRST = {"date": 1791687600, "data": [
    {"name": "Tmp", "did": "base_Temp", "type": "Temp", "value": "80.8"},
    {"name": "pH", "did": "base_pH", "type": "pH", "value": "8.43"},
    {"name": "ORP", "did": "base_ORP", "type": "ORP", "value": "359"},
    {"name": "Amp_3", "did": "3_0", "type": "Amps", "value": "2.3"},
    {"name": "Amp_4", "did": "4_0", "type": "Amps", "value": "1.8"},
    {"name": "Tmpx5", "did": "5_0", "type": "Temp", "value": "21.7"},
    {"name": "pHx5", "did": "5_1", "type": "pH", "value": "7.05"},
    {"name": "Tmpx6", "did": "6_0", "type": "Temp", "value": "82.0"},
    {"name": "Condx6", "did": "6_1", "type": "Cond", "value": "36.3"},
    {"name": "Amp_9", "did": "9_0", "type": "Amps", "value": "2.0"},
]}  # fmt: skip
NIGHT = datetime(2026, 10, 13, 3, 0, tzinfo=UTC).timestamp()  # 23:00 in New York, inside the quiet hours 22-06
ACTIVE_PROBES = 9  # of the ten logged probes, Tmpx5 is not in use


def log_doc(day: str) -> dict[str, Any]:
    """A whole day as the unit logs it: 144 records from midnight in its timezone (-3.00, 03:00 UTC)."""
    start = datetime.strptime(day, "%y%m%d").replace(tzinfo=UTC).timestamp() + 3 * 3600
    return {
        "ilog": {"serial": "AC4:12345", "timezone": "-3.00", "record": [FIRST | {"date": start + n * 600} for n in range(144)]}
    }


def test_the_units_log_of_firmware_4_53() -> None:
    serial, records = parse_datalog({"ilog": {"serial": "AC4:12345", "record": [FIRST, {"date": "x"}]}})
    assert serial == "AC4:12345" and len(records) == 1
    assert records[0].ts == 1791687600 and records[0].values["Tmp"] == ("base_Temp", "Temp", 80.8)
    assert len(records[0].values) == 10
    with pytest.raises(ApexError):
        parse_datalog({"istat": {}})


def test_logged_values_fill_only_where_the_polls_have_none(tmp_path: Path) -> None:
    store = Store(tmp_path / "reef.db")
    polled = parse_status(RAW, 1791687600 + 125)  # a poll inside the first 10-minute step
    store.record(polled, SITE)
    _serial, records = parse_datalog(log_doc("261011"))
    inserted = store.fill_from_log("AC4:12345", records[:3], SITE)
    assert inserted == 2 * ACTIVE_PROBES  # the first step has a poll: the poll wins
    tmp = store.db.execute(
        "SELECT r.ts, r.value FROM readings r JOIN sensors s ON s.id = r.sensor_id WHERE s.name = 'Tmp' ORDER BY r.ts"
    ).fetchall()
    assert tmp == [(1791687725, 79.0), (1791688200, 80.8), (1791688800, 80.8)]
    assert store.db.execute("SELECT COUNT(*) FROM sensors WHERE name = 'Tmpx5'").fetchone()[0] == 0
    first_seen, last_seen = store.db.execute("SELECT first_seen, last_seen FROM sensors WHERE name = 'Tmp'").fetchone()
    assert first_seen == 1791687725 and last_seen == 1791687725  # the log reached no earlier than the poll here
    assert store.fill_from_log("AC4:12345", records[:3], SITE) == 0  # asking twice stores nothing twice
    store.close()


class LogClient(FixedClient):
    def __init__(self, documents: list[dict[str, Any]], fail: bool = False) -> None:
        super().__init__(documents)
        self.days: list[str] = []
        self.failures = 0
        self.fail = fail

    async def datalog(self, day: str) -> tuple[str, list[Any]]:
        self.days.append(day)
        if self.fail:
            raise ApexError("ReadTimeout: timed out")
        return parse_datalog(log_doc(day))


async def polled_worker(tmp_path: Path, client: LogClient, backfill: bool = True) -> Worker:
    site = dataclasses.replace(SITE, datalog=Datalog(backfill=backfill, hours=(22, 6), spacing=300, days_back=3))
    store = Store(tmp_path / "reef.db")
    engine = Engine(site)
    worker = Worker(site, store, client, Metrics(site, engine), engine, tmp_path)  # type: ignore[arg-type]
    for _ in range(3):
        await worker.poll_once()  # polls at T0 + 60 ... + 180: 2026-10-12 00:48 UTC on
    return worker


async def test_the_month_the_unit_holds_comes_one_day_at_a_time_at_night(tmp_path: Path) -> None:
    client = LogClient([RAW, RAW, RAW])
    worker = await polled_worker(tmp_path, client)
    await worker.fill_gaps(T0 + 300)  # 20:52 in New York: not in the quiet hours
    assert client.days == []
    await worker.fill_gaps(NIGHT)
    await worker.fill_gaps(NIGHT + 60)  # minutes apart, not one after another
    assert client.days == ["261010"]  # the oldest day first: before polling began, so only with backfill on
    await worker.fill_gaps(NIGHT + 300)
    await worker.fill_gaps(NIGHT + 600)
    await worker.fill_gaps(NIGHT + 900)
    assert client.days == ["261010", "261011", "261012"]
    await worker.fill_gaps(NIGHT + 1200)  # nothing left: today is not over
    assert len(client.days) == 3
    rows = worker.store.db.execute("SELECT day, records, inserted, note FROM datalog_days ORDER BY day").fetchall()
    assert rows == [("2026-10-10", 144, 144 * ACTIVE_PROBES, "ok"), ("2026-10-11", 144, 142 * ACTIVE_PROBES, "ok"),
                    ("2026-10-12", 144, 144 * ACTIVE_PROBES, "ok")]  # fmt: skip
    events = worker.store.db.execute(
        "SELECT kind, subject, detail FROM events WHERE kind LIKE 'history.%' ORDER BY id"
    ).fetchall()
    assert events == [
        ("history.fill", "2026-10-11", f"{142 * ACTIVE_PROBES} readings from the unit's own log fill a gap"),
        ("history.fill", "2026-10-12", f"{144 * ACTIVE_PROBES} readings from the unit's own log fill a gap"),
        ("history.backfill", "apex", f"{2 * 144 * ACTIVE_PROBES - 2 * ACTIVE_PROBES} readings for 2 days from 2026-10-10, "
                                     "from the unit's own log"),
    ]  # fmt: skip
    since = worker.store.db.execute("SELECT MIN(first_seen) FROM sensors").fetchone()[0]
    assert since == datetime(2026, 10, 10, 3, 0, tzinfo=UTC).timestamp()  # the page's "history since" moves back
    worker.store.close()


async def test_without_backfill_only_gaps_after_polling_began_are_filled(tmp_path: Path) -> None:
    client = LogClient([RAW, RAW, RAW])
    worker = await polled_worker(tmp_path, client, backfill=False)
    for n in range(4):
        await worker.fill_gaps(NIGHT + 600 + n * 300)
    assert client.days == ["261011", "261012"]
    worker.store.close()


async def test_an_error_ends_tonights_requests_and_the_day_is_tried_again(tmp_path: Path) -> None:
    client = LogClient([RAW, RAW, RAW], fail=True)
    worker = await polled_worker(tmp_path, client)
    await worker.fill_gaps(NIGHT)
    await worker.fill_gaps(NIGHT + 300)
    assert client.days == ["261010"] and worker.next_log_request == NIGHT + LOG_PAUSE_SECONDS
    assert worker.store.log_day("2026-10-10") is None  # not marked done
    kind, detail = worker.store.db.execute("SELECT kind, detail FROM events WHERE kind = 'history.paused'").fetchone()
    assert "no more tonight" in detail and "timed out" in detail
    worker.latency = 3.5  # the unit answers status slowly: no log request at all
    client.fail = False
    worker.next_log_request = 0
    await worker.fill_gaps(NIGHT + 86400)
    assert client.days == ["261010"]
    worker.store.close()


async def test_a_day_the_polls_cover_is_not_asked_for(tmp_path: Path) -> None:
    client = LogClient([])
    worker = await polled_worker(tmp_path, LogClient([RAW, RAW, RAW]))
    worker.client = client  # type: ignore[assignment]
    start = datetime(2026, 10, 12, 3, 0, tzinfo=UTC).timestamp()
    for n in range(144):
        worker.store.record(parse_status(RAW, start + n * 600 + 30), SITE)
    found = worker.next_log_day(NIGHT + 600)
    assert found is not None and found[0].isoformat() == "2026-10-10"
    assert worker.store.log_day("2026-10-12") is None  # not reached yet: the older days come first
    worker.store.save_log_day("2026-10-10", NIGHT, 0, 0, "empty")
    worker.store.save_log_day("2026-10-11", NIGHT, 0, 0, "ok")
    assert worker.next_log_day(NIGHT + 600) is None
    assert worker.store.log_day("2026-10-12") == (NIGHT + 600, "covered by polls")
    worker.store.close()


def test_the_quiet_hours_may_wrap_midnight() -> None:
    log = Datalog(hours=(22, 6))
    assert [h for h in range(24) if log.in_hours(h)] == [0, 1, 2, 3, 4, 5, 22, 23]
    assert [h for h in range(24) if Datalog(hours=(1, 5)).in_hours(h)] == [1, 2, 3, 4]
    with pytest.raises(ValueError):
        parse_site({"datalog": {"hours": [3, 3]}})


# ---- the unit's outlet log ---------------------------------------------------------------------------------------
OUTLOG = {"olog": {"serial": "AC4:12345", "record": [
    {"date": 1788843376, "did": "base_Warn", "name": "SndWrn_I7", "value": "OFF"},
    {"date": 1788843377, "did": "base_Warn", "name": "SndWrn_I7", "value": "ON"},
    {"date": 1788861600, "did": "3_3", "name": "fugelight", "value": "ON"},
    {"date": 1788861601, "did": "3_3", "name": "fugelight", "value": "dimmed"},
]}}  # fmt: skip


def test_the_units_outlet_log_joins_the_outlets_the_polls_know(tmp_path: Path) -> None:
    serial, changes = parse_outlog(OUTLOG)
    assert serial == "AC4:12345" and [(c.name, c.energized) for c in changes] == [
        ("SndWrn_I7", False), ("SndWrn_I7", True), ("fugelight", True)]  # fmt: skip
    store = Store(tmp_path / "reef.db")
    store.record(parse_status(RAW, T0), SITE)  # the polls know SndWrn_I7 as an alarm output
    assert store.fill_outlog(serial, changes) == 3 and store.fill_outlog(serial, changes) == 0
    rows = store.db.execute(
        "SELECT o.name, o.type, l.energized FROM outlet_log l JOIN outlets o ON o.id = l.outlet_id ORDER BY l.ts, l.energized"
    ).fetchall()
    assert rows == [("SndWrn_I7", "alert", 0), ("SndWrn_I7", "alert", 1), ("fugelight", "outlet", 1)]
    store.close()


class OutlogClient(LogClient):
    def __init__(self, documents: list[dict[str, Any]], fail: bool = False) -> None:
        super().__init__(documents, fail)
        self.outlog_days: list[str] = []

    async def outlog(self, day: str) -> tuple[str, list[Any]]:
        self.outlog_days.append(day)
        if self.fail:
            raise ApexError("ReadTimeout: timed out")
        return parse_outlog(OUTLOG)


async def test_todays_outlet_log_hourly_and_the_past_days_once_at_night(tmp_path: Path) -> None:
    client = OutlogClient([RAW, RAW, RAW])
    worker = await polled_worker(tmp_path, client)
    await worker.refresh_outlog(NIGHT)  # today first
    await worker.refresh_outlog(NIGHT + 30)  # nothing: a minute apart for the past days
    await worker.refresh_outlog(NIGHT + 60)
    await worker.refresh_outlog(NIGHT + 120)
    await worker.refresh_outlog(NIGHT + 180)
    await worker.refresh_outlog(NIGHT + 240)  # all three past days done
    assert client.outlog_days == ["261013", "261010", "261011", "261012"]
    await worker.refresh_outlog(NIGHT + 3600)  # today again, an hour later
    assert client.outlog_days[-1] == "261013" and len(client.outlog_days) == 5
    assert worker.store.outlog_day("2026-10-11") == NIGHT + 120 and worker.store.outlog_day("2026-10-13") is None
    worker.store.close()


async def test_an_outlet_log_error_waits_an_hour(tmp_path: Path) -> None:
    client = OutlogClient([RAW, RAW, RAW], fail=True)
    worker = await polled_worker(tmp_path, client)
    await worker.refresh_outlog(NIGHT)
    await worker.refresh_outlog(NIGHT + 600)
    assert client.outlog_days == ["261013"] and worker.next_outlog_past == NIGHT + 3600
    worker.store.close()
