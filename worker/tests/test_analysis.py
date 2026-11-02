"""The analysis on made-up tanks: a quiet one gives nothing; a spike, a drift toward an alert limit, a level step and a
frozen probe are each found once, with their evidence; the worker keeps the result and announces new findings once."""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from reef import main as main_module
from reef.analysis import STEP, analyse, fit, forecast
from reef.conditions import Engine
from reef.main import Worker
from reef.metrics import Metrics
from reef.store import Store

from .test_worker import SITE, FixedClient

NOW = 1_791_000_000.0  # any time: the analysis uses only the times it is given
DAYS = 21


def tank(
    base: float, swing: float, noise: float, days: int = DAYS, drift: float = 0.0, seed: int = 1
) -> list[tuple[float, float]]:
    """A probe read every 10 minutes for `days` up to NOW: a daily cycle, noise, and an optional drift per day."""
    rng = random.Random(seed)
    start = NOW - days * 86400
    rows = []
    for n in range(days * 144):
        ts = start + n * STEP
        day = (ts % 86400) / 86400
        rows.append((ts, round(base + swing * math.sin(2 * math.pi * day) + rng.gauss(0, noise) + drift * n / 144, 2)))
    return rows


def kinds(result: dict) -> list[tuple[str, str]]:
    return [(f["kind"], f["sensor"]) for f in result["findings"]]


def test_a_quiet_tank_gives_no_findings_and_a_forecast() -> None:
    result = analyse(SITE, {"Tmp": tank(79.0, 0.6, 0.05), "pH": tank(8.2, 0.08, 0.01)}, NOW)
    assert result["findings"] == []
    tmp = next(s for s in result["sensors"] if s["sensor"] == "Tmp")
    assert not tmp["learning"] and 1.0 < tmp["daily_range"] < 1.6 and tmp["spread"] <= 0.12
    assert len(tmp["forecast"]) == 48 and len(tmp["recent"]) == 7 * 48
    low = min(f[1] for f in tmp["forecast"])
    high = max(f[1] for f in tmp["forecast"])
    assert 78.2 < low < 78.7 and 79.3 < high < 79.8  # tomorrow's expected cycle is today's


def test_an_hour_long_spike_is_unusual() -> None:
    rows = tank(79.0, 0.6, 0.05)
    spike_start = NOW - 2 * 86400
    rows = [(ts, v + 1.5 if spike_start <= ts < spike_start + 3600 else v) for ts, v in rows]
    result = analyse(SITE, {"Tmp": rows}, NOW)
    assert kinds(result) == [("unusual", "Tmp")]
    finding = result["findings"][0]
    assert spike_start <= finding["start"] < spike_start + STEP and finding["end"] == spike_start + 3600
    assert "unusually high" in finding["summary"] and finding["evidence"]["z"] >= 4


def test_what_the_unit_switched_meanwhile_is_evidence() -> None:
    rows = tank(35.5, 0.05, 0.03)
    dip = NOW - 2 * 86400
    rows = [(ts, v - 2.0 if dip <= ts < dip + 7200 else v) for ts, v in rows]
    outlets = [(dip + 300, "SndWrn_I7", "alert", True), (dip + 900, "SndWrn_I7", "alert", False),
               (dip + 1200, "SndWrn_I7", "alert", True), (dip - 7200, "fugelight", "outlet", True)]  # fmt: skip
    result = analyse(SITE, {"Condx6": rows}, NOW, outlets)
    finding = result["findings"][0]
    assert finding["kind"] == "unusual" and "unusually low" in finding["summary"]
    assert finding["summary"].endswith("the Apex's own alarm output SndWrn_I7 went on meanwhile")
    assert finding["evidence"]["outlets"] == [{"output": "SndWrn_I7", "type": "alert", "on": 2, "off": 1, "first": dip + 300}]


def test_a_journal_entry_near_a_finding_is_shown_with_it() -> None:
    rows = tank(35.0, 0.05, 0.03)
    stepped = NOW - 6 * 86400
    rows = [(ts, v + 0.8 if ts >= stepped else v) for ts, v in rows]
    journal = [(stepped - 3600, "water change", "25 %, new salt"), (NOW - 20 * 86400, "dosing", "far away")]
    result = analyse(SITE, {"Condx6": rows}, NOW, [], journal)
    finding = result["findings"][0]
    assert finding["kind"] == "step" and finding["summary"].endswith("; journal: water change")
    assert finding["evidence"]["journal"] == [{"ts": stepped - 3600, "kind": "water change", "text": "25 %, new salt"}]


