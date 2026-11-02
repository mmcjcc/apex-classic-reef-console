"""Rate-of-change alerts, equipment fingerprints, the probes against the tests, the weekly report, energy and the
outdoor temperature ."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from reef.analysis import analyse, equipment, probe_health, steps
from reef.apex import parse_status
from reef.conditions import Engine
from reef.main import Worker
from reef.metrics import Metrics
from reef.report import build, due_end, energy, text
from reef.site import Extras, parse_site
from reef.store import Store
from reef.weather import fetch, parse_hourly

from .test_worker import RAW, SITE, T0, FixedClient, doc

NOW = 1_791_000_000.0


# ---- how fast, not how far -----------------------------------------------------------------------------------------
def test_the_site_file_has_the_rate_conditions() -> None:
    rates = {c.id: (c.kind, c.limit, c.minutes, c.severity) for c in SITE.conditions if c.kind in ("rise", "fall")}
    assert rates["temperature-rising-fast"] == ("rise", 1.0, 30, "critical")
    assert rates["salinity-falling-fast"] == ("fall", 1.5, 30, "warning")
    assert len(rates) == 6
    text_ = next(c.text for c in SITE.conditions if c.id == "temperature-rising-fast")
    assert text_ == "Temperature (Tmp) rising 1 °F or more in 30 minutes"


def run_temps(engine: Engine, temps: list[float], start: float) -> list[str]:
    changed = []
    for n, t in enumerate(temps):
        for change in engine.evaluate(parse_status(doc(Tmp=t), start + n * 60), start + n * 60):
            changed.append(("+" if change.active else "-") + change.condition.id)
    return changed


def test_a_heater_stuck_on_alerts_on_the_rise_before_the_limit() -> None:
    engine = Engine(SITE)
    steady = [79.0] * 30
    assert run_temps(engine, steady, T0) == []  # 30 minutes of history first
    rising = [79.0 + 0.05 * n for n in range(1, 25)]  # +1.2 °F in 24 minutes, still far below 82.5
    changed = run_temps(engine, rising, T0 + 30 * 60)
    assert changed == ["+temperature-rising-fast"]
    assert engine.states["temperature-rising-fast"].detail.startswith("Tmp 79 -> ")
    settled = [80.2] * 40  # it stops climbing: once the 30-minute change is under half the limit, it ends
    assert run_temps(engine, settled, T0 + 54 * 60) == ["-temperature-rising-fast"]


def test_no_rate_judgement_before_the_window_is_covered() -> None:
    engine = Engine(SITE)
    # a jump in the first minutes after a start says nothing: there is no reading from 30 minutes ago
    assert run_temps(engine, [79.0, 79.0, 81.0, 81.0, 81.0], T0) == []


# ---- what each output draws when it switches -------------------------------------------------------------------
def test_an_output_drawing_less_than_it_used_to_is_found() -> None:
    amps: list[tuple[float, float]] = []
    outlets: list[tuple] = []
    for day in range(20):
        on = NOW - (20 - day) * 86400 + 3600
        draws = 1.0 if day < 15 else 0.3  # the lamp's driver fails five days ago
        ramp = 2.0 + draws / 2  # a lamp ramping up: the first minutes are not what it draws
        amps += [(on - 120, 2.0), (on - 60, 2.0), (on + 60, ramp), (on + 360, 2.0 + draws), (on + 600, 2.0 + draws)]
        amps += [(on + 7200 - 60, 2.0 + draws), (on + 7200 + 60, 2.0), (on + 7200 + 360, 2.0)]
        outlets += [(on, "kessil2_4_6", "outlet", True, "4_6"), (on + 7200, "kessil2_4_6", "outlet", False, "4_6")]
    bars = {"kessil2_4_6": "Amp_4"}
    drawn = steps(outlets, {"Amp_4": sorted(amps)}, bars)
    assert len(drawn["kessil2_4_6"]) == 40 and drawn["kessil2_4_6"][0][1] == 1.0
    prints, findings = equipment(outlets, {"Amp_4": sorted(amps)}, bars, NOW)
    result = analyse(SITE, {"Amp_4": sorted(amps)}, NOW, outlets, [], [], switchings_complete_since=None)
    assert result["equipment"] == []  # no switchings known to be complete (no polls): no fingerprint at all
    result = analyse(SITE, {"Amp_4": sorted(amps)}, NOW, outlets, [], [], switchings_complete_since=NOW - 30 * 86400)
    assert [p["output"] for p in result["equipment"]] == ["kessil2_4_6"]
    assert prints[0]["output"] == "kessil2_4_6" and prints[0]["draws"] == 1.0 and prints[0]["lately"] == 0.3
    assert len(findings) == 1 and "draws 0.3 A when it switches lately, against 1.0 A before" in findings[0]["summary"]


def test_switchings_close_together_on_one_bar_are_left_out() -> None:
    amps = [(NOW - 120, 2.0), (NOW + 400, 3.6)]
    outlets = [(NOW, "PapioLed_4_1", "outlet", True, "4_1"), (NOW + 240, "blueLEDR_4_3", "outlet", True, "4_3")]
    assert steps(outlets, {"Amp_4": amps}, {"PapioLed_4_1": "Amp_4", "blueLEDR_4_3": "Amp_4"}) == {}


# ---- the probes against the tests ------------------------------------------------------------------------------
def test_a_probe_reading_above_the_last_tests_asks_for_a_calibration() -> None:
    history = {"pH": sorted((NOW - d * 86400 + s, 8.40) for d in (1, 3, 6) for s in range(-300, 301, 60))}
    tests = [(NOW - 6 * 86400, "ph", 8.25), (NOW - 3 * 86400, "ph", 8.27), (NOW - 86400, "ph", 8.26)]
    found = probe_health(SITE, history, tests, [])
    assert len(found) == 1 and found[0]["kind"] == "probe"
    assert "pH reads 0.14 above your last 3 tests (8.25, 8.27, 8.26): recalibrate the probe?" in found[0]["summary"]
    assert probe_health(SITE, history, tests[:1], []) == []  # one test is not enough
    close = [(ts, p, 8.35) for ts, p, _ in tests]
    assert probe_health(SITE, history, close, []) == []  # 0.05 off: within what a kit and a probe disagree by


# ---- the weekly report -----------------------------------------------------------------------------------------
async def filled(tmp_path: Path) -> Store:
    store = Store(tmp_path / "reef.db")
    engine = Engine(SITE)
    worker = Worker(SITE, store, FixedClient([doc(Leak1B=60), doc(Leak1B=60), RAW]), Metrics(SITE, engine), engine, tmp_path)  # type: ignore[arg-type]
    for _ in range(3):
        await worker.poll_once()
    store.db.execute(
        "INSERT INTO water_tests (measured_at, parameter, value, unit, entered_value, entered_unit, kit, kit_error, note, "
        "entered_at, entered_by, source) VALUES (?, 'alkalinity', 8.1, 'dKH', 8.1, 'dKH', 'Hanna', NULL, '', ?, 'alex', 'app')",
        (T0 + 100, T0 + 100),
    )
    store.db.execute(
        "INSERT INTO journal (ts, kind, text, entered_at, entered_by, source) VALUES (?, 'water change', '20 %', ?, 'alex', 'app')",
        (T0 + 150, T0 + 150),
    )
    store.db.commit()
    return store


async def test_the_weekly_report_says_how_the_week_went(tmp_path: Path) -> None:
    store = await filled(tmp_path)
    report = build(store.db, SITE, T0 + 3600, T0 + 3600)
    titles = [s["title"] for s in report["sections"]]
    assert titles == ["Alerts", "Analysis", "Probes", "Water tests", "Energy", "Apex programs", "Journal", "The Apex"]
    sections = {s["title"]: s["lines"] for s in report["sections"]}
    assert sections["Alerts"] == ["Leak sensor Leak1B (Leak1B) is CLOSED: 1 time"]
    assert any(line.startswith("Temperature: 79.0 °F to 79.0 °F, mean 79.0 °F") for line in sections["Probes"])
    assert sections["Water tests"][0].endswith("alkalinity 8.1 dKH (Hanna)")
    assert sections["Water tests"][-1].startswith("Due: calcium, magnesium, phosphate, nitrate")  # never tested
    assert sections["Energy"][0].startswith("EB8_3: ") and "kWh a day" in sections["Energy"][0]
    assert sections["Journal"][0].endswith("water change, 20 %")
    assert report["headline"].startswith("1 alert; test due: calcium, magnesium, phosphate, nitrate")
    page = text(report, SITE.timezone)
    assert page.startswith("Aquarium, the week of ") and "\nAlerts\n  Leak sensor Leak1B" in page
    store.close()


def test_energy_per_bar_and_its_cost_once_the_tariff_is_set(tmp_path: Path) -> None:
    store = Store(tmp_path / "reef.db")
    store.record(parse_status(RAW, T0), SITE)
    site = dataclasses.replace(SITE, extras=Extras(volts=120, price_per_kwh=0.20))
    bars = {b["module"]: b for b in energy(store.db, site, T0 - 10, T0 + 10)}
    assert bars["EB8_9"]["amps"] == 2.8 and bars["EB8_9"]["kwh_per_day"] == round(2.8 * 120 * 24 / 1000, 2)
    assert bars["EB8_9"]["cost_per_month"] == round(2.8 * 120 * 24 / 1000 * 30.4 * 0.20, 2)
    store.close()


def test_the_report_is_made_on_its_day_from_its_hour_once_a_week() -> None:
    sunday_8 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC).timestamp()  # 08:00 EDT on a Sunday
    assert due_end(sunday_8, SITE, None) is None
    sunday_9 = sunday_8 + 3600
    end = due_end(sunday_9, SITE, None)
    assert end == datetime(2026, 10, 18, 4, 0, tzinfo=UTC).timestamp()  # local midnight
    assert due_end(sunday_9 + 600, SITE, end) is None  # once
    assert due_end(sunday_9 + 86400, SITE, end) is None  # Monday


async def test_the_worker_keeps_the_report_announces_it_and_publishes_its_headline(tmp_path: Path) -> None:
    store = await filled(tmp_path)
    engine = Engine(SITE)
    metrics = Metrics(SITE, engine)
    worker = Worker(SITE, store, FixedClient([]), metrics, engine, tmp_path)  # type: ignore[arg-type]
    sunday_9 = datetime(2026, 10, 18, 13, 0, tzinfo=UTC).timestamp()
    await worker.refresh_report(sunday_9)
    await worker.refresh_report(sunday_9 + 600)
    rows = store.db.execute("SELECT id, headline FROM reports").fetchall()
    assert len(rows) == 1
    assert store.db.execute("SELECT kind, subject FROM events WHERE kind = 'report.weekly'").fetchall() == [
        ("report.weekly", "1")
    ]
    published = next(f for f in metrics.collect() if f.name == "reef_report_published_timestamp_seconds")
    assert published.samples[0].value == sunday_9 and published.samples[0].labels["headline"] == rows[0][1]
    restarted = Worker(SITE, store, FixedClient([]), Metrics(SITE, engine), engine, tmp_path)  # type: ignore[arg-type]
    assert restarted.metrics.report == (sunday_9, rows[0][1])  # the metric survives a restart
    store.close()


# ---- the outdoor temperature ----------------------------------------------------------------------------------
HOURLY = {"hourly": {"time": ["2026-10-13T04:00", "2026-10-13T05:00", "2026-10-13T06:00"], "temperature_2m": [51.4, None, 50.2]}}


def test_open_meteos_hourly_answer() -> None:
    assert parse_hourly(HOURLY) == [
        (datetime(2026, 10, 13, 4, tzinfo=UTC).timestamp(), 51.4), (datetime(2026, 10, 13, 6, tzinfo=UTC).timestamp(), 50.2)]  # fmt: skip


async def test_the_outdoor_temperature_once_the_site_has_a_location(tmp_path: Path) -> None:
    asked: list[httpx.URL] = []

    def meteo(request: httpx.Request) -> httpx.Response:
        asked.append(request.url)
        return httpx.Response(200, json=HOURLY)

    client = httpx.AsyncClient(transport=httpx.MockTransport(meteo))
    now = datetime(2026, 10, 13, 7, tzinfo=UTC).timestamp()
    rows = await fetch(client, 40.71, -74.01, now, backfill_days=40)
    assert len(rows) == 2 and asked[-1].host == "archive-api.open-meteo.com"
    assert asked[-1].params["latitude"] == "40.71" and asked[-1].params["end_date"] == "2026-10-11"

    store = Store(tmp_path / "reef.db")
    engine = Engine(SITE)
    located = dataclasses.replace(SITE, extras=Extras(latitude=40.71, longitude=-74.01))
    worker = Worker(located, store, FixedClient([]), Metrics(SITE, engine), engine, tmp_path, client)  # type: ignore[arg-type]
    await worker.refresh_weather(now)  # the archive first
    await worker.refresh_weather(now + 60)  # then the last two days
    await worker.refresh_weather(now + 120)  # then hourly
    assert [u.host for u in asked[1:]] == ["archive-api.open-meteo.com", "api.open-meteo.com"]
    values = store.db.execute(
        "SELECT r.value FROM readings r JOIN sensors s ON s.id = r.sensor_id WHERE s.name = 'Outdoor' ORDER BY r.ts"
    ).fetchall()
    assert values == [(51.4,), (50.2,)]
    unlocated = Worker(SITE, store, FixedClient([]), Metrics(SITE, engine), engine, tmp_path, client)  # type: ignore[arg-type]
    await unlocated.refresh_weather(now + 7200)
    assert len(asked) == 3  # no location: nothing is asked
    store.close()
    await client.aclose()


def test_the_extras_of_the_site_file() -> None:
    assert SITE.extras.volts == 120 and SITE.extras.price_per_kwh is None and SITE.extras.latitude is None
    assert SITE.extras.report_weekday == 6 and SITE.extras.report_hour == 9 and SITE.extras.tests_due["alkalinity"] == 7
    assert SITE.sensors["Outdoor"].external
    site = parse_site({"weather": {"latitude": 40.712776, "longitude": -74.005974}, "report": {"weekday": "Monday"}})
    assert (site.extras.latitude, site.extras.longitude, site.extras.report_weekday) == (40.71, -74.01, 0)


# ---- the fresh-water can: a one-time notice when it is full, and the valve left on is critical --------------------
def run_switch(engine: Engine, documents: list[dict], start: float) -> list[str]:
    changed = []
    for n, document in enumerate(documents):
        for change in engine.evaluate(parse_status(document, start + n * 60), start + n * 60):
            changed.append(("+" if change.active else "-") + change.condition.id)
    return changed


def test_the_fill_valve_left_on_with_the_can_full_is_critical() -> None:
    assert "fresh-high-float" not in {c.id for c in SITE.conditions}  # a full can is normal now
    condition = next(c for c in SITE.conditions if c.id == "fill-valve-open-when-full")
    assert condition.severity == "critical" and condition.output == "Topoff_9_2" and condition.pending == 120
    engine = Engine(SITE)
    full_and_off = doc(FreshH=60, Topoff_9_2=["AOF", "", "OK", ""])
    assert run_switch(engine, [full_and_off] * 4, T0) == []  # full, valve off: as it should be
    full_and_on = doc(FreshH=60, Topoff_9_2=["ON", "", "OK", ""])
    assert run_switch(engine, [full_and_on] * 3, T0 + 600) == ["+fill-valve-open-when-full"]
    assert run_switch(engine, [full_and_off] * 3, T0 + 900) == ["-fill-valve-open-when-full"]


async def test_the_can_full_notice_is_sent_once_per_fill(tmp_path: Path) -> None:
    empty, full = doc(FreshH=0), doc(FreshH=60)
    polls = [empty, full, full, full, empty, full, full] + [empty] * 31 + [full, full]
    store = Store(tmp_path / "reef.db")
    engine = Engine(SITE)
    metrics = Metrics(SITE, engine)
    worker = Worker(SITE, store, FixedClient(list(polls)), metrics, engine, tmp_path)  # type: ignore[arg-type]
    for _ in range(len(polls)):  # the client takes its own copy: it uses the polls up
        await worker.poll_once()
    sent = store.db.execute("SELECT ts, detail FROM events WHERE kind = 'notice' ORDER BY id").fetchall()
    # full at poll 2 (held a minute at poll 3): one; it bobs at poll 5: nothing; a real refill after 31 minutes: again
    assert [ts - T0 for ts, _ in sent] == [180, 40 * 60]  # poll k comes at T0 + 60 (k + 1)
    assert sent[0][1] == "The fresh-water container is full: check that the fill valve (Topoff_9_2) turned off"
    notice = next(f for f in metrics.collect() if f.name == "reef_notice_timestamp_seconds")
    assert notice.samples[0].labels["notice"] == "fresh-can-full" and notice.samples[0].value == T0 + 40 * 60
    store.close()


async def test_a_can_already_full_when_the_worker_starts_is_not_news(tmp_path: Path) -> None:
    full = doc(FreshH=60)
    store = Store(tmp_path / "reef.db")
    engine = Engine(SITE)
    worker = Worker(SITE, store, FixedClient([full] * 5), Metrics(SITE, engine), engine, tmp_path)  # type: ignore[arg-type]
    for _ in range(5):
        await worker.poll_once()
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE kind = 'notice'").fetchone()[0] == 0
    store.close()


# ---- the calcium reactor ----------------------------------------------------------------------------------------
def test_the_reactor_day_counts_co2_time_and_the_night_window_on_the_apexs_clock(tmp_path: Path) -> None:
    from reefweb.reactor import balance, days

    store = Store(tmp_path / "reef.db")
    store.kv_set("latest", {"timezone": "-3.00"})
    oid = store.db.execute(
        "INSERT INTO outlets (serial, did, name, type, first_seen, last_seen) VALUES ('AC4', '3_1', 'Newco21_3_1', 'outlet', 0, 0)"
    ).lastrowid

    def edt(day: int, hour: int, minute: int) -> float:  # a time in October (EDT, UTC-4)
        return (datetime(2026, 10, day, minute=minute, tzinfo=UTC) + timedelta(hours=hour + 4)).timestamp()

    for ts, on in [(edt(20, 8, 0), 0), (edt(20, 12, 0), 1), (edt(20, 12, 30), 0), (edt(20, 23, 30), 1), (edt(20, 23, 50), 0)]:
        store.db.execute(
            "INSERT INTO outlet_states VALUES (?, ?, ?, 0, ?, 'OK', NULL)", (oid, int(ts), "AON" if on else "AOF", on)
        )
    sid = store.db.execute(
        "INSERT INTO sensors (serial, did, name, type, first_seen, last_seen) VALUES ('AC4', '5_1', 'pHx5', 'pH', 0, 0)"
    ).lastrowid
    store.db.executemany(
        "INSERT INTO readings VALUES (?, ?, ?)", [(sid, int(edt(20, 12, 15)), 7.05), (sid, int(edt(20, 23, 40)), 7.20)]
    )
    store.db.commit()
    rows = days(store.db, SITE, edt(20, 0, 0), edt(21, 12, 0))
    first = rows[0]
    assert (
        first["day"] == "2026-10-20" and first["co2_minutes"] == 50 and first["co2_night_minutes"] == 20
    )  # 23:30 EDT is 00:30 on the Apex
    assert first["switchings"] == 4 and first["effluent_day"] == 7.05 and first["effluent_night"] == 7.2

    def t(ts: float, value: float, unit: str) -> dict:
        return {"ts": ts, "value": value, "unit": unit, "kit": "", "kit_error": None, "per_day": None}

    alk = [t(NOW, 8.0, "dKH"), t(NOW + 3 * 86400, 9.0, "dKH"), t(NOW + 6 * 86400, 10.0, "dKH")]
    ca = [t(NOW + 3600, 420, "ppm"), t(NOW + 3 * 86400 + 3600, 427, "ppm"), t(NOW + 6 * 86400 + 3600, 380, "ppm")]
    verdicts = balance({"alkalinity": alk, "calcium": ca})
    assert [(b["alk_change"], b["ca_change"], b["ca_expected"], b["verdict"]) for b in verdicts] == [
        (1.0, 7.0, 7.2, "in step"), (1.0, -47.0, 7.2, "calcium behind")]  # fmt: skip
    store.close()


def test_the_drip_warning_fires_when_the_co2s_daytime_minutes_fall_with_its_band_unchanged(tmp_path: Path) -> None:
    from reef.reactor import band_changed_at, drip_finding

    now = datetime(2026, 10, 26, 16, 0, tzinfo=UTC).timestamp()  # noon EDT

    def rows(minutes: list[int], night: int = 10) -> list[dict]:
        out = [{"day": f"2026-10-{20 + n:02d}", "co2_minutes": m + night, "co2_night_minutes": night}
               for n, m in enumerate(minutes)]  # fmt: skip
        return [*out, {"day": "2026-10-26", "co2_minutes": 5, "co2_night_minutes": 0}]  # today: left out

    found = drip_finding(SITE, rows([150, 148, 146, 100, 95, 90]), None, now)
    assert found is not None and found["kind"] == "drip" and found["sensor"] == "Newco21_3_1"
    assert found["evidence"]["before"] == [150, 148, 146] and found["evidence"]["lately"] == [100, 95, 90]
    assert "95 min a day" in found["summary"] and "148 the 3 days before (36% less)" in found["summary"]
    assert drip_finding(SITE, rows([150, 148, 146, 125, 120, 115]), None, now) is None  # 19 % less
    assert drip_finding(SITE, rows([20, 20, 20, 5, 5, 5]), None, now) is None  # too little CO2 time to judge
    assert drip_finding(SITE, rows([150, 148, 146, 60, 60, 150]), None, now) is None  # the last day is back up
    changed = datetime(2026, 10, 22, 14, 0, tzinfo=UTC).timestamp()
    assert drip_finding(SITE, rows([150, 148, 146, 100, 95, 90]), changed, now) is None  # 3 days since the band changed

    store = Store(tmp_path / "reef.db")
    co2 = ["Fallback OFF", "If pHx5 > 7.10 Then ON", "If pHx5 < 7.00 Then OFF", "If Time 00:00 to 05:00 Then OFF"]
    for ts, lines in [(100.0, co2), (200.0, [*co2[:3], "If Time 00:00 to 06:00 Then OFF"]),
                      (300.0, ["Fallback OFF", "If pHx5 > 7.05 Then ON", "If pHx5 < 6.95 Then OFF"]),
                      (400.0, ["Fallback OFF", "If pHx5 < 6.95 Then OFF", "If pHx5 > 7.05 Then ON"])]:  # fmt: skip
        store.save_programs(ts, "AC4", {"Newco21_3_1": lines, "Topoff_9_2": [f"Fallback {ts}"]})
    assert band_changed_at(store.db, SITE) == 300.0  # the time line and the order do not count, the band does
    store.close()


def test_the_apexs_clock_is_read_with_the_timezone_setting_of_each_moment(tmp_path: Path) -> None:
    from reef.reactor import Offsets, _in_night

    store = Store(tmp_path / "reef.db")
    store.kv_set("latest", {"timezone": "-4.00"})
    night = ("00:00", "05:00")
    before = datetime(2026, 11, 1, 3, 30, tzinfo=UTC).timestamp()  # 23:30 EDT: 00:30 on the Apex at -3
    after = datetime(2026, 11, 2, 3, 30, tzinfo=UTC).timestamp()  # 22:30 EST: 23:30 on the Apex at -4
    assert Offsets(store.db).at(before) == -4.0 and not _in_night(before, night, Offsets(store.db))  # no record yet
    worker = Worker(SITE, store, FixedClient([]), Metrics(SITE, Engine(SITE)), Engine(SITE), tmp_path)  # type: ignore[arg-type]
    worker.unit_offset = -3.0
    worker.note_offset(100.0)
    worker.note_offset(200.0)
    changed = datetime(2026, 11, 1, 4, 0, tzinfo=UTC).timestamp()
    worker.unit_offset = -4.0
    worker.note_offset(changed)
    assert store.kv_get("apex_offsets") == [[0.0, -3.0], [changed, -4.0]]
    events = store.db.execute("SELECT kind, detail FROM events").fetchall()
    assert events == [("apex.timezone", "the Apex's timezone setting changed from -3.00 to -4.00 hours: its time-based "
                                        "programs run by that clock")]  # fmt: skip
    offsets = Offsets(store.db)
    assert offsets.at(before) == -3.0 and offsets.at(after) == -4.0
    assert _in_night(before, night, offsets) and not _in_night(after, night, offsets)
    store.close()
