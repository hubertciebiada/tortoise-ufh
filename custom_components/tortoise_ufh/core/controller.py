"""Per-room UFH black-box controller and whole-building orchestrator.

This module is the *heart* of the tortoise-ufh core. It implements the two
public control classes:

* :class:`RoomController` — one independent closed-loop controller per room
  (zone). It maps raw room inputs (temperature, humidity, water probes, mode,
  heat-pump availability) onto the two per-room commands (a single valve
  position and a fast-source command) plus a rich under-the-hood
  :class:`~tortoise_ufh.models.RoomReport`.
* :class:`BuildingController` — a thin orchestrator that runs one
  :class:`RoomController` per room, never raises on a single room's failure,
  and computes the *global safe dew point* fed to the heat pump.

The control law is PID-family (single PI loop on the room-temperature error
plus a trend-damping term ``kt * dT_room/dt`` that tames the overshoot of a
high-thermal-mass floor). There is deliberately **no** derivative term on the
error, no Kalman filter and no MPC.

The module is pure Python (stdlib + sibling core modules only) and MUST NOT
import ``homeassistant``.

Units (repo-wide, non-negotiable):
    * Temperatures / setpoints / dew points: degrees Celsius (``_c``).
    * Errors / trends expressed in kelvin: ``error_c`` [K], ``trend`` [K/h].
    * Valve / actuator positions: percent 0..100 (``_pct``), float.
    * Control step ``dt_seconds``: seconds (default 300 = 5-minute cycle).
    * Fast-source min ON/OFF dwell times: configured in minutes, tracked in
      seconds internally.
"""

from __future__ import annotations

import math
from dataclasses import replace

from .config import ControllerConfig
from .dew_point import cooling_throttle_factor, dew_point
from .models import (
    BuildingOutputs,
    FastSourceCommand,
    FastSourceKind,
    FastSourceMode,
    Mode,
    RoomInputs,
    RoomOutputs,
    RoomReport,
)
from .pid import PIDController
from .safety import SafetyAction, SafetyEvaluator, SensorSnapshot

__all__ = [
    "BuildingController",
    "RoomController",
    "classify_dew_eligibility",
]


def classify_dew_eligibility(room_inputs: RoomInputs) -> str | None:
    """Classify a room's eligibility for the global safe dew-point maximum.

    Single source of truth shared by two consumers: :meth:`RoomController.step`
    records the result in :attr:`~tortoise_ufh.models.RoomReport.dew_excluded_reason`,
    and :meth:`BuildingController._eligible_dew_point` uses it (a ``None`` return
    meaning eligible) to decide whether the room feeds the global maximum.

    A room is eligible (returns ``None``) only when it is in :attr:`Mode.COOLING`
    with ``cooling_enabled`` and both a room-temperature and a usable humidity
    reading. Otherwise the first failing precondition names the reason.

    Args:
        room_inputs: The room's raw inputs for this cycle.

    Returns:
        ``None`` when the room is eligible, else one of ``"not_cooling_mode"``,
        ``"cooling_disabled"``, ``"no_temperature"`` or ``"no_humidity"``.
    """
    if room_inputs.mode is not Mode.COOLING:
        return "not_cooling_mode"
    if not room_inputs.cooling_enabled:
        return "cooling_disabled"
    if room_inputs.room_temperature_c is None:
        return "no_temperature"
    rh = room_inputs.humidity_pct
    if rh is None or rh <= 0.0:
        return "no_humidity"
    return None


# --- Module constants -------------------------------------------------------

_SECONDS_PER_HOUR: float = 3600.0
"""Seconds in one hour, for converting ``dt_seconds`` to the trend's K/h."""

_INITIAL_FAST_TIMER_S: float = 1.0e9
"""Initial fast-source dwell timer [s].

Seeded large so the very first ON/OFF transition is never blocked by the
minimum OFF/ON dwell time (there is no prior state to protect).
"""

_FF_NEUTRAL_C: float = 15.0
"""Outdoor temperature [degC] at which the optional feedforward term is zero."""

_FF_GAIN_PCT_PER_K: float = 1.0
"""Optional-feedforward gain [%/K] of outdoor deviation from neutral."""

_FF_MAX_PCT: float = 20.0
"""Upper clamp [%] on the optional outdoor feedforward baseline term."""

GLOBAL_SAFE_DEW_MARGIN_K: float = 2.0
"""Safety margin [K] added on top of ``max_i(T_dew_i)`` for the global sensor."""