def test_one_odd_reading_is_not_unusual() -> None:
    rows = tank(79.0, 0.6, 0.05)
    rows[-100] = (rows[-100][0], rows[-100][1] + 3.0)
    assert analyse(SITE, {"Tmp": rows}, NOW)["findings"] == []


def test_a_drift_toward_an_alert_limit_says_when_it_gets_there() -> None:
    result = analyse(SITE, {"pH": tank(8.30, 0.08, 0.01, drift=-0.015)}, NOW)
    assert kinds(result) == [("drift", "pH")]
    finding = result["findings"][0]
    assert "falling by 0.10 a week" in finding["summary"] or "falling by 0.11 a week" in finding["summary"]
    assert finding["evidence"]["reaches"]["condition"] == "ph-low" and 3 < finding["evidence"]["reaches"]["days"] < 9


def test_a_level_step_is_a_step_not_a_drift() -> None:
    rows = tank(35.0, 0.05, 0.03)
    stepped = NOW - 6 * 86400
    rows = [(ts, v + 0.8 if ts >= stepped else v) for ts, v in rows]
    result = analyse(SITE, {"Condx6": rows}, NOW)
    assert kinds(result) == [("step", "Condx6")]
    assert result["findings"][0]["evidence"]["change"] > 0.7
    assert result["findings"][0]["start"] == stepped // 86400 * 86400  # dated on the day it happened


def test_a_probe_that_stops_moving_is_frozen() -> None:
    rows = tank(-0.0 + 360, 15, 2.0)  # ORP moves a few mV every hour
    frozen_from = NOW - 8 * 3600
    rows = [(ts, v) for ts, v in rows if ts < frozen_from] + [(frozen_from + n * 60, 351.0) for n in range(8 * 60)]
    result = analyse(SITE, {"ORP": rows}, NOW)
    assert ("frozen", "ORP") in kinds(result)
    assert "read exactly 351 mV for 6 hours" in next(f["summary"] for f in result["findings"] if f["kind"] == "frozen")


def test_too_little_history_is_still_learning() -> None:
    result = analyse(SITE, {"Tmp": tank(79.0, 0.6, 0.05, days=3)}, NOW)
    assert result["sensors"] == [{"sensor": "Tmp", "label": "Temperature", "unit": "°F", "learning": True, "days": 3.0}]
    assert fit("Tmp", "Temperature", "°F", []) is None


def test_the_forecast_follows_the_latest_level() -> None:
    model = fit("Tmp", "Temperature", "°F", tank(79.0, 0.6, 0.05))
    assert model is not None
    band = forecast(model, NOW, hours=2)
    assert len(band) == 4 and all(b[2] < b[1] < b[3] for b in band)


async def test_the_worker_keeps_the_analysis_and_announces_a_finding_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "reef.db")
    rows = tank(79.0, 0.6, 0.05)
    spike_start = NOW - 86400
    sid = store.db.execute(
        "INSERT INTO sensors (serial, did, name, type, first_seen, last_seen) VALUES ('AC4', 'base_Temp', 'Tmp', 'Temp', 0, 0)"
    ).lastrowid
    store.db.executemany(
        "INSERT INTO readings VALUES (?, ?, ?)",
        [(sid, int(ts), v + 1.5 if spike_start <= ts < spike_start + 3600 else v) for ts, v in rows],
    )
    store.db.commit()
    engine = Engine(SITE)
    worker = Worker(SITE, store, FixedClient([]), Metrics(SITE, engine), engine, tmp_path)  # type: ignore[arg-type]
    await worker.refresh_analysis(NOW)
    await worker.refresh_analysis(NOW + 600)  # within the hour: not again
    await worker.refresh_analysis(NOW + 3600)  # again, the same finding: no second event
    result = store.kv_get("analysis")
    assert [(f["kind"], f["sensor"]) for f in result["findings"]] == [("unusual", "Tmp")]
    events = store.db.execute("SELECT kind, subject FROM events").fetchall()
    assert events == [("analysis.unusual", "Tmp")]
    store.close()


