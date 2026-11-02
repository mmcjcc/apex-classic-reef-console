"""The status page's data and its routes, from a database the worker filled with the captured status: who may read
and write, the water tests, the program history and the MCP endpoint."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from reef.analysis import analyse
from reef.conditions import Engine
from reef.main import Worker
from reef.metrics import Metrics
from reef.report import build as build_report
from reef.store import Store
from reefweb.auth import Identity, IdentityUnavailable, Principal
from reefweb.overview import build_overview, connect, ignored_lines

from .test_analysis import NOW as ANALYSIS_NOW
from .test_analysis import tank
from .test_worker import RAW, SITE, T0, FixedClient, doc

PROGRAMS = {
    "whiteLEDR_4_": [
        "Fallback OFF",
        "Set OFF",
        "If Time 12:00 to 20:30 Then ON",
        "If Tmp > 82.0 Then OFF",
        "Min Time 030:00 Then OFF",
    ],
    "Topoff_9_2": ["Fallback OFF", "Set ON", "If freshL OPEN Then ON", "If FreshH CLOSED Then OFF"],
    "Pump1_A3": ["Fallback ON", "OSC 000:00/005:00/005:00 Then ON", "If FeedA 000 Then OFF"],
}


async def filled(tmp_path: Path, documents: list[dict[str, Any]]) -> Path:
    store = Store(tmp_path / "reef.db")
    engine = Engine(SITE)
    worker = Worker(SITE, store, FixedClient(documents), Metrics(SITE, engine), engine, tmp_path)  # type: ignore[arg-type]
    for _ in documents[:]:
        await worker.poll_once()
    store.save_programs(T0, "AC4:12345", PROGRAMS)
    store.close()
    return tmp_path / "reef.db"


def test_the_lines_an_override_ignores_are_the_reacting_ones() -> None:
    assert ignored_lines(PROGRAMS["whiteLEDR_4_"]) == ["If Tmp > 82.0 Then OFF", "Min Time 030:00 Then OFF"]
    assert ignored_lines(PROGRAMS["Topoff_9_2"]) == ["If freshL OPEN Then ON", "If FreshH CLOSED Then OFF"]


async def test_the_overview_has_everything_the_page_shows(tmp_path: Path) -> None:
    path = await filled(tmp_path, [doc(Leak1B=60), doc(Leak1B=60), RAW])
    db = connect(path)
    data = build_overview(db, SITE, T0 + 200)
    db.close()
    assert data["poll"]["current"] and data["apex"]["serial"] == "AC4:12345"
    assert [a["id"] for a in data["alerts"]] == ["leak"]  # still held: one dry poll does not end it
    names = [p["name"] for p in data["probes"]]
    assert names[:3] == ["Tmp", "pH", "ORP"] and "Tmpx5" not in names and "Amp_3" not in names
    tmp = next(p for p in data["probes"] if p["name"] == "Tmp")
    assert tmp["unit"] == "°F" and tmp["history"] and tmp["history"][-1][1] == 79.0
    leak = next(s for s in data["switches"] if s["name"] == "Leak1B")
    assert leak["alarm"] and not leak["closed"]  # the alert holds although the last poll was dry
    eb8_9 = next(m for m in data["modules"] if m["id"] == "EB8_9")
    assert eb8_9["connected"] and eb8_9["amps"] == 2.8 and eb8_9["history"]
    assert [g["id"] for g in data["groups"]] == ["EB8_3", "EB8_4", "EB8_9", "base", "virtual"]
    virtual = data["groups"][-1]
    assert len(virtual["outputs"]) == 8 and virtual["unused"] == 8
    overrides = {o["name"]: o for o in data["overrides"]}
    assert set(overrides) == {"Topoff_9_2", "uv_3_7", "whiteLEDR_4_", "opLedR_4_4", "ophLeft_4_7"}
    assert overrides["whiteLEDR_4_"]["ignored"] == ["If Tmp > 82.0 Then OFF", "Min Time 030:00 Then OFF"]
    assert not overrides["uv_3_7"]["program_known"] and overrides["uv_3_7"]["since"] == T0 + 60
    assert [e["kind"] for e in data["events"]][-1] == "apex.power"


def test_before_the_first_poll_the_page_says_so(tmp_path: Path) -> None:
    Store(tmp_path / "reef.db").close()
    db = connect(tmp_path / "reef.db")
    data = build_overview(db, SITE, T0)
    db.close()
    assert data["apex"] is None and not data["poll"]["current"] and data["probes"] == [] and data["events"] == []


# ---- the routes, as a reverse proxy passes requests on -------------------------------------------------------------------
PEOPLE = {
    "cookie:admin-session": Principal("alex", "admin", "human", True),
    "cookie:viewer-session": Principal("guest", "viewer", "human", False),
    "bearer:agent-token": Principal("claude", "agent", "token", True),
    "bearer:viewer-token": Principal("dashboard", "viewer", "token", False),
}
CSRF = {"X-Requested-With": "reef-console"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """reefweb.app on a filled database (a leak alert held), the console's sign-in check replaced by PEOPLE, and the
    clock five minutes after the captured status."""
    asyncio.run(filled(tmp_path, [doc(Leak1B=60), doc(Leak1B=60), RAW]))
    monkeypatch.setenv("REEF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REEF_SITE_FILE", str(Path(__file__).parent.parent.parent / "config" / "site.example.yaml"))
    monkeypatch.setenv("REEF_PUBLIC_URL", "https://reef.example.test/reef")
    monkeypatch.setenv("REEF_AUTH", "forward")  # behind a console that holds the sign-in; lookup is replaced below
    monkeypatch.setenv("REEF_AUTH_URL", "http://console:8000/api/auth/me")
    monkeypatch.delenv("REEF_HOME_URL", raising=False)
    dist = tmp_path / "dist"  # a stand-in for the built app
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<div id="root"></div><script src="/reef/assets/app-1234.js"></script>', encoding="utf-8")
    (dist / "assets" / "app-1234.js").write_text("console.log('reef')", encoding="utf-8")
    (dist / "manifest.webmanifest").write_text('{"scope": "/reef/"}', encoding="utf-8")
    monkeypatch.setenv("REEF_FRONTEND_DIR", str(dist))
    import reefweb.app

    module = importlib.reload(reefweb.app)

    async def lookup(authorization: str | None, cookie: str | None) -> Principal | None:
        if authorization:
            return PEOPLE.get("bearer:" + authorization.split(" ", 1)[1])
        return PEOPLE.get(f"cookie:{cookie}") if cookie else None

    monkeypatch.setattr(module.identity, "lookup", lookup)
    monkeypatch.setattr(module, "clock", lambda: T0 + 300)
    import reefweb.mcp_server

    monkeypatch.setattr(reefweb.mcp_server, "clock", lambda: T0 + 300)
    yield module


def client(web: ModuleType, session: str | None = None) -> TestClient:
    cookies = {"session": session} if session else None
    return TestClient(web.app, base_url="https://reef.example.test", cookies=cookies)


def test_the_page_and_its_api_need_a_signed_in_caller(web: ModuleType) -> None:
    anonymous = client(web)
    assert anonymous.get("/reef", follow_redirects=False).headers["location"] == "/reef/"
    page = anonymous.get("/reef/")  # forward mode: the proxy in front keeps strangers away from the files
    assert page.status_code == 200 and 'id="root"' in page.text and page.headers["cache-control"] == "no-cache"
    asset = anonymous.get("/reef/assets/app-1234.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    assert anonymous.get("/reef/manifest.webmanifest").json() == {"scope": "/reef/"}
    assert 'id="root"' in anonymous.get("/reef/tests").text  # a page route: the app itself, so a reload works
    assert anonymous.get("/reef/api/nothing-here").status_code == 404
    assert anonymous.get("/reef/../config/site.yaml").status_code == 404
    assert anonymous.get("/reef/api/overview").status_code == 401
    assert anonymous.get("/reef/healthz").status_code == 200
    with client(web, "viewer-session") as viewer:
        overview = viewer.get("/reef/api/overview").json()
        assert overview["poll"]["current"] and overview["groups"] and overview["alerts"][0]["id"] == "leak"
        assert viewer.get("/reef/api/me").json() == {"name": "guest", "role": "viewer", "can_write": False, "home": None}
        assert overview["polling_since"] == T0 + 60  # the overrides' "before": outputs are known from the first poll
    assert client(web, "someone-elses").get("/reef/api/overview").status_code == 401


def test_water_tests_from_the_page(web: ModuleType) -> None:
    body = {"parameter": "alkalinity", "value": 3.0, "unit": "meq/L", "measured_at": T0, "kit": "Hanna HI772", "kit_error": 0.1}
    with client(web, "admin-session") as admin:
        assert admin.post("/reef/api/tests", json=body).status_code == 403  # no X-Requested-With: could be CSRF
        saved = admin.post("/reef/api/tests", json=body, headers=CSRF)
        assert saved.status_code == 201, saved.text
        test = saved.json()
        assert test["value"] == pytest.approx(8.41, abs=0.01) and test["unit"] == "dKH" and test["entered_unit"] == "meq/L"
        assert test["entered_by"] == "alex" and test["source"] == "app" and test["kit_error"] == pytest.approx(0.28, abs=0.01)

        doubtful = admin.post("/reef/api/tests", json=body | {"value": 81, "unit": "dKH"}, headers=CSRF)
        assert doubtful.status_code == 422 and "81 dKH" in doubtful.json()["detail"]["question"]
        anyway = admin.post("/reef/api/tests", json=body | {"value": 81, "unit": "dKH", "unusual_ok": True}, headers=CSRF)
        assert anyway.status_code == 201
        future = admin.post("/reef/api/tests", json=body | {"measured_at": T0 + 3600}, headers=CSRF)
        assert future.status_code == 400 and "future" in future.json()["detail"]
        assert admin.post("/reef/api/tests", json=body | {"parameter": "copper"}, headers=CSRF).status_code == 400

        listed = admin.get("/reef/api/tests").json()
        assert [t["value"] for t in listed["tests"]] == [81.0, pytest.approx(8.41, abs=0.01)]
        assert listed["latest"][0]["parameter"] == "alkalinity" and listed["latest"][0]["previous"]["value"] == test["value"]
        assert {p["id"] for p in listed["parameters"]} >= {"alkalinity", "calcium", "magnesium", "phosphate", "nitrate"}

        newest = listed["tests"][0]["id"]
        assert admin.delete(f"/reef/api/tests/{newest}").status_code == 403  # the CSRF header again
        assert admin.delete(f"/reef/api/tests/{newest}", headers=CSRF).status_code == 204
        assert admin.delete(f"/reef/api/tests/{newest}", headers=CSRF).status_code == 404
        assert [t["id"] for t in admin.get("/reef/api/tests").json()["tests"]] == [test["id"]]

    refused = client(web, "viewer-session").post("/reef/api/tests", json=body, headers=CSRF)  # the app runs once per test
    assert refused.status_code == 403 and "may read but not add" in refused.json()["detail"]


def test_a_manual_test_beside_the_probe_that_measures_the_same(web: ModuleType) -> None:
    body = {"parameter": "ph", "value": 8.30, "measured_at": T0 + 120, "kit": "Hanna HI98100"}
    with client(web, "admin-session") as admin:
        assert admin.post("/reef/api/tests", json=body, headers=CSRF).status_code == 201
        history = admin.get("/reef/api/history", params={"sensor": "pH", "range": "24h"}).json()
    assert history["tests"] == [{"measured_at": T0 + 120, "value": 8.3, "unit": "pH", "kit": "Hanna HI98100", "probe": 8.41,
                                 "difference": 0.11}]  # fmt: skip


def test_the_journal_from_the_page_and_over_mcp(web: ModuleType) -> None:
    entry = {"ts": T0 - 3600, "kind": "Water change", "text": "20 % water change"}
    with client(web, "admin-session") as admin:
        assert admin.post("/reef/api/journal", json=entry).status_code == 403  # the CSRF header
        saved = admin.post("/reef/api/journal", json=entry, headers=CSRF)
        assert saved.status_code == 201 and saved.json()["kind"] == "water change" and saved.json()["entered_by"] == "alex"
        assert admin.post("/reef/api/journal", json=entry | {"kind": "party"}, headers=CSRF).status_code == 400
        assert admin.post("/reef/api/journal", json=entry | {"ts": T0 + 7200}, headers=CSRF).status_code == 400
        listed = admin.get("/reef/api/journal").json()
        assert [e["text"] for e in listed["entries"]] == ["20 % water change"] and "calibration" in listed["kinds"]
        error, preview = tool(admin, "agent-token", "record_journal_entry", kind="calibration", text="pH probe, 7 and 10",
                              when="2026-10-11T09:00")  # fmt: skip
        assert not error and preview["stored"] is False and preview["preview"]["when"] == "2026-10-11T09:00:00-04:00"
        error, stored = tool(admin, "agent-token", "record_journal_entry", kind="calibration", text="pH probe, 7 and 10",
                             when="2026-10-11T09:00", confirm=True)  # fmt: skip
        assert not error and stored["entry"]["source"] == "mcp"
        error, text = tool(admin, "viewer-token", "record_journal_entry", kind="other", text="x", when="now", confirm=True)
        assert error and "may read but not record" in text
        error, entries = tool(admin, "viewer-token", "journal", range="30d")
        assert not error and [e["kind"] for e in entries["entries"]] == ["water change", "calibration"]  # newest first
        assert admin.delete(f"/reef/api/journal/{saved.json()['id']}", headers=CSRF).status_code == 204
        assert [e["kind"] for e in admin.get("/reef/api/journal").json()["entries"]] == ["calibration"]


def test_the_program_history_from_the_page(web: ModuleType) -> None:
    store = Store(Path(web.DATA_FILE))
    store.save_programs(T0 + 86400, "AC4:12345", dict(PROGRAMS, Topoff_9_2=["Fallback OFF", "Set OFF"]))
    store.close()
    with client(web, "viewer-session") as viewer:
        versions = viewer.get("/reef/api/programs").json()["versions"]
        assert [(v["id"], v["first"]) for v in versions] == [(2, False), (1, True)]
        assert versions[0]["summary"] == "Topoff_9_2 changed (+1 -3)" and versions[1]["summary"] == "first version read"
        detail = viewer.get("/reef/api/programs/2").json()
        assert detail["previous"]["id"] == 1 and [c["output"] for c in detail["changes"]] == ["Topoff_9_2"]
        assert detail["programs"]["Topoff_9_2"] == ["Fallback OFF", "Set OFF"]
        assert viewer.get("/reef/api/programs/99").status_code == 404
        overview = viewer.get("/reef/api/overview").json()
        topoff = next(o for o in overview["overrides"] if o["name"] == "Topoff_9_2")
        assert topoff["program_known"] and topoff["ignored"] == []  # the page uses the latest version


# ---- MCP ---------------------------------------------------------------------------------------------------------
def rpc(http: TestClient, token: str | None, method: str, params: dict[str, Any] | None = None) -> httpx.Response:
    headers = MCP_HEADERS | ({"Authorization": f"Bearer {token}"} if token else {})
    return http.post("/reef/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=headers)


def tool(http: TestClient, token: str, name: str, **arguments: Any) -> tuple[bool, Any]:
    answer = rpc(http, token, "tools/call", {"name": name, "arguments": arguments})
    assert answer.status_code == 200, answer.text
    result = answer.json()["result"]
    text = result["content"][0]["text"]
    return bool(result.get("isError")), (text if result.get("isError") else json.loads(text))


def test_mcp_reads_with_any_token(web: ModuleType) -> None:
    assert web.mcp_asgi is not None  # the endpoint can be built with the installed MCP library
    with client(web) as http:
        assert rpc(http, None, "tools/list").status_code == 401
        assert rpc(http, "no-such-token", "tools/list").status_code == 401
        listed = rpc(http, "viewer-token", "tools/list")
        assert listed.status_code == 200, listed.text
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert names == {"status", "outputs", "sensor_history", "output_history", "conditions", "events", "programs",
                         "program_history", "water_tests", "record_water_test", "analysis", "journal",
                         "record_journal_entry", "weekly_report", "reactor"}  # fmt: skip

        error, status = tool(http, "viewer-token", "status")
        assert not error and status["apex"]["serial"] == "AC4:12345" and status["alerts"][0]["id"] == "leak"
        assert status["poll"]["at"].endswith("-04:00")  # times in the house's zone (EDT in October)
        assert "history" not in status["probes"][0] and "events" not in status
        error, history = tool(http, "viewer-token", "sensor_history", sensor="Tmp", range="24h")
        assert not error and history["unit"] == "°F" and history["points"][0][1] == 79.0
        error, leak = tool(http, "viewer-token", "sensor_history", sensor="Leak1B", range="24h")
        assert not error and [c[1] for c in leak["changes"]] == ["CLOSED", "OPEN"]
        error, text = tool(http, "viewer-token", "sensor_history", sensor="Nope")
        assert error and "no history for 'Nope'" in text
        error, conditions = tool(http, "viewer-token", "conditions")
        leak_condition = next(c for c in conditions["conditions"] if c["id"] == "leak")
        assert not error and leak_condition["severity"] == "critical" and leak_condition["held"]
        error, events = tool(http, "viewer-token", "events", range="365d", kind="apex")
        assert not error and [e["kind"] for e in events["events"]] == ["apex.power"]
        error, programs = tool(http, "viewer-token", "programs", output="Topoff_9_2")
        assert not error and programs["programs"] == {"Topoff_9_2": PROGRAMS["Topoff_9_2"]}
        error, versions = tool(http, "viewer-token", "program_history")
        assert not error and versions["versions"][0]["first"]
        error, outlet = tool(http, "viewer-token", "output_history", output="uv_3_7", range="24h")
        assert not error and outlet["changes"][0]["mode"] == "ON"


def test_mcp_records_a_water_test_only_for_an_agent_and_only_after_confirm(web: ModuleType) -> None:
    alk = {"parameter": "alk", "value": 8.1, "unit": "dKH", "measured_at": "2026-10-11T09:30", "kit": "Hanna"}
    with client(web) as http:
        error, text = tool(http, "viewer-token", "record_water_test", **alk, confirm=True)
        assert error and "may read but not record" in text
        error, preview = tool(http, "agent-token", "record_water_test", **alk)
        assert not error and preview["stored"] is False and preview["question"] is None
        assert preview["preview"]["measured_at"] == "2026-10-11T09:30:00-04:00" and preview["preview"]["value"] == 8.1
        error, tests = tool(http, "viewer-token", "water_tests", range="30d")
        assert not error and tests["tests"] == []  # a preview stores nothing
        error, stored = tool(http, "agent-token", "record_water_test", **alk, confirm=True)
        assert not error and stored["stored"] and stored["test"]["entered_by"] == "claude" and stored["test"]["source"] == "mcp"
        error, text = tool(http, "agent-token", "record_water_test", **(alk | {"value": 81}), confirm=True)
        assert error and "unusual_ok=true" in text
        error, text = tool(http, "agent-token", "record_water_test", **(alk | {"measured_at": "this morning"}))
        assert error and "not an ISO 8601 time" in text
        error, text = tool(http, "agent-token", "record_water_test", **(alk | {"measured_at": "2026-10-12T09:30"}), confirm=True)
        assert error and "future" in text
        error, tests = tool(http, "viewer-token", "water_tests", parameter="alkalinity", range="30d")
        assert not error and [t["value"] for t in tests["tests"]] == [8.1]
        assert tests["tests"][0]["measured_at"] == "2026-10-11T09:30:00-04:00"


# ---- the console's sign-in check ---------------------------------------------------------------------------------
async def test_the_sign_in_check_asks_the_console_once_a_minute() -> None:
    calls: list[dict[str, str]] = []

    def console(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if request.headers.get("authorization") == "Bearer good":
            return httpx.Response(200, json={"name": "claude", "role": "agent", "kind": "token", "can_request": True})
        if request.headers.get("cookie") == "session=alexs":
            return httpx.Response(200, json={"name": "alex", "role": "admin", "kind": "human", "can_request": True})
        if request.headers.get("authorization") == "Bearer broken":
            return httpx.Response(502)
        return httpx.Response(401)

    identity = Identity("http://console:8000/api/auth/me", "session", transport=httpx.MockTransport(console))
    assert await identity.lookup("Bearer good", None) == Principal("claude", "agent", "token", True)
    assert await identity.lookup("Bearer good", None) is not None and len(calls) == 1  # kept for a minute
    assert await identity.lookup(None, "alexs") == Principal("alex", "admin", "human", True)
    assert calls[-1]["cookie"] == "session=alexs" and "authorization" not in calls[-1]
    assert await identity.lookup("Bearer bad", None) is None and await identity.lookup(None, None) is None
    with pytest.raises(IdentityUnavailable):
        await identity.lookup("Bearer broken", None)
    assert await Identity("", "session").lookup("Bearer good", None) is None  # no console configured: nobody
    await identity.close()


def test_the_api_answers_the_frontend_tests_use(web: ModuleType) -> None:
    """The answers the frontend's tests render (src/test/api/*.json). The image build writes them with
    REEF_FIXTURE_DIR set and copies them into the frontend's stage, so the pages are tested against the real API."""
    store = Store(Path(web.DATA_FILE))
    store.save_programs(T0 + 60, "AC4:12345", dict(PROGRAMS, Topoff_9_2=["Fallback OFF", "Set OFF"]))
    rows = [(ts + T0 - ANALYSIS_NOW, v) for ts, v in tank(79.0, 0.6, 0.05)]
    rows = [(ts, v + 1.5 if T0 - 86400 <= ts < T0 - 82800 else v) for ts, v in rows]  # an hour too warm, yesterday
    store.kv_set("analysis", analyse(SITE, {"Tmp": rows, "pH": rows[-300:]}, T0))
    store.save_report(T0 + 300, build_report(store.db, SITE, T0 + 300, T0 + 300))
    store.close()
    body = {
        "parameter": "alkalinity",
        "value": 8.1,
        "unit": "dKH",
        "measured_at": T0 - 86400,
        "kit": "Hanna HI772",
        "kit_error": 0.3,
    }
    with client(web, "admin-session") as admin:
        entry = {"ts": T0 - 3600, "kind": "water change", "text": "20 % water change, new salt batch"}
        assert admin.post("/reef/api/journal", json=entry, headers=CSRF).status_code == 201
        assert admin.post("/reef/api/tests", json=body, headers=CSRF).status_code == 201
        assert admin.post("/reef/api/tests", json=body | {"value": 7.9, "measured_at": T0}, headers=CSRF).status_code == 201
        calcium = {"parameter": "calcium", "value": 430, "measured_at": T0, "kit": "Hanna HI758"}
        assert admin.post("/reef/api/tests", json=calcium, headers=CSRF).status_code == 201
        answers = {
            "me": admin.get("/reef/api/me"),
            "overview": admin.get("/reef/api/overview"),
            "history": admin.get("/reef/api/history", params={"sensor": "Tmp", "range": "7d"}),
            "history-switch": admin.get("/reef/api/history", params={"sensor": "Leak1B", "range": "7d"}),
            "events": admin.get("/reef/api/events", params={"range": "365d"}),
            "conditions": admin.get("/reef/api/conditions"),
            "tests": admin.get("/reef/api/tests"),
            "programs": admin.get("/reef/api/programs"),
            "program": admin.get("/reef/api/programs/2"),
            "analysis": admin.get("/reef/api/analysis"),
            "journal": admin.get("/reef/api/journal"),
            "reports": admin.get("/reef/api/reports"),
            "reactor": admin.get("/reef/api/reactor"),
            "report": admin.get("/reef/api/reports/1"),
        }
        preview = admin.get("/reef/api/reports/preview").json()
        assert preview["id"] is None and preview["sections"][0]["title"] == "Alerts"
        assert admin.get("/reef/api/reports/9").status_code == 404
        error, weekly = tool(admin, "viewer-token", "weekly_report")
        assert not error and weekly["text"].startswith("Aquarium, the week of") and weekly["id"] == 1
    assert all(r.status_code == 200 for r in answers.values()), {k: r.status_code for k, r in answers.items()}
    assert answers["history"].json()["points"] and answers["history-switch"].json()["changes"]
    assert "tests" not in answers["history"].json()  # no manual test measures what Tmp measures
    assert len(answers["conditions"].json()["conditions"]) == len(SITE.conditions)
    assert [f["kind"] for f in answers["analysis"].json()["findings"]] == ["unusual"]
    overview = answers["overview"].json()
    assert {b["module"] for b in overview["energy"]["bars"]} == {"EB8_3", "EB8_4", "EB8_9"}
    assert overview["external_sensors"] == [{"name": "Outdoor", "label": "Outdoor temperature", "unit": "°F"}]
    assert answers["reports"].json()["reports"][0]["id"] == 1 and answers["report"].json()["sections"]
    reactor = answers["reactor"].json()
    assert reactor["configured"] and reactor["co2"] == "Newco21_3_1" and reactor["days"]
    assert [t["value"] for t in reactor["tests"]["alkalinity"]] == [8.1, 7.9] and "No calcium test" not in (reactor["note"] or "")
    folder = os.environ.get("REEF_FIXTURE_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        for name, response in answers.items():
            (Path(folder) / f"{name}.json").write_text(
                json.dumps(response.json(), indent=1, ensure_ascii=False), encoding="utf-8"
            )
