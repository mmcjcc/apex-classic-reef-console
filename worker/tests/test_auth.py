"""reef-web's own sign-in (basic: a users file), no sign-in (none), and the worker's Apex sign-in from the environment."""

from __future__ import annotations

import asyncio
import base64
import importlib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from reef.apex import ApexError, Credential
from reef.main import read_secret
from reefweb.auth import BasicIdentity, Principal, hash_password, main, parse_users, verify_password

from .test_web import CSRF, filled, rpc, tool
from .test_worker import RAW, SITE, doc

FAST = 1000  # hash iterations for the tests; a real users file uses the default


def basic(name: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()}


def test_a_password_hash_verifies_only_its_password() -> None:
    stored = hash_password("correct horse", FAST)
    assert stored.startswith("pbkdf2_sha256$1000$") and verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored) and not verify_password("correct horse", "md5$1$x$y")
    assert hash_password("correct horse", FAST) != stored  # a new salt each time


def test_the_users_file_is_name_role_hash_and_a_mistake_stops_the_start() -> None:
    line = hash_password("pw-123456", FAST)
    assert parse_users(f"# people\n\nalex:admin:{line}\nscreen:viewer:{line}\n") == {
        "alex": ("admin", line),
        "screen": ("viewer", line),
    }
    for bad in (f"alex:owner:{line}", "alex:admin:plaintext", f":admin:{line}", "alex:admin"):
        with pytest.raises(ValueError):
            parse_users(bad)


def test_the_cli_prints_a_users_file_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("a-long-password\n"))
    assert main(["alex", "agent"]) == 0
    name, role, stored = capsys.readouterr().out.strip().split(":", 2)
    assert (name, role) == ("alex", "agent") and verify_password("a-long-password", stored)
    assert main(["alex", "owner"]) == 2 and main(["a:b", "agent"]) == 2


async def test_basic_mode_knows_people_by_password_and_tokens_by_name_and_password(tmp_path: Path) -> None:
    users = tmp_path / "users.txt"
    users.write_text(f"alex:admin:{hash_password('pw-alex-1', FAST)}\nscreen:viewer:{hash_password('pw-screen', FAST)}\n")
    identity = BasicIdentity(users)
    header = basic("alex", "pw-alex-1")["Authorization"]
    assert await identity.lookup(header, None) == Principal("alex", "admin", "human", True)
    assert await identity.lookup("Bearer screen:pw-screen", None) == Principal("screen", "viewer", "token", False)
    assert await identity.lookup(basic("alex", "nope")["Authorization"], None) is None
    assert await identity.lookup("Bearer pw-screen", None) is None  # a token is name:password
    assert await identity.lookup("Basic !!!", None) is None and await identity.lookup(None, "cookie") is None
    users.write_text(f"alex:viewer:{hash_password('pw-alex-2', FAST)}\n")  # read again when it changes
    assert await identity.lookup(header, None) is None
    assert await identity.lookup(basic("alex", "pw-alex-2")["Authorization"], None) == Principal("alex", "viewer", "human", False)
    users.write_text("alex:admin:typo\n")  # a mistake after the start: the users read before stay
    assert await identity.lookup(basic("alex", "pw-alex-2")["Authorization"], None) is not None
    assert await BasicIdentity(tmp_path / "missing.txt").lookup(header, None) is None  # no file: nobody


@pytest.fixture
def web_basic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    asyncio.run(filled(tmp_path, [doc(Leak1B=60), RAW]))
    users = tmp_path / "users.txt"
    users.write_text(f"alex:admin:{hash_password('pw-alex-1', FAST)}\nscreen:viewer:{hash_password('pw-screen', FAST)}\n")
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    (dist / "manifest.webmanifest").write_text('{"scope": "/reef/"}', encoding="utf-8")
    for key, value in {
        "REEF_DATA_DIR": str(tmp_path),
        "REEF_SITE_FILE": str(Path(__file__).parent.parent.parent / "config" / "site.example.yaml"),
        "REEF_PUBLIC_URL": "http://reef.example.test:8001/reef",
        "REEF_AUTH": "basic",
        "REEF_USERS_FILE": str(users),
        "REEF_FRONTEND_DIR": str(dist),
        "REEF_HOME_URL": "/",
        "REEF_HOME_LABEL": "Home lab",
    }.items():
        monkeypatch.setenv(key, value)
    import reefweb.app

    yield importlib.reload(reefweb.app)


