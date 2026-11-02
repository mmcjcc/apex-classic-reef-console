"""Program history (the worker keeps dated versions) and water tests (units, conversions, the questions asked)."""

from __future__ import annotations

from pathlib import Path

import pytest

from reef.conditions import Engine
from reef.main import Worker
from reef.metrics import Metrics
from reef.programs import diff, summary
from reef.store import Store
from reef.water import check, parameter, ppt_from_sg, sg_from_ppt

from .test_web import PROGRAMS
from .test_worker import SITE, T0, FixedClient


# ---- programs --------------------------------------------------------------------------------------------------
def test_the_difference_between_two_versions_is_line_by_line() -> None:
    after = dict(PROGRAMS)
    after["whiteLEDR_4_"] = [line.replace("82.0", "81.5") for line in PROGRAMS["whiteLEDR_4_"]]
    after["fan_3_6"] = ["Fallback OFF", "If Tmp > 80.0 Then ON"]
    del after["Pump1_A3"]
    changes = diff(PROGRAMS, after)
    assert [(c["output"], c["change"]) for c in changes] == [("Pump1_A3", "removed"), ("fan_3_6", "added"),
                                                              ("whiteLEDR_4_", "changed")]  # fmt: skip
    led = changes[-1]["lines"]
    assert ["-", "If Tmp > 82.0 Then OFF"] in led and ["+", "If Tmp > 81.5 Then OFF"] in led
    assert [" ", "Fallback OFF"] in led  # unchanged lines stay for context
    assert summary(changes) == "Pump1_A3 removed, fan_3_6 added, whiteLEDR_4_ changed (+1 -1)"
    assert diff(None, PROGRAMS) == [] and diff(PROGRAMS, PROGRAMS) == []


class ProgramClient(FixedClient):
    def __init__(self, versions: list[dict[str, list[str]]]) -> None:
        super().__init__([])
        self.versions = versions

    async def programs(self) -> dict[str, list[str]]:
        return self.versions.pop(0)


async def test_the_worker_keeps_a_version_only_when_the_programs_change(tmp_path: Path) -> None:
    store = Store(tmp_path / "reef.db")
    changed = dict(PROGRAMS, Topoff_9_2=["Fallback OFF", "Set OFF"])
    client = ProgramClient([PROGRAMS, PROGRAMS, changed])
    engine = Engine(SITE)
    worker = Worker(SITE, store, client, Metrics(SITE, engine), engine, tmp_path)  # type: ignore[arg-type]
    for day in range(3):
        await worker.refresh_programs(T0 + day * 86400)
    await worker.refresh_programs(T0 + 2 * 86400 + 60)  # read today already: nothing is asked of the unit
    rows = store.db.execute("SELECT ts FROM program_versions ORDER BY id").fetchall()
    assert rows == [(T0,), (T0 + 2 * 86400,)]
    events = store.db.execute("SELECT kind, detail FROM events").fetchall()
    assert events == [("apex.program", "programs changed: Topoff_9_2 changed (+1 -3)")]
    store.close()


def test_programs_kept_before_the_history_become_its_first_version(tmp_path: Path) -> None:
    store = Store(tmp_path / "reef.db")
    store.kv_set("programs", PROGRAMS)
    store.kv_set("programs_at", T0)
    store.kv_set("latest", {"serial": "AC4:12345"})
    engine = Engine(SITE)
    Worker(SITE, store, FixedClient([]), Metrics(SITE, engine), engine, tmp_path)  # type: ignore[arg-type]
    assert store.db.execute("SELECT ts, serial FROM program_versions").fetchall() == [(T0, "AC4:12345")]
    assert store.kv_get("programs") is None and store.latest_programs() == (1, PROGRAMS)
    store.close()


# ---- water tests ------------------------------------------------------------------------------------------------
def test_units_convert_to_the_parameters_own() -> None:
    assert check("alkalinity", 3.0, "meq/L").value == pytest.approx(8.41, abs=0.01)
    assert check("alk", 143.0, "ppm").value == pytest.approx(8.01, abs=0.02)  # as CaCO3
    assert check("phosphate", 30, "ppb P").value == pytest.approx(0.092, abs=0.001)  # Hanna ULR phosphorus
    assert check("po4", 80, "ppb").value == pytest.approx(0.08)
    assert check("nitrate", 1.0, "ppm NO3-N").value == pytest.approx(4.43, abs=0.01)
    assert check("salinity", 1.0264, "SG").value == pytest.approx(35.0, abs=0.15)
    assert sg_from_ppt(35.0) == pytest.approx(1.0264, abs=0.0002) and ppt_from_sg(sg_from_ppt(33.0)) == pytest.approx(33.0)
    assert check("Ca", 430, None).value == 430 and check("dkh", 8.1, "DKH").entered_unit == "dKH"
    assert parameter("KH").id == "alkalinity"


def test_a_kits_error_converts_with_the_value() -> None:
    assert check("alkalinity", 3.0, "meq/L", kit_error=0.1).kit_error == pytest.approx(0.28, abs=0.001)
    assert check("salinity", 1.026, "SG", kit_error=0.001).kit_error == pytest.approx(1.33, abs=0.05)


def test_an_implausible_value_is_questioned_not_refused_outright() -> None:
    assert check("alkalinity", 8.1, "dKH").question is None
    doubt = check("alkalinity", 81, "dKH").question
    assert doubt is not None and "81 dKH" in doubt and "2 to 20 dKH" in doubt
    assert check("calcium", 4300).question is not None
    with pytest.raises(ValueError, match="outside what seawater can have"):  # not a doubt: no seawater has it
        check("salinity", 1.2, "SG")


def test_wrong_input_is_explained() -> None:
    with pytest.raises(ValueError, match="unknown parameter"):
        check("copper", 0.1)
    with pytest.raises(ValueError, match="entered in dKH, meq/L, ppm"):
        check("alkalinity", 8.1, "mg/L")
    with pytest.raises(ValueError, match="a number"):
        check("alkalinity", float("nan"))
    with pytest.raises(ValueError, match="positive number"):
        check("alkalinity", 8.1, kit_error=-1)
