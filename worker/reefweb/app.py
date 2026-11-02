"""reef-console's pages, served under /reef: the app (React, built into dist/ by the image), its JSON API, the water
tests and journal (the only things written here), and the MCP endpoint at /reef/mcp. Who may do what: auth.py.
Start: uvicorn reefweb.app:app --port 8001

Settings: REEF_DATA_DIR (the worker's database), REEF_SITE_FILE (config/site.yaml), REEF_AUTH and the settings of its
mode (auth.py), REEF_PUBLIC_URL (the address people and MCP clients use, e.g. http://reef.lan:8001/reef: the MCP
endpoint answers only to its host and REEF_EXTRA_HOSTS, comma-separated), REEF_HOME_URL and REEF_HOME_LABEL (a link
back to a home page, shown at the foot of the menu), REEF_FRONTEND_DIR (the built app, default reefweb/dist)."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from reef.report import build as build_report
from reef.site import load_site
from reef.water import check

from . import queries as q
from .auth import Identity, Principal, caller, reader, writer
from .overview import build_overview
from .reactor import reactor as reactor_view

clock = time.time  # the tests set their own
log = logging.getLogger("reefweb")
PREFIX = "/reef"
FRONTEND = Path(os.environ.get("REEF_FRONTEND_DIR", str(Path(__file__).parent / "dist")))
DATA_FILE = Path(os.environ.get("REEF_DATA_DIR", "/data")) / "reef.db"
SITE = load_site(Path(os.environ.get("REEF_SITE_FILE", "/app/config/site.yaml")))
PUBLIC_URL = os.environ.get("REEF_PUBLIC_URL", "http://localhost:8001/reef")
EXTRA_HOSTS = [h.strip() for h in os.environ.get("REEF_EXTRA_HOSTS", "").split(",") if h.strip()]
HOME = (
    {"label": os.environ.get("REEF_HOME_LABEL", "Home"), "href": os.environ["REEF_HOME_URL"]}
    if os.environ.get("REEF_HOME_URL")
    else None
)

Reader = Annotated[Principal, Depends(reader)]
Writer = Annotated[Principal, Depends(writer)]


def _build_mcp(identity: Identity) -> tuple[Any, ASGIApp | None]:
    """The MCP endpoint is optional at start: a problem there must not take the pages down."""
    try:
        from .mcp_server import build_mcp

        return build_mcp(SITE, DATA_FILE, identity, PUBLIC_URL, EXTRA_HOSTS)
    except Exception:  # noqa: BLE001
        log.exception("MCP endpoint disabled: it could not be built")
        return None, None


identity = Identity.from_env()
mcp, mcp_asgi = _build_mcp(identity)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    db = q.connect(DATA_FILE, write=True)  # the tables reef-web writes exist before the first request
    db.close()
    async with AsyncExitStack() as stack:
        if mcp is not None:
            await stack.enter_async_context(mcp.session_manager.run())
        try:
            yield
        finally:
            await identity.close()


api = FastAPI(title="reef-console", docs_url=None, redoc_url=None, openapi_url=f"{PREFIX}/api/openapi.json", lifespan=lifespan)
api.state.identity = identity


@api.exception_handler(ValueError)
async def _value_error(_request: Any, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@api.get(PREFIX, include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(f"{PREFIX}/")


@api.get(f"{PREFIX}/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@api.get(f"{PREFIX}/api/me")
def me(principal: Reader) -> dict[str, Any]:
    return {"name": principal.name, "role": principal.role, "can_write": principal.can_write, "home": HOME}


@api.get(f"{PREFIX}/api/overview")
def overview(_p: Reader) -> dict[str, Any]:
    db = q.connect(DATA_FILE)
    try:
        return build_overview(db, SITE, clock())
    finally:
        db.close()


@api.get(f"{PREFIX}/api/history")
def history(_p: Reader, sensor: str, range: str = "24h") -> dict[str, Any]:
    db = q.connect(DATA_FILE)
    try:
        now = clock()
        return q.sensor_history(db, SITE, sensor, now - q.span(range), now)
    finally:
        db.close()


@api.get(f"{PREFIX}/api/events")
def events(_p: Reader, range: str = "7d", kind: str | None = None, limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    db = q.connect(DATA_FILE)
    try:
        return {"events": q.events(db, clock() - q.span(range), kind, limit)}
    finally:
        db.close()


@api.get(f"{PREFIX}/api/conditions")
def conditions(_p: Reader) -> dict[str, Any]:
    db = q.connect(DATA_FILE)
    try:
        return {"conditions": q.conditions(db, SITE)}
    finally:
        db.close()


@api.get(f"{PREFIX}/api/analysis")
def analysis(_p: Reader) -> dict[str, Any]:
    """The worker's latest analysis of the probe history (once an hour): findings, and per probe the last week against
    what was expected and the next 24 hours' band."""
    db = q.connect(DATA_FILE)
    try:
        return q.kv(db, "analysis") or {"computed_at": None, "window_days": None, "sensors": [], "findings": []}
    finally:
        db.close()


