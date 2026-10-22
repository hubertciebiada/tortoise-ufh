"""Pure decision logic for the OPTIONAL heat-pump link (B2, 2026-07-12).

Tortoise-UFH still never controls the compressor or the pump's own weather
curve (PRD §8.10 stands). This module holds the pure, unit-testable half of
the opt-in extension recorded in ``prd-control-brain.md`` §8.13: mapping the
global Tortoise :class:`~tortoise_ufh.models.Mode` onto a HeishaMon-style
(Panasonic Aquarea) pump-mode select while ALWAYS preserving the pump's
``+DHW`` flag, toggling that flag for the manual DHW switch, and computing the
two bounded water setpoints (cooling = ``max(base, safe dew point)``; heating
= a simple weather curve reusing :class:`~tortoise_ufh.weather_comp.
WeatherCompCurve`), the opt-in cooling setpoint-flicker (issue #7) and the
heating-setpoint write gate (issue #33, :class:`HeatingSetpointGate`).

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
from collections.abc import Iterable
from dataclasses import dataclass

from .config import ControllerConfig
from .const import DEW_MARGIN_DEFAULT_K
from .models import Mode, RoomOutputs
from .weather_comp import WeatherCompCurve

__all__ = [
    "FLICKER_DEMAND_ERROR_K",
    "FLICKER_DEMAND_THROTTLE_MIN",
    "FLICKER_DEW_RESERVE_K",
    "FLICKER_START_OFFSET_K",
    "HEATING_CURVE_PREVIEW_OUTDOOR_C",
    "HEATING_GATE_BAND_SOURCES",
    "HEATING_GATE_REASONS",
    "HEATING_RESTART_BAND_MAX_K",
    "HEATING_RESTART_BAND_MIN_K",
    "HEISHAMON_MODE_OPTIONS",
    "CoolingDemand",
    "FlickerDecision",
    "HeatingGateDecision",
    "HeatingSetpointGate",
    "SetpointFlicker",
    "cooling_demand",
    "cooling_setpoint_c",
    "dhw_option",
    "direction_option",
    "heating_curve",
    "heating_curve_points",
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

HEATING_CURVE_PREVIEW_OUTDOOR_C: tuple[float, ...] = (15.0, 5.0, -5.0, -15.0)
"""Outdoor temperatures [degC] at which the panel previews the heating curve.

Issue #35: a user compares these points with the pump's own curve before
wiring the heating-setpoint entity. (The curve's 20/40 degC clamps, once the
fixed ``HEATING_SUPPLY_MIN_C`` / ``HEATING_SUPPLY_MAX_C``, are the knobs
``heating_supply_min_c`` / ``heating_supply_max_c`` since issue #35.)
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
    already carries the full 2 K ``DEW_MARGIN_DEFAULT_K`` condensation buffer,
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
    max(0, heating_curve_neutral_c - t_out), heating_supply_min_c,
    heating_supply_max_c)``. Since issue #35 the curve has its own neutral
    point; it no longer reads the valve feedforward's ``ff_neutral_c``.

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
        t_neutral=config.heating_curve_neutral_c,
        t_supply_max=config.heating_supply_max_c,
        t_supply_min=config.heating_supply_min_c,
    )


def heating_curve_points(
    config: ControllerConfig,
    outdoor_c: Iterable[float] = HEATING_CURVE_PREVIEW_OUTDOOR_C,
) -> tuple[tuple[float, float], ...]:
    """Evaluate the heating curve at a few outdoor temperatures (issue #35).

    The single source of the panel's curve preview, so the table shows
    exactly what :func:`heating_curve` would write.

    Args:
        config: The GLOBAL controller tuning.
        outdoor_c: The outdoor temperatures to evaluate [degC], in display
            order (default :data:`HEATING_CURVE_PREVIEW_OUTDOOR_C`).

    Returns:
        ``((t_out, t_supply), ...)`` pairs [degC], clamped to the curve's
        min/max, in the order of ``outdoor_c``.
    """
    curve = heating_curve(config)
    return tuple((float(t_out), curve.t_supply(float(t_out))) for t_out in outdoor_c)


