"""The one client of the Apex Classic, and the reading of its status.json.

The Apex is a small embedded web server: research and the unit itself (a two-day log took it 15-21 s to build) say
to be gentle. Every request goes through one ApexClient, one at a time, and a breaker backs off when the unit is slow
or silent, because Fusion and the owner's browser use the same unit.

status.json (firmware 4.53, read 2026-10-11): `date` and the power times are UTC epoch seconds; each input has did,
type, name, value; each output has did, name, type and a status list [mode, intensity, health, ...]. Switches report 0
when OPEN and 60 when CLOSED.
"""

from __future__ import annotations

import asyncio
import csv
import io
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import httpx

STATUS_PATH = "/cgi-bin/status.json"
PROGRAM_PATH = "/cgi-bin/program.xml"
DATALOG_PATH = "/cgi-bin/datalog.json"
DATALOG_SLOW_SECONDS = 30.0  # one day (144 records, 92 KB) took the unit 8.3 s on 2026-10-12
OUTLOG_PATH = "/cgi-bin/outlog.json"
OUTLOG_SLOW_SECONDS = 10.0  # one day took 0.4 s on 2026-10-12
SLOW_SECONDS = 5.0  # an answer slower than this opens the breaker, like a failure
PROGRAM_SLOW_SECONDS = 30.0  # program.xml took the unit 5 s on 2026-10-11
MAX_PROGRAM_BYTES = 1_000_000
BACKOFF_SECONDS = (120.0, 300.0, 600.0)  # waits after one, two, three or more bad answers in a row
MANUAL_MODES = ("ON", "OFF")
ENERGIZED_MODES = ("ON", "AON")


class ApexError(Exception):
    pass


@dataclass(frozen=True)
class Credential:
    address: str
    user: str
    password: str

    def __repr__(self) -> str:  # never show the password, not even in a traceback
        return f"Credential(address={self.address!r}, user={self.user!r})"


def read_credential(text: str) -> Credential:
    """The worker's secret file: a CSV file (a header line "ip, username, password", then one row)."""
    rows = list(csv.reader(io.StringIO(text.replace("\r", ""))))
    rows = [[cell.strip() for cell in row] for row in rows if any(cell.strip() for cell in row)]
    if rows and rows[0] and rows[0][0].lower() == "ip":
        rows = rows[1:]
    if not rows or len(rows[0]) < 3 or not all(rows[0][:3]):
        raise ApexError("the Apex secret file needs a line: ip, username, password")
    address, user, password = rows[0][:3]
    return Credential(address, user, password)


def module_of(did: str) -> str:
    """The AquaBus address a device id belongs to: "3_1" and "3_0" -> "3", "5_I1" -> "5"; "base_pH" -> "base"."""
    head = did.split("_", 1)[0]
    return head if head.isdigit() else ("base" if head == "base" else head)


@dataclass(frozen=True)
class Input:
    did: str
    name: str
    type: str
    value: float

    @property
    def closed(self) -> bool:
        return self.value != 0  # switches: 0 OPEN, 60 CLOSED


@dataclass(frozen=True)
class Output:
    did: str
    name: str
    type: str  # outlet, variable, alert, virtual
    mode: str  # AON, AOF, ON, OFF; a profile name for a variable output in AUTO
    health: str  # the third status field: "OK" while the module reports it
    intensity: int | None = None

    @property
    def manual(self) -> bool:
        return self.mode in MANUAL_MODES

    @property
    def energized(self) -> bool:
        if self.type == "variable":
            return self.mode == "ON" or (self.mode != "OFF" and (self.intensity or 0) > 0)
        return self.mode in ENERGIZED_MODES


