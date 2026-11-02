"""The site file (config/site.yaml): sensors, the modules the console expects, and the alert conditions. The module
checks (lost, no current) are derived from the module list here, so every condition lives in one list."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

KINDS = ("above", "below", "closed", "open", "module_lost", "no_current", "rise", "fall")
SEVERITIES = ("critical", "warning")


@dataclass(frozen=True)
class Sensor:
    name: str
    label: str
    unit: str
    active: bool = True
    test: str | None = None  # the manual water test that measures the same (ph, salinity): shown beside the probe
    external: bool = False  # not an Apex input (the outdoor temperature): charts only, no alerts, no analysis


@dataclass(frozen=True)
class Module:
    id: str  # EB8_9
    kind: str  # EB8, PM1, PM2, inputs
    address: str  # the AquaBus address: "9"
    severity: str
    current: str | None = None  # the bar's current input (EB8 only)
    draws_when_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Condition:
    id: str
    alarm: str  # the group one message is sent for (D3)
    severity: str
    kind: str
    subject: str  # a sensor name, or a module id for module_lost and no_current
    text: str
    pending: float
    limit: float | None = None
    clear: float = 0.0
    minutes: float = 0.0  # rise, fall: the change of `limit` or more within this many minutes
    output: str | None = None  # closed, open: only while this output is energized too (a valve left open)


@dataclass(frozen=True)
class Notice:
    """A one-time message when a switch comes into a state (the fresh-water can full): once it has held `hold` seconds,
    and not again until the switch has been the other way for `rearm` seconds (a float bobbing is one message)."""

    id: str
    sensor: str
    when: str  # closed or open
    text: str
    hold: float = 30.0
    rearm: float = 1800.0


@dataclass(frozen=True)
class Datalog:
    """Filling the history from the unit's own 10-minute log (datalog.json), one day per request, in quiet hours."""

    backfill: bool = False  # also the days before the console started polling (the month the unit holds), on the owner's go
    hours: tuple[int, int] = (1, 5)  # local hours (site timezone) when a day may be asked for: from, to (may wrap midnight)
    spacing: float = 300.0  # seconds between two requests
    days_back: int = 40  # the oldest day asked for; the unit keeps about a month

    def in_hours(self, hour: int) -> bool:
        start, end = self.hours
        return start <= hour < end if start < end else hour >= start or hour < end


@dataclass(frozen=True)
class Reactor:
    """A calcium reactor (site.yaml reactor): its CO2 output, the effluent pH probe, and the night window of its program
    on the Apex's own clock (so it keeps matching the program whatever daylight saving does)."""

    co2: str
    effluent: str
    night_apex: tuple[str, str] | None = None  # ("00:00", "05:00") on the Apex's clock


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TESTS_DUE = {"alkalinity": 7, "calcium": 14, "magnesium": 30, "phosphate": 14, "nitrate": 14}


@dataclass(frozen=True)
class Extras:
    """The energy cost, the weekly report and the outdoor weather (site.yaml energy, report, weather)."""

    volts: float = 120.0  # the EnergyBars report amps; kWh = amps x volts x hours (power factor taken as 1)
    price_per_kwh: float | None = None  # all-in tariff; None: kWh only
    report_weekday: int = 6  # Monday 0 ... Sunday 6
    report_hour: int = 9  # local hour the weekly report is made
    tests_due: dict[str, int] = field(default_factory=lambda: dict(TESTS_DUE))  # parameter -> days between tests
    latitude: float | None = None  # outdoor temperature from Open-Meteo once set (no key; two decimals is plenty)
    longitude: float | None = None


@dataclass(frozen=True)
class Site:
    address: str
    poll_seconds: float
    sensors: dict[str, Sensor]
    modules: tuple[Module, ...]
    conditions: tuple[Condition, ...] = field(default_factory=tuple)
    timezone: str = "UTC"  # the house's civil time, for times the MCP tools show
    datalog: Datalog = field(default_factory=Datalog)
    extras: Extras = field(default_factory=Extras)
    notices: tuple[Notice, ...] = ()
    reactor: Reactor | None = None

    def module(self, module_id: str) -> Module | None:
        return next((m for m in self.modules if m.id == module_id), None)


def _number(value: Any) -> float:
    return float(value)  # pyyaml gives int or float; anything else is a mistake in the file and should stop the start


def _threshold_text(sensor: Sensor, kind: str, limit: float) -> str:
    unit = "" if sensor.unit in ("", "switch", "pH") else f" {sensor.unit}"
    return f"{sensor.label} ({sensor.name}) {'above' if kind == 'above' else 'below'} {limit:g}{unit}"