@api.get(f"{PREFIX}/api/programs")
def programs(_p: Reader, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    db = q.connect(DATA_FILE)
    try:
        return {"versions": q.program_versions(db, limit)}
    finally:
        db.close()


@api.get(f"{PREFIX}/api/programs/{{version_id}}")
def program(_p: Reader, version_id: int) -> dict[str, Any]:
    db = q.connect(DATA_FILE)
    try:
        version = q.program_version(db, version_id)
    finally:
        db.close()
    if version is None:
        raise HTTPException(404, "no such program version")
    return version


@api.get(f"{PREFIX}/api/tests")
def tests(
    _p: Reader, parameter: str | None = None, days: int = Query(365, ge=1, le=3650), limit: int = Query(200, ge=1, le=2000)
) -> dict[str, Any]:
    db = q.connect(DATA_FILE)
    try:
        since = clock() - days * 86400
        return {"tests": q.water_tests(db, parameter, since, limit), "latest": q.latest_tests(db), "parameters": q.catalogue()}
    finally:
        db.close()


class WaterTestIn(BaseModel):
    parameter: str = Field(min_length=1, max_length=40)
    value: float
    unit: str | None = Field(default=None, max_length=20)
    measured_at: float  # UTC epoch seconds: the page sends the time the person saw in the form
    kit: str = Field(default="", max_length=120)
    kit_error: float | None = None
    note: str = Field(default="", max_length=500)
    unusual_ok: bool = False  # the person confirmed a value outside the plausible range


@api.post(f"{PREFIX}/api/tests", status_code=201)
def add_test(body: WaterTestIn, principal: Writer) -> dict[str, Any]:
    checked = check(body.parameter, body.value, body.unit, body.kit_error)
    if checked.question and not body.unusual_ok:
        raise HTTPException(422, {"question": checked.question})
    db = q.connect(DATA_FILE, write=True)
    try:
        return q.add_water_test(db, checked, body.measured_at, body.kit, body.note, principal.name, "app", clock())
    finally:
        db.close()


@api.delete(f"{PREFIX}/api/tests/{{test_id}}", status_code=204)
def delete_test(test_id: int, principal: Writer) -> None:
    db = q.connect(DATA_FILE, write=True)
    try:
        if not q.delete_water_test(db, test_id, principal.name, clock()):
            raise HTTPException(404, "no such test")
    finally:
        db.close()


@api.get(f"{PREFIX}/api/reports")
def reports(_p: Reader) -> dict[str, Any]:
    db = q.connect(DATA_FILE)
    try:
        return {"reports": q.reports(db)}
    finally:
        db.close()


@api.get(f"{PREFIX}/api/reports/preview")
def report_preview(_p: Reader) -> dict[str, Any]:
    """The report the worker would make for the 7 days up to now (not kept)."""
    db = q.connect(DATA_FILE)
    try:
        now = clock()
        return {"id": None, "ts": now} | build_report(db, SITE, now, now)
    finally:
        db.close()


@api.get(f"{PREFIX}/api/reports/{{report_id}}")
def report(_p: Reader, report_id: int) -> dict[str, Any]:
    db = q.connect(DATA_FILE)
    try:
        found = q.report(db, report_id)
    finally:
        db.close()
    if found is None:
        raise HTTPException(404, "no such report")
    return found


@api.get(f"{PREFIX}/api/reactor")
def reactor(_p: Reader, days: int = Query(30, ge=3, le=365)) -> dict[str, Any]:
    """The calcium reactor: CO2 minutes and effluent pH per day, the alkalinity and calcium tests, and their balance."""
    db = q.connect(DATA_FILE)
    try:
        return reactor_view(db, SITE, days, clock())
    finally:
        db.close()


@api.get(f"{PREFIX}/api/journal")
def journal(_p: Reader, days: int = Query(365, ge=1, le=3650)) -> dict[str, Any]:
    db = q.connect(DATA_FILE)
    try:
        return {"entries": q.journal(db, clock() - days * 86400), "kinds": list(q.JOURNAL_KINDS)}
    finally:
        db.close()


class JournalIn(BaseModel):
    ts: float  # UTC epoch seconds, as the person saw it in the form
    kind: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=1000)


