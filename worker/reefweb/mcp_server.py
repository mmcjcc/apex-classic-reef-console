"""MCP endpoint (/reef/mcp, Streamable HTTP): the aquarium for Claude Code and other MCP clients.

Authentication is a bearer token, checked by reef-web's sign-in (auth.py): in basic mode `name:password` of a line in
the users file, in forward mode a token of the service that holds the sign-in, in none mode any token. viewers read;
agents and admins may also record water tests and journal entries. Nothing here changes an outlet or the Apex: the
tools read the worker's database, and the only writes are records, which need confirm=true after the person has seen
them.

Written against the MCP Python SDK 2.x (MCPServer)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl

from reef.report import build as build_report
from reef.report import text as report_text
from reef.site import Site
from reef.water import check
from reef.water import parameter as find_parameter

from . import queries as q
from .auth import Identity, IdentityUnavailable
from .overview import build_overview
from .reactor import reactor as reactor_view

clock = time.time  # the tests set their own
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
RECORDS = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
TIME_KEYS = {"ts", "since", "measured_at", "entered_at", "at", "last_good", "received_at", "power_failed", "power_restored",
             "history_since", "now", "computed_at", "start", "end"}  # fmt: skip


class CallerTokenVerifier(TokenVerifier):
    """Accepts the tokens reef-web's sign-in knows (auth.Identity); it answers who the token is."""

    def __init__(self, identity: Identity, resource: str) -> None:
        self.identity = identity
        self.resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = await self.identity.lookup(f"Bearer {token}", None)
        except IdentityUnavailable:
            return None
        if principal is None:
            return None
        scopes = [principal.role] + (["write"] if principal.can_write else [])
        return AccessToken(token=token, client_id=principal.name, scopes=scopes, resource=self.resource, subject=principal.name)


def _caller(write: bool = False) -> str:
    token = get_access_token()
    if token is None:
        raise ToolError("no access token on this request")
    if write and "write" not in token.scopes:
        raise ToolError("this token may read but not record (a viewer token); use an agent token")
    return token.subject or token.client_id


def localize(value: Any, zone: str) -> Any:
    """Epoch seconds under the known time keys become ISO 8601 in the house's zone, all the way down."""
    if isinstance(value, dict):
        return {k: (q.iso(v, zone) if k in TIME_KEYS and isinstance(v, (int, float)) and v else localize(v, zone))
                for k, v in value.items()}  # fmt: skip
    if isinstance(value, list):
        return [localize(v, zone) for v in value]
    return value


def parse_time(text: str, zone: str, now: float) -> float:
    """ISO 8601 ("2026-10-12T07:30", with or without an offset; without one it is the house's time) or "now"."""
    if text.strip().lower() == "now":
        return now
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ToolError(f"measured_at {text!r} is not an ISO 8601 time (e.g. 2026-10-12T07:30) or 'now'") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(zone))
    return moment.timestamp()