def parse_site(raw: dict[str, Any]) -> Site:
    apex = raw.get("apex") or {}
    defaults = {"probe_pending": 180, "switch_pending": 30, "module_pending": 60, "current_pending": 180, "rate_pending": 60}
    defaults.update(raw.get("defaults") or {})
    sensors = {
        name: Sensor(
            name,
            str(spec.get("label", name)),
            str(spec.get("unit", "")),
            bool(spec.get("active", True)),
            str(spec["test"]) if spec.get("test") else None,
            bool(spec.get("external", False)),
        )  # fmt: skip
        for name, spec in (raw.get("sensors") or {}).items()
    }
    modules = tuple(
        Module(
            id=str(m["id"]),
            kind=str(m.get("kind", "")),
            address=str(m["address"]),
            severity=str(m.get("severity", "critical")),
            current=str(m["current"]) if m.get("current") else None,
            draws_when_on=tuple(str(o) for o in (m.get("draws_when_on") or [])),
        )
        for m in raw.get("modules") or []
    )
    conditions: list[Condition] = []
    for c in raw.get("conditions") or []:
        name = str(c["sensor"])
        sensor = sensors.get(name, Sensor(name, name, ""))
        severity = str(c.get("severity", "warning"))
        if "rise" in c or "fall" in c:
            kind = "rise" if "rise" in c else "fall"
            limit = abs(_number(c[kind]))
            minutes = _number(c.get("minutes", 30))
            unit = "" if sensor.unit in ("", "pH") else f" {sensor.unit}"
            text = str(c.get("text") or f"{sensor.label} ({name}) {'rising' if kind == 'rise' else 'falling'} "
                                        f"{limit:g}{unit} or more in {minutes:g} minutes")  # fmt: skip
            pending = _number(c.get("pending", defaults["rate_pending"]))
            conditions.append(Condition(str(c["id"]), str(c.get("alarm", "tank")), severity, kind, name, text, pending,
                                        limit, minutes=minutes))  # fmt: skip
        elif "above" in c or "below" in c:
            kind = "above" if "above" in c else "below"
            limit = _number(c[kind])
            text = str(c.get("text") or _threshold_text(sensor, kind, limit))
            pending = _number(c.get("pending", defaults["probe_pending"]))
            conditions.append(Condition(str(c["id"]), str(c.get("alarm", "tank")), severity, kind, name, text, pending,
                                        limit, _number(c.get("clear", 0.0))))  # fmt: skip
        else:
            kind = str(c.get("when", "closed"))
            output = str(c["output"]) if c.get("output") else None
            default = f"{sensor.label} ({name}) is {kind.upper()}" + (f" while {output} is on" if output else "")
            text = str(c.get("text") or default)
            pending = _number(c.get("pending", defaults["switch_pending"]))
            conditions.append(Condition(str(c["id"]), str(c.get("alarm", "tank")), severity, kind, name, text, pending,
                                        output=output))  # fmt: skip
    for m in modules:
        what = "AquaBus connection or power lost" if m.kind == "EB8" else "AquaBus connection lost"
        conditions.append(Condition(f"{m.id.lower()}-lost", "equipment", m.severity, "module_lost", m.id,
                                    f"{m.id} is not reporting ({what})", _number(defaults["module_pending"])))  # fmt: skip
        if m.current and m.draws_when_on:
            text = f"{m.id} shows no current while {', '.join(m.draws_when_on)} is on (the bar lost power, or the device stopped)"
            conditions.append(Condition(f"{m.id.lower()}-no-current", "equipment", m.severity, "no_current", m.id, text,
                                        _number(defaults["current_pending"])))  # fmt: skip
    for c in conditions:
        if c.kind not in KINDS or c.severity not in SEVERITIES:
            raise ValueError(f"condition {c.id}: kind {c.kind!r} or severity {c.severity!r} is not known")
    ids = [c.id for c in conditions]
    if len(ids) != len(set(ids)):
        raise ValueError("condition ids must be unique")
    log = raw.get("datalog") or {}
    hours = tuple(int(h) for h in log.get("hours", (1, 5)))
    if len(hours) != 2 or not all(0 <= h <= 23 for h in hours) or hours[0] == hours[1]:
        raise ValueError("datalog.hours is two different hours, 0-23: [from, to]")
    datalog = Datalog(bool(log.get("backfill", False)), (hours[0], hours[1]), _number(log.get("spacing_seconds", 300)),
                      int(log.get("days_back", 40)))  # fmt: skip
    energy, report, weather = raw.get("energy") or {}, raw.get("report") or {}, raw.get("weather") or {}
    weekday = str(report.get("weekday", "sunday")).lower()
    if weekday not in WEEKDAYS:
        raise ValueError(f"report.weekday is one of {', '.join(WEEKDAYS)}")
    price = energy.get("price_per_kwh")
    lat, lon = weather.get("latitude"), weather.get("longitude")
    extras = Extras(
        volts=_number(energy.get("volts", 120)),
        price_per_kwh=None if price is None else _number(price),
        report_weekday=WEEKDAYS.index(weekday),
        report_hour=int(report.get("hour", 9)),
        tests_due={str(k): int(v) for k, v in (report.get("tests_due") or TESTS_DUE).items()},
        latitude=None if lat is None else round(_number(lat), 2),
        longitude=None if lon is None else round(_number(lon), 2),
    )
    notices = tuple(
        Notice(
            str(n["id"]),
            str(n["sensor"]),
            str(n.get("when", "closed")),
            str(n["text"]),
            _number(n.get("hold", 30)),
            _number(n.get("rearm", 1800)),
        )  # fmt: skip
        for n in raw.get("notices") or []
    )
    if any(n.when not in ("closed", "open") for n in notices):
        raise ValueError("a notice's when is closed or open")
    r = raw.get("reactor")
    reactor = None
    if r:
        night = r.get("night_apex")
        if night is not None and (len(night) != 2 or not all(re.fullmatch(r"\d{2}:\d{2}", str(t)) for t in night)):
            raise ValueError('reactor.night_apex is two times on the Apex\'s clock: ["00:00", "05:00"]')
        reactor = Reactor(str(r["co2"]), str(r["effluent"]), (str(night[0]), str(night[1])) if night else None)
    zone = str(raw.get("timezone", "UTC"))
    ZoneInfo(zone)  # an unknown zone stops the start
    return Site(
        address=str(apex.get("address", "")),
        poll_seconds=_number(apex.get("poll_seconds", 60)),
        sensors=sensors,
        modules=modules,
        conditions=tuple(conditions),
        timezone=zone,
        datalog=datalog,
        extras=extras,
        notices=notices,
        reactor=reactor,
    )


def load_site(path: Path) -> Site:
    return parse_site(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