def test_in_basic_mode_the_page_asks_for_the_sign_in_and_writes_need_the_csrf_header(web_basic: ModuleType) -> None:
    anonymous = TestClient(web_basic.app, base_url="http://reef.example.test:8001")
    page = anonymous.get("/reef/")
    assert page.status_code == 401 and page.headers["www-authenticate"].startswith('Basic realm="reef-console"')
    assert anonymous.get("/reef/index.html").status_code == 401 and anonymous.get("/reef/charts").status_code == 401
    assert anonymous.get("/reef/manifest.webmanifest").status_code == 200  # the files beside the page hold no data
    api = anonymous.get("/reef/api/overview")
    assert api.status_code == 401 and "www-authenticate" in api.headers
    assert anonymous.get("/reef/healthz").status_code == 200
    alex = basic("alex", "pw-alex-1")
    assert 'id="root"' in anonymous.get("/reef/", headers=alex).text
    me = anonymous.get("/reef/api/me", headers=alex).json()
    assert me == {"name": "alex", "role": "admin", "can_write": True, "home": {"label": "Home lab", "href": "/"}}
    entry = {"ts": 1791766000, "kind": "water change", "text": "10 %"}
    assert anonymous.post("/reef/api/journal", json=entry, headers=alex).status_code == 403  # a browser's Basic: CSRF
    assert anonymous.post("/reef/api/journal", json=entry, headers=alex | CSRF).status_code == 201
    token = {"Authorization": "Bearer alex:pw-alex-1"}
    assert anonymous.post("/reef/api/journal", json=entry, headers=token).status_code == 201  # a token needs no header
    viewer = basic("screen", "pw-screen") | CSRF
    assert anonymous.post("/reef/api/journal", json=entry, headers=viewer).status_code == 403
    assert anonymous.get("/reef/api/overview", headers=basic("alex", "wrong")).status_code == 401


def test_in_basic_mode_mcp_takes_name_and_password_as_its_token(web_basic: ModuleType) -> None:
    with TestClient(web_basic.app, base_url="http://reef.example.test:8001") as http:
        assert rpc(http, None, "tools/list").status_code == 401
        assert rpc(http, "alex:wrong", "tools/list").status_code == 401
        listed = rpc(http, "alex:pw-alex-1", "tools/list")
        assert listed.status_code == 200, listed.text
        error, status = tool(http, "screen:pw-screen", "status")
        assert not error and status["apex"]["serial"] == "AC4:12345"


def test_with_no_sign_in_everyone_is_the_local_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(filled(tmp_path, [RAW]))
    monkeypatch.setenv("REEF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REEF_SITE_FILE", str(Path(__file__).parent.parent.parent / "config" / "site.example.yaml"))
    monkeypatch.setenv("REEF_AUTH", "none")
    import reefweb.app

    web = importlib.reload(reefweb.app)
    c = TestClient(web.app, base_url="http://localhost:8001")
    assert c.get("/reef/api/me").json()["name"] == "local" and c.get("/reef/api/overview").status_code == 200
    entry = {"ts": 1791766000, "kind": "water change", "text": "10 %"}
    assert c.post("/reef/api/journal", json=entry).status_code == 403  # still no cross-site writes
    assert c.post("/reef/api/journal", json=entry, headers=CSRF).status_code == 201
    monkeypatch.setenv("REEF_AUTH", "sometimes")
    with pytest.raises(ValueError):
        importlib.reload(reefweb.app)


def test_the_apex_sign_in_may_come_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "apex"
    secret.write_text("ip, username, password\n192.0.2.9, filer, from-the-file\n")
    monkeypatch.setenv("REEF_SECRET_FILE", str(secret))
    monkeypatch.delenv("REEF_APEX_USER", raising=False)
    monkeypatch.delenv("REEF_APEX_PASSWORD", raising=False)
    assert read_secret(SITE) == Credential("192.0.2.9", "filer", "from-the-file")
    monkeypatch.setenv("REEF_APEX_USER", "admin")
    monkeypatch.setenv("REEF_APEX_PASSWORD", "from-the-env")
    assert read_secret(SITE) == Credential(SITE.address, "admin", "from-the-env")
    import dataclasses

    with pytest.raises(ApexError):
        read_secret(dataclasses.replace(SITE, address=""))