def build_mcp(site: Site, data_file: Path, identity: Identity, public_url: str, extra_hosts: list[str]) -> tuple[MCPServer, Any]:
    """The MCP server and its ASGI app (route /reef/mcp). The caller runs mcp.session_manager.run()."""
    resource = public_url.rstrip("/") + "/mcp"
    origin = urlparse(public_url)
    mcp = MCPServer(
        "reef-console",
        instructions=(
            "The reef aquarium's Neptune Apex Classic: live status, history, alert conditions, events, the Apex's "
            "programs and their changes, manual water tests and the journal. Read-only, except record_water_test and "
            "record_journal_entry. Times are in "
            f"{site.timezone}. Nothing here changes an outlet."
        ),
        token_verifier=CallerTokenVerifier(identity, resource),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(f"{origin.scheme}://{origin.netloc}"),
            resource_server_url=AnyHttpUrl(resource),
            validate_token_resource=True,
        ),
    )
    zone = site.timezone

    def read(fn: Any, *args: Any) -> Any:
        _caller()
        db = q.connect(data_file)
        try:
            return localize(fn(db, *args), zone)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        finally:
            db.close()

    @mcp.tool(annotations=READ_ONLY)
    async def status() -> dict[str, Any]:
        """The tank now: whether the Apex answers, the alerts, every probe (temperature, pH, ORP, salinity, ...),
        the floats and leak sensor, the EnergyBars with their current, the outputs in a manual override with the
        program lines they ignore, and the newest water test of each parameter."""

        def build(db: Any) -> dict[str, Any]:
            data = build_overview(db, site, clock())
            for p in data.get("probes", []):
                p.pop("history", None)
            for m in data.get("modules", []):
                m.pop("history", None)
            data.pop("events", None)
            data.pop("groups", None)
            data["water_tests"] = [{k: t[k] for k in ("parameter", "label", "value", "unit", "measured_at", "kit")}
                                   for t in q.latest_tests(db)]  # fmt: skip
            return data

        return read(build)

    @mcp.tool(annotations=READ_ONLY)
    async def outputs() -> dict[str, Any]:
        """Every output (outlets, variable-speed and alarm outputs, virtual outlets) by EnergyBar: its mode (AON/AOF
        automatic, ON/OFF manual override) and whether it is energized."""
        return read(lambda db: {"groups": build_overview(db, site, clock()).get("groups", [])})

    @mcp.tool(annotations=READ_ONLY)
    async def sensor_history(sensor: str, range: str = "24h") -> dict[str, Any]:
        """One input's history. A probe (Tmp, pH, ORP, Condx6, pHx5, Amp_3, ...) as [time, mean, min, max] per step;
        a switch (sumpLo, SumpHi, freshL, FreshH, Leak1B) as its changes. range: 1h, 6h, 24h, 7d, 30d, 90d, 365d."""

        def build(db: Any) -> dict[str, Any]:
            data = q.sensor_history(db, site, sensor, clock() - q.span(range), clock())
            for key in ("points", "changes"):
                if key in data:
                    data[key] = [[q.iso(row[0], zone), *row[1:]] for row in data[key]]
            return data

        return read(build)

    @mcp.tool(annotations=READ_ONLY)
    async def output_history(output: str, range: str = "7d") -> dict[str, Any]:
        """One output's changes of mode (AON, AOF, ON, OFF) or energized state, with the state before the range.
        output is the Apex's name (e.g. mainpump_9_8). range: 1h, 6h, 24h, 7d, 30d, 90d, 365d."""
        return read(lambda db: q.outlet_history(db, output, clock() - q.span(range)))

    @mcp.tool(annotations=READ_ONLY)
    async def conditions() -> dict[str, Any]:
        """Every alert condition (imported from the Apex's alarm programs, plus the EnergyBar checks): its limit,
        clearing band, pending time, severity, and whether it is active now."""
        return read(lambda db: {"conditions": q.conditions(db, site)})

    @mcp.tool(annotations=READ_ONLY)
    async def events(range: str = "7d", kind: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Events, newest first: alerts started and ended (condition), Apex power failures and clock steps (apex),
        program changes (apex.program), new modules (module). kind filters by prefix. range: 1h ... 365d."""
        return read(lambda db: {"events": q.events(db, clock() - q.span(range), kind, limit)})

    @mcp.tool(annotations=READ_ONLY)
    async def programs(output: str | None = None) -> dict[str, Any]:
        """The Apex's programs as last read (once a day): every output's lines, or one output's. Read-only: they
        are changed in Fusion, never from here."""

        def build(db: Any) -> dict[str, Any]:
            latest = q.latest_programs(db)
            if latest is None:
                raise ValueError("the programs have not been read yet")
            ts, lines = latest
            if output is not None:
                if output not in lines:
                    raise ValueError(f"no program for {output!r}; outputs: {', '.join(sorted(lines))}")
                lines = {output: lines[output]}
            return {"read_at": q.iso(ts, zone), "programs": lines}

        return read(build)

    @mcp.tool(annotations=READ_ONLY)
    async def program_history(version_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """The dated versions of the Apex's programs, newest first, with what changed. With version_id: that version's
        line-by-line differences from the one before ("+" added, "-" removed, " " unchanged)."""

        def build(db: Any) -> dict[str, Any]:
            if version_id is None:
                return {"versions": q.program_versions(db, max(1, min(limit, 200)))}
            version = q.program_version(db, version_id)
            if version is None:
                raise ValueError(f"no program version {version_id}")
            version.pop("programs")
            return version

        return read(build)

    @mcp.tool(annotations=READ_ONLY)
    async def water_tests(parameter: str | None = None, range: str = "90d", limit: int = 100) -> dict[str, Any]:
        """Manual water tests, newest first: value in the parameter's own unit, as entered, kit and its error, who
        entered it. parameter: alkalinity, calcium, magnesium, phosphate, nitrate, salinity, ph, potassium, ... (or
        none for all). range: 7d, 30d, 90d, 365d."""

        def build(db: Any) -> dict[str, Any]:
            pid = find_parameter(parameter).id if parameter else None  # also resolves alk, ca, po4, ...
            return {"tests": q.water_tests(db, pid, clock() - q.span(range), limit), "parameters": q.catalogue()}

        return read(build)

    @mcp.tool(annotations=READ_ONLY)
    async def analysis(sensor: str | None = None) -> dict[str, Any]:
        """The analysis of the probe history (recomputed hourly from about four weeks of 10-minute values): findings
        (unusual spells against the expected daily pattern, drifts with when an alert limit would be reached, level
        steps, frozen probes), and per probe its usual spread, daily range and the next 24 hours' expected low and high.
        Statistics with their evidence, not alerts: confirm with a test or the tank before acting."""

        def build(db: Any) -> dict[str, Any]:
            data = q.kv(db, "analysis")
            if not data:
                raise ValueError("no analysis yet: the worker runs it once an hour")
            sensors = []
            for s in data["sensors"]:
                if sensor and s["sensor"] != sensor:
                    continue
                item = {k: v for k, v in s.items() if k not in ("recent", "forecast")}
                band = s.get("forecast") or []
                if band:
                    item["next_24h"] = {
                        "low": min(b[2] for b in band),
                        "high": max(b[3] for b in band),
                        "expected_low": min(b[1] for b in band),
                        "expected_high": max(b[1] for b in band),
                    }
                sensors.append(item)
            findings = [f for f in data["findings"] if not sensor or f["sensor"] == sensor]
            return {"computed_at": data["computed_at"], "findings": findings, "sensors": sensors}

        return read(build)

    @mcp.tool(annotations=READ_ONLY)
    async def weekly_report(report_id: int | None = None, preview: bool = False) -> dict[str, Any]:
        """The weekly tank report as text: alerts, the analysis' findings, each probe's week against the week before,
        water tests taken and due, energy per EnergyBar, program changes, the journal, how well the Apex answered.
        Without arguments the newest kept report (and the list of the others); preview=true makes one for the 7 days up
        to now; report_id picks a kept one."""

        def build(db: Any) -> dict[str, Any]:
            if preview:
                found: dict[str, Any] | None = build_report(db, site, clock(), clock())
            else:
                listed = q.reports(db)
                if not listed:
                    raise ValueError("no weekly report kept yet: ask for preview=true")
                found = q.report(db, report_id or listed[0]["id"])
                if found is None:
                    raise ValueError(f"no report {report_id}; kept: {', '.join(str(r['id']) for r in listed)}")
            assert found is not None
            return {"text": report_text(found, zone), "headline": found["headline"], "id": found.get("id"),
                    "kept": [{"id": r["id"], "headline": r["headline"], "ts": r["ts"]} for r in q.reports(db, 12)]}  # fmt: skip

        return read(build)

    @mcp.tool(annotations=READ_ONLY)
    async def reactor(days: int = 30) -> dict[str, Any]:
        """The calcium reactor: per day the CO2 minutes (all day, and in the night window of its program), switchings and
        the effluent pH by day and night; the alkalinity, calcium and magnesium tests with change per day; and whether
        calcium moved about 7.15 ppm per dKH of alkalinity between paired tests (the reactor's balanced output)."""
        return read(lambda db: reactor_view(db, site, max(3, min(days, 365)), clock()))

    @mcp.tool(annotations=READ_ONLY)
    async def journal(range: str = "90d") -> dict[str, Any]:
        """The tank's journal, newest first: water changes, dosing, probe calibrations, maintenance, equipment,
        livestock. range: 7d, 30d, 90d, 365d."""
        return read(lambda db: {"entries": q.journal(db, clock() - q.span(range)), "kinds": list(q.JOURNAL_KINDS)})

    @mcp.tool(annotations=RECORDS)
    async def record_journal_entry(kind: str, text: str, when: str, confirm: bool = False) -> dict[str, Any]:
        """Write a journal entry ("did a 20 % water change this morning"). kind: water change, dosing, calibration,
        maintenance, equipment, livestock, feeding, other. when: ISO 8601 (no offset = house time) or "now"; never guess
        a time the person did not give. First call with confirm=false: the answer shows the parsed time; show it to the
        person and call again with confirm=true once they agree."""
        who = _caller(write=True)
        now = clock()
        moment = parse_time(when, zone, now)
        preview = {"kind": kind.strip().lower(), "text": text.strip(), "when": q.iso(moment, zone),
                   "when_text": datetime.fromtimestamp(moment, ZoneInfo(zone)).strftime("%a %d %b %Y %H:%M %Z")}  # fmt: skip
        if preview["kind"] not in q.JOURNAL_KINDS:
            raise ToolError(f"kind is one of: {', '.join(q.JOURNAL_KINDS)}")
        if not confirm:
            return {
                "stored": False,
                "preview": preview,
                "next": "Show this to the person; call again with confirm=true to store it.",
            }
        db = q.connect(data_file, write=True)
        try:
            entry = q.add_journal(db, moment, kind, text, who, "mcp", now)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        finally:
            db.close()
        return {"stored": True, "entry": localize(entry, zone)}

    @mcp.tool(annotations=RECORDS)
    async def record_water_test(
        parameter: str,
        value: float,
        measured_at: str,
        unit: str | None = None,
        kit: str = "",
        kit_error: float | None = None,
        note: str = "",
        confirm: bool = False,
        unusual_ok: bool = False,
    ) -> dict[str, Any]:
        """Record a manual water test ("alk 8.1 dKH this morning, Hanna"). First call with confirm=false: the answer
        shows the parsed time in the house's zone, the value converted to the parameter's unit and any doubt about it;
        show that to the person, and call again with confirm=true once they agree. measured_at: ISO 8601 (no offset =
        house time) or "now"; never guess a time the person did not give. unit: one the parameter accepts (alkalinity
        dKH, meq/L, ppm; phosphate ppm, ppb, ppb P, ppm P; nitrate ppm, ppm NO3-N; salinity ppt, SG); none = its own.
        kit_error: the kit's stated error, ± in the entered unit. A value outside the plausible range is refused
        unless unusual_ok=true, which needs the person's word that it is right."""
        who = _caller(write=True)
        now = clock()
        moment = parse_time(measured_at, zone, now)
        try:
            checked = check(parameter, value, unit, kit_error)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        p = checked.parameter
        preview = {
            "parameter": p.id,
            "label": p.label,
            "entered": f"{checked.entered_value:g} {checked.entered_unit}",
            "value": round(checked.value, p.decimals + 1),
            "unit": p.unit,
            "kit": kit,
            "kit_error": None if checked.kit_error is None else round(checked.kit_error, p.decimals + 1),
            "measured_at": q.iso(moment, zone),
            "measured_at_text": datetime.fromtimestamp(moment, ZoneInfo(zone)).strftime("%a %d %b %Y %H:%M %Z"),
            "note": note,
        }
        if not confirm:
            return {"stored": False, "preview": preview, "question": checked.question,
                    "next": "Show this to the person; call again with confirm=true to store it."}  # fmt: skip
        if checked.question and not unusual_ok:
            raise ToolError(checked.question + " If the person confirms it, call again with unusual_ok=true.")
        db = q.connect(data_file, write=True)
        try:
            test = q.add_water_test(db, checked, moment, kit, note, who, "mcp", now)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        finally:
            db.close()
        return {"stored": True, "test": localize(test, zone)}

    hosts = {origin.hostname or "", *extra_hosts}
    allowed = sorted({h for host in hosts if host for h in (host, f"{host}:*")})
    asgi = mcp.streamable_http_app(
        streamable_http_path="/reef/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed, allowed_origins=[]
        ),
    )
    return mcp, asgi