# ---------------------------------------------------------------------------
# Cooling setpoint-flicker (issue #7, 2026-07-15) — pure decision logic
# ---------------------------------------------------------------------------
#
# On a Panasonic Aquarea the cooling compressor is gated by a FIXED 3 K
# hysteresis on the RETURN (inlet) water: it STARTS when the inlet climbs to
# ``cool-setpoint + 3 K`` and STOPS when the inlet falls back to ~setpoint.
# That hysteresis is firmware-baked and cannot be changed over HeishaMon, so
# through long idles the return parks near ``setpoint + 3 K`` while rooms still
# call for cooling and the floor under-delivers. The "setpoint-flicker" trips
# the compressor deliberately: when the pump sits idle in the deadband with
# genuine unmet demand, Tortoise drops the WRITTEN cooling setpoint for ONE
# cycle (to the dew-safe pulse floor ``p``) so the pump's own ``+3 K`` rule
# fires a start, then restores the normal setpoint the very next cycle so the
# run finishes at the dew-safe value. Net effect: a tighter EFFECTIVE deadband
# (colder AVERAGE water) while the return floor stays dew-safe. Opt-in,
# Panasonic-specific, verified on the owner's live unit.

FLICKER_START_OFFSET_K: float = 3.0
"""The pump's FIXED firmware START band on the return water [K] (issue #7).

The Panasonic Aquarea cooling compressor starts when the inlet reaches
``cool-setpoint + this`` and stops at ~setpoint. Verified on the owner's unit
and deliberately NOT modelled as a variable — it is a constant of the pump's
firmware.
"""

FLICKER_DEW_RESERVE_K: float = DEW_MARGIN_DEFAULT_K
"""Reserve subtracted from the global safe dew point to get the pulse floor [K].

The global safe dew point the heat pump is fed is
``max_over_cooled(T_dew) + GLOBAL_SAFE_DEW_MARGIN_K``. Subtracting the SAME
margin recovers the RAW worst-room dew point, which is where the one-cycle
pulse target ``p`` is floored (then ceiled onto the pump's grid).

Single source of truth (2026-07-15, issue #7): BOTH this reserve AND
``controller.GLOBAL_SAFE_DEW_MARGIN_K`` reference the ONE shared low-level
constant :data:`~tortoise_ufh.const.DEW_MARGIN_DEFAULT_K`, so they can never
drift apart — a change to the design margin moves the safe dew point and the
pulse floor together, and ``p`` always lands exactly on the raw dew point,
never below it.
"""

FLICKER_DEMAND_ERROR_K: float = 0.3
"""How far a cooled room must sit ABOVE its setpoint to "call" for cooling [K].

A room counts toward flicker demand only when its error (``setpoint -
room_temp``, cooling sign) is at least this far negative — i.e. the room is at
least 0.3 K too warm.
"""

FLICKER_DEMAND_THROTTLE_MIN: float = 0.5
"""Minimum local dew-throttle factor for a room to count as calling [0..1].

A room whose local S2 dew throttle has it mostly shut (factor below this) is
ignored — its floor is already dew-limited, so tripping the compressor would
not help it.
"""


@dataclass(frozen=True)
class CoolingDemand:
    """Loop-weighted cooling demand seen by the flicker gate (2026-07-16, §23).

    ``open_pct`` is a hydraulic-draw proxy in "percent-loops": one loop
    commanded fully open contributes 100. It answers "can the calling rooms
    actually absorb the cold a forced compressor start would produce, faster
    than the parallel buffer tank covers them?" — below the threshold the
    buffer (plus the pump's own return trigger) handles the demand and no
    start is forced.

    Attributes:
        open_pct: Sum over CALLING rooms of ``valve_position_pct x loop count``
            [% x loops].
        threshold_pct: The ``hp_flicker_min_open_pct`` knob value [% x loops].
        demand: ``True`` when at least one room calls AND ``open_pct >=
            threshold_pct`` — only then may the flicker force a start.
    """

    open_pct: float
    threshold_pct: float
    demand: bool