def test_single_odd_readings_half_an_hour_apart_are_not_a_spell() -> None:
    rows = tank(360.0, 15, 1.0)
    odd = {int(NOW - 86400) // STEP * STEP + n * 1800 for n in range(3)}  # 01:00, 01:30, 02:00 style pulses
    rows = [(ts, v + 30 if int(ts) in odd else v) for ts, v in rows]
    assert analyse(SITE, {"ORP": rows}, NOW)["findings"] == []


def test_a_dip_on_most_nights_is_one_recurring_finding_not_the_normal_day() -> None:
    rows = tank(35.5, 0.05, 0.03)
    start = NOW - DAYS * 86400

    def dipped(ts: float) -> bool:
        day, second = divmod(ts - start, 86400)
        return day % 7 not in (2, 5) and 18000 <= second < 25200  # two hours on 5 nights out of 7

    rows = [(ts, v - 2.0 if dipped(ts) else v) for ts, v in rows]
    result = analyse(SITE, {"Condx6": rows}, NOW)
    assert kinds(result) == [("recurring", "Condx6")]  # the nights without the dip are not "unusually high"
    finding = result["findings"][0]
    assert "recurring dip of about" in finding["summary"] and abs(finding["evidence"]["size"] + 2.0) < 0.15
    assert finding["evidence"]["days_showing"] >= 13
    assert finding["start"] < NOW and finding["end"] - finding["start"] >= 7200  # its latest occurrence, never ahead
    expected = next(s for s in result["sensors"] if s["sensor"] == "Condx6")["forecast"]
    assert min(f[1] for f in expected) > 34.9  # the expected day stays a plausible day


def test_a_bar_with_only_constant_loads_is_not_frozen() -> None:
    rows = tank(2.4, 0.6, 0.15)
    still = NOW - 8 * 3600
    rows = [(ts, v) for ts, v in rows if ts < still] + [(still + n * 60, 1.8) for n in range(8 * 60)]
    assert analyse(SITE, {"Amp_3": rows}, NOW)["findings"] == []


async def test_a_finding_is_announced_once_however_long_it_lasts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def found(kind: str, start: float, end: float, summary: str) -> dict:
        return {"kind": kind, "sensor": "ORP", "start": start, "end": end, "summary": summary, "evidence": {}}

    runs = [
        [found("unusual", NOW - 7200, NOW - 3600, "spell")],
        [found("unusual", NOW - 6600, NOW, "spell, longer")],  # the same spell: its start moved
        [found("unusual", NOW + 7200, NOW + 9000, "another spell"), found("frozen", NOW - 3600, NOW + 7200, "stuck")],
        [found("frozen", NOW, NOW + 10800, "stuck")],  # still stuck an hour later
    ]
    monkeypatch.setattr(main_module, "analyse", lambda *args: {"findings": runs.pop(0), "sensors": []})
    store = Store(tmp_path / "reef.db")
    engine = Engine(SITE)
    worker = Worker(SITE, store, FixedClient([]), Metrics(SITE, engine), engine, tmp_path)  # type: ignore[arg-type]
    for hour in range(4):
        await worker.refresh_analysis(NOW + hour * 3600)
    events = store.db.execute("SELECT kind, detail FROM events ORDER BY id").fetchall()
    assert events == [("analysis.unusual", "spell"), ("analysis.frozen", "stuck"), ("analysis.unusual", "another spell")]
    store.close()


def test_a_steady_tank_that_often_reads_one_value_for_hours_is_not_frozen() -> None:
    rows = tank(79.0, 0.3, 0.04)
    # four steady nights in 21 days (5 % of the six-hour blocks), on the blocks as the check counts them
    still_blocks = [int(NOW - d * 86400) // 21600 * 21600 - 21600 for d in (3, 7, 11, 15)]
    rows = [(ts, 79.0 if any(b <= ts < b + 6 * 3600 for b in still_blocks) else v) for ts, v in rows]
    still = NOW - 8 * 3600
    rows = [(ts, v) for ts, v in rows if ts < still] + [(still + n * 60, 79.1) for n in range(8 * 60)]
    assert ("frozen", "Tmp") not in kinds(analyse(SITE, {"Tmp": rows}, NOW))
