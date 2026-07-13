"""Pure decision logic for the OPTIONAL heat-pump link (B2, 2026-07-12).

Tortoise-UFH still never controls the compressor or the pump's own weather
curve (PRD §8.10 stands). This module holds the pure, unit-testable half of
the opt-in extension recorded in ``prd-control-brain.md`` §8.13: mapping the
global Tortoise :class:`~tortoise_ufh.models.Mode` onto a HeishaMon-style
(Panasonic Aquarea) pump-mode select while ALWAYS preserving the pump's
``+DHW`` flag, toggling that flag for the manual DHW switch, and computing the
two bounded water setpoints (cooling = ``max(base, safe dew point)``; heating
= a simple weather curve reusing :class:`~tortoise_ufh.weather_comp.
WeatherCompCurve`).

The HeishaMon mode vocabulary (public HeishaMon firmware standard) is::

    "Heat only" | "Cool only" | "Auto" | "DHW only"
    | "Heat+DHW" | "Cool+DHW" | "Auto+DHW"

Matching is case-insensitive and whitespace-trimmed; RETURN values are always
the canonical strings above — the adapter re-canonicalises against the live
select entity's own option list before writing, and skips the write when no
option matches.

Hard rules (owner decision 2026-07-12, race with the external DHW automation):

* The DHW flag ALWAYS survives a direction write: a current ``*+DHW`` option
  maps to the new direction's ``*+DHW`` variant.
* ``"DHW only"`` is never written as a direction, and a pump currently in
  ``"DHW only"`` is never written at all (the external DHW automation is
  mid-cycle and will restore its remembered direction itself).
* TRANSITIONAL / OFF never force a direction — the pump's own automation and
  the DHW automation stay in charge.

This module is pure Python (stdlib + sibling core modules only) and MUST NOT
import ``homeassistant``.

Units: temperatures in degrees Celsius (``_c``); curve slope in K/K.
"""

from __future__ import annotations

import math

from .config import ControllerConfig
from .models import Mode
from .weather_comp import WeatherCompCurve

__all__ = [
    "HEATING_SUPPLY_MAX_C",
    "HEATING_SUPPLY_MIN_C",
    "HEISHAMON_MODE_OPTIONS",
    "cooling_setpoint_c",
    "dhw_option",
    "direction_option",
    "heating_curve",
    "round_to_step_c",
]

HEISHAMON_MODE_OPTIONS: tuple[str, ...] = (
    "Heat only",
    "Cool only",
    "Auto",
    "DHW only",
    "Heat+DHW",
    "Cool+DHW",
    "Auto+DHW",
)
"""Canonical HeishaMon pump-mode option strings (public firmware standard)."""

HEATING_SUPPLY_MIN_C: float = 20.0
"""Lower clamp of the optional heating-water curve [degC].

A fixed firmware/floor limit, deliberately NOT a tuning knob: below ~20 degC a
"heating" setpoint is meaningless for a UFH slab.
"""

HEATING_SUPPLY_MAX_C: float = 40.0
"""Upper clamp of the optional heating-water curve [degC].

A fixed firmware/floor limit, deliberately NOT a tuning knob: hotter supply
water risks the screed and exceeds sane A2W heat-pump territory.
"""

_DHW_ONLY: str = "DHW only"

_CANONICAL_BY_KEY: dict[str, str] = {
    option.strip().lower(): option for option in HEISHAMON_MODE_OPTIONS
}


def _canonical(option: str | None) -> str | None:
    """Map a raw select state to its canonical HeishaMon option, or ``None``.

    Args:
        option: The raw option string (any case / stray whitespace), or
            ``None``.

    Returns:
        The canonical string from :data:`HEISHAMON_MODE_OPTIONS`, or ``None``
        when the value is missing or not a recognised HeishaMon mode.
    """
    if option is None:
        return None
    return _CANONICAL_BY_KEY.get(option.strip().lower())


def direction_option(mode: Mode, current_option: str | None) -> str | None:
    """Desired pump-mode option for a Tortoise mode, preserving the DHW flag.

    The write table (owner decision 2026-07-12): HEATING maps to the ``Heat``
    variant and COOLING to the ``Cool`` variant of the CURRENT option — a
    current ``*+DHW`` keeps its ``+DHW`` part. The return value is the DESIRED
    canonical option; the adapter writes only when it differs from the pump's
    current option (so "already in sync" is simply ``desired == current``).

    ``None`` — meaning "do not write anything" — is returned when:

    * ``mode`` is TRANSITIONAL or OFF (a direction is never forced),
    * ``current_option`` is missing or not a recognised HeishaMon mode
      (never write blind), or
    * the pump is currently in ``"DHW only"`` (the external DHW automation is
      mid-cycle and will restore its remembered direction; writing now would
      race it).

    Args:
        mode: The global Tortoise operating mode.
        current_option: The pump-mode select's current option (raw string).

    Returns:
        The desired canonical option string, or ``None`` to leave the pump
        untouched.
    """
    if mode not in (Mode.HEATING, Mode.COOLING):
        return None
    current = _canonical(current_option)
    if current is None or current == _DHW_ONLY:
        return None
    has_dhw = current.endswith("+DHW")
    direction = "Heat" if mode is Mode.HEATING else "Cool"
    return f"{direction}+DHW" if has_dhw else f"{direction} only"