def cooling_demand(
    outputs: Iterable[RoomOutputs], *, min_open_pct: float
) -> CoolingDemand:
    """Aggregate the loop-weighted cooling demand (issue #7; gate §23).

    A room CALLS for cooling iff its report satisfies all of:

    * ``report.dew_excluded_reason is None`` — the room IS eligible for the
      global safe dew point (COOLING, ``cooling_enabled`` and usable
      temperature + humidity);
    * ``report.error_c is not None and error_c <= -FLICKER_DEMAND_ERROR_K`` —
      cooling sign (``error_c = setpoint - room_temp``), so a room ABOVE its
      setpoint is negative;
    * ``report.dew_throttle_factor >= FLICKER_DEMAND_THROTTLE_MIN`` — the room
      is not already mostly throttled shut by its local dew defence;
    * ``"sensor_lost" not in report.flags`` — the room is not degraded.

    Each calling room contributes its FINAL commanded valve position times its
    loop count (``report.loop_count``, stamped by the room controller to
    exactly the configured loop count; a report without the stamp counts as
    one loop). Demand is real only when the total meets ``min_open_pct``: a
    single loop trickling from the buffer tank must not force a compressor
    start that would merely knock the buffer down a few kelvin and short-cycle
    (owner decision 2026-07-16, 100 l parallel buffer).

    Args:
        outputs: The per-room :class:`~tortoise_ufh.models.RoomOutputs` of one
            control cycle (final valve command + report).
        min_open_pct: The ``hp_flicker_min_open_pct`` knob — minimum
            loop-weighted opening [% x loops] for the flicker to act.

    Returns:
        The :class:`CoolingDemand` aggregate for this cycle.
    """
    open_pct = 0.0
    calling = False
    for out in outputs:
        report = out.report
        if (
            report.dew_excluded_reason is None
            and report.error_c is not None
            and report.error_c <= -FLICKER_DEMAND_ERROR_K
            and report.dew_throttle_factor >= FLICKER_DEMAND_THROTTLE_MIN
            and "sensor_lost" not in report.flags
        ):
            calling = True
            open_pct += out.valve_position_pct * max(1, report.loop_count)
    return CoolingDemand(
        open_pct=open_pct,
        threshold_pct=min_open_pct,
        demand=calling and open_pct >= min_open_pct,
    )


_FLICKER_STATES: frozenset[str] = frozenset({"idle", "pulse", "cooldown"})
"""The three :class:`SetpointFlicker` states."""

_FLICKER_FLAGS: frozenset[str] = frozenset(
    {"flicker_pulsing", "flicker_dew_blocked", "flicker_no_sensor"}
)
"""The flag vocabulary a :class:`FlickerDecision` may carry."""

_FLICKER_WINDOW_S: float = 3600.0
"""Rolling window for the max-starts-per-hour cap [s]."""


@dataclass(frozen=True)
class FlickerDecision:
    """One cycle's cooling setpoint-flicker verdict + diagnostics (issue #7).

    JSON-friendly (all fields are primitives or ``None``): the adapter maps it
    straight into the heat-pump runtime payload and the panel.

    Attributes:
        pulse_target_c: The cooling setpoint to WRITE this cycle when pulsing
            [degC]; ``None`` means "write the normal target ``w``".
        restore_pending: ``True`` on the pulse -> cooldown edge — tells the
            adapter it MUST write the normal ``w`` even if the cooling branch
            would otherwise skip the write (the mode flipped out of COOLING
            mid-pulse). The restore is unconditional.
        state: The machine state AFTER this step: ``"idle"`` / ``"pulse"`` /
            ``"cooldown"``.
        flags: Subset of ``"flicker_pulsing"`` (a pulse is being written this
            cycle), ``"flicker_dew_blocked"`` (armed but a pulse would cross
            the raw dew point — cannot pulse) and ``"flicker_no_sensor"``
            (enabled + cooling but the inlet/compressor reading is
            missing/stale).
        trigger_c: The armed threshold ``max(w + band_k, p + 3)`` [degC] this
            cycle, or ``None`` when the feature is idle.
        stuck_remaining_s: Seconds of "stuck & armed" still needed before a
            pulse, or ``None`` when not currently accumulating.
        cooldown_remaining_s: Seconds left on the forced-start cooldown, or
            ``None`` when not in cooldown.
        pulses_last_hour: Forced starts recorded in the last rolling hour.
        last_pulse_target_c: The pulse floor ``p`` of the most recent pulse
            [degC], or ``None`` when none has fired.

    Raises:
        ValueError: If ``state`` or a ``flags`` entry is outside its
            vocabulary.
    """

    pulse_target_c: float | None
    restore_pending: bool
    state: str
    flags: tuple[str, ...] = ()
    trigger_c: float | None = None
    stuck_remaining_s: float | None = None
    cooldown_remaining_s: float | None = None
    pulses_last_hour: int = 0
    last_pulse_target_c: float | None = None

    def __post_init__(self) -> None:
        """Validate the state and flag vocabularies."""
        if self.state not in _FLICKER_STATES:
            msg = f"state must be one of {sorted(_FLICKER_STATES)}, got {self.state!r}"
            raise ValueError(msg)
        for flag in self.flags:
            if flag not in _FLICKER_FLAGS:
                msg = (
                    "flags entries must be one of "
                    f"{sorted(_FLICKER_FLAGS)}, got {flag!r}"
                )
                raise ValueError(msg)


