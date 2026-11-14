"""Single source of truth for Signal K path units in Boat Chat.

Signal K delivers SI values.  Boat Chat stores them in a US-customary form
(see ``display_units`` for the display-time conversion to metric).  The
conversion and the stored-unit label used to live in two functions with
different rules, so they could disagree.  Here one ordered table yields both
from a single lookup (``resolve``), so a value and its label can never
disagree.

A US-customary label (F, ft, psi, inHg, US gal, gal/h, kn, NM) is only ever
produced when the path is recognised by the table and any Signal K metadata
unit agrees with the table's SI unit.  Anything else is passed through
unconverted, labelled with the metadata unit as given (or not labelled).
"""
from __future__ import annotations

import math
from typing import Callable, NamedTuple

M3_TO_US_GAL = 264.1720524
FLOW_M3S_TO_GAL_H = M3_TO_US_GAL * 3600.0
MS_TO_KN = 1.943844492
M_TO_FT = 3.280839895
PA_PER_INHG = 3386.389
PA_PER_PSI = 6894.757293
M_PER_NM = 1852.0


class Resolution(NamedTuple):
    si_unit: str                      # "" when unknown
    quantity: str
    unit: str                         # stored label, "" when unknown
    convert: Callable[[float], float]
    origin: str                       # "table" | "meta" | "unknown" | "conflict"


class Pattern(NamedTuple):
    """kind: "path" (dot segments, ``*`` = one segment), "leaf" (last segment
    equals ``text``), "suffix" (last segment ends with ``text``), "tail" (the
    last segments equal ``text``).  ``under`` restricts leaf/suffix/tail
    matches to paths starting with that dotted prefix."""
    kind: str
    text: str
    under: str = ""


class Rule(NamedTuple):
    quantity: str
    si_unit: str
    unit: str
    fn: Callable[[float], float]
    patterns: tuple[Pattern, ...]


def _identity(value: float) -> float:
    return value


def _scale(factor: float) -> Callable[[float], float]:
    return lambda value: value * factor


def _divide(divisor: float) -> Callable[[float], float]:
    return lambda value: value / divisor


def _k_to_f(value: float) -> float:
    return (value - 273.15) * 9.0 / 5.0 + 32.0


def _heading(value: float) -> float:
    if not math.isfinite(value):
        return value
    degrees = math.degrees(value) % 360.0
    return 0.0 if degrees >= 360.0 else degrees


def _signed_angle(value: float) -> float:
    return math.degrees(value)


def _rate_of_turn(value: float) -> float:
    return math.degrees(value) * 60.0


def _ratio_to_percent(value: float) -> float:
    return value * 100.0


def _state_of_charge(value: float) -> float:
    # Kept from the previous behaviour: a value above 1.2 is already a percentage.
    return value if abs(value) > 1.2 else value * 100.0


def _p(text: str) -> Pattern:
    return Pattern("path", text)


def _leaf(text: str, under: str = "") -> Pattern:
    return Pattern("leaf", text, under)