@api.post(f"{PREFIX}/api/journal", status_code=201)
def add_journal(body: JournalIn, principal: Writer) -> dict[str, Any]:
    db = q.connect(DATA_FILE, write=True)
    try:
        return q.add_journal(db, body.ts, body.kind, body.text, principal.name, "app", clock())
    finally:
        db.close()


@api.delete(f"{PREFIX}/api/journal/{{entry_id}}", status_code=204)
def delete_journal(entry_id: int, principal: Writer) -> None:
    db = q.connect(DATA_FILE, write=True)
    try:
        if not q.delete_journal(db, entry_id, principal.name, clock()):
            raise HTTPException(404, "no such entry")
    finally:
        db.close()


def _files(path: str) -> tuple[Path | None, Path | None]:
    """The app's file at this path (never outside the app's folder, never index.html), and index.html if built."""
    root = FRONTEND.resolve()
    candidate = (FRONTEND / path).resolve()
    found = candidate if path and path != "index.html" and candidate.is_file() and root in candidate.parents else None
    index = FRONTEND / "index.html"
    return found, index if index.is_file() else None


@api.get(f"{PREFIX}/{{path:path}}", include_in_schema=False)
async def app_files(path: str, request: Request) -> Any:
    """The built app: its files (hashed assets kept a year), and index.html for every page route, so a reload of
    /reef/tests works. Routes match in order: this one stays the last, after every API route. In basic mode the page
    itself asks for the sign-in (so the browser keeps it for all of /reef/); the files beside it hold no data."""
    if path.startswith(("api/", "mcp")):
        raise HTTPException(404, "Not found")
    candidate, index = _files(path)
    if candidate is not None:
        if path.startswith("assets/"):
            return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"})
        return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
    if index is None:
        raise HTTPException(404, "The app is not built into this image")
    who: Identity = request.app.state.identity
    if who.protects_pages and await caller(request) is None:
        headers = {"WWW-Authenticate": who.challenge} if who.challenge else None
        return HTMLResponse("<p>Sign-in needed: reload the page to try again.</p>", status_code=401, headers=headers)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


class Dispatcher:
    """/reef/mcp to the MCP app, everything else to the pages."""

    def __init__(self, pages: ASGIApp, mcp_app: ASGIApp | None) -> None:
        self.pages = pages
        self.mcp_app = mcp_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.mcp_app is not None and scope["type"] == "http" and scope.get("path", "").rstrip("/") == f"{PREFIX}/mcp":
            await self.mcp_app(scope, receive, send)
            return
        await self.pages(scope, receive, send)


app = Dispatcher(api, mcp_asgi)