class SetpointFlicker:
    """Stateful cooling setpoint-flicker machine (issue #7, 2026-07-15).

    Pure: it never reads a wall clock. Time is accumulated by :meth:`tick`
    exactly ONCE per control step (the same discipline as
    :class:`~tortoise_ufh.fast_source.FastSourceMachine`, whose double-tick
    bug this deliberately avoids); the rolling-hour start cap uses an internal
    elapsed-seconds accumulator, never ``time`` / ``Date``.

    Lifecycle of one control step::

        flicker.tick(dt_seconds)     # advance time ONCE
        decision = flicker.step(...) # decide + transition

    The machine starts in ``"cooldown"`` with the full cooldown still to run:
    after an HA restart no pulse fires immediately, and because the adapter's
    setpoint write cache is empty the first cooling cycle writes the normal
    target — self-healing.
    """

    def __init__(self, config: ControllerConfig) -> None:
        """Read the four flicker knobs off the global config.

        Args:
            config: The GLOBAL controller tuning (the flicker knobs are
                global-only; per-room overrides never carry them).
        """
        self._band_k: float = config.hp_flicker_band_k
        self._stuck_threshold_s: float = config.hp_flicker_stuck_minutes * 60.0
        self._min_off_s: float = config.hp_flicker_min_off_minutes * 60.0
        self._max_starts: float = config.hp_flicker_max_starts_per_h
        # Start in cooldown with the full gap to run (safe after a restart).
        self._state: str = "cooldown"
        self._elapsed_s: float = 0.0
        self._stuck_s: float = 0.0
        self._cooldown_s: float = 0.0
        self._pulse_times_s: list[float] = []
        self._last_pulse_target_c: float | None = None

    @property
    def state(self) -> str:
        """Current machine state (``"idle"`` / ``"pulse"`` / ``"cooldown"``)."""
        return self._state

    def tick(self, dt_seconds: float) -> None:
        """Advance every internal timer by ``dt_seconds`` — once per step.

        Advances the elapsed accumulator and the state timers, then prunes the
        pulse-times window of entries older than one hour. The decision method
        only RESETS timers on transitions (never advances them), so calling
        this more than once per cycle would over-count time (the
        :class:`~tortoise_ufh.fast_source.FastSourceMachine` double-tick bug).

        Args:
            dt_seconds: Elapsed time since the previous control step [s].
        """
        self._elapsed_s += dt_seconds
        self._stuck_s += dt_seconds
        self._cooldown_s += dt_seconds
        cutoff = self._elapsed_s - _FLICKER_WINDOW_S
        self._pulse_times_s = [t for t in self._pulse_times_s if t > cutoff]

    def _pulse_floor_c(self, safe_dew_c: float, step_c: float) -> float:
        """The pulse target ``p``: the raw dew point ceiled onto the pump grid.

        ``p`` is the lowest pump-grid multiple at or above
        ``safe_dew_c - FLICKER_DEW_RESERVE_K`` (the raw worst-room dew point),
        ceiled — never floored — so the pulse can never land BELOW the dew
        point.

        Args:
            safe_dew_c: The global safe dew point [degC].
            step_c: The pump number entity's grid step [degC / K].

        Returns:
            The pulse floor ``p`` [degC].
        """
        raw = safe_dew_c - FLICKER_DEW_RESERVE_K
        if step_c <= 0.0:
            return raw
        return math.ceil(raw / step_c - 1e-9) * step_c

    def step(
        self,
        *,
        cooling_active: bool,
        demand: bool,
        hp_return_c: float | None,
        compressor_freq_hz: float | None,
        written_target_c: float | None,
        safe_dew_c: float | None,
        step_c: float,
    ) -> FlickerDecision:
        """Decide this cycle's flicker action and advance the state machine.

        Args:
            cooling_active: Whether the home is actively cooling AND the link
                may write (COOLING mode, writes enabled, not doing DHW).
            demand: Whether the calling rooms' loop-weighted valve opening
                clears the demand gate (``CoolingDemand.demand`` — see
                :func:`cooling_demand`).
            hp_return_c: The pump inlet/return water temperature [degC], or
                ``None`` when unreadable.
            compressor_freq_hz: The compressor frequency [Hz] (``0`` = off), or
                ``None`` when unreadable.
            written_target_c: The normal cooling setpoint ``w`` written this
                cycle [degC], or ``None`` when there is none.
            safe_dew_c: The global safe dew point [degC], or ``None``.
            step_c: The pump setpoint entity's grid step [degC / K].

        Returns:
            The :class:`FlickerDecision` for this cycle.
        """
        # 1. A pending pulse ALWAYS restores exactly one cycle later, even if a
        #    gate broke mid-pulse (the mode flipped out of COOLING): the pump
        #    must not be left parked at the low pulse floor.
        if self._state == "pulse":
            self._state = "cooldown"
            self._cooldown_s = 0.0
            return FlickerDecision(
                pulse_target_c=None,
                restore_pending=True,
                state="cooldown",
                cooldown_remaining_s=self._min_off_s,
                pulses_last_hour=len(self._pulse_times_s),
                last_pulse_target_c=self._last_pulse_target_c,
            )

        # 2. Feature idle: missing dew / target, or the home is not cooling.
        #    Nothing can be "stuck" without demand and a live cooling write.
        if safe_dew_c is None or written_target_c is None or not cooling_active:
            self._stuck_s = 0.0
            return FlickerDecision(
                pulse_target_c=None,
                restore_pending=False,
                state=self._state,
                pulses_last_hour=len(self._pulse_times_s),
                last_pulse_target_c=self._last_pulse_target_c,
            )

        # 3. Active cooling: evaluate the arming and dew-clamp conditions.
        p = self._pulse_floor_c(safe_dew_c, step_c)
        trigger_c = max(written_target_c + self._band_k, p + FLICKER_START_OFFSET_K)
        sensor_missing = hp_return_c is None or compressor_freq_hz is None
        would_arm = (
            demand
            and hp_return_c is not None
            and compressor_freq_hz is not None
            and compressor_freq_hz == 0.0
            and hp_return_c >= trigger_c
        )
        dew_blocked = p > written_target_c - step_c
        can_pulse = would_arm and not dew_blocked

        flags: list[str] = []
        if sensor_missing:
            flags.append("flicker_no_sensor")
        if would_arm and dew_blocked:
            flags.append("flicker_dew_blocked")

        stuck_remaining_s: float | None = None
        cooldown_remaining_s: float | None = None

        if self._state == "cooldown":
            cooldown_remaining_s = max(0.0, self._min_off_s - self._cooldown_s)
            if self._cooldown_s >= self._min_off_s:
                self._state = "idle"
                self._stuck_s = 0.0
                cooldown_remaining_s = None
        elif self._state == "idle":
            if can_pulse:
                stuck_remaining_s = max(0.0, self._stuck_threshold_s - self._stuck_s)
                if (
                    self._stuck_s >= self._stuck_threshold_s
                    and len(self._pulse_times_s) < self._max_starts
                ):
                    # EMIT PULSE: write the dew-safe floor for one cycle.
                    self._pulse_times_s.append(self._elapsed_s)
                    self._last_pulse_target_c = p
                    self._state = "pulse"
                    self._stuck_s = 0.0
                    flags.append("flicker_pulsing")
                    return FlickerDecision(
                        pulse_target_c=p,
                        restore_pending=False,
                        state="pulse",
                        flags=tuple(flags),
                        trigger_c=trigger_c,
                        cooldown_remaining_s=None,
                        pulses_last_hour=len(self._pulse_times_s),
                        last_pulse_target_c=self._last_pulse_target_c,
                    )
            else:
                # Not armed (or dew-blocked): do not accumulate toward a pulse.
                self._stuck_s = 0.0

        return FlickerDecision(
            pulse_target_c=None,
            restore_pending=False,
            state=self._state,
            flags=tuple(flags),
            trigger_c=trigger_c,
            stuck_remaining_s=stuck_remaining_s,
            cooldown_remaining_s=cooldown_remaining_s,
            pulses_last_hour=len(self._pulse_times_s),
            last_pulse_target_c=self._last_pulse_target_c,
        )


