"""The worker against the Apex's real status.json of 2026-10-11 (the Fusion link key removed) and the site file."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from reef import apex as apex_module
from reef.apex import ApexClient, ApexError, Credential, parse_status, read_credential
from reef.conditions import Engine, module_check
from reef.main import Worker
from reef.metrics import Metrics
from reef.site import load_site, parse_site
from reef.store import Store

HERE = Path(__file__).parent
SITE = load_site(HERE.parent.parent / "config" / "site.example.yaml")
RAW = json.loads((HERE / "data" / "status-2026-10-11.json").read_text(encoding="utf-8"))
T0 = 1_791_766_052.0


def doc(**changes: Any) -> dict[str, Any]:
    """The captured status with some inputs or outputs changed: name -> value (input) or status list (output)."""
    out = copy.deepcopy(RAW)
    for item in out["istat"]["inputs"]:
        if item["name"] in changes:
            item["value"] = changes[item["name"]]
    for item in out["istat"]["outputs"]:
        if item["name"] in changes:
            item["status"] = changes[item["name"]]
    return out


def without(*dids_prefix: str) -> dict[str, Any]:
    out = copy.deepcopy(RAW)
    out["istat"]["inputs"] = [i for i in out["istat"]["inputs"] if not i["did"].startswith(dids_prefix)]
    out["istat"]["outputs"] = [o for o in out["istat"]["outputs"] if not o["did"].startswith(dids_prefix)]
    return out


def run(engine: Engine, document: dict[str, Any], start: float, polls: int, step: float = 60.0) -> list[str]:
    """Poll the same document several times; the ids of the conditions that changed, '+' started, '-' ended."""
    changed = []
    for n in range(polls):
        for change in engine.evaluate(parse_status(document, start + n * step), start + n * step):
            changed.append(("+" if change.active else "-") + change.condition.id)
    return changed


# ---- reading the Apex -------------------------------------------------------------------------------------------
def test_status_json_of_firmware_4_53() -> None:
    status = parse_status(RAW, T0)
    assert status.serial == "AC4:12345" and status.timezone == "-3.00" and status.clock_offset() == 0.0
    assert status.inputs["Tmp"].value == 79.0 and status.inputs["Condx6"].value == 35.5
    assert not status.inputs["sumpLo"].closed and status.inputs["SumpHi"].closed  # 0 OPEN, 60 CLOSED
    uv, ozone, var = status.outputs["uv_3_7"], status.outputs["ozone_9_7"], status.outputs["VarSpd1_I1"]
    assert uv.manual and uv.energized and uv.health == "OK"
    assert not ozone.manual and not ozone.energized
    assert var.type == "variable" and var.intensity == 75 and var.energized and not var.manual
    assert status.power_failed == 1791076600 and status.power_restored == 1791076673


def test_the_secret_file_is_a_csv_and_never_shows_the_password() -> None:
    cred = read_credential("ip, username, password\r\n192.0.2.16, someone, not-a-real-one\r\n")
    assert cred == Credential("192.0.2.16", "someone", "not-a-real-one")
    assert "not-a-real-one" not in repr(cred)
    with pytest.raises(ApexError):
        read_credential("ip, username, password\n")


# ---- the conditions -------------------------------------------------------------------------------------------
def test_the_site_file_has_the_imported_alarms_and_the_module_checks() -> None:
    ids = {c.id for c in SITE.conditions}
    assert {"leak", "temperature-high", "orp-very-low", "salinity-low", "fresh-low-float", "fill-valve-open-when-full"} <= ids
    assert {"eb8_3-lost", "eb8_4-lost", "eb8_9-lost", "eb8_9-no-current", "eb8_3-no-current"} <= ids
    assert "eb8_4-no-current" not in ids  # nothing on bar 4 is known to always draw
    assert not SITE.sensors["Tmpx5"].active  # not in use (site file: active: false)


def test_todays_tank_raises_nothing() -> None:
    engine = Engine(SITE)
    assert run(engine, RAW, T0, 6) == [] and engine.active() == []


def test_a_probe_alerts_after_its_pending_time_and_ends_inside_its_band() -> None:
    engine = Engine(SITE)
    assert run(engine, doc(Tmp=82.9), T0, 3) == []  # 2 minutes: still pending
    assert run(engine, doc(Tmp=82.9), T0 + 180, 1) == ["+temperature-high"]
    assert run(engine, doc(Tmp=82.4), T0 + 240, 1) == []  # below the limit, not yet inside the 0.3 band
    assert run(engine, doc(Tmp=82.2), T0 + 300, 1) == ["-temperature-high"]


def test_one_bad_sample_does_not_alert() -> None:
    engine = Engine(SITE)
    assert run(engine, doc(Leak1B=60), T0, 1) == [] and run(engine, RAW, T0 + 60, 3) == []


def test_a_leak_alerts_after_two_polls_and_clears_after_two_dry_ones() -> None:
    engine = Engine(SITE)
    assert run(engine, doc(Leak1B=60), T0, 2) == ["+leak"]
    assert run(engine, RAW, T0 + 120, 1) == [] and run(engine, RAW, T0 + 180, 1) == ["-leak"]


def test_every_true_condition_is_its_own_alert_and_critical_hides_the_warning_on_the_same_probe() -> None:
    engine = Engine(SITE)
    run(engine, doc(ORP=150, Leak1B=60, pH=8.6), T0, 4)
    active = sorted(c.id for c, _ in engine.active())
    assert active == ["leak", "orp-very-low", "ph-high"]  # orp-low (warning) is hidden by orp-very-low
    assert engine.states["orp-low"].active  # still tracked


def test_an_energybar_that_drops_off_the_aquabus_is_lost() -> None:
    eb8_9 = SITE.module("EB8_9")
    assert eb8_9 is not None and module_check(eb8_9, parse_status(RAW, T0)) == (True, "")
    gone = without("9_")
    assert module_check(eb8_9, parse_status(gone, T0)) == (False, "none of its outlets is listed")
    bad = doc(mainpump_9_8=["AON", "", "Err", ""])
    assert module_check(eb8_9, parse_status(bad, T0)) == (False, "outlet health mainpump_9_8=Err")
    engine = Engine(SITE)
    assert run(engine, gone, T0, 2) == ["+eb8_9-lost"]  # the current check says nothing without its input


def test_an_energybar_without_current_while_its_pump_runs() -> None:
    engine = Engine(SITE)
    assert run(engine, doc(Amp_9=0.0), T0, 4) == ["+eb8_9-no-current"]
    off = doc(Amp_9=0.0, mainpump_9_8=["OFF", "", "OK", ""])  # the pump switched off by hand: no current is right
    assert run(engine, off, T0 + 240, 4) == ["-eb8_9-no-current"]


def test_a_poll_that_says_nothing_about_a_sensor_leaves_its_condition_as_it_was() -> None:
    engine = Engine(SITE)
    run(engine, doc(Leak1B=60), T0, 2)
    no_inputs = copy.deepcopy(RAW)
    no_inputs["istat"]["inputs"] = []
    changed = run(engine, no_inputs, T0 + 120, 3)  # nothing about the leak sensor: its alert stays
    assert engine.states["leak"].active and "-leak" not in changed
    assert "+mod_7-lost" in changed  # but the module that carries it is reported missing


def test_unknown_kinds_and_duplicate_ids_stop_the_start() -> None:
    with pytest.raises(ValueError):
        parse_site({"conditions": [{"id": "x", "sensor": "Tmp", "when": "wet"}]})
    with pytest.raises(ValueError):
        parse_site({"conditions": [{"id": "x", "sensor": "Tmp", "above": 1}, {"id": "x", "sensor": "pH", "below": 1}]})


# ---- the client: one at a time, backing off --------------------------------------------------------------------
async def test_the_client_backs_off_after_a_failure_and_after_a_slow_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("authorization", ""))
        return httpx.Response(500) if len(calls) == 1 else httpx.Response(200, json=RAW)

    client = ApexClient(Credential("192.0.2.16", "u", "p"), transport=httpx.MockTransport(answer))
    with pytest.raises(ApexError, match="HTTP 500"):
        await client.status()
    assert client.waiting() > 100  # two minutes
    with pytest.raises(ApexError, match="backing off"):
        await client.status()
    assert len(calls) == 1  # nothing was sent while backing off
    client.closed_until = 0.0
    status = await client.status()
    assert status.inputs["pH"].value == 8.41 and client.failures == 0 and calls[-1].startswith("Basic ")
    monkeypatch.setattr(apex_module, "SLOW_SECONDS", -1.0)  # every answer is "slow"
    await client.status()
    assert client.waiting() > 100
    await client.close()


# ---- history and the worker -----------------------------------------------------------------------------------
class FixedClient:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents
        self.t = T0

    async def status(self) -> Any:
        if not self.documents:
            raise ApexError("timed out")
        self.t += 60
        return parse_status(self.documents.pop(0), self.t)

    def waiting(self) -> float:
        return 0.0


async def test_the_worker_keeps_history_events_and_its_conditions_across_a_restart(tmp_path: Path) -> None:
    store = Store(tmp_path / "reef.db")
    engine = Engine(SITE)
    metrics = Metrics(SITE, engine)
    worker = Worker(SITE, store, FixedClient([doc(Leak1B=60), doc(Leak1B=60), doc(uv_3_7=["AOF", "", "OK", ""])]),
                    metrics, engine, tmp_path)  # type: ignore[arg-type]  # fmt: skip
    for _ in range(3):
        await worker.poll_once()
    assert await worker.poll_once() is None and metrics.failing  # the Apex went quiet
    db = store.db
    assert (
        db.execute("SELECT COUNT(*) FROM readings r JOIN sensors s ON s.id = r.sensor_id WHERE s.name = 'Tmp'").fetchone()[0] == 3
    )
    assert db.execute("SELECT COUNT(*) FROM sensors WHERE name = 'Tmpx5'").fetchone()[0] == 0  # not active: no history
    leak = db.execute(
        "SELECT closed FROM switch_states w JOIN sensors s ON s.id = w.sensor_id WHERE s.name = 'Leak1B' ORDER BY ts"
    ).fetchall()
    assert leak == [(1,), (0,)]  # stored only when it changed
    uv = db.execute(
        "SELECT mode FROM outlet_states x JOIN outlets o ON o.id = x.outlet_id WHERE o.name = 'uv_3_7' ORDER BY ts"
    ).fetchall()
    assert uv == [("ON",), ("AOF",)]
    kinds = [k for (k,) in db.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["apex.power", "condition.start"]  # the power failure the Apex reported once, then the leak
    assert store.load_active().keys() == {"leak"}
    restarted = Engine(SITE)
    restarted.restore(store.load_active())
    assert [c.id for c, _ in restarted.active()] == ["leak"]
    worker.nightly_copy(T0)
    assert (tmp_path / "backups").is_dir() and len(list((tmp_path / "backups").glob("reef-*.db"))) == 1
    store.close()


def test_metrics_never_show_old_readings_but_keep_the_conditions() -> None:
    engine = Engine(SITE)
    metrics = Metrics(SITE, engine)
    run(engine, doc(Leak1B=60), T0, 2)
    metrics.update(parse_status(RAW, 9_999_999_999.0))  # a poll "now" (far future for the test clock)
    names = {family.name for family in metrics.collect()}
    assert {"reef_sensor_value", "reef_outlet_energized", "reef_module_connected", "reef_condition_active"} <= names
    metrics.failed()
    names = {family.name: family for family in metrics.collect()}
    assert "reef_sensor_value" not in names and names["reef_apex_up"].samples[0].value == 0.0
    leak = [s for s in names["reef_condition_active"].samples if s.labels["condition"] == "leak"]
    assert leak[0].value == 1.0 and leak[0].labels["severity"] == "critical" and leak[0].labels["alarm"] == "tank"