def dhw_option(current_option: str | None, want_dhw: bool) -> str | None:
    """Pump-mode option that adds/removes the ``+DHW`` flag (manual switch).

    Toggles only the DHW part, never the direction: ``"Heat only"`` <->
    ``"Heat+DHW"``, ``"Cool only"`` <-> ``"Cool+DHW"``, ``"Auto"`` <->
    ``"Auto+DHW"``. An option already in the requested state maps to itself
    (the adapter then has nothing to write).

    ``None`` — meaning "the request cannot be honoured" — is returned when
    the current option is missing / unrecognised, or when the caller asks to
    REMOVE the flag from ``"DHW only"``: there is no base direction to fall
    back to (the websocket surfaces this as ``hp_dhw_unavailable``).

    Args:
        current_option: The pump-mode select's current option (raw string).
        want_dhw: ``True`` to add the ``+DHW`` flag, ``False`` to remove it.

    Returns:
        The desired canonical option string, or ``None`` when unavailable.
    """
    current = _canonical(current_option)
    if current is None:
        return None
    if current == _DHW_ONLY:
        return _DHW_ONLY if want_dhw else None
    has_dhw = current.endswith("+DHW")
    if want_dhw == has_dhw:
        return current
    if want_dhw:
        base = current.removesuffix(" only")
        return f"{base}+DHW"
    base = current.removesuffix("+DHW")
    return "Auto" if base == "Auto" else f"{base} only"


def cooling_setpoint_c(base_c: float, safe_dew_c: float | None) -> float:
    """Cooling-water setpoint: the base floored by the global safe dew point.

    The written value is ``max(base_c, safe_dew_c)`` so the water never enters
    the condensation zone; without an available safe dew point (no eligible
    cooled room) the base alone applies.

    Args:
        base_c: The ``cooling_supply_base_c`` tuning knob [degC].
        safe_dew_c: The global safe dew point [degC]
            (``max_over_cooled(T_dew) + 2 K``), or ``None`` when no room is
            eligible.

    Returns:
        The cooling-water setpoint to write [degC].
    """
    if safe_dew_c is None:
        return base_c
    return max(base_c, safe_dew_c)


def round_to_step_c(value_c: float, step_c: float) -> float:
    """Quantize a water setpoint to the pump number entity's step grid.

    Rounds to the NEAREST grid point (half rounds up), so the value written to
    the pump lands exactly on its own resolution and its UI never shows a
    curve/dew artefact like 27.35, and the entity does not silently re-quantize
    it on the way in.

    Owner decision (issue #5, 2026-07-13): plain round-to-nearest, deliberately
    NOT ``ceil``/``floor``. The cooling setpoint is a dew-safety floor
    (``max(base, safe_dew_c)``), and an earlier idea was to ``ceil`` it so
    quantization could never dip below the floor. That was rejected: the floor
    already carries the full ``dew_margin_k`` (2 K default) condensation buffer,
    so ceiling would sacrifice cooling capacity to defend a margin that is
    already generous. Round-to-nearest may surrender at most half a step of that
    2 K buffer — an accepted trade. The heating setpoint (an upper comfort/
    energy target, no condensation floor) uses the SAME single rule.

    Quantizing to the entity's REAL ``step`` (not a hard-coded 0.5 K grid) is
    the crux of the fix: with a 1 degC-step pump, rounding to 0.5 K first
    produced 16.5, which the pump then floored to 16 — below the 16.56 floor.

    A ``step_c`` of zero or below means "no grid" and ``value_c`` is returned
    unchanged.

    Args:
        value_c: The raw water setpoint [degC].
        step_c: The number entity's ``step`` attribute [degC / K].

    Returns:
        ``value_c`` snapped to the nearest multiple of ``step_c`` [degC], or
        ``value_c`` unchanged when ``step_c <= 0``.
    """
    if step_c <= 0.0:
        return value_c
    return math.floor(value_c / step_c + 0.5) * step_c


def heating_curve(config: ControllerConfig) -> WeatherCompCurve:
    """Build the optional heating-water curve from the global tuning knobs.

    Reuses the existing :class:`~tortoise_ufh.weather_comp.WeatherCompCurve`:
    ``t_supply = clip(heating_supply_base_c + heating_supply_slope *
    max(0, ff_neutral_c - t_out), 20, 40)``. ``ff_neutral_c`` is deliberately
    shared with the valve feedforward — it carries the same meaning ("the
    outdoor temperature at which nothing extra is needed").

    Args:
        config: The GLOBAL controller tuning (per-room overrides never carry
            the heat-pump knobs).

    Returns:
        A validated :class:`~tortoise_ufh.weather_comp.WeatherCompCurve`.

    Raises:
        ValueError: If the knobs violate the curve invariants (cannot happen
            for a :class:`ControllerConfig` that passed its own validation).
    """
    return WeatherCompCurve(
        t_supply_base=config.heating_supply_base_c,
        slope=config.heating_supply_slope,
        t_neutral=config.ff_neutral_c,
        t_supply_max=HEATING_SUPPLY_MAX_C,
        t_supply_min=HEATING_SUPPLY_MIN_C,
    )