@dataclass(frozen=True)
class Status:
    received_at: float  # the worker's clock: the time axis of the history
    latency: float
    controller_time: float | None  # the Apex's own clock (UTC epoch), kept as a diagnostic
    timezone: str
    serial: str
    software: str
    feed_active: bool
    power_failed: float | None
    power_restored: float | None
    inputs: dict[str, Input] = field(default_factory=dict)  # by name
    outputs: dict[str, Output] = field(default_factory=dict)  # by name

    def clock_offset(self) -> float | None:
        return None if self.controller_time is None else self.controller_time - self.received_at


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_status(doc: Any, received_at: float, latency: float = 0.0) -> Status:
    if not isinstance(doc, dict) or not isinstance(doc.get("istat"), dict):
        raise ApexError("status.json has no istat object")
    stat = doc["istat"]
    inputs: dict[str, Input] = {}
    for item in stat.get("inputs", []) or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        value = _number(item.get("value"))
        if value is None:
            continue
        inputs[str(item["name"])] = Input(str(item.get("did", "")), str(item["name"]), str(item.get("type", "")), value)
    outputs: dict[str, Output] = {}
    for item in stat.get("outputs", []) or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        status = item.get("status") if isinstance(item.get("status"), list) else []
        status = [str(s) for s in status] + [""] * 3
        intensity = item.get("intensity")
        outputs[str(item["name"])] = Output(
            did=str(item.get("did", "")),
            name=str(item["name"]),
            type=str(item.get("type", "")),
            mode=status[0],
            health=status[2],
            intensity=int(intensity) if isinstance(intensity, (int, float)) else None,
        )
    power = stat.get("power") if isinstance(stat.get("power"), dict) else {}
    feed = stat.get("feed") if isinstance(stat.get("feed"), dict) else {}
    return Status(
        received_at=received_at,
        latency=latency,
        controller_time=_number(stat.get("date")),
        timezone=str(stat.get("timezone", "")),
        serial=str(stat.get("serial", "")),
        software=str(stat.get("software", "")),
        feed_active=bool(feed.get("active")),
        power_failed=_number(power.get("failed")) or None,
        power_restored=_number(power.get("restored")) or None,
        inputs=inputs,
        outputs=outputs,
    )


def parse_programs(raw: bytes) -> dict[str, list[str]]:
    """program.xml: <proglist><outlet><name/><outputID/><outputType/><program/></outlet>...: name -> its lines."""
    if len(raw) > MAX_PROGRAM_BYTES:
        raise ValueError("program.xml is larger than expected")
    root = ET.fromstring(raw)
    programs: dict[str, list[str]] = {}
    for outlet in root.iter("outlet"):
        name = (outlet.findtext("name") or "").strip()
        if name:
            programs[name] = [line.strip() for line in (outlet.findtext("program") or "").splitlines() if line.strip()]
    return programs


@dataclass(frozen=True)
class LogRecord:
    """One sample of the unit's own probe log (datalog.json, every 10 minutes): UTC time, name -> (did, type, value)."""

    ts: float
    values: dict[str, tuple[str, str, float]]


def parse_datalog(doc: Any) -> tuple[str, list[LogRecord]]:
    """datalog.json (firmware 4.53, read 2026-10-12): {"ilog": {serial, timezone, record: [{date: UTC epoch, data:
    [{name, did, type, value: "80.8"}, ...]}]}}. Returns the serial and the records."""
    if not isinstance(doc, dict) or not isinstance(doc.get("ilog"), dict):
        raise ApexError("datalog.json has no ilog object")
    log = doc["ilog"]
    records = []
    for item in log.get("record", []) or []:
        ts = _number(item.get("date")) if isinstance(item, dict) else None
        if ts is None:
            continue
        values = {}
        for probe in item.get("data", []) or []:
            value = _number(probe.get("value")) if isinstance(probe, dict) else None
            if value is not None and probe.get("name"):
                values[str(probe["name"])] = (str(probe.get("did", "")), str(probe.get("type", "")), value)
        records.append(LogRecord(ts, values))
    return str(log.get("serial", "")), records


@dataclass(frozen=True)
class OutletChange:
    """One line of the unit's outlet log: an output was switched on or off (energized, not its mode)."""

    ts: float
    did: str
    name: str
    energized: bool


def parse_outlog(doc: Any) -> tuple[str, list[OutletChange]]:
    """outlog.json (firmware 4.53, read 2026-10-12): {"olog": {serial, record: [{date: UTC epoch, did, name, value:
    "ON" | "OFF"}]}}. Returns the serial and the changes."""
    if not isinstance(doc, dict) or not isinstance(doc.get("olog"), dict):
        raise ApexError("outlog.json has no olog object")
    log = doc["olog"]
    changes = []
    for item in log.get("record", []) or []:
        if not isinstance(item, dict) or not item.get("name") or item.get("value") not in ("ON", "OFF"):
            continue
        ts = _number(item.get("date"))
        if ts is not None:
            changes.append(OutletChange(ts, str(item.get("did", "")), str(item["name"]), item["value"] == "ON"))
    return str(log.get("serial", "")), changes