# ---------------------------------------------------------------------------
# Heating-setpoint write gate (issue #33, 2026-10-22) — pure decision logic
# ---------------------------------------------------------------------------
#
# On a Panasonic Aquarea (HeishaMon) a step of the heating water target made
# while the compressor is IDLE can lock out the restart: the compressor then
# stays off with the outlet several kelvin below the target for a long time.
# One install, 11 days: all 4 lockouts followed an idle-period step that left
# the outlet 1.5-3.0 K below the new target. A step that left the outlet more
# than the pump's restart band (3 K) below the new target started the
# compressor in the same minute (10/10); a step that left the outlet at or
# above the new target never locked out; ~30 idle periods WITHOUT a step
# restarted normally. The mechanism is undocumented, so the gate only allows
# the step shapes that were observed to be safe and holds the others.
#
# The gate also owns item 3 of the issue: a play (backlash) operator on the
# RAW curve value before it is quantized to the pump's grid, so a 0.1 K
# outdoor flicker at a rounding boundary cannot toggle a 1 K target.

HEATING_GATE_REASONS: frozenset[str] = frozenset(
    {
        "no_target",
        "in_sync",
        "ungated_disabled",
        "ungated_no_input",
        "write_increase_running",
        "write_decrease_running",
        "write_increase_idle",
        "write_decrease_idle",
        "write_unknown_running",
        "write_unknown_idle",
        "hold_decrease_running",
        "hold_increase_idle",
        "hold_decrease_idle",
        "hold_unknown_running",
        "hold_unknown_idle",
    }
)
"""The ``reason`` vocabulary of a :class:`HeatingGateDecision` (issue #33).

* ``no_target`` — nothing to write (not HEATING, or no outdoor reading).
* ``in_sync`` — the pump already holds the target; nothing is written (and
  an unchanged setpoint is never re-asserted while the gate is active).
* ``ungated_disabled`` / ``ungated_no_input`` — the gate is switched off, or
  the compressor-frequency / outlet reading is missing: the target is written
  as before #33 (hysteresis still applies).
* ``write_*`` — the step is written this cycle; ``hold_*`` — it is held and
  re-evaluated next cycle. ``*_unknown_*`` — the pump's current setpoint is
  unreadable, so the direction of the step is unknown and only a write that
  is safe either way is allowed.
"""

