"""Unit conversions for display instruments (SignalK SI values in, display units out)."""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Callable

_GAL = 0.003785411784  # US gallon in m3
MAX_ABS_VALUE = 1e15  # larger converted readings are treated as invalid


def _signed_deg(rad: float) -> float:
    deg = math.fmod(math.degrees(rad), 360.0)
    if deg > 180.0:
        deg -= 360.0
    elif deg <= -180.0:
        deg += 360.0
    return deg


def _deg_360(rad: float) -> float:
    deg = math.degrees(rad) % 360.0
    return 0.0 if deg >= 360.0 else deg


CONVERSIONS: dict[str, Callable[[float], float]] = {
    "none": lambda v: v,
    "m/s->kn": lambda v: v * 3600.0 / 1852.0,
    "m/s->km/h": lambda v: v * 3.6,
    "m/s->mph": lambda v: v * 3600.0 / 1609.344,
    "K->C": lambda v: v - 273.15,
    "K->F": lambda v: (v - 273.15) * 9.0 / 5.0 + 32.0,
    "rad->deg": math.degrees,
    "rad->deg_signed": _signed_deg,
    "rad->deg_360": _deg_360,
    "m->ft": lambda v: v / 0.3048,
    "m->fathom": lambda v: v / 1.8288,
    "m->nmi": lambda v: v / 1852.0,
    "ratio->%": lambda v: v * 100.0,
    "Pa->hPa": lambda v: v / 100.0,
    "Pa->bar": lambda v: v / 100000.0,
    "Pa->psi": lambda v: v / 6894.757293168,
    "m3->L": lambda v: v * 1000.0,
    "m3->gal": lambda v: v / _GAL,
    "m3/s->L/h": lambda v: v * 1000.0 * 3600.0,
    "m3/s->gal/h": lambda v: v / _GAL * 3600.0,
    "Hz->rpm": lambda v: v * 60.0,
    "C->Ah": lambda v: v / 3600.0,
    "s->min": lambda v: v / 60.0,
    "s->h": lambda v: v / 3600.0,
}


def convert(name: str, value: float) -> float:
    return CONVERSIONS[name](float(value))


def round_value(value: float, precision: int) -> float | int | None:
    """Round half away from zero to `precision` places; never returns -0.0.

    Returns None for non-finite values, values beyond MAX_ABS_VALUE, or any
    value that cannot be converted or rounded.
    """
    try:
        number = float(value)
        if not math.isfinite(number) or abs(number) > MAX_ABS_VALUE:
            return None
        q = Decimal(1).scaleb(-precision)
        out = Decimal(repr(number)).quantize(q, rounding=ROUND_HALF_UP)
        if precision == 0:
            return int(out)
        result = float(out)
    except (InvalidOperation, OverflowError, ValueError):
        return None
    return 0.0 if result == 0 else result


def convert_and_round(name: str, value: float, precision: int) -> float | int | None:
    try:
        converted = convert(name, value)
    except (OverflowError, ValueError):
        return None
    return round_value(converted, precision)
