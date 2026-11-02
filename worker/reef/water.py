"""Manual water tests: the parameters, their units and conversions, and the checks before a test is stored.

Every test keeps the value as entered (value and unit) and the value in the parameter's own unit, which charts and
trends use. A value implausible for its parameter (81 dKH) is questioned, not stored, unless the caller confirms it.

Conversions: alkalinity 1 meq/L = 2.8 dKH = 50.04 ppm as CaCO3; phosphate as PO4 (ppm = mg/L), a phosphorus
reading (P, as the Hanna ULR checker shows it) times 3.066; nitrate as NO3, nitrate-nitrogen (NO3-N) times 4.427;
salinity in ppt (PSU), a specific gravity converted at 25 °C against pure water at 25 °C (UNESCO EOS-80)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

DKH_PER_MEQ = 2.804  # 1 dKH = 10 mg/L CaO = 0.3566 meq/L
PPM_CACO3_PER_MEQ = 50.04
PO4_PER_P = 94.971 / 30.974
NO3_PER_N = 62.004 / 14.007


def _water_density(t: float) -> float:
    return (999.842594 + 6.793952e-2 * t - 9.095290e-3 * t**2 + 1.001685e-4 * t**3 - 1.120083e-6 * t**4
            + 6.536332e-9 * t**5)  # fmt: skip


def _seawater_density(s: float, t: float) -> float:
    """UNESCO EOS-80 at one atmosphere: kg/m3 for salinity s (PSU) at t °C."""
    a = 8.24493e-1 - 4.0899e-3 * t + 7.6438e-5 * t**2 - 8.2467e-7 * t**3 + 5.3875e-9 * t**4
    b = -5.72466e-3 + 1.0227e-4 * t - 1.6546e-6 * t**2
    return _water_density(t) + a * s + b * s**1.5 + 4.8314e-4 * s**2


def sg_from_ppt(ppt: float, t: float = 25.0) -> float:
    return _seawater_density(max(ppt, 0.0), t) / _water_density(t)


def ppt_from_sg(sg: float, t: float = 25.0) -> float:
    """The salinity whose specific gravity (t °C against pure water at t °C) is sg: bisection over 0-60 ppt."""
    low, high = 0.0, 60.0
    if not sg_from_ppt(low, t) <= sg <= sg_from_ppt(high, t):
        raise ValueError(f"a specific gravity of {sg:g} is outside what seawater can have")
    for _ in range(60):
        mid = (low + high) / 2
        low, high = (mid, high) if sg_from_ppt(mid, t) < sg else (low, mid)
    return (low + high) / 2


@dataclass(frozen=True)
class Unit:
    name: str
    to_base: Callable[[float], float]
    from_base: Callable[[float], float]


def _scale(name: str, factor: float) -> Unit:
    return Unit(name, lambda v: v * factor, lambda v: v / factor)


@dataclass(frozen=True)
class Parameter:
    id: str
    label: str
    unit: str  # the base unit: charts and trends use it
    plausible: tuple[float, float]  # in the base unit; outside it a value is questioned
    decimals: int
    units: dict[str, Unit] = field(default_factory=dict)  # other units accepted, by name

    def all_units(self) -> list[str]:
        return [self.unit, *self.units]

    def to_base(self, value: float, unit: str) -> float:
        if unit == self.unit:
            return value
        if unit not in self.units:
            raise ValueError(f"{self.label} is entered in {', '.join(self.all_units())}, not {unit!r}")
        return self.units[unit].to_base(value)

    def error_to_base(self, value: float, error: float, unit: str) -> float:
        """A kit's stated error (± in the entered unit) in the base unit, measured at the value (SG is not
        proportional to ppt)."""
        return abs(self.to_base(value + error, unit) - self.to_base(value, unit))


def _parameters() -> dict[str, Parameter]:
    sg = Unit("SG", lambda v: ppt_from_sg(v), lambda v: sg_from_ppt(v))
    items = [
        Parameter("alkalinity", "Alkalinity", "dKH", (2.0, 20.0), 2,
                  {"meq/L": _scale("meq/L", DKH_PER_MEQ), "ppm": _scale("ppm", DKH_PER_MEQ / PPM_CACO3_PER_MEQ)}),
        Parameter("calcium", "Calcium", "ppm", (200.0, 700.0), 0),
        Parameter("magnesium", "Magnesium", "ppm", (700.0, 1800.0), 0),
        Parameter("phosphate", "Phosphate (PO4)", "ppm", (0.0, 5.0), 3,
                  {"ppb": _scale("ppb", 0.001), "ppb P": _scale("ppb P", PO4_PER_P / 1000), "ppm P": _scale("ppm P", PO4_PER_P)}),
        Parameter("nitrate", "Nitrate (NO3)", "ppm", (0.0, 200.0), 1, {"ppm NO3-N": _scale("ppm NO3-N", NO3_PER_N)}),
        Parameter("salinity", "Salinity", "ppt", (15.0, 45.0), 1, {"SG": sg}),
        Parameter("ph", "pH (manual)", "pH", (6.5, 9.5), 2),
        Parameter("potassium", "Potassium", "ppm", (200.0, 600.0), 0),
        Parameter("strontium", "Strontium", "ppm", (0.0, 30.0), 1),
        Parameter("iodine", "Iodine", "ppm", (0.0, 1.0), 3, {"ppb": _scale("ppb", 0.001)}),
        Parameter("nitrite", "Nitrite (NO2)", "ppm", (0.0, 10.0), 3),
        Parameter("ammonia", "Ammonia", "ppm", (0.0, 10.0), 3),
    ]  # fmt: skip
    return {p.id: p for p in items}


PARAMETERS = _parameters()
ALIASES = {"alk": "alkalinity", "kh": "alkalinity", "dkh": "alkalinity", "ca": "calcium", "mg": "magnesium",
           "po4": "phosphate", "no3": "nitrate", "sal": "salinity", "salt": "salinity", "k": "potassium",
           "sr": "strontium", "i": "iodine", "no2": "nitrite", "nh3": "ammonia", "nh4": "ammonia"}  # fmt: skip


def parameter(name: str) -> Parameter:
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key not in PARAMETERS:
        raise ValueError(f"unknown parameter {name!r}; known: {', '.join(PARAMETERS)}")
    return PARAMETERS[key]


def unit_name(p: Parameter, unit: str | None) -> str:
    """The unit as the catalogue spells it, matched without case ("dkh" -> "dKH"); none given: the base unit."""
    if not unit:
        return p.unit
    for name in p.all_units():
        if name.lower() == unit.strip().lower():
            return name
    raise ValueError(f"{p.label} is entered in {', '.join(p.all_units())}, not {unit!r}")


@dataclass(frozen=True)
class Checked:
    """A test ready to store: as entered and in the parameter's base unit, with any doubt about it."""

    parameter: Parameter
    entered_value: float
    entered_unit: str
    value: float  # base unit
    kit_error: float | None  # base unit
    question: str | None  # not None: implausible, store only after the person confirms


def check(name: str, value: float, unit: str | None = None, kit_error: float | None = None) -> Checked:
    p = parameter(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("the value must be a number")
    entered_unit = unit_name(p, unit)
    base = p.to_base(float(value), entered_unit)
    error = None
    if kit_error is not None:
        if not math.isfinite(kit_error) or kit_error < 0:
            raise ValueError("the kit's error is a positive number in the entered unit (± that much)")
        error = p.error_to_base(float(value), float(kit_error), entered_unit)
    low, high = p.plausible
    question = None
    if not low <= base <= high:
        shown = f"{value:g} {entered_unit}" + (f" ({base:.{p.decimals}f} {p.unit})" if entered_unit != p.unit else "")
        question = (f"{shown} is outside what is plausible for {p.label.lower()} ({low:g} to {high:g} {p.unit}). "
                    "Check the value and its unit; store it only if it is right.")  # fmt: skip
    return Checked(p, float(value), entered_unit, round(base, p.decimals + 2), error, question)