class RoomController:
    """Independent per-room closed-loop UFH controller (the black box).

    One instance controls exactly one room / zone. It is stateful: it owns a
    :class:`~tortoise_ufh.pid.PIDController`, the previous room temperature (for
    the trend), the last commanded valve position (for safe-degrade holding),
    and the fast-source min ON/OFF dwell timer. Call :meth:`step` once per
    control cycle and :meth:`reset` to clear all internal state.

    The controller emits three things every cycle: a single valve position
    (0..100 %) shared by every loop in the room, a fast-source command
    (``on`` + direction + room target), and a full
    :class:`~tortoise_ufh.models.RoomReport`.

    Typical usage::

        controller = RoomController(ControllerConfig(), name="salon")
        outputs = controller.step(room_inputs, dt_seconds=300.0)
        valve_pct = outputs.valve_position_pct
    """

    def __init__(self, config: ControllerConfig, *, name: str = "") -> None:
        """Initialise the room controller.

        Args:
            config: Per-room tuning knobs (gains, deadband, valve floor, dew and
                fast-source parameters).
            name: Human-readable room identifier used only in report text.
        """
        self._config = config
        self._name = name
        self._pid = PIDController(
            kp=config.kp,
            ki=config.ki,
            kd=config.kd,
            dt=config.cycle_seconds,
        )
        # Safe-degrade default: until at least one live step has established a
        # real valve position (``_seeded``), the cold-start hold value is
        # mode-aware (heating floor for HEATING, 0 for COOLING/OFF) per
        # BUILD_SPEC step 1, rather than the heating floor unconditionally.
        self._prev_t_room: float | None = None
        self._last_valve_pct: float = config.valve_floor_pct
        self._seeded: bool = False
        self._fast_on: bool = False
        self._fast_timer_s: float = _INITIAL_FAST_TIMER_S
        # Seconds remaining on the min ON/OFF dwell lock (None = unlocked / no
        # fast source). Recomputed each cycle by the fast-source decision and
        # surfaced in the report for the panel's assist timer.
        self._fast_dwell_remaining_s: float | None = None
        # Stateful hard-safety layer (S1..S5) with per-rule hysteresis, held
        # across cycles and applied as a post-processing override of the
        # computed outputs (PRD 8.7).
        self._safety = SafetyEvaluator()

    # -- properties ---------------------------------------------------------

    @property
    def name(self) -> str:
        """Room identifier (read-only)."""
        return self._name

    @property
    def last_valve_pct(self) -> float:
        """Last commanded valve position [%] (read-only)."""
        return self._last_valve_pct

    # -- public API ---------------------------------------------------------

    def step(self, inputs: RoomInputs, *, dt_seconds: float = 300.0) -> RoomOutputs:
        """Run one control cycle and return the per-room result.

        Implements the full 15-step algorithm: missing-sensor safe degrade,
        OFF / TRANSITIONAL handling, trend estimation, deadband, integrator
        freeze, PI compute, trend damping, optional feedforward, heating valve
        floor, cooling local dew-point throttle (S2), clamping/saturation, and
        fast-source coordination with anti priority-inversion.

        Args:
            inputs: The room's raw inputs for this cycle (values may be
                ``None`` for missing sensors).
            dt_seconds: Elapsed time since the previous step [s]. Must be > 0.
                Defaults to 300 (a 5-minute cycle). Used for the trend, the
                integrator scaling is handled by the PID's own ``dt``.

        Returns:
            The :class:`~tortoise_ufh.models.RoomOutputs` (valve + fast source +
            report).

        Raises:
            ValueError: If ``dt_seconds`` is not positive.
        """
        if dt_seconds <= 0:
            msg = f"dt_seconds must be > 0, got {dt_seconds}"
            raise ValueError(msg)

        # -- Step 1: missing room temperature -> safe degrade ---------------
        t_room = inputs.room_temperature_c
        if t_room is None:
            degraded = self._apply_safety(inputs, self._safe_degrade(inputs.mode))
            return self._finalize(inputs, degraded)

        # A live-sensor step has run: future safe-degrade holds the real last
        # commanded valve rather than the mode-aware cold-start default.
        self._seeded = True
        setpoint = inputs.setpoint_c
        mode = inputs.mode
        error_c = setpoint - t_room  # report uses the heating sign convention

        # -- Step 4: trend (dT_room/dt), 0 on first call --------------------
        dt_hours = dt_seconds / _SECONDS_PER_HOUR
        if self._prev_t_room is None:
            trend = 0.0
        else:
            trend = (t_room - self._prev_t_room) / dt_hours
        self._prev_t_room = t_room

        # Room dew point (for the report and, in cooling, the S2 throttle).
        room_dew = self._room_dew_point(t_room, inputs.humidity_pct)

        # -- Step 2: OFF ----------------------------------------------------
        if mode is Mode.OFF:
            result = self._passive_result(
                valve=0.0,
                error_c=error_c,
                trend=trend,
                room_dew=room_dew,
                explanation=f"Off ({self._name}): zawor 0%, brak komend.".strip(),
                room_temperature_c=t_room,
            )
        # -- Step 3: TRANSITIONAL (valves parked, split bidirectional) ------
        elif mode is Mode.TRANSITIONAL:
            result = self._transitional_result(
                inputs=inputs,
                error_c=error_c,
                trend=trend,
                room_dew=room_dew,
                dt_seconds=dt_seconds,
            )
        else:
            # -- Steps 5-14: heating / cooling PID path ---------------------
            result = self._active_result(
                inputs=inputs,
                error_c=error_c,
                trend=trend,
                room_dew=room_dew,
                dt_seconds=dt_seconds,
            )

        return self._finalize(inputs, self._apply_safety(inputs, result))

    def _finalize(self, inputs: RoomInputs, result: RoomOutputs) -> RoomOutputs:
        """Stamp the additive dew-reason and dwell-remaining report fields.

        Runs after the safety override so the dwell value reflects the final
        fast-source state (a safety force-off clears it). Uses ``replace`` to
        keep the frozen result immutable.

        Args:
            inputs: The room's raw inputs for this cycle (for the dew-reason
                classification).
            result: The post-safety computed outputs.

        Returns:
            The result with ``dew_excluded_reason`` and ``fast_dwell_remaining_s``
            filled in.
        """
        report = replace(
            result.report,
            dew_excluded_reason=classify_dew_eligibility(inputs),
            fast_dwell_remaining_s=self._fast_dwell_remaining_s,
        )
        return replace(result, report=report)

    def reset(self) -> None:
        """Clear all internal state (PID, trend, held valve, fast timers)."""
        self._pid.reset()
        self._prev_t_room = None
        self._last_valve_pct = self._config.valve_floor_pct
        self._seeded = False
        self._fast_on = False
        self._fast_timer_s = _INITIAL_FAST_TIMER_S
        self._fast_dwell_remaining_s = None
        self._safety.reset()

    # -- internal: result builders -----------------------------------------

    def _safe_degrade(self, mode: Mode) -> RoomOutputs:
        """Build the safe-degraded result when the room sensor is lost.

        Holds the last valve position (freeze), forces the fast source OFF and
        flags ``"sensor_lost"``. The PID is not run. On a cold start (no live
        step yet) the hold value is mode-aware: the heating floor for HEATING,
        but 0 for COOLING/TRANSITIONAL/OFF so a lost sensor never sends
        unprotected cold water through the floor (BUILD_SPEC step 1).

        Args:
            mode: The room's current operating mode, used to pick the cold-start
                hold value before any live step has run.

        Returns:
            A safe-degraded :class:`~tortoise_ufh.models.RoomOutputs`.
        """
        fast = self._force_fast_off()
        if self._seeded:
            valve = self._last_valve_pct
        else:
            valve = self._config.valve_floor_pct if mode is Mode.HEATING else 0.0
        self._last_valve_pct = valve
        # Drop the stale reference so the first recovered cycle takes the
        # trend==0 branch instead of dividing a multi-cycle delta by one dt.
        self._prev_t_room = None
        report = RoomReport(
            error_c=None,
            trend_c_per_h=None,
            room_dew_point_c=None,
            p_term=0.0,
            i_term=self._pid.integral,
            trend_term=0.0,
            feedforward_term=0.0,
            raw_valve_pct=valve,
            valve_floor_applied=False,
            saturated=False,
            dew_throttle_factor=1.0,
            integrator_frozen=True,
            flags=("sensor_lost",),
            explanation=(
                "Utrata czujnika pokoju: zawor trzyma ostatnia pozycje "
                f"{valve:.0f}%, split OFF."
            ),
            room_temperature_c=None,
        )
        return RoomOutputs(valve_position_pct=valve, fast_source=fast, report=report)

    def _passive_result(
        self,
        *,
        valve: float,
        error_c: float,
        trend: float,
        room_dew: float | None,
        explanation: str,
        room_temperature_c: float | None,
    ) -> RoomOutputs:
        """Build an OFF-mode result (valve parked, fast source forced OFF).

        Args:
            valve: The parked valve position [%].
            error_c: ``setpoint - room_temp`` [K] (heating convention).
            trend: Measured trend [K/h].
            room_dew: Room dew point [degC] or ``None``.
            explanation: Report text.
            room_temperature_c: Measured room temperature [degC] echoed into the
                report, or ``None`` when unavailable.

        Returns:
            The passive :class:`~tortoise_ufh.models.RoomOutputs`.
        """
        fast = self._force_fast_off()
        self._last_valve_pct = valve
        report = RoomReport(
            error_c=error_c,
            trend_c_per_h=trend,
            room_dew_point_c=room_dew,
            p_term=0.0,
            i_term=self._pid.integral,
            trend_term=0.0,
            feedforward_term=0.0,
            raw_valve_pct=valve,
            valve_floor_applied=False,
            saturated=valve <= 0.0 or valve >= 100.0,
            dew_throttle_factor=1.0,
            integrator_frozen=True,
            flags=(),
            explanation=explanation,
            room_temperature_c=room_temperature_c,
        )
        return RoomOutputs(valve_position_pct=valve, fast_source=fast, report=report)

    def _transitional_result(
        self,
        *,
        inputs: RoomInputs,
        error_c: float,
        trend: float,
        room_dew: float | None,
        dt_seconds: float,
    ) -> RoomOutputs:
        """Build a TRANSITIONAL-mode result (valve parked at 0, split only).

        The fast source runs bidirectionally on the sign of the error, subject
        to the boost offset and the min ON/OFF timers. The valve is parked.

        Args:
            inputs: The room's raw inputs.
            error_c: ``setpoint - room_temp`` [K] (heating convention).
            trend: Measured trend [K/h].
            room_dew: Room dew point [degC] or ``None``.
            dt_seconds: Elapsed time [s].

        Returns:
            The transitional :class:`~tortoise_ufh.models.RoomOutputs`.
        """
        cfg = self._config
        flags: list[str] = []
        valve = 0.0
        self._last_valve_pct = valve

        if inputs.fast_source_kind is FastSourceKind.NONE:
            fast = self._force_fast_off()
            direction = "brak"
        else:
            # Bidirectional: heating below setpoint, cooling above.
            heating_demand = error_c  # setpoint - t_room
            cooling_demand = -error_c
            heater_cannot_cool = False
            if abs(error_c) > cfg.boost_offset_c or (
                self._fast_on and abs(error_c) > cfg.deadband_c
            ):
                if heating_demand >= cooling_demand:
                    fs_mode = FastSourceMode.HEATING
                    want_on = self._want_fast(heating_demand)
                    direction = "grzanie"
                elif inputs.fast_source_kind is FastSourceKind.HEATER:
                    # A heater cannot cool: force OFF, bypassing the min-ON
                    # dwell so a held-on heater is never re-emitted as cooling.
                    heater_cannot_cool = True
                    direction = "brak (grzejnik nie chlodzi)"
                    if "fast_source_cannot_cool" not in flags:
                        flags.append("fast_source_cannot_cool")
                else:
                    fs_mode = FastSourceMode.COOLING
                    want_on = self._want_fast(cooling_demand)
                    direction = "chlodzenie"
            else:
                fs_mode = self._fallback_mode(error_c)
                want_on = False
                direction = "brak"
                if (
                    inputs.fast_source_kind is FastSourceKind.HEATER
                    and fs_mode is FastSourceMode.COOLING
                ):
                    # A held-on heater must never be re-emitted as cooling.
                    heater_cannot_cool = True
                    direction = "brak (grzejnik nie chlodzi)"
                    if "fast_source_cannot_cool" not in flags:
                        flags.append("fast_source_cannot_cool")
            if heater_cannot_cool:
                fast = self._force_fast_off()
            else:
                fast = self._decide_fast_source(
                    want_on=want_on,
                    fs_mode=fs_mode,
                    target=inputs.setpoint_c,
                    dt_seconds=dt_seconds,
                    flags=flags,
                )

        report = RoomReport(
            error_c=error_c,
            trend_c_per_h=trend,
            room_dew_point_c=room_dew,
            p_term=0.0,
            i_term=self._pid.integral,
            trend_term=0.0,
            feedforward_term=0.0,
            raw_valve_pct=valve,
            valve_floor_applied=False,
            saturated=False,
            dew_throttle_factor=1.0,
            integrator_frozen=True,
            flags=tuple(flags),
            explanation=(
                f"Przejsciowy: zawor zaparkowany (0%). Split {direction}, "
                f"{'ON' if fast.on else 'OFF'}. Blad {error_c:+.1f} K."
            ),
            room_temperature_c=inputs.room_temperature_c,
        )
        return RoomOutputs(valve_position_pct=valve, fast_source=fast, report=report)

    def _active_result(
        self,
        *,
        inputs: RoomInputs,
        error_c: float,
        trend: float,
        room_dew: float | None,
        dt_seconds: float,
    ) -> RoomOutputs:
        """Build a HEATING/COOLING result (steps 5-14 of the algorithm).

        Args:
            inputs: The room's raw inputs.
            error_c: ``setpoint - room_temp`` [K] (heating convention).
            trend: Measured trend [K/h].
            room_dew: Room dew point [degC] or ``None``.
            dt_seconds: Elapsed time [s].

        Returns:
            The active-mode :class:`~tortoise_ufh.models.RoomOutputs`.
        """
        cfg = self._config
        mode = inputs.mode
        flags: list[str] = []

        # -- Cooling opt-out: a room excluded from cooling must not floor-cool.
        # Park the floor valve at 0 (so no chilled water reaches the floor and
        # the S2 dew-point throttle is moot) and force the fast source OFF,
        # mirroring the OFF path. Never run the cooling PID for such a room:
        # opening the valve here would bypass both condensation defences.
        if mode is Mode.COOLING and not inputs.cooling_enabled:
            fast = self._force_fast_off()
            self._last_valve_pct = 0.0
            report = RoomReport(
                error_c=error_c,
                trend_c_per_h=trend,
                room_dew_point_c=room_dew,
                p_term=0.0,
                i_term=self._pid.integral,
                trend_term=0.0,
                feedforward_term=0.0,
                raw_valve_pct=0.0,
                valve_floor_applied=False,
                saturated=True,
                dew_throttle_factor=1.0,
                integrator_frozen=True,
                flags=("cooling_disabled",),
                explanation=(
                    f"Chlodzenie wykluczone ({self._name}): zawor 0%, split OFF."
                ),
                room_temperature_c=inputs.room_temperature_c,
            )
            return RoomOutputs(valve_position_pct=0.0, fast_source=fast, report=report)

        # -- Step 5: error in "need more actuation" convention --------------
        if mode is Mode.HEATING:
            error = error_c  # setpoint - t_room
            trend_toward = trend  # rising toward setpoint
        else:  # Mode.COOLING
            error = -error_c  # t_room - setpoint
            trend_toward = -trend  # falling toward setpoint

        # -- Step 6: deadband -> reduce magnitude, keep sign ----------------
        error_db = math.copysign(max(0.0, abs(error) - cfg.deadband_c), error)

        # -- Step 7: integrator freeze during DHW / defrost -----------------
        freeze = inputs.hp_active_for_ufh is False

        # -- Step 8: PI compute ---------------------------------------------
        pid_out = self._pid.compute(error_db, freeze_integrator=freeze)
        p_term = cfg.kp * error_db
        i_term = self._pid.integral

        # -- Step 9: trend damping (anti-overshoot) -------------------------
        trend_damp = cfg.kt * max(0.0, trend_toward)
        trend_term = -trend_damp
        valve = pid_out + trend_term

        # -- Step 10: optional outdoor feedforward --------------------------
        ff_term = self._feedforward(mode, inputs.outdoor_temperature_c)
        valve += ff_term

        # raw = pre-floor / pre-dew / pre-clamp value
        raw_valve = valve

        # -- Step 11: heating valve floor (only when calling for heat) ------
        valve_floor_applied = False
        if mode is Mode.HEATING and error_db > 0.0 and valve < cfg.valve_floor_pct:
            valve = cfg.valve_floor_pct
            valve_floor_applied = True

        # -- Step 12: cooling local dew-point throttle (S2) -----------------
        # Unconditional for any COOLING room whose valve can open. Rooms
        # excluded from cooling returned above with the valve parked at 0, so
        # the throttle is never the thing skipped for an unprotected room.
        dew_factor = 1.0
        if mode is Mode.COOLING:
            dew_factor = self._cooling_throttle(inputs, room_dew, flags)
            valve *= dew_factor

        # -- Step 13: clamp + saturation ------------------------------------
        saturated = valve <= 0.0 or valve >= 100.0
        valve = max(0.0, min(100.0, valve))
        self._last_valve_pct = valve

        # -- Step 14: fast-source coordination (anti priority-inversion) ----
        fast = self._coordinate_fast_source(
            inputs=inputs,
            error_c=error_c,
            dt_seconds=dt_seconds,
            flags=flags,
        )

        # -- Step 15: report -------------------------------------------------
        mode_pl = "Grzanie" if mode is Mode.HEATING else "Chlodzenie"
        split_txt = ""
        if inputs.fast_source_kind is not FastSourceKind.NONE:
            split_txt = f" Split {'ON (boost)' if fast.on else 'OFF'}."
        explanation = (
            f"{mode_pl}, blad {error_c:+.1f} K, trend {trend:+.1f} K/h. "
            f"Zawor {valve:.0f}%.{split_txt}"
        )
        report = RoomReport(
            error_c=error_c,
            trend_c_per_h=trend,
            room_dew_point_c=room_dew,
            p_term=p_term,
            i_term=i_term,
            trend_term=trend_term,
            feedforward_term=ff_term,
            raw_valve_pct=raw_valve,
            valve_floor_applied=valve_floor_applied,
            saturated=saturated,
            dew_throttle_factor=dew_factor,
            integrator_frozen=freeze,
            flags=tuple(flags),
            explanation=explanation,
            room_temperature_c=inputs.room_temperature_c,
        )
        return RoomOutputs(valve_position_pct=valve, fast_source=fast, report=report)

    # -- internal: helpers --------------------------------------------------

    @staticmethod
    def _room_dew_point(t_room: float, humidity_pct: float | None) -> float | None:
        """Compute the room dew point when humidity is usable.

        Args:
            t_room: Room air temperature [degC].
            humidity_pct: Relative humidity [%] or ``None``.

        Returns:
            Dew-point temperature [degC], or ``None`` when humidity is missing
            or non-positive.
        """
        if humidity_pct is None or humidity_pct <= 0.0:
            return None
        return dew_point(t_room, humidity_pct)

    def _feedforward(self, mode: Mode, t_out: float | None) -> float:
        """Compute the optional, bounded outdoor feedforward baseline [%].

        Heating: colder outside -> higher baseline. Cooling: hotter outside ->
        higher baseline. Disabled unless ``outdoor_ff_enabled`` and an outdoor
        temperature is available.

        Args:
            mode: The active mode (HEATING or COOLING).
            t_out: Outdoor temperature [degC] or ``None``.

        Returns:
            Feedforward contribution [%] in ``[0, _FF_MAX_PCT]``.
        """
        if not self._config.outdoor_ff_enabled or t_out is None:
            return 0.0
        if mode is Mode.HEATING:
            deviation = max(0.0, _FF_NEUTRAL_C - t_out)
        else:
            deviation = max(0.0, t_out - _FF_NEUTRAL_C)
        return min(_FF_MAX_PCT, _FF_GAIN_PCT_PER_K * deviation)

    def _cooling_throttle(
        self,
        inputs: RoomInputs,
        room_dew: float | None,
        flags: list[str],
    ) -> float:
        """Compute the local S2 dew-point throttle factor for cooling.

        Uses the coldest loop supply temperature against the room dew point. If
        humidity or supply data is missing, the throttle is conservative
        (factor 0.0) and ``"s2_condensation"`` is flagged.

        Args:
            inputs: The room's raw inputs.
            room_dew: Room dew point [degC] or ``None``.
            flags: Mutable flag list (appended in place).

        Returns:
            Throttle factor in ``[0, 1]`` (1.0 open, 0.0 fully throttled).
        """
        cfg = self._config
        supplies = [
            loop.supply_temperature_c
            for loop in inputs.loops
            if loop.supply_temperature_c is not None
        ]
        if room_dew is None or not supplies:
            if "s2_condensation" not in flags:
                flags.append("s2_condensation")
            return 0.0
        t_supply_min = min(supplies)
        factor = cooling_throttle_factor(
            t_supply_min,
            room_dew,
            margin=cfg.dew_margin_k,
            ramp=cfg.dew_ramp_k,
        )
        if factor == 0.0 and "s2_condensation" not in flags:
            flags.append("s2_condensation")
        return factor

    @staticmethod
    def _fallback_mode(error_c: float) -> FastSourceMode:
        """Pick a non-OFF direction for a held-on fast source.

        Used when the min ON timer keeps a split running even though the target
        want-state is OFF; the direction follows the current error sign.

        Args:
            error_c: ``setpoint - room_temp`` [K] (heating convention).

        Returns:
            :attr:`FastSourceMode.HEATING` when the room is below setpoint,
            otherwise :attr:`FastSourceMode.COOLING`.
        """
        return FastSourceMode.HEATING if error_c >= 0.0 else FastSourceMode.COOLING

    def _coordinate_fast_source(
        self,
        *,
        inputs: RoomInputs,
        error_c: float,
        dt_seconds: float,
        flags: list[str],
    ) -> FastSourceCommand:
        """Decide the fast-source command for HEATING/COOLING (step 14).

        Enforces anti priority-inversion: the valve is the base and is never
        lowered because of the split; the split only *adds* boost above the
        boost offset and releases once inside the comfort (deadband) band.
        Honours the min ON / min OFF dwell timers.

        Args:
            inputs: The room's raw inputs.
            error_c: ``setpoint - room_temp`` [K] (heating convention).
            dt_seconds: Elapsed time [s].
            flags: Mutable flag list (appended in place).

        Returns:
            The :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        if inputs.fast_source_kind is FastSourceKind.NONE:
            return self._force_fast_off()

        if inputs.mode is Mode.HEATING:
            demand = error_c  # setpoint - t_room
            fs_mode = FastSourceMode.HEATING
        else:  # Mode.COOLING
            demand = -error_c  # t_room - setpoint
            fs_mode = FastSourceMode.COOLING

        # A HEATER-kind fast source can only heat; never command it to cool.
        if fs_mode is FastSourceMode.COOLING and (
            inputs.fast_source_kind is FastSourceKind.HEATER
        ):
            if "fast_source_cannot_cool" not in flags:
                flags.append("fast_source_cannot_cool")
            return self._force_fast_off()

        want_on = self._want_fast(demand)
        return self._decide_fast_source(
            want_on=want_on,
            fs_mode=fs_mode,
            target=inputs.setpoint_c,
            dt_seconds=dt_seconds,
            flags=flags,
        )

    def _want_fast(self, demand: float) -> bool:
        """Hysteretic engage/release decision for the fast source.

        Engages when ``demand`` exceeds the boost offset; once engaged, stays
        engaged until ``demand`` falls back inside the deadband (comfort band).

        Args:
            demand: Actuation demand [K] in the mode's needed direction
                (positive means "boost needed").

        Returns:
            ``True`` if the fast source should run this cycle (pre-timer).
        """
        cfg = self._config
        if self._fast_on:
            return demand > cfg.deadband_c
        return demand > cfg.boost_offset_c

    def _decide_fast_source(
        self,
        *,
        want_on: bool,
        fs_mode: FastSourceMode,
        target: float,
        dt_seconds: float,
        flags: list[str],
    ) -> FastSourceCommand:
        """Apply the min ON/OFF dwell timer to a desired fast-source state.

        Advances the dwell timer, permits a state change only when the relevant
        minimum dwell has elapsed, and flags ``"fast_source_min_runtime"`` when
        a requested change is blocked.

        Args:
            want_on: Desired ON/OFF state before the timer gate.
            fs_mode: Direction to command when running.
            target: Room target temperature [degC] for the split.
            dt_seconds: Elapsed time [s].
            flags: Mutable flag list (appended in place).

        Returns:
            The gated :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        cfg = self._config
        self._fast_timer_s += dt_seconds
        if want_on != self._fast_on:
            min_dwell_min = (
                cfg.fast_min_on_minutes if self._fast_on else cfg.fast_min_off_minutes
            )
            if self._fast_timer_s >= min_dwell_min * 60.0:
                self._fast_on = want_on
                self._fast_timer_s = 0.0
            elif "fast_source_min_runtime" not in flags:
                flags.append("fast_source_min_runtime")
        # Remaining lock on the CURRENT state: min ON while running (cannot turn
        # off yet), min OFF while idle (cannot turn on yet). None once elapsed.
        min_lock_min = (
            cfg.fast_min_on_minutes if self._fast_on else cfg.fast_min_off_minutes
        )
        remaining = min_lock_min * 60.0 - self._fast_timer_s
        self._fast_dwell_remaining_s = remaining if remaining > 0.0 else None
        if self._fast_on:
            return FastSourceCommand(on=True, mode=fs_mode, target_temperature_c=target)
        return FastSourceCommand(
            on=False, mode=FastSourceMode.OFF, target_temperature_c=None
        )

    def _force_fast_off(self) -> FastSourceCommand:
        """Force the fast source OFF immediately (safety / OFF mode).

        Bypasses the min ON timer because a lost sensor or an explicit OFF is a
        safety condition. Resets the dwell timer so re-engaging respects the
        min OFF dwell afterwards.

        Returns:
            An OFF :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        if self._fast_on:
            self._fast_timer_s = 0.0
        self._fast_on = False
        # A forced OFF is a safety / OFF-mode condition, not a normal dwell gate:
        # the panel shows no assist timer here (the min-runtime flag conveys any
        # block instead).
        self._fast_dwell_remaining_s = None
        return FastSourceCommand(
            on=False, mode=FastSourceMode.OFF, target_temperature_c=None
        )

    @staticmethod
    def _governing_supply(inputs: RoomInputs) -> float | None:
        """Return the governing loop supply temperature (S1/S2 proxy) [degC].

        The floor-surface proxy is the hottest loop in heating (worst case for
        overheat) and the coldest loop in cooling (worst case for
        condensation). ``None`` when no loop reports a supply temperature.

        Args:
            inputs: The room's raw inputs.

        Returns:
            The governing supply-water temperature [degC], or ``None``.
        """
        supplies = [
            loop.supply_temperature_c
            for loop in inputs.loops
            if loop.supply_temperature_c is not None
        ]
        if not supplies:
            return None
        return max(supplies) if inputs.mode is Mode.HEATING else min(supplies)

    def _apply_safety(self, inputs: RoomInputs, result: RoomOutputs) -> RoomOutputs:
        """Apply the hard-safety layer (S1..S5) as a post-processing override.

        Builds a :class:`~tortoise_ufh.safety.SensorSnapshot` from the governing
        supply temperature, the room temperature and humidity, evaluates the
        stateful rule set (hysteresis carried across cycles), and — if any rule
        is active — overrides the computed valve / fast-source command with the
        highest-priority action and merges the rule flags into the report.

        The S5 watchdog age is fed as 0 here: the real update age (and hence
        staleness) is owned by the orchestrator, so the controller never
        double-counts the watchdog.

        Args:
            inputs: The room's raw inputs for this cycle.
            result: The pre-safety computed outputs.

        Returns:
            The (possibly overridden) :class:`~tortoise_ufh.models.RoomOutputs`.
        """
        snapshot = SensorSnapshot(
            supply_temperature_c=self._governing_supply(inputs),
            room_temperature_c=inputs.room_temperature_c,
            humidity_pct=inputs.humidity_pct,
            last_update_age_minutes=0.0,
        )
        active = [r for r in self._safety.evaluate(snapshot) if r.triggered]
        if not active:
            return result

        # Results are priority-ordered (ascending); the first active rule wins.
        action = active[0].action
        flags = tuple(
            dict.fromkeys((*result.report.flags, *(r.rule.name for r in active)))
        )
        target = inputs.setpoint_c
        has_split = inputs.fast_source_kind is not FastSourceKind.NONE

        if action is SafetyAction.EMERGENCY_HEAT:
            valve = 100.0
            fast = (
                FastSourceCommand(
                    on=True, mode=FastSourceMode.HEATING, target_temperature_c=target
                )
                if has_split
                else self._force_fast_off()
            )
        elif action is SafetyAction.EMERGENCY_COOL:
            valve = 0.0  # air-side only; never open the floor without S2 cover
            # Only a SPLIT can cool; a HEATER must never be told to cool.
            if inputs.fast_source_kind is FastSourceKind.SPLIT:
                fast = FastSourceCommand(
                    on=True, mode=FastSourceMode.COOLING, target_temperature_c=target
                )
            else:
                fast = self._force_fast_off()
                if inputs.fast_source_kind is FastSourceKind.HEATER:
                    flags = tuple(dict.fromkeys((*flags, "fast_source_cannot_cool")))
        else:
            # CLOSE_VALVE (S1/S2) and FALLBACK_HP_CURVE (S5): park the valve and
            # release the fast source, deferring to the heat-pump native curve.
            valve = 0.0
            fast = self._force_fast_off()

        self._last_valve_pct = valve
        saturated = valve <= 0.0 or valve >= 100.0
        explanation = f"Bezpieczenstwo {active[0].rule.name}: zawor {valve:.0f}%."
        report = replace(
            result.report,
            flags=flags,
            saturated=saturated,
            explanation=explanation,
        )
        return RoomOutputs(valve_position_pct=valve, fast_source=fast, report=report)


class BuildingController:
    """Whole-building orchestrator running one controller per room.

    Holds one :class:`RoomController` per configured room, steps them each
    cycle, and computes the global safe dew point handed to the heat pump. A
    single room failing never breaks the building step: that room degrades to a
    safe :class:`~tortoise_ufh.models.RoomOutputs` carrying a flag.

    Typical usage::

        building = BuildingController({"salon": ControllerConfig()})
        outputs = building.step({"salon": room_inputs}, dt_seconds=300.0)
        safe_dew = outputs.global_safe_dew_point_c
    """

    def __init__(self, configs: dict[str, ControllerConfig]) -> None:
        """Initialise one room controller per configured room.

        Args:
            configs: Per-room :class:`~tortoise_ufh.config.ControllerConfig`
                keyed by room name. Must contain at least one room.

        Raises:
            ValueError: If ``configs`` is empty.
        """
        if not configs:
            msg = "configs must contain at least 1 room"
            raise ValueError(msg)
        self._controllers: dict[str, RoomController] = {
            name: RoomController(cfg, name=name) for name, cfg in configs.items()
        }

    def step(
        self, inputs: dict[str, RoomInputs], *, dt_seconds: float = 300.0
    ) -> BuildingOutputs:
        """Run every room controller and compute the global safe dew point.

        The global safe dew point is ``max_i(T_dew_i) + 2 K`` over rooms that
        are in COOLING with ``cooling_enabled`` and a usable humidity reading;
        it is ``None`` when no room is eligible. It never *lowers* on any single
        room — it is a maximum plus a fixed safety margin.

        Args:
            inputs: Per-room :class:`~tortoise_ufh.models.RoomInputs` keyed by
                room name.
            dt_seconds: Elapsed time since the previous step [s]. Must be > 0.

        Returns:
            The :class:`~tortoise_ufh.models.BuildingOutputs`.

        Raises:
            ValueError: If ``dt_seconds`` is not positive.
        """
        if dt_seconds <= 0:
            msg = f"dt_seconds must be > 0, got {dt_seconds}"
            raise ValueError(msg)

        rooms: dict[str, RoomOutputs] = {}
        dew_points: list[float] = []

        for name, room_inputs in inputs.items():
            controller = self._controllers.get(name)
            if controller is None:
                rooms[name] = self._unknown_room_output(
                    name, room_inputs.room_temperature_c
                )
                continue
            try:
                rooms[name] = controller.step(room_inputs, dt_seconds=dt_seconds)
            except (ValueError, ArithmeticError) as exc:
                rooms[name] = self._degraded_room_output(
                    controller, exc, room_inputs.room_temperature_c
                )

            dew = self._eligible_dew_point(room_inputs)
            if dew is not None:
                dew_points.append(dew)

        global_dew = max(dew_points) + GLOBAL_SAFE_DEW_MARGIN_K if dew_points else None
        return BuildingOutputs(rooms=rooms, global_safe_dew_point_c=global_dew)

    def reset(self) -> None:
        """Reset every room controller's internal state."""
        for controller in self._controllers.values():
            controller.reset()

    # -- internal helpers ---------------------------------------------------

    @staticmethod
    def _eligible_dew_point(room_inputs: RoomInputs) -> float | None:
        """Return a room's dew point if it is eligible for the global maximum.

        Eligibility is decided by :func:`classify_dew_eligibility` (the same
        classifier that fills ``RoomReport.dew_excluded_reason``): a ``None``
        reason means COOLING mode, ``cooling_enabled`` and usable temperature +
        humidity — one logic, two consumers.

        Args:
            room_inputs: The room's raw inputs.

        Returns:
            Dew-point temperature [degC], or ``None`` when the room is not
            eligible.
        """
        if classify_dew_eligibility(room_inputs) is not None:
            return None
        t_room = room_inputs.room_temperature_c
        rh = room_inputs.humidity_pct
        if t_room is None or rh is None:  # narrowed by the classifier above
            return None
        return dew_point(t_room, rh)

    @staticmethod
    def _unknown_room_output(
        name: str, room_temperature_c: float | None = None
    ) -> RoomOutputs:
        """Build a safe-degraded output for a room with no controller.

        Args:
            name: The unknown room name.
            room_temperature_c: Measured room temperature [degC] echoed into the
                report, or ``None`` when unavailable.

        Returns:
            A closed-valve, fast-OFF :class:`~tortoise_ufh.models.RoomOutputs`.
        """
        report = RoomReport(
            error_c=None,
            trend_c_per_h=None,
            room_dew_point_c=None,
            p_term=0.0,
            i_term=0.0,
            trend_term=0.0,
            feedforward_term=0.0,
            raw_valve_pct=0.0,
            valve_floor_applied=False,
            saturated=False,
            dew_throttle_factor=1.0,
            integrator_frozen=True,
            flags=("unknown_room",),
            explanation=f"Brak konfiguracji regulatora dla pokoju '{name}': zawor 0%.",
            room_temperature_c=room_temperature_c,
        )
        return RoomOutputs(
            valve_position_pct=0.0,
            fast_source=FastSourceCommand(on=False, mode=FastSourceMode.OFF),
            report=report,
        )

    @staticmethod
    def _degraded_room_output(
        controller: RoomController,
        exc: Exception,
        room_temperature_c: float | None = None,
    ) -> RoomOutputs:
        """Build a safe-degraded output after a room controller raised.

        Holds the controller's last valve position, forces the fast source OFF
        and flags ``"controller_error"``.

        Args:
            controller: The room controller that raised.
            exc: The exception raised.
            room_temperature_c: Measured room temperature [degC] echoed into the
                report, or ``None`` when unavailable.

        Returns:
            A held-valve, fast-OFF :class:`~tortoise_ufh.models.RoomOutputs`.
        """
        valve = controller.last_valve_pct
        report = RoomReport(
            error_c=None,
            trend_c_per_h=None,
            room_dew_point_c=None,
            p_term=0.0,
            i_term=0.0,
            trend_term=0.0,
            feedforward_term=0.0,
            raw_valve_pct=valve,
            valve_floor_applied=False,
            saturated=False,
            dew_throttle_factor=1.0,
            integrator_frozen=True,
            flags=("controller_error",),
            explanation=(
                f"Blad regulatora pokoju '{controller.name}': {exc}. "
                f"Zawor trzyma {valve:.0f}%, split OFF."
            ),
            room_temperature_c=room_temperature_c,
        )
        return RoomOutputs(
            valve_position_pct=valve,
            fast_source=FastSourceCommand(on=False, mode=FastSourceMode.OFF),
            report=report,
        )
