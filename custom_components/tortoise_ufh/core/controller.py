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

_TREND_MIN_DT_S: float = 60.0
"""Minimum elapsed time before a new raw trend sample is taken [s].

A debounced 2-second recompute after a setpoint change must HOLD the previous
filtered trend instead of dividing a 0.1 K sensor tick by 2 s (a fictitious
180 K/h); shorter intervals accumulate until the threshold is reached
(S10, 2026-07-09).
"""

_TREND_FILTER_TAU_S: float = 900.0
"""EMA time constant of the trend filter [s] (~15 min).

Sensor noise of sigma = 0.1 K produces raw cycle-to-cycle trend noise on the
order of the true signal; the first-order filter removes it before the trend
gain ``kt`` is applied, so raising ``kt`` no longer converts noise into
actuator wear (S10, 2026-07-09).
"""

_INTEGRATOR_DECAY_AFTER_S: float = 12.0 * 3600.0
"""Inactivity (OFF/TRANSITIONAL/sensor-lost) after which the integrator is
cleared [s] — the accumulated integral of one season must not become the
first valve command of the next (S2, 2026-07-09)."""

_SAFETY_EXPLANATION_MAX_LEN: int = 200
"""Maximum length of a safety-prefixed report explanation [characters].

The safety override PREPENDS its banner to the regulation explanation
(fix 2026-07-10) instead of replacing it; the concatenation is capped so the
report/websocket payload can never bloat.
"""

_INITIAL_FAST_TIMER_S: float = 1.0e9
"""Initial fast-source dwell timer [s] used only while the physical fast-source
state is UNKNOWN (no ``fast_source_on`` feedback configured).

Seeded large so the very first ON/OFF transition is never blocked by the
minimum OFF/ON dwell time. The moment a physical on/off feedback is first
observed the machine re-seeds the timer conservatively to 0 (a full dwell must
elapse before any state change), so an HA restart/reload loop can never
short-cycle a compressor (amendment 2026-07-09, S4).
"""

