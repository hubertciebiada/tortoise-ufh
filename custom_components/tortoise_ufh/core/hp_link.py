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
    "HEATING_SUPPLY_MAX_C",
    "HEATING_SUPPLY_MIN_C",
    "HEISHAMON_MODE_OPTIONS",
    "CoolingDemand",
    "FlickerDecision",
    "SetpointFlicker",
    "cooling_demand",
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
    loop count (``len(report.loop_flow_status)``, stamped by the S6 wrapper to
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
            open_pct += out.valve_position_pct * max(1, len(report.loop_flow_status))
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