# Ordered, first match wins: specific before general.  Temperature leaves come
# first so tanks.fuel.0.temperature, electrical.batteries.house.temperature and
# propulsion.port.transmission.oilTemperature are temperatures and not "%", a
# battery reading or a pressure.  stateOfCharge precedes nothing that could
# capture it; environment.current.* is never an electrical current because
# the electrical rules require the "electrical." prefix.
PATTERNS: tuple[Rule, ...] = (
    Rule("temperature", "K", "F", _k_to_f,
         (_leaf("temperature"), Pattern("suffix", "Temperature"))),
    Rule("heading", "rad", "deg", _heading, (
        _p("navigation.headingTrue"), _p("navigation.headingMagnetic"),
        _p("navigation.courseOverGroundTrue"), _p("navigation.courseOverGroundMagnetic"),
        _p("environment.wind.directionTrue"), _p("environment.wind.directionMagnetic"),
        _p("environment.current.setTrue"), _p("environment.current.setMagnetic"),
        _leaf("bearingTrue"), _leaf("bearingMagnetic"))),
    Rule("angle", "rad", "deg", _signed_angle, (
        _p("navigation.magneticVariation"), _p("environment.wind.angleApparent"),
        _p("environment.wind.angleTrueWater"), _p("environment.wind.angleTrueGround"),
        _p("steering.rudderAngle"), _p("navigation.attitude.roll"),
        _p("navigation.attitude.pitch"), _p("navigation.attitude.yaw"),
        _p("performance.leeway"))),
    Rule("rate_of_turn", "rad/s", "deg/min", _rate_of_turn, (_p("navigation.rateOfTurn"),)),
    Rule("speed", "m/s", "kn", _scale(MS_TO_KN), (
        _p("navigation.speedOverGround"), _p("navigation.speedThroughWater"),
        _p("navigation.speedThroughWaterTransverse"), _p("navigation.speedThroughWaterLongitudinal"),
        _p("environment.wind.speedApparent"), _p("environment.wind.speedTrue"),
        _p("environment.wind.speedOverGround"), _p("environment.current.drift"),
        _p("performance.velocityMadeGood"))),
    # stateOfCharge has its own rule (value-dependent) and must precede the generic ratio rule.
    Rule("ratio", "ratio", "%", _state_of_charge, (_leaf("stateOfCharge"),)),
    Rule("ratio", "ratio", "%", _ratio_to_percent, (
        _leaf("currentLevel", "tanks."), _leaf("stateOfHealth"),
        _leaf("relativeHumidity"), _leaf("humidity"),
        _p("propulsion.*.engineLoad"), _p("propulsion.*.engineTorque"),
        _p("propulsion.*.drive.trimState"))),
    Rule("volume", "m3", "US gal", _scale(M3_TO_US_GAL), (
        _p("tanks.*.*.capacity"), _p("tanks.*.*.currentVolume"), _p("propulsion.*.fuel.used"))),
    Rule("flow", "m3/s", "gal/h", _scale(FLOW_M3S_TO_GAL_H), (
        _p("propulsion.*.fuel.rate"), _p("propulsion.*.fuel.averageRate"))),
    Rule("barometric", "Pa", "inHg", _divide(PA_PER_INHG), (_p("environment.*.pressure"),)),
    Rule("pressure", "Pa", "psi", _divide(PA_PER_PSI), (
        _p("propulsion.*.oilPressure"), _p("propulsion.*.boostPressure"),
        _p("propulsion.*.coolantPressure"), _p("propulsion.*.fuel.pressure"),
        _p("propulsion.*.transmission.oilPressure"))),
    Rule("depth", "m", "ft", _scale(M_TO_FT), (_p("environment.depth.*"),)),
    Rule("distance", "m", "NM", _divide(M_PER_NM), (
        _p("navigation.log"), _p("navigation.trip.log"), Pattern("tail", "nextPoint.distance"))),
    Rule("voltage", "V", "V", _identity, (
        _leaf("voltage", "electrical."), Pattern("suffix", "Voltage", "electrical."))),
    Rule("current", "A", "A", _identity, (_leaf("current", "electrical."),)),
    Rule("power", "W", "W", _identity, (
        _leaf("power", "electrical."), Pattern("suffix", "Power", "electrical."))),
    Rule("frequency", "Hz", "Hz", _identity, (_leaf("frequency", "electrical."),)),
    Rule("charge", "C", "Ah", _divide(3600.0), (
        _leaf("consumedCharge"), _p("electrical.batteries.*.capacity.consumed"))),
    Rule("rpm", "Hz", "RPM", _scale(60.0), (_leaf("revolutions", "propulsion."),)),
    Rule("duration", "s", "h", _divide(3600.0), (
        _p("propulsion.*.runTime"), _leaf("timeRemaining", "electrical."))),
)

STORED_UNITS = frozenset(rule.unit for rule in PATTERNS)


def _matches(pattern: Pattern, path: str, segments: list[str]) -> bool:
    if pattern.kind == "path":
        want = pattern.text.split(".")
        return len(want) == len(segments) and all(w == "*" or w == s for w, s in zip(want, segments))
    if pattern.under and not path.startswith(pattern.under):
        return False
    if pattern.kind == "leaf":
        return segments[-1] == pattern.text
    if pattern.kind == "suffix":
        return segments[-1].endswith(pattern.text)
    if pattern.kind == "tail":
        want = pattern.text.split(".")
        return len(segments) >= len(want) and segments[-len(want):] == want
    return False


def _lookup(path: str) -> Rule | None:
    segments = path.split(".") if isinstance(path, str) else []
    if not segments or not all(segments):
        return None
    for rule in PATTERNS:
        if any(_matches(pattern, path, segments) for pattern in rule.patterns):
            return rule
    return None


def resolve(path: str, meta_units: str | None = None) -> Resolution:
    meta = (meta_units or "").strip() if isinstance(meta_units, str) else ""
    rule = _lookup(path)
    if rule is not None:
        if not meta or meta == rule.si_unit:
            return Resolution(rule.si_unit, rule.quantity, rule.unit, rule.fn, "table")
        return Resolution(rule.si_unit, rule.quantity, meta, _identity, "conflict")
    if meta:
        return Resolution(meta, "", meta, _identity, "meta")
    return Resolution("", "", "", _identity, "unknown")


def canonical(path: str, value: float, meta_units: str | None = None) -> tuple[float, str]:
    """Return (converted value, stored label) from one ``resolve`` call."""
    resolution = resolve(path, meta_units)
    try:
        finite = math.isfinite(value)
    except TypeError:
        finite = False
    if not finite:
        return value, resolution.unit
    return resolution.convert(value), resolution.unit