HEATING_GATE_BAND_SOURCES: frozenset[str] = frozenset({"entity", "knob"})
"""Where the restart band of a decision came from."""

HEATING_RESTART_BAND_MIN_K: float = 1.0
"""Lowest plausible restart band read from the pump entity [K]."""

HEATING_RESTART_BAND_MAX_K: float = 10.0
"""Highest plausible restart band read from the pump entity [K]."""

_SYNC_TOLERANCE_MIN_K: float = 0.05
"""Smallest "same setpoint" tolerance, used when the pump has no grid [K]."""


@dataclass(frozen=True)
class HeatingGateDecision:
    """One cycle's heating-setpoint write-gate verdict (issue #33).

    JSON-friendly (primitives or ``None``): the adapter maps it straight into
    the heat-pump runtime payload, the panel and the diagnostics sensor.

    Attributes:
        reason: What the gate decided and why (see
            :data:`HEATING_GATE_REASONS`).
        target_c: The desired target on the pump's grid [degC] (curve ->
            hysteresis -> quantization -> optional offset -> entity limits),
            or ``None`` when there is no target.
        write_c: The value to write THIS cycle [degC], or ``None`` (hold, in
            sync, or no target). While ungated it equals ``target_c`` and the
            adapter's write threshold / re-assert applies as before #33.
        pump_setpoint_c: The pump's current setpoint used as the reference
            [degC] (the entity state, else the last written value), or
            ``None`` when unknown.
        curve_c: The raw curve value before hysteresis [degC], or ``None``.
        hysteresis_c: The curve value after the play operator, before
            quantization [degC], or ``None``.
        gate_active: Whether the compressor-state rules decided this cycle
            (enabled and both the compressor and outlet readings present).
        held: Whether a different target is being held back this cycle.
        compressor_running: ``True`` / ``False`` from the compressor
            frequency, ``None`` when unreadable.
        outlet_c: The pump outlet water temperature [degC], or ``None``.
        restart_band_k: The restart band used [K].
        restart_band_source: ``"entity"`` (live pump value) or ``"knob"``.
        stop_offset_k: The stop offset used [K].
        held_for_s: How long the current hold has lasted [s], or ``None``
            when nothing is held.

    Raises:
        ValueError: If ``reason`` or ``restart_band_source`` is outside its
            vocabulary, or ``held`` disagrees with ``reason``.
    """

    reason: str
    target_c: float | None
    write_c: float | None
    pump_setpoint_c: float | None
    curve_c: float | None
    hysteresis_c: float | None
    gate_active: bool
    held: bool
    compressor_running: bool | None
    outlet_c: float | None
    restart_band_k: float
    restart_band_source: str
    stop_offset_k: float
    held_for_s: float | None = None

    def __post_init__(self) -> None:
        """Validate the vocabularies and the hold flag."""
        if self.reason not in HEATING_GATE_REASONS:
            msg = (
                f"reason must be one of {sorted(HEATING_GATE_REASONS)}, "
                f"got {self.reason!r}"
            )
            raise ValueError(msg)
        if self.restart_band_source not in HEATING_GATE_BAND_SOURCES:
            msg = (
                "restart_band_source must be one of "
                f"{sorted(HEATING_GATE_BAND_SOURCES)}, "
                f"got {self.restart_band_source!r}"
            )
            raise ValueError(msg)
        if self.held != self.reason.startswith("hold_"):
            msg = f"held={self.held} disagrees with reason {self.reason!r}"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable dict for the runtime / panel."""
        return {
            "reason": self.reason,
            "target_c": self.target_c,
            "write_c": self.write_c,
            "pump_setpoint_c": self.pump_setpoint_c,
            "curve_c": self.curve_c,
            "hysteresis_c": self.hysteresis_c,
            "gate_active": self.gate_active,
            "held": self.held,
            "compressor_running": self.compressor_running,
            "outlet_c": self.outlet_c,
            "restart_band_k": self.restart_band_k,
            "restart_band_source": self.restart_band_source,
            "stop_offset_k": self.stop_offset_k,
            "held_for_s": self.held_for_s,
        }


def _valid_restart_band(value_k: float | None) -> bool:
    """Whether a restart band read from the pump is plausible."""
    return (
        value_k is not None
        and math.isfinite(value_k)
        and HEATING_RESTART_BAND_MIN_K <= value_k <= HEATING_RESTART_BAND_MAX_K
    )


class HeatingSetpointGate:
    """Stateful heating-setpoint write gate (issue #33, 2026-10-22).

    Pure: no wall clock. Like :class:`SetpointFlicker`, time advances only via
    :meth:`tick`, exactly once per control step::

        gate.tick(dt_seconds)      # advance the hold timer ONCE
        decision = gate.step(...)  # hysteresis + quantize + decide

    The rules (owner's proposal, issue #33), with ``q`` the new target, ``c``
    the pump's current setpoint and ``o`` the outlet temperature:

    * compressor RUNNING: an increase (``q > c``) is written at once; a
      decrease only once ``o < q + stop_offset_k`` — otherwise the pump's own
      stop rule (outlet more than the offset above the target) would end the
      run at once;
    * compressor IDLE: an increase only if ``o < q - restart_band_k`` (that
      starts the compressor at once); a decrease only if ``o >= q``;
      otherwise the pump keeps ``c`` and the step is re-evaluated next cycle;
    * ``q`` equal to ``c`` (within half a grid step): nothing is written —
      in particular an unchanged setpoint is never re-asserted while idle;
    * ``c`` unknown: only a step safe in either direction is written
      (running: ``o < q + stop_offset_k``; idle: ``o < q - restart_band_k``).

    Without a compressor-frequency or outlet reading, or with
    ``heating_gate_enabled`` off, the gate is inactive and the target is
    written as before #33. The hysteresis applies in every case.

    Issue #34 hook: ``offset_k`` is added to the quantized target (after the
    hysteresis), so a temporary kick (e.g. +1 K) goes through the same rules
    — its restore is an ordinary decrease and obeys the decrease rule.
    """

    def __init__(self, config: ControllerConfig) -> None:
        """Read the gate knobs off the global config.

        Args:
            config: The GLOBAL controller tuning (the gate knobs are
                global-only).
        """
        self._enabled: bool = config.heating_gate_enabled
        self._hysteresis_k: float = config.heating_curve_hysteresis_k
        self._restart_band_k: float = config.heating_restart_band_k
        self._stop_offset_k: float = config.heating_stop_offset_k
        self._play_c: float | None = None
        self._holding: bool = False
        self._held_s: float = 0.0

    def tick(self, dt_seconds: float) -> None:
        """Advance the hold timer by ``dt_seconds`` — once per step.

        Args:
            dt_seconds: Elapsed time since the previous control step [s].
        """
        if self._holding:
            self._held_s += dt_seconds

    def _hysteresis(self, curve_c: float) -> float:
        """Play operator: follow the curve with a backlash of the hysteresis.

        Args:
            curve_c: The raw curve value this cycle [degC].

        Returns:
            The curve value after the play operator [degC].
        """
        if self._play_c is None:
            self._play_c = curve_c
        else:
            self._play_c = min(
                max(self._play_c, curve_c - self._hysteresis_k),
                curve_c + self._hysteresis_k,
            )
        return self._play_c

    def step(
        self,
        *,
        curve_c: float | None,
        step_c: float,
        pump_setpoint_c: float | None,
        compressor_freq_hz: float | None,
        outlet_c: float | None,
        restart_band_k: float | None = None,
        entity_min_c: float | None = None,
        entity_max_c: float | None = None,
        offset_k: float = 0.0,
    ) -> HeatingGateDecision:
        """Decide this cycle's heating-setpoint write.

        Args:
            curve_c: The raw heating-curve value [degC], or ``None`` when
                there is nothing to write (not HEATING / no outdoor reading);
                ``None`` also resets the hysteresis memory.
            step_c: The setpoint entity's grid step [degC / K] (``<= 0`` =
                no grid).
            pump_setpoint_c: The pump's current setpoint [degC] (entity
                state, else the last written value), or ``None``.
            compressor_freq_hz: Compressor frequency [Hz] (``0`` = idle), or
                ``None`` when not configured / unreadable.
            outlet_c: Pump outlet water temperature [degC], or ``None``.
            restart_band_k: The restart band read from the pump [K], or
                ``None``; an implausible value (outside
                [:data:`HEATING_RESTART_BAND_MIN_K`,
                :data:`HEATING_RESTART_BAND_MAX_K`]) falls back to the knob.
            entity_min_c: The setpoint entity's ``min`` [degC], or ``None``.
            entity_max_c: The setpoint entity's ``max`` [degC], or ``None``.
            offset_k: A temporary offset added after quantization [K]
                (issue #34 hook; ``0`` today).

        Returns:
            The :class:`HeatingGateDecision` for this cycle.
        """
        if _valid_restart_band(restart_band_k):
            assert restart_band_k is not None  # narrowed by the check
            band_k, band_source = restart_band_k, "entity"
        else:
            band_k, band_source = self._restart_band_k, "knob"
        running = None if compressor_freq_hz is None else compressor_freq_hz > 0.0

        def decide(
            reason: str,
            *,
            target_c: float | None = None,
            write_c: float | None = None,
            hysteresis_c: float | None = None,
            gate_active: bool = False,
        ) -> HeatingGateDecision:
            held = reason.startswith("hold_")
            if not held:
                self._held_s = 0.0
            self._holding = held
            return HeatingGateDecision(
                reason=reason,
                target_c=target_c,
                write_c=write_c,
                pump_setpoint_c=pump_setpoint_c,
                curve_c=curve_c,
                hysteresis_c=hysteresis_c,
                gate_active=gate_active,
                held=held,
                compressor_running=running,
                outlet_c=outlet_c,
                restart_band_k=band_k,
                restart_band_source=band_source,
                stop_offset_k=self._stop_offset_k,
                held_for_s=self._held_s if held else None,
            )

        if curve_c is None:
            self._play_c = None
            return decide("no_target")

        play_c = self._hysteresis(curve_c)
        target = round_to_step_c(play_c, step_c) + offset_k
        if entity_max_c is not None:
            target = min(target, entity_max_c)
        if entity_min_c is not None:
            target = max(target, entity_min_c)

        if not self._enabled or running is None or outlet_c is None:
            return decide(
                "ungated_disabled" if not self._enabled else "ungated_no_input",
                target_c=target,
                write_c=target,
                hysteresis_c=play_c,
            )

        stop_ok = outlet_c < target + self._stop_offset_k
        starts = outlet_c < target - band_k
        tolerance = max(step_c / 2.0, _SYNC_TOLERANCE_MIN_K)

        if pump_setpoint_c is None:
            if running:
                reason = "write_unknown_running" if stop_ok else "hold_unknown_running"
            else:
                reason = "write_unknown_idle" if starts else "hold_unknown_idle"
        elif abs(target - pump_setpoint_c) < tolerance:
            reason = "in_sync"
        elif target > pump_setpoint_c:
            if running:
                reason = "write_increase_running"
            else:
                reason = "write_increase_idle" if starts else "hold_increase_idle"
        elif running:
            reason = "write_decrease_running" if stop_ok else "hold_decrease_running"
        else:
            reason = (
                "write_decrease_idle" if outlet_c >= target else "hold_decrease_idle"
            )
        write_c = target if reason.startswith("write_") else None
        return decide(
            reason,
            target_c=target,
            write_c=write_c,
            hysteresis_c=play_c,
            gate_active=True,
        )