class ApexClient:
    """One request at a time; after a failure or a slow answer, nothing until the breaker's wait is over."""

    def __init__(self, credential: Credential, timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.credential = credential
        self.lock = asyncio.Lock()
        self.failures = 0
        self.closed_until = 0.0  # no request before this time
        self.last_error: str | None = None
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=f"http://{credential.address}",
            auth=httpx.BasicAuth(credential.user, credential.password),
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            headers={"User-Agent": "reef-console"},
            transport=transport,
        )

    def waiting(self, now: float | None = None) -> float:
        """Seconds the breaker still holds requests back (0 = a request may go)."""
        return max(0.0, self.closed_until - (time.time() if now is None else now))

    def _trip(self, reason: str, now: float) -> None:
        self.failures += 1
        self.last_error = reason
        self.closed_until = now + BACKOFF_SECONDS[min(self.failures, len(BACKOFF_SECONDS)) - 1]

    async def _get(self, path: str, slow: float) -> tuple[httpx.Response, float]:
        """One GET under the lock and the breaker. A failure, or an answer slower than `slow`, trips the breaker."""
        now = time.time()
        if self.waiting(now) > 0:
            raise ApexError(f"backing off for {self.waiting(now):.0f} s after: {self.last_error}")
        start = time.monotonic()
        try:
            # the slow limit is a judgement on a good answer; the timeout leaves room beyond it (datalog: 30 s)
            response = await self._client.get(path, timeout=max(self._timeout, slow + 10))
        except httpx.HTTPError as exc:
            self._trip(f"{exc.__class__.__name__}: {exc}"[:200], time.time())
            raise ApexError(self.last_error or "no answer") from exc
        latency = time.monotonic() - start
        if response.status_code != 200:
            self._trip(f"HTTP {response.status_code}", time.time())
            raise ApexError(self.last_error or "bad answer")
        if latency > slow:
            # the answer is good and is used, but the unit is struggling: give it room before the next request
            self._trip(f"slow answer ({latency:.1f} s)", time.time())
        else:
            self.failures, self.last_error = 0, None
        return response, latency

    async def status(self) -> Status:
        async with self.lock:
            response, latency = await self._get(STATUS_PATH, SLOW_SECONDS)
            try:
                return parse_status(response.json(), time.time(), latency)
            except (ValueError, ApexError) as exc:
                self._trip(f"unreadable status.json: {exc}"[:200], time.time())
                raise ApexError(self.last_error or "unreadable") from exc

    async def programs(self) -> dict[str, list[str]]:
        """Every output's program (program.xml, about 5 s of work for the unit): name -> its lines."""
        async with self.lock:
            response, _latency = await self._get(PROGRAM_PATH, PROGRAM_SLOW_SECONDS)
            try:
                return parse_programs(response.content)
            except (ET.ParseError, ValueError) as exc:
                raise ApexError(f"unreadable program.xml: {exc}"[:200]) from exc

    async def datalog(self, day: str) -> tuple[str, list[LogRecord]]:
        """One day of the unit's probe log; day is the unit's own date as YYMMDD (its days start at midnight in its
        timezone). days=0 asks for that day alone (days=1 returns two)."""
        async with self.lock:
            response, _latency = await self._get(f"{DATALOG_PATH}?sdate={day}&days=0", DATALOG_SLOW_SECONDS)
            try:
                return parse_datalog(response.json())
            except (ValueError, ApexError) as exc:
                raise ApexError(f"unreadable datalog.json: {exc}"[:200]) from exc

    async def outlog(self, day: str) -> tuple[str, list[OutletChange]]:
        """One of the unit's days of outlet changes (YYMMDD, its own calendar); cheap for the unit."""
        async with self.lock:
            response, _latency = await self._get(f"{OUTLOG_PATH}?sdate={day}&days=0", OUTLOG_SLOW_SECONDS)
            try:
                return parse_outlog(response.json())
            except (ValueError, ApexError) as exc:
                raise ApexError(f"unreadable outlog.json: {exc}"[:200]) from exc

    async def close(self) -> None:
        await self._client.aclose()