FAST_TARGET_OFFSET_K: float = 1.0
"""Split target offset from the room setpoint in HEATING/COOLING [K].

Amendment 2026-07-09 (S12): the split's own air sensor sits near the ceiling
and reads warmer than the room sensor, so a target equal to the setpoint makes
the unit throttle itself before the boost is delivered. Commanding
``setpoint + 1 K`` (heating) / ``setpoint - 1 K`` (cooling) keeps the split
working through the boost; the RELEASE decision still belongs to OUR room
sensor (hysteresis + min-ON dwell), so the room cannot run away. TRANSITIONAL
keeps ``target = setpoint`` — there the split is the only source and its own
regulation holding the room AT the setpoint is exactly what removes the old
-0.65 K seasonal bias.
"""

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
        # Filtered trend state (S10, 2026-07-09): EMA of the raw dT/dt samples
        # plus the time accumulated since the last accepted sample (so a fast
        # debounced recompute holds the trend instead of amplifying noise).
        self._trend_filtered: float = 0.0
        self._trend_pending_dt_s: float = 0.0
        # Integrator seasonal hygiene (S2, 2026-07-09): the last mode the PI
        # actually ran in, and the accumulated inactive time.
        self._last_pid_mode: Mode | None = None
        self._inactive_s: float = 0.0
        self._last_valve_pct: float = config.valve_floor_pct
        self._seeded: bool = False
        # Fast-source state machine (amendment 2026-07-09, C6): the DIRECTION is
        # part of the state — OFF / HEATING / COOLING. A HEATING<->COOLING flip
        # is only reachable through OFF with the full min-OFF dwell, and a hold
        # (min-ON not yet elapsed) re-emits the REMEMBERED direction, never a
        # freshly computed one. Indoor units may share a multisplit outdoor
        # unit, so mixing directions across a 2-second recompute is forbidden.
        self._fast_state: FastSourceMode = FastSourceMode.OFF
        self._fast_timer_s: float = _INITIAL_FAST_TIMER_S
        # Physical-feedback bookkeeping (S4): the first observed
        # ``fast_source_on`` wins over the cold machine (conservative timer
        # seed); later divergence raises the ``fast_source_mismatch`` flag.
        self._fast_synced: bool = False
        self._fast_mismatch: bool = False
        self._prev_fast_cmd_on: bool | None = None
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

        # -- Step 0: reconcile the fast-source machine with the physical unit
        # (S4, 2026-07-09): on the FIRST observed feedback the physical state
        # wins and the dwell timer is seeded conservatively; afterwards a
        # divergence only raises a report flag.
        self._sync_fast_state(inputs)

        # Fast-source dwell clock: dt accumulates here EXACTLY ONCE per step
        # (fix 2026-07-10); the decision helpers below only RESET the timer on
        # ON<->OFF edges. Previously every fast-source decision added dt itself
        # and a safety override in the same step added it AGAIN, so the min-OFF
        # wait under an active S1 elapsed twice as fast as wall-clock time.
        self._fast_timer_s += dt_seconds

        # -- Step 1: missing room temperature -> safe degrade ---------------
        t_room = inputs.room_temperature_c
        if t_room is None:
            degraded = self._apply_safety(
                inputs, self._safe_degrade(inputs.mode, dt_seconds)
            )
            return self._finalize(inputs, degraded)

        # A live-sensor step has run: future safe-degrade holds the real last
        # commanded valve rather than the mode-aware cold-start default.
        self._seeded = True
        setpoint = inputs.setpoint_c
        mode = inputs.mode
        error_c = setpoint - t_room  # report uses the heating sign convention

        # -- Step 4: filtered trend (dT_room/dt), 0 on first call -----------
        # S10 (2026-07-09): raw samples are taken only once at least
        # _TREND_MIN_DT_S has accumulated (a 2 s recompute HOLDS the trend)
        # and are smoothed by a ~15 min EMA before the kt damping sees them.
        if self._prev_t_room is None:
            self._trend_filtered = 0.0
            self._trend_pending_dt_s = 0.0
            self._prev_t_room = t_room
        else:
            self._trend_pending_dt_s += dt_seconds
            if self._trend_pending_dt_s >= _TREND_MIN_DT_S:
                dt_hours = self._trend_pending_dt_s / _SECONDS_PER_HOUR
                raw_trend = (t_room - self._prev_t_room) / dt_hours
                alpha = 1.0 - math.exp(-self._trend_pending_dt_s / _TREND_FILTER_TAU_S)
                self._trend_filtered += alpha * (raw_trend - self._trend_filtered)
                self._prev_t_room = t_room
                self._trend_pending_dt_s = 0.0
        trend = self._trend_filtered

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
                dt_seconds=dt_seconds,
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
        keep the frozen result immutable. Also stamps the additive
        ``fast_source_mismatch`` flag (S4) when the physical fast-source
        feedback disagreed with the previous cycle's command, and records this
        cycle's commanded on-state for the next comparison.

        Args:
            inputs: The room's raw inputs for this cycle (for the dew-reason
                classification).
            result: The post-safety computed outputs.

        Returns:
            The result with ``dew_excluded_reason`` and ``fast_dwell_remaining_s``
            filled in.
        """
        flags = result.report.flags
        if self._fast_mismatch:
            flags = tuple(dict.fromkeys((*flags, "fast_source_mismatch")))
        report = replace(
            result.report,
            dew_excluded_reason=classify_dew_eligibility(inputs),
            fast_dwell_remaining_s=self._fast_dwell_remaining_s,
            flags=flags,
        )
        self._prev_fast_cmd_on = result.fast_source.on
        return replace(result, report=report)

    def _sync_fast_state(self, inputs: RoomInputs) -> None:
        """Reconcile the fast-source machine with the physical unit (S4).

        On the FIRST cycle that carries a physical ``fast_source_on`` feedback
        AND the machine has not emitted any command yet
        (``_prev_fast_cmd_on is None``), the physical state wins: a running
        unit is adopted as ON (direction follows the global mode; COOLING is
        adopted only for a SPLIT — a HEATER can never cool), a stopped unit as
        OFF, and in BOTH cases the dwell timer is re-seeded to 0 so a full
        min-ON/min-OFF must elapse before the machine may change state — a
        restart/reload (every tuning change!) can therefore never short-cycle
        a compressor.

        If the machine already emitted commands before the first feedback
        arrived (feedback entity unavailable at engagement, appearing cycles
        later with a possibly stale reading), the late first reading must NOT
        overwrite the machine — it is treated like a regular settling cycle
        and at most raises the mismatch flag.

        On later cycles a feedback that disagrees with the PREVIOUS cycle's
        emitted command (one full cycle of settling allowance) only sets the
        ``fast_source_mismatch`` flag; the machine stays the owner and the
        adapter's periodic re-assert converges the hardware back.

        Args:
            inputs: The room's raw inputs for this cycle.
        """
        self._fast_mismatch = False
        if inputs.fast_source_kind is FastSourceKind.NONE:
            return
        physical = inputs.fast_source_on
        if physical is None:
            return
        if not self._fast_synced:
            self._fast_synced = True
            if self._prev_fast_cmd_on is None:
                # No command emitted yet — adopt the physical state.
                if physical and self._fast_state is FastSourceMode.OFF:
                    self._fast_state = (
                        FastSourceMode.COOLING
                        if (
                            inputs.mode is Mode.COOLING
                            and inputs.fast_source_kind is FastSourceKind.SPLIT
                        )
                        else FastSourceMode.HEATING
                    )
                elif not physical:
                    self._fast_state = FastSourceMode.OFF
                # Conservative seed: a full dwell from now, whatever the state.
                self._fast_timer_s = 0.0
                return
            # The machine already owns the unit; a late first feedback falls
            # through to the regular mismatch check below.
        if (
            self._prev_fast_cmd_on is not None
            and physical is not self._prev_fast_cmd_on
        ):
            self._fast_mismatch = True

    def reset(self) -> None:
        """Clear all internal state (PID, trend, held valve, fast timers)."""
        self._pid.reset()
        self._prev_t_room = None
        self._trend_filtered = 0.0
        self._trend_pending_dt_s = 0.0
        self._last_pid_mode = None
        self._inactive_s = 0.0
        self._last_valve_pct = self._config.valve_floor_pct
        self._seeded = False
        self._fast_state = FastSourceMode.OFF
        self._fast_timer_s = _INITIAL_FAST_TIMER_S
        self._fast_synced = False
        self._fast_mismatch = False
        self._prev_fast_cmd_on = None
        self._fast_dwell_remaining_s = None
        self._safety.reset()

    # -- internal: result builders -----------------------------------------

    def _safe_degrade(self, mode: Mode, dt_seconds: float) -> RoomOutputs:
        """Build the safe-degraded result when the room sensor is lost.

        HEATING: holds the last valve position (freeze — warm water bounded by
        the heat-pump curve and S1 is harmless to hold), forces the fast source
        OFF and flags ``"sensor_lost"``. The PID is not run. On a cold start
        (no live step yet) the heating hold value is ``config.valve_floor_pct``.

        COOLING (amendment 2026-07-09, see BUILD_SPEC step 1): the valve is
        driven to **0**, never frozen open. A lost temperature sensor breaks
        BOTH condensation defences at once — the room drops out of the global
        safe dew-point maximum (``no_temperature``) and the local S2 throttle
        cannot run without ``T_room`` — so a frozen-open valve would pass
        unprotected chilled water indefinitely. TRANSITIONAL/OFF also park at 0
        (their valves are parked by mode anyway). The heating hold memory
        (``_last_valve_pct``) is left untouched by the cooling branch.

        Args:
            mode: The room's current operating mode, used to pick the safe
                valve position.
            dt_seconds: Elapsed time [s]; accumulated into the integrator
                inactivity clock (the fast dwell timer is advanced once per
                step by :meth:`step`; fast-F6, 2026-07-09 still holds — a
                recovered sensor does not restart the min-OFF wait).

        Returns:
            A safe-degraded :class:`~tortoise_ufh.models.RoomOutputs`.
        """
        fast = self._force_fast_off()
        if mode is Mode.HEATING:
            if self._seeded:
                valve = self._last_valve_pct
            else:
                valve = self._config.valve_floor_pct
            self._last_valve_pct = valve
            explanation = (
                "Utrata czujnika pokoju: zawor trzyma ostatnia pozycje "
                f"{valve:.0f}%, split OFF."
            )
        else:
            # COOLING / TRANSITIONAL / OFF: close the valve — freeze-open in
            # cooling would bypass both condensation defences (dew-F1).
            valve = 0.0
            explanation = "Utrata czujnika pokoju: zawor 0% (chlodzenie), split OFF."
        # Drop the stale reference so the first recovered cycle takes the
        # trend==0 branch instead of dividing a multi-cycle delta by one dt;
        # the filtered trend restarts from 0 too (a gap invalidates it).
        self._prev_t_room = None
        self._trend_filtered = 0.0
        self._trend_pending_dt_s = 0.0
        # A long sensor-lost stretch counts as inactivity for the integrator
        # decay (S2): the object drifts while nobody integrates honestly.
        self._note_inactive(dt_seconds)
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
            explanation=explanation,
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
        dt_seconds: float,
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
            dt_seconds: Elapsed time [s], accumulated into the integrator
                inactivity clock (the fast dwell timer advances once per step
                in :meth:`step`; fast-F6: an OFF stretch counts toward the
                min-OFF wait).

        Returns:
            The passive :class:`~tortoise_ufh.models.RoomOutputs`.
        """
        fast = self._force_fast_off()
        self._last_valve_pct = valve
        self._note_inactive(dt_seconds)
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

    def _note_inactive(self, dt_seconds: float) -> None:
        """Accumulate inactive time and decay a stale integrator (S2).

        Called on every cycle in which the PI loop does NOT run (OFF,
        TRANSITIONAL, cooling opt-out, sensor lost). Once the accumulated
        inactivity exceeds :data:`_INTEGRATOR_DECAY_AFTER_S` the integrator is
        cleared, so e.g. the heating season's accumulated integral never
        becomes the first valve command of the cooling season.

        Args:
            dt_seconds: Elapsed time this cycle [s].
        """
        self._inactive_s += dt_seconds
        if self._inactive_s >= _INTEGRATOR_DECAY_AFTER_S and (
            self._pid.integral != 0.0
        ):
            self._pid.reset()

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

        The fast source runs bidirectionally, driven by the three-state
        direction machine (C6): an idle split engages in the direction whose
        demand exceeds ``boost_offset_c``; a running split keeps its REMEMBERED
        direction with ``target = setpoint`` (its own regulation holds the room
        AT the setpoint — no seasonal bias, S12 2026-07-09) and releases only
        once the room crosses the far edge of the comfort band (free gains
        carry it), through the min-ON dwell. A HEATING<->COOLING flip is only
        reachable through OFF with the full min-OFF dwell. The valve is parked.

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
        self._note_inactive(dt_seconds)

        if inputs.fast_source_kind is FastSourceKind.NONE:
            fast = self._force_fast_off()
            direction = "brak"
        else:
            # Bidirectional demands: heating below setpoint, cooling above.
            heating_demand = error_c  # setpoint - t_room
            cooling_demand = -error_c
            engaged = self._fast_state
            heater_cannot_cool = False
            want_on = False
            fs_mode = FastSourceMode.HEATING
            direction = "brak"
            if engaged is FastSourceMode.HEATING:
                # Running heater: release only when the room crosses the FAR
                # edge of the comfort band (setpoint + deadband) — while ON the
                # split self-regulates at the setpoint, so there is no bias.
                fs_mode = FastSourceMode.HEATING
                want_on = heating_demand > -cfg.deadband_c
                direction = "grzanie" if want_on else "brak"
            elif engaged is FastSourceMode.COOLING:
                fs_mode = FastSourceMode.COOLING
                want_on = cooling_demand > -cfg.deadband_c
                direction = "chlodzenie" if want_on else "brak"
            elif heating_demand > cfg.boost_offset_c:
                fs_mode = FastSourceMode.HEATING
                want_on = True
                direction = "grzanie"
            elif cooling_demand > cfg.boost_offset_c:
                if inputs.fast_source_kind is FastSourceKind.HEATER:
                    # A heater cannot cool: stay OFF.
                    heater_cannot_cool = True
                    direction = "brak (grzejnik nie chlodzi)"
                    if "fast_source_cannot_cool" not in flags:
                        flags.append("fast_source_cannot_cool")
                else:
                    fs_mode = FastSourceMode.COOLING
                    want_on = True
                    direction = "chlodzenie"
            if heater_cannot_cool:
                fast = self._force_fast_off()
            else:
                # S12: transitional target is exactly the setpoint in BOTH
                # directions — the split is the only source here and its own
                # thermostat holding the room at the setpoint removes the old
                # [setpoint-1.0, setpoint-0.3] bias band.
                fast = self._decide_fast_source(
                    want_on=want_on,
                    fs_mode=fs_mode,
                    target_heating=inputs.setpoint_c,
                    target_cooling=inputs.setpoint_c,
                    flags=flags,
                )
                if fast.on:
                    # A min-ON hold re-emits the REMEMBERED direction; make the
                    # report text follow the actually emitted command.
                    direction = (
                        "grzanie"
                        if fast.mode is FastSourceMode.HEATING
                        else "chlodzenie"
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
            self._note_inactive(dt_seconds)
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

        # Integrator seasonal hygiene (S2, 2026-07-09): a HEATING<->COOLING
        # transition resets the integrator — the error sign convention flips,
        # so the accumulated integral of one season is anti-knowledge for the
        # other. The PI loop is active this cycle: clear the inactivity clock.
        if self._last_pid_mode is not None and mode is not self._last_pid_mode:
            self._pid.reset()
        self._last_pid_mode = mode
        self._inactive_s = 0.0

        # -- Step 6: deadband -> reduce magnitude, keep sign ----------------
        error_db = math.copysign(max(0.0, abs(error) - cfg.deadband_c), error)

        # -- Step 7: integrator freeze (DHW / defrost, S2-throttled cooling) -
        # The local dew throttle (step 12) multiplies the valve AFTER the PID,
        # invisibly to the back-calculation anti-windup; computing the factor
        # here (it depends only on inputs) and freezing the integrator while
        # the throttle is active (< 1.0) closes that windup hole (S1/dew-F2,
        # 2026-07-09): hours of throttled cooling no longer bank an integral
        # that would slam the valve open the moment the humidity clears.
        dew_factor = 1.0
        if mode is Mode.COOLING:
            dew_factor = self._cooling_throttle(inputs, room_dew, flags)
        freeze = inputs.hp_active_for_ufh is False or dew_factor < 1.0

        # -- Step 8: PI compute (integral on the REAL elapsed dt) ------------
        pid_out = self._pid.compute(
            error_db, dt_seconds=dt_seconds, freeze_integrator=freeze
        )
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
        # Unconditional for any COOLING room whose valve can open (the factor
        # itself was computed in step 7). Rooms excluded from cooling returned
        # above with the valve parked at 0, so the throttle is never the thing
        # skipped for an unprotected room.
        low_before_throttle = valve <= 0.0
        if mode is Mode.COOLING:
            valve *= dew_factor

        # -- Step 13: clamp + saturation ------------------------------------
        # A zero produced by the S2 throttle is NOT saturation of the control
        # law (control-F8, 2026-07-09): saturated stays the "PI hit a bound"
        # signal, dew_throttle_factor carries the condensation story.
        saturated = valve >= 100.0 or (
            valve <= 0.0 and (dew_factor >= 1.0 or low_before_throttle)
        )
        valve = max(0.0, min(100.0, valve))
        self._last_valve_pct = valve

        # -- Step 14: fast-source coordination (anti priority-inversion) ----
        fast = self._coordinate_fast_source(
            inputs=inputs,
            error_c=error_c,
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
        temperature is available. All three shaping constants live in
        :class:`~tortoise_ufh.core.config.ControllerConfig`
        (control-F6, 2026-07-09).

        Args:
            mode: The active mode (HEATING or COOLING).
            t_out: Outdoor temperature [degC] or ``None``.

        Returns:
            Feedforward contribution [%] in ``[0, config.ff_max_pct]``.
        """
        cfg = self._config
        if not cfg.outdoor_ff_enabled or t_out is None:
            return 0.0
        if mode is Mode.HEATING:
            deviation = max(0.0, cfg.ff_neutral_c - t_out)
        else:
            deviation = max(0.0, t_out - cfg.ff_neutral_c)
        return min(cfg.ff_max_pct, cfg.ff_gain_pct_per_k * deviation)

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

    def _coordinate_fast_source(
        self,
        *,
        inputs: RoomInputs,
        error_c: float,
        flags: list[str],
    ) -> FastSourceCommand:
        """Decide the fast-source command for HEATING/COOLING (step 14).

        Enforces anti priority-inversion: the valve is the base and is never
        lowered because of the split; the split only *adds* boost above the
        boost offset and releases once inside the comfort (deadband) band.
        Honours the three-state direction machine (C6): a global mode change
        while the split is held by min-ON keeps re-emitting the REMEMBERED
        direction and can only reverse through OFF with the full min-OFF. The
        boost target is offset by :data:`FAST_TARGET_OFFSET_K` in the boost
        direction (S12) so the split's ceiling-mounted sensor does not throttle
        the unit before the boost is delivered.

        Args:
            inputs: The room's raw inputs.
            error_c: ``setpoint - room_temp`` [K] (heating convention).
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
        # A held-on HEATER in cooling mode is switched off immediately (a
        # resistive heater has no compressor to protect).
        if fs_mode is FastSourceMode.COOLING and (
            inputs.fast_source_kind is FastSourceKind.HEATER
        ):
            if "fast_source_cannot_cool" not in flags:
                flags.append("fast_source_cannot_cool")
            return self._force_fast_off()

        want_on = self._want_fast(demand, engaged=self._fast_state is fs_mode)
        return self._decide_fast_source(
            want_on=want_on,
            fs_mode=fs_mode,
            target_heating=inputs.setpoint_c + FAST_TARGET_OFFSET_K,
            target_cooling=inputs.setpoint_c - FAST_TARGET_OFFSET_K,
            flags=flags,
        )

    def _want_fast(self, demand: float, *, engaged: bool) -> bool:
        """Hysteretic engage/release decision for the fast source.

        Engages when ``demand`` exceeds the boost offset; once engaged in this
        direction, stays engaged until ``demand`` falls back inside the
        deadband (comfort band).

        Args:
            demand: Actuation demand [K] in the mode's needed direction
                (positive means "boost needed").
            engaged: Whether the machine is currently running in THIS
                direction (release threshold applies) rather than idle or
                running the other way (engage threshold applies).

        Returns:
            ``True`` if the fast source should run this cycle (pre-timer).
        """
        cfg = self._config
        if engaged:
            return demand > cfg.deadband_c
        return demand > cfg.boost_offset_c

    def _decide_fast_source(
        self,
        *,
        want_on: bool,
        fs_mode: FastSourceMode,
        target_heating: float,
        target_cooling: float,
        flags: list[str],
    ) -> FastSourceCommand:
        """Advance the three-state fast-source machine (C6, 2026-07-09).

        The machine state is OFF / HEATING / COOLING. The dwell clock is
        advanced once per step by :meth:`step` (fix 2026-07-10); this method
        only resets it on state transitions:

        * ``OFF -> fs_mode`` when ``want_on`` and the min-OFF dwell elapsed.
        * ``running -> OFF`` when the request is OFF **or a different
          direction** and the min-ON dwell elapsed — a HEATING<->COOLING flip
          is only reachable through OFF with the full min-OFF dwell (indoor
          units may share a multisplit outdoor unit).
        * A blocked request flags ``"fast_source_min_runtime"`` and the
          machine re-emits its REMEMBERED direction (never a freshly computed
          one).

        Args:
            want_on: Desired ON/OFF state before the timer gate.
            fs_mode: Direction requested when ``want_on`` is ``True``.
            target_heating: Split target [degC] emitted while HEATING.
            target_cooling: Split target [degC] emitted while COOLING.
            flags: Mutable flag list (appended in place).

        Returns:
            The gated :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        cfg = self._config
        current = self._fast_state
        if current is FastSourceMode.OFF:
            if want_on:
                if self._fast_timer_s >= cfg.fast_min_off_minutes * 60.0:
                    self._fast_state = fs_mode
                    self._fast_timer_s = 0.0
                elif "fast_source_min_runtime" not in flags:
                    flags.append("fast_source_min_runtime")
        else:
            keep_running = want_on and fs_mode is current
            if not keep_running:
                # OFF requested, or a direction flip: both mean "stop first".
                if self._fast_timer_s >= cfg.fast_min_on_minutes * 60.0:
                    self._fast_state = FastSourceMode.OFF
                    self._fast_timer_s = 0.0
                elif "fast_source_min_runtime" not in flags:
                    flags.append("fast_source_min_runtime")
        # Remaining lock on the CURRENT state: min ON while running (cannot turn
        # off yet), min OFF while idle (cannot turn on yet). None once elapsed.
        state = self._fast_state
        min_lock_min = (
            cfg.fast_min_off_minutes
            if state is FastSourceMode.OFF
            else cfg.fast_min_on_minutes
        )
        remaining = min_lock_min * 60.0 - self._fast_timer_s
        self._fast_dwell_remaining_s = remaining if remaining > 0.0 else None
        if state is FastSourceMode.HEATING:
            return FastSourceCommand(
                on=True, mode=state, target_temperature_c=target_heating
            )
        if state is FastSourceMode.COOLING:
            return FastSourceCommand(
                on=True, mode=state, target_temperature_c=target_cooling
            )
        return FastSourceCommand(
            on=False, mode=FastSourceMode.OFF, target_temperature_c=None
        )

    def _force_fast_on(
        self, fs_mode: FastSourceMode, target: float
    ) -> FastSourceCommand:
        """Force the fast source ON immediately (safety S3/S4 override).

        Unlike building the command directly, this keeps the fast-source state
        machine in sync (S5, 2026-07-09): the machine state is set to the
        commanded direction and the dwell timer restarted on any state change,
        so releasing the safety override later hands a *running* machine back
        to the normal min-ON dwell logic instead of instantly stopping a
        compressor that just started. This is the ONE deliberate exception to
        the change-direction-through-OFF rule: a hard S3/S4 emergency outranks
        compressor hygiene (and S3-in-summer / S4-in-winter cannot co-occur
        with the opposite direction in practice).

        Args:
            fs_mode: Direction to command (HEATING or COOLING).
            target: Room target temperature [degC] for the split.

        Returns:
            An ON :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        if self._fast_state is not fs_mode:
            self._fast_state = fs_mode
            self._fast_timer_s = 0.0
        remaining = self._config.fast_min_on_minutes * 60.0 - self._fast_timer_s
        self._fast_dwell_remaining_s = remaining if remaining > 0.0 else None
        return FastSourceCommand(on=True, mode=fs_mode, target_temperature_c=target)

    def _force_fast_off(self) -> FastSourceCommand:
        """Force the fast source OFF immediately (safety / OFF mode).

        Bypasses the min ON timer because a lost sensor or an explicit OFF is a
        safety condition. Resets the dwell timer on the ON->OFF edge so
        re-engaging respects the min OFF dwell afterwards; on subsequent
        already-OFF cycles the timer keeps growing via the single per-step
        accumulation in :meth:`step` (fast-F6, 2026-07-09; single-accumulation
        fix 2026-07-10), so a long sensor-lost or OFF stretch counts toward the
        min-OFF wait instead of restarting it on recovery.

        Returns:
            An OFF :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        if self._fast_state is not FastSourceMode.OFF:
            self._fast_state = FastSourceMode.OFF
            self._fast_timer_s = 0.0
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
        The fast dwell clock is NOT advanced here — :meth:`step` accumulates
        dt exactly once per cycle (fix 2026-07-10); a forced OFF/ON below only
        resets the timer on an actual state edge.

        The S5 watchdog age comes from ``inputs.last_update_age_minutes``
        (S6, 2026-07-09): the adapter tracks how long each room has gone
        without fresh data and the S5 rule finally has a live input instead of
        a hard-coded 0. The adapter's own building-level watchdog stays
        report-only; S5 is the per-room actuator-side escalation.

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
            last_update_age_minutes=inputs.last_update_age_minutes,
        )
        active = [r for r in self._safety.evaluate(snapshot) if r.triggered]
        if not active:
            return result

        # Amendment 2026-07-09 (S7): the water-side action ("close the valve")
        # and the air-side action ("run/stop the fast source") are decided
        # INDEPENDENTLY across all active rules. S1 floor-overheat closing the
        # valve must not silence an S3 emergency-heat split — the split is the
        # only remaining heat source for a freezing room with overheated water.
        actions = {r.action for r in active if r.action is not None}
        flags = tuple(
            dict.fromkeys((*result.report.flags, *(r.rule.name for r in active)))
        )
        target = inputs.setpoint_c
        has_split = inputs.fast_source_kind is not FastSourceKind.NONE

        close_valve = SafetyAction.CLOSE_VALVE in actions
        if SafetyAction.EMERGENCY_HEAT in actions:
            # S3: open the floor fully unless a CLOSE_VALVE rule (S1/S2) also
            # holds; the air-side heat boost runs regardless of the valve.
            valve = 0.0 if close_valve else 100.0
            fast = (
                self._force_fast_on(FastSourceMode.HEATING, target)
                if has_split
                else self._force_fast_off()
            )
        elif SafetyAction.EMERGENCY_COOL in actions:
            valve = 0.0  # air-side only; never open the floor without S2 cover
            # Only a SPLIT can cool; a HEATER must never be told to cool.
            if inputs.fast_source_kind is FastSourceKind.SPLIT:
                fast = self._force_fast_on(FastSourceMode.COOLING, target)
            else:
                fast = self._force_fast_off()
                if inputs.fast_source_kind is FastSourceKind.HEATER:
                    flags = tuple(dict.fromkeys((*flags, "fast_source_cannot_cool")))
        elif close_valve:
            # CLOSE_VALVE (S1/S2): park the valve, release the fast source.
            valve = 0.0
            fast = self._force_fast_off()
        else:
            # FALLBACK_HP_CURVE (S5 watchdog) alone: NEUTRAL position, not a
            # hard close (amendment 2026-07-09, S6) — "defer to the heat-pump
            # native curve" means a passive baseline: the heating valve floor
            # in HEATING (keeps the house tempered by the HP curve), 0 in
            # COOLING (chilled water without fresh data is never safe).
            valve = self._config.valve_floor_pct if inputs.mode is Mode.HEATING else 0.0
            fast = self._force_fast_off()

        # Amendment 2026-07-09 (S5): do NOT overwrite _last_valve_pct with the
        # emergency 0/100 — the sensor-lost freeze must hold the last position
        # of HEALTHY regulation (already stored by the pre-safety path), not a
        # safety extreme that may outlive the fault by days.
        saturated = valve <= 0.0 or valve >= 100.0
        # Fix 2026-07-10: prepend the safety banner instead of REPLACING the
        # explanation — a safety cycle keeps telling the operator what the
        # underlying regulation was doing (capped against pathological growth).
        explanation = (
            f"Bezpieczenstwo {active[0].rule.name}: zawor {valve:.0f}%. | "
            f"{result.report.explanation}"
        )
        if len(explanation) > _SAFETY_EXPLANATION_MAX_LEN:
            explanation = explanation[: _SAFETY_EXPLANATION_MAX_LEN - 3] + "..."
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
        sensor_lost = sum(
            1 for out in rooms.values() if "sensor_lost" in out.report.flags
        )
        return BuildingOutputs(
            rooms=rooms,
            global_safe_dew_point_c=global_dew,
            sensor_lost_rooms=sensor_lost,
        )

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
