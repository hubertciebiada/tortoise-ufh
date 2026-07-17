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
from .const import DEW_MARGIN_DEFAULT_K
from .dew_point import cooling_throttle_factor, dew_point
from .fast_source import (  # noqa: F401
    DRY_HYSTERESIS_K,
    FAST_TARGET_OFFSET_K,
    FastSourceMachine,
    direction_of,
)
from .flow_watchdog import (
    CIRCULATION_DELTA_K,
    GLOBAL_SUPPLY_MARGIN_K,
    ActuationSelfTest,
    FlowWatchdog,
)
from .models import (
    BuildingOutputs,
    FastSourceCommand,
    FastSourceKind,
    FastSourceMode,
    LoopInput,
    Mode,
    RoomInputs,
    RoomOutputs,
    RoomReport,
)
from .pid import PIDController
from .safety import SafetyAction, SafetyEvaluator, SensorSnapshot
from .trend import TrendEstimator

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


def _passive_report(
    *,
    error_c: float | None,
    trend: float | None,
    room_dew: float | None,
    i_term: float,
    raw_valve_pct: float,
    saturated: bool,
    flags: tuple[str, ...],
    explanation: str,
    room_temperature_c: float | None,
    valve_floor_applied: bool = False,
) -> RoomReport:
    """Build a passive-path :class:`~tortoise_ufh.models.RoomReport`.

    Shared by every non-PI result builder (safe degrade, OFF, TRANSITIONAL,
    cooling opt-out, unknown room, controller error): those reports differ
    only in the fields exposed here, while the PI-inactive commons are baked
    in — ``p_term = trend_term = feedforward_term = 0.0``,
    ``dew_throttle_factor = 1.0`` and ``integrator_frozen = True``. The active
    HEATING/COOLING path builds its report inline (every field is live there).

    Args:
        error_c: ``setpoint - room_temp`` [K] or ``None`` without a sensor.
        trend: Filtered trend [K/h] or ``None`` without a sensor.
        room_dew: Room dew point [degC] or ``None``.
        i_term: The (frozen) PID integral surfaced in the report [%].
        raw_valve_pct: The parked/held valve position [%].
        saturated: The passive path's saturation semantics (each caller keeps
            its historical value).
        flags: Report flags.
        explanation: Report text.
        room_temperature_c: Measured room temperature [degC] or ``None``.
        valve_floor_applied: Whether the heating valve floor was applied
            (always ``False`` on today's passive paths).

    Returns:
        The assembled :class:`~tortoise_ufh.models.RoomReport`.
    """
    return RoomReport(
        error_c=error_c,
        trend_c_per_h=trend,
        room_dew_point_c=room_dew,
        p_term=0.0,
        i_term=i_term,
        trend_term=0.0,
        feedforward_term=0.0,
        raw_valve_pct=raw_valve_pct,
        valve_floor_applied=valve_floor_applied,
        saturated=saturated,
        dew_throttle_factor=1.0,
        integrator_frozen=True,
        flags=flags,
        explanation=explanation,
        room_temperature_c=room_temperature_c,
    )


# --- Module constants -------------------------------------------------------

# The trend constants (min sample dt, EMA tau) moved to
# :mod:`tortoise_ufh.trend` with the TrendEstimator extraction (2026-07-10).

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

# ``FAST_TARGET_OFFSET_K`` and the fast-source dwell/direction machinery moved
# to :mod:`tortoise_ufh.fast_source` (2026-07-10); the constant is re-exported
# above so existing importers keep working (BUILD_SPEC §2 note). Since
# 2026-07-13 it is only the documented DEFAULT of the
# ``ControllerConfig.fast_target_offset_k`` knob, which the controller reads
# instead.

GLOBAL_SAFE_DEW_MARGIN_K: float = DEW_MARGIN_DEFAULT_K
"""Safety margin [K] added on top of ``max_i(T_dew_i)`` for the global sensor.

The system's ONE working condensation margin (2026-07-12, K6): the local
per-room throttle ramps BELOW this gap instead of stacking a second margin on
top of it (see :func:`~tortoise_ufh.dew_point.cooling_throttle_factor`).

Bound to the shared low-level constant :data:`~tortoise_ufh.const.
DEW_MARGIN_DEFAULT_K` (2026-07-15, issue #7) so this margin and the cooling
setpoint-flicker's :data:`~tortoise_ufh.hp_link.FLICKER_DEW_RESERVE_K` (which
also references it) can never drift apart — the flicker's pulse floor is
computed as ``safe_dew − reserve`` and MUST land exactly on the raw worst-room
dew point.
"""

_INTEGRATOR_UNWIND_FACTOR: float = 8.0
"""Asymmetric integral-unwind multiplier (K1, 2026-07-12).

While the deadbanded error opposes the accumulated integral (e.g. a heating
integral left over after a night-setback lowered the setpoint), the PID
discharges the integral this many times faster than it accumulated. Measured
on the twin (23 -> 21 setpoint drop, well_insulated @ 300 s): with the plain
ki the saturated integral kept the valve actively heating an already-too-warm
room for 17.4 h (642 %*h valve integral, trough -0.54 K, back in the band
48.8 h after the drop); the bumpless -kp*dK re-seed alone cut that to 13 h,
and the combination with this 8x unwind brings the valve to ~0 within 0.8 h
(23 %*h), returns to the band FASTER (35 h) with a 100 % settled tail, at the
cost of a deeper coast-down trough (-0.92 K — the price of not heating an
overheated slab). The accelerated step only ever pulls the integral TOWARD
zero (never destabilises the loop), and inside the comfort band the
deadbanded error is 0, so steady-state behaviour is untouched.
"""

_STALE_RH_DEW_PAD_K: float = 1.0
"""Full-scale dew-point pad [K] for a STALE RH reading (K7, 2026-07-12;
linearised 2026-07-12, D5): the adapter holds an RH whose sensor last
reported 60-120 min ago instead of dropping it to ``None`` and reports the
staleness as a linear fraction (``RoomInputs.humidity_stale_frac`` — 0 fresh,
1 at 120 min). The cooling layers price the staleness in by computing every
protective dew point ``frac`` times this pad higher, so the protection grows
continuously with the age instead of jumping at the 60-min edge. Report flag:
``"rh_stale_gated"`` whenever ``frac > 0``."""


_GROUP_CHALLENGER_HYSTERESIS_K: float = 0.5
"""Incumbent hysteresis of the multisplit group arbiter [K] (K2, 2026-07-12).

In a direction conflict the challenging direction takes the group over only
when its best comfort-band excess exceeds the incumbent direction's best
excess by MORE than this margin. Without it the winner was a bare
``max(band_excess)`` — two rooms with persistent opposite ~2 K demands plus
sensor noise (sigma = 0.05 K) ping-ponged the shared outdoor unit through
15 direction reversals in 2.5 h at zero dwells (7 at the default 10/10 min);
with the hysteresis the first winner holds unless the other side genuinely
outgrows it. Deliberately a fixed constant, not a config knob."""


class RoomController:
    """Independent per-room closed-loop UFH controller (the black box).

    One instance controls exactly one room / zone. It is stateful: it owns a
    :class:`~tortoise_ufh.pid.PIDController`, a
    :class:`~tortoise_ufh.trend.TrendEstimator` (the filtered dT/dt), the last
    commanded valve position (for safe-degrade holding), and the
    :class:`~tortoise_ufh.fast_source.FastSourceMachine` with its min ON/OFF
    dwell timer. Call :meth:`step` once per control cycle and :meth:`reset` to
    clear all internal state.

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
            unwind_factor=_INTEGRATOR_UNWIND_FACTOR,
        )
        # Safe-degrade default: until at least one live step has established a
        # real valve position (``_seeded``), the cold-start hold value is
        # mode-aware (heating floor for HEATING, 0 for COOLING/OFF) per
        # BUILD_SPEC step 1, rather than the heating floor unconditionally.
        # Filtered trend estimator (S10, 2026-07-09): debounce-aware EMA of
        # the raw dT/dt samples, encapsulated in
        # :class:`~tortoise_ufh.trend.TrendEstimator` (2026-07-10).
        self._trend = TrendEstimator()
        # Integrator seasonal hygiene (S2, 2026-07-09): the last mode the PI
        # actually ran in, and the accumulated inactive time.
        self._last_pid_mode: Mode | None = None
        self._inactive_s: float = 0.0
        # Bumpless setpoint transfer (K1, 2026-07-12): the effective setpoint
        # the PI last ran with; a change between PID-active cycles re-seeds
        # the integral by kp * delta_error so the operating point moves WITH
        # the setpoint instead of discharging the difference at ki speed.
        # Cleared whenever the PID itself is reset.
        self._last_pid_setpoint_c: float | None = None
        self._last_valve_pct: float = config.valve_floor_pct
        self._seeded: bool = False
        # Cooling boost-hold (2026-07-13): while the split is ENGAGED in
        # cooling, the floor valve is held at ``_boost_hold_pct`` — the raw
        # (pre-throttle, clamped) valve of the cycle the split engaged —
        # instead of retreating to 0 as the split-cooled air drives the room
        # error toward zero (anti measurement-path inversion; see
        # :meth:`_active_result`). ``_last_raw_valve_pct`` carries the previous
        # active cycle's raw clamped valve so the engage edge can snapshot it.
        self._boost_hold_pct: float | None = None
        self._last_raw_valve_pct: float = 0.0
        # Fast-source state machine (amendment 2026-07-09, C6): the DIRECTION
        # is part of the state — OFF / HEATING / COOLING, with the min ON/OFF
        # dwell clock and the S4 physical-feedback bookkeeping. Encapsulated in
        # :class:`~tortoise_ufh.fast_source.FastSourceMachine` (2026-07-10);
        # this controller delegates and keeps only the mode->demand mapping.
        self._fast = FastSourceMachine(config)
        # Machine direction at the entry of the current step (K4, 2026-07-12):
        # the group arbiter treats only a unit that was ALREADY running before
        # this cycle as min-ON-pinned — a machine that engaged this very cycle
        # has not physically started and may be freely arbitrated away.
        self._fast_entry_state: FastSourceMode = FastSourceMode.OFF
        # Stateful hard-safety layer (S1..S5) with per-rule hysteresis, held
        # across cycles and applied as a post-processing override of the
        # computed outputs (PRD 8.7).
        self._safety = SafetyEvaluator()
        # S6 hydraulic no-flow watchdog (2026-07-13): per-loop window
        # machines over the supply/return probes — an independent physical
        # witness of actuation that never trusts the valve-entity feedback.
        self._flow = FlowWatchdog(config)
        # Manual actuation self-test (S6/C): deliberate 100 % excursion with
        # a displacement-based hydraulic verdict; see flow_watchdog.py.
        self._selftest = ActuationSelfTest()
        # The valve position the core actually EMITTED last cycle — the S6
        # watchdog's command reference. Deliberately distinct from
        # ``_last_valve_pct``: that one is NOT overwritten by the safety
        # override (the sensor-lost freeze must hold healthy regulation), so
        # during an S1/S2 episode it would hold a pre-safety value and fake
        # a no-flow alarm on a valve that is honestly commanded 0.
        self._last_emitted_valve_pct: float | None = None
        # Snapshot of the last step's mode/loops/dew factor, so the
        # out-of-band ``begin_actuation_test`` can validate its
        # preconditions without a wall clock or fresh inputs.
        self._last_seen_mode: Mode | None = None
        self._last_loops: tuple[LoopInput, ...] = ()
        self._last_dew_factor: float = 1.0

    # -- properties ---------------------------------------------------------

    @property
    def name(self) -> str:
        """Room identifier (read-only)."""
        return self._name

    @property
    def last_valve_pct(self) -> float:
        """Last commanded valve position [%] (read-only)."""
        return self._last_valve_pct

    @property
    def _fast_timer_s(self) -> float:
        """Fast-source dwell clock [s] (read-only delegate).

        Kept after the 2026-07-10 extraction of
        :class:`~tortoise_ufh.fast_source.FastSourceMachine` so existing
        white-box dwell tests keep observing the timer through the controller.
        """
        return self._fast.timer_s

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
        self._fast.sync(inputs)
        # Snapshot the direction the unit was ACTUALLY running in before any
        # of this cycle's decisions (K4): feeds fast_source_locked_on.
        self._fast_entry_state = self._fast.state

        # Fast-source dwell clock: dt accumulates here EXACTLY ONCE per step
        # (fix 2026-07-10); the decision helpers below only RESET the timer on
        # ON<->OFF edges. Previously every fast-source decision added dt itself
        # and a safety override in the same step added it AGAIN, so the min-OFF
        # wait under an active S1 elapsed twice as fast as wall-clock time.
        self._fast.tick(dt_seconds)

        # -- S6: hydraulic no-flow watchdog (2026-07-13) ---------------------
        # Updated at the START of the step (before the PID) with the valve
        # position the core EMITTED last cycle, so the integrator-freeze
        # verdict in step 7 is already current. Held while a self-test runs
        # (the deliberate excursion would corrupt the window references).
        self._flow.update(
            inputs,
            commanded_pct=self._last_emitted_valve_pct,
            dt_seconds=dt_seconds,
            paused=self._selftest.running,
        )
        self._last_seen_mode = inputs.mode
        self._last_loops = inputs.loops

        # Self-test abort conditions independent of the control path: a lost
        # room sensor or a mode change invalidates the measurement.
        if self._selftest.running and (
            inputs.room_temperature_c is None or inputs.mode is not self._selftest.mode
        ):
            self._abort_selftest()

        # -- Step 1: missing room temperature -> safe degrade ---------------
        t_room = inputs.room_temperature_c
        if t_room is None:
            degraded = self._apply_safety(
                inputs, self._safe_degrade(inputs.mode, dt_seconds)
            )
            return self._finalize(inputs, degraded, dt_seconds=dt_seconds)

        # A live-sensor step has run: future safe-degrade holds the real last
        # commanded valve rather than the mode-aware cold-start default.
        self._seeded = True
        setpoint = inputs.setpoint_c
        mode = inputs.mode
        error_c = setpoint - t_room  # report uses the heating sign convention

        # -- Step 4: filtered trend (dT_room/dt), 0 on first call -----------
        # S10 (2026-07-09): raw samples are taken only once at least
        # trend._TREND_MIN_DT_S has accumulated (a 2 s recompute HOLDS the
        # trend) and are smoothed by a ~15 min EMA before the kt damping sees
        # them (see :class:`~tortoise_ufh.trend.TrendEstimator`).
        trend = self._trend.update(t_room, dt_seconds)

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

        # Actuation self-test excursion (S6/C): the valve is overridden to
        # 100 % BEFORE the safety layer, so S1 (overheat) and S2
        # (condensation) always win over the test.
        if self._selftest.running and mode in (Mode.HEATING, Mode.COOLING):
            result = replace(result, valve_position_pct=100.0)
        result = self._apply_safety(inputs, result)
        # A safety override on top of a running test means the measurement is
        # meaningless (the valve is no longer at 100 %): abort, do not force.
        if self._selftest.running and (
            "s1_floor_overheat" in result.report.flags
            or "s2_condensation" in result.report.flags
        ):
            self._abort_selftest()
        return self._finalize(inputs, result, dt_seconds=dt_seconds)

    def _finalize(
        self, inputs: RoomInputs, result: RoomOutputs, *, dt_seconds: float
    ) -> RoomOutputs:
        """Stamp the additive report fields and advance the self-test.

        Runs after the safety override so the dwell value reflects the final
        fast-source state (a safety force-off clears it). Uses ``replace`` to
        keep the frozen result immutable. Also stamps the additive
        ``fast_source_mismatch`` flag (S4) when the physical fast-source
        feedback disagreed with the previous cycle's command, and records this
        cycle's commanded on-state for the next comparison.

        S6 (2026-07-13): merges the ``loop_no_flow`` / ``actuation_test_*``
        flags, stamps the per-loop flow statuses and the self-test payload,
        advances a running self-test by ``dt_seconds`` (restarting the
        watchdog windows when the test just ended — the excursion moved the
        loop temperatures), and records the FINAL emitted valve position as
        the watchdog's next command reference.

        Args:
            inputs: The room's raw inputs for this cycle (for the dew-reason
                classification and the self-test's current probe values).
            result: The post-safety computed outputs.
            dt_seconds: Elapsed time this cycle [s] (self-test progress).

        Returns:
            The result with every additive report field filled in.
        """
        if self._selftest.running:
            self._selftest.advance(
                dt_seconds,
                inputs.loops,
                epsilon_k=self._config.flow_epsilon_k,
            )
            if not self._selftest.running:
                # The excursion moved the loop temperatures: pre-test window
                # references are stale, so S6 re-arms from scratch.
                self._flow.restart_windows()
        flags = result.report.flags
        if self._fast.mismatch:
            flags = tuple(dict.fromkeys((*flags, "fast_source_mismatch")))
        extra: list[str] = []
        if self._flow.no_flow_active:
            extra.append("loop_no_flow")
        if self._selftest.running:
            extra.append("actuation_test_running")
        if self._selftest.failed:
            extra.append("actuation_test_failed")
        if extra:
            flags = tuple(dict.fromkeys((*flags, *extra)))
        report = replace(
            result.report,
            dew_excluded_reason=classify_dew_eligibility(inputs),
            fast_dwell_remaining_s=self._fast.dwell_remaining_s,
            flags=flags,
            loop_flow_status=self._flow.loop_statuses,
            actuation_test_status=self._selftest.report_status,
            actuation_test_remaining_min=self._selftest.remaining_min,
            actuation_test_loops=self._selftest.loop_results,
        )
        self._fast.note_command(result.fast_source.on, result.fast_source.mode)
        # The S6 command reference: what the core ACTUALLY emitted, including
        # safety overrides and the self-test excursion.
        self._last_emitted_valve_pct = result.valve_position_pct
        return replace(result, report=report)

    def reset(self) -> None:
        """Clear all internal state (PID, trend, held valve, fast timers)."""
        self._pid.reset()
        self._trend.reset()
        self._last_pid_mode = None
        self._last_pid_setpoint_c = None
        self._inactive_s = 0.0
        self._last_valve_pct = self._config.valve_floor_pct
        self._seeded = False
        self._boost_hold_pct = None
        self._last_raw_valve_pct = 0.0
        self._fast.reset()
        self._fast_entry_state = FastSourceMode.OFF
        self._safety.reset()
        self._flow.reset()
        self._selftest.reset()
        self._last_emitted_valve_pct = None
        self._last_seen_mode = None
        self._last_loops = ()
        self._last_dew_factor = 1.0

    def begin_actuation_test(self, duration_s: float) -> str | None:
        """Start the manual actuation self-test for this room (S6/C).

        Validates the preconditions against the LAST control cycle's snapshot
        (the core has no wall clock and no fresh inputs out-of-band), then
        arms the :class:`~tortoise_ufh.core.flow_watchdog.ActuationSelfTest`
        with the last cycle's probe values as displacement references. From
        the next :meth:`step` on, the valve is driven to 100 % (safety rules
        stay supreme), the integrator freezes, and after ``duration_s`` the
        per-loop hydraulic response is graded.

        Cooling gate (the key safety decision): a 100 % excursion of chilled
        water is allowed ONLY with the local dew throttle fully open
        (``dew_throttle_factor == 1.0`` — supply at least ``dew_margin_k``
        above the room dew point); the throttle collapsing mid-test aborts
        it, and the S2 hard rule keeps overriding the excursion regardless.

        Args:
            duration_s: Test duration [s] (> 0; the service layer bounds it
                to 20-30 min).

        Returns:
            ``None`` when the test started, or a refusal reason:
            ``"already_running"``, ``"mode_inactive"`` (mode is not
            HEATING/COOLING), ``"no_probes"`` (no loop carries both water
            probes), or ``"dew_unsafe"`` (cooling with a throttled dew gap).

        Raises:
            ValueError: If ``duration_s`` is not positive.
        """
        if self._selftest.running:
            return "already_running"
        if self._last_seen_mode not in (Mode.HEATING, Mode.COOLING):
            return "mode_inactive"
        if not any(
            loop.supply_temperature_c is not None
            and loop.return_temperature_c is not None
            for loop in self._last_loops
        ):
            return "no_probes"
        if self._last_seen_mode is Mode.COOLING and self._last_dew_factor < 1.0:
            return "dew_unsafe"
        return self._selftest.begin(
            duration_s=duration_s,
            loops=self._last_loops,
            mode=self._last_seen_mode,
        )

    def cancel_actuation_test(self) -> None:
        """Cancel a running actuation self-test (no-op when idle).

        The valve returns to the PI value on the next cycle through the
        normal write path; the S6 windows restart (the partial excursion
        already moved the loop temperatures).
        """
        if self._selftest.running:
            self._selftest.cancel()
            self._flow.restart_windows()

    def _abort_selftest(self) -> None:
        """Abort a running self-test and re-arm the S6 windows."""
        self._selftest.abort()
        self._flow.restart_windows()

    def invalidate_trend(self) -> None:
        """Invalidate the filtered trend after a control-cycle gap.

        Public hook for the adapter (R2-F6, 2026-07-12): when the measured
        elapsed time since the previous step exceeded the adapter's dt clamp
        (900 s), the temperature kept moving for LONGER than the ``dt`` the
        core is about to be fed, so the next raw dT/dt sample would be
        inflated and the ~15-min EMA would carry that artefact for 2-3
        cycles. Dropping the reference restarts the trend from 0 exactly like
        a sensor-loss gap does.
        """
        self._trend.invalidate()

    def notify_fast_source_farewell(self) -> None:
        """Synchronise the machine with an out-of-band farewell OFF (K10).

        The adapter's farewell command (C5: room leaving ``live``, entry
        unload) writes a physical OFF OUTSIDE the control loop; without this
        hook the direction machine kept emitting ON after the out-of-band
        farewell OFF, so a return
        to live could write ON seconds after the farewell OFF with no dwell
        in between. Transitioning the machine to OFF here resets the dwell
        clock on the ON->OFF edge — the way back to live passes through an
        honest min-OFF — and records the OFF command so the next physical
        feedback is reconciled against what was actually written.
        """
        self._fast.force_off()
        self._fast.note_command(False, FastSourceMode.OFF)

    def resolve_group_conflict(self, outputs: RoomOutputs) -> RoomOutputs:
        """Rewrite this cycle's result after LOSING the group arbitration (K4).

        Called by :meth:`BuildingController._arbitrate_fast_groups` for a room
        whose fast-source command requested the direction that lost its
        multisplit group's arbitration. The machine is transitioned to OFF
        (never bypassing another room's min-ON — the arbiter only overrides
        rooms whose own min-ON lock has elapsed), the emitted command becomes
        OFF, and the flag ``"fast_source_group_conflict"`` is merged into the
        report. Because the ON->OFF edge resets the dwell clock, the loser
        re-engages only through a full min-OFF — deliberately biased toward
        the stability of the winning direction.

        Args:
            outputs: The room's already-finalised outputs for this cycle.

        Returns:
            The outputs with the fast source forced OFF and the report
            re-stamped (flags + dwell countdown).
        """
        fast = self._fast.force_off()
        self._fast.note_command(False, FastSourceMode.OFF)
        flags = tuple(
            dict.fromkeys((*outputs.report.flags, "fast_source_group_conflict"))
        )
        report = replace(
            outputs.report,
            flags=flags,
            fast_dwell_remaining_s=self._fast.dwell_remaining_s,
        )
        return replace(outputs, fast_source=fast, report=report)

    @property
    def fast_source_locked_on(self) -> bool:
        """Whether the fast source runs and is still inside its min-ON lock.

        Read by the group arbiter (K4): a unit locked ON in direction A pins
        the whole group to A until its dwell elapses — the arbiter never
        breaks a min-ON. Only a unit that was ALREADY running when this step
        began counts: a machine that engaged this very cycle has not
        physically started (its command has not even been written yet) and may
        be freely arbitrated away.
        """
        return (
            self._fast.state is not FastSourceMode.OFF
            and self._fast_entry_state is self._fast.state
            and self._fast.dwell_remaining_s is not None
        )

    @property
    def fast_source_entry_direction(self) -> FastSourceMode:
        """Direction the machine was ALREADY running in when this step began.

        Read by the group arbiter's incumbent hysteresis (K2, 2026-07-12): a
        unit that was physically running before this cycle carries the
        incumbency of its direction even after its min-ON dwell has elapsed,
        while a machine that engaged this very cycle is a challenger.
        :attr:`FastSourceMode.OFF` when the unit was idle at step entry.
        """
        return self._fast_entry_state

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
        fast = self._fast.force_off()
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
        self._trend.invalidate()
        # A long sensor-lost stretch counts as inactivity for the integrator
        # decay (S2): the object drifts while nobody integrates honestly.
        self._note_inactive(dt_seconds)
        report = _passive_report(
            error_c=None,
            trend=None,
            room_dew=None,
            i_term=self._pid.integral,
            raw_valve_pct=valve,
            saturated=False,
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
        fast = self._fast.force_off()
        self._last_valve_pct = valve
        self._note_inactive(dt_seconds)
        report = _passive_report(
            error_c=error_c,
            trend=trend,
            room_dew=room_dew,
            i_term=self._pid.integral,
            raw_valve_pct=valve,
            saturated=valve <= 0.0 or valve >= 100.0,
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
        # The cooling boost-hold snapshot is only meaningful while the PI
        # cooling loop actively regulates an engaged-split room: any inactive
        # cycle (OFF, TRANSITIONAL, cooling opt-out, sensor lost) voids it, so
        # a return to cooling re-snapshots from a fresh engage edge.
        self._boost_hold_pct = None
        self._inactive_s += dt_seconds
        if self._inactive_s >= _INTEGRATOR_DECAY_AFTER_S and (
            self._pid.integral != 0.0
        ):
            self._pid.reset()
            # The bumpless setpoint reference dies with the integral (K1): a
            # cleared accumulator must not receive a stale-delta re-seed on
            # the first active cycle back.
            self._last_pid_setpoint_c = None

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
            fast = self._fast.force_off()
            direction = "brak"
        else:
            # Quiet hours (B1, 2026-07-12): outside the room's allowed-hours
            # window the split must not engage — in TRANSITIONAL it is the
            # room's ONLY source, but quiet is quiet (a deliberate, documented
            # trade-off; S3/S4 emergencies still override downstream).
            quiet = not inputs.fast_source_allowed
            if quiet and "fast_source_quiet_hours" not in flags:
                flags.append("fast_source_quiet_hours")
            # Bidirectional demands: heating below setpoint, cooling above.
            heating_demand = error_c  # setpoint - t_room
            cooling_demand = -error_c
            engaged = self._fast.state
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
            if quiet and want_on:
                # A running unit reaches the window edge: request OFF through
                # the NORMAL decide() path, so the min-ON dwell is honoured
                # (compressor protection outranks punctuality) and an idle
                # unit simply never starts. No new state-machine paths.
                want_on = False
                direction = "brak"
            if heater_cannot_cool:
                fast = self._fast.force_off()
            else:
                # S12: transitional target is exactly the setpoint in BOTH
                # directions — the split is the only source here and its own
                # thermostat holding the room at the setpoint removes the old
                # [setpoint-1.0, setpoint-0.3] bias band.
                fast = self._fast.decide(
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

        report = _passive_report(
            error_c=error_c,
            trend=trend,
            room_dew=room_dew,
            i_term=self._pid.integral,
            raw_valve_pct=valve,
            saturated=False,
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
            fast = self._fast.force_off()
            self._last_valve_pct = 0.0
            # No throttle is computed for an opted-out room: keep the
            # self-test dew gate conservative (a 100 % chilled-water
            # excursion into a cooling-disabled room is never allowed) and
            # abort a test that somehow survived into this branch.
            self._last_dew_factor = 0.0
            if self._selftest.running:
                self._abort_selftest()
            self._note_inactive(dt_seconds)
            report = _passive_report(
                error_c=error_c,
                trend=trend,
                room_dew=room_dew,
                i_term=self._pid.integral,
                raw_valve_pct=0.0,
                saturated=True,
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
            self._last_pid_setpoint_c = None
        self._last_pid_mode = mode
        self._inactive_s = 0.0

        # Bumpless setpoint transfer (K1, 2026-07-12): a setpoint change of
        # dK between PID-active cycles shifts the required steady-state valve
        # by roughly kp * dK, so the integral — the loop's memory of that
        # operating point — is re-seeded along instead of discharging the
        # difference at ki speed (measured: a 23 -> 21 drop left the valve
        # actively heating an overheated room for 17.4 h / 642 %*h). The
        # shift follows the mode's error convention (sign INVERTS in COOLING)
        # and is clamped to the PID output range inside ``shift_integral``.
        if self._last_pid_setpoint_c is not None:
            delta_sp = inputs.setpoint_c - self._last_pid_setpoint_c
            if delta_sp != 0.0:
                delta_error = delta_sp if mode is Mode.HEATING else -delta_sp
                self._pid.shift_integral(cfg.kp * delta_error)
        self._last_pid_setpoint_c = inputs.setpoint_c

        # -- Step 6: deadband -> reduce magnitude, keep sign ----------------
        error_db = math.copysign(max(0.0, abs(error) - cfg.deadband_c), error)

        # -- Step 7: integrator freeze (DHW / defrost, S2-throttled cooling) -
        # The local dew throttle (step 12) multiplies the valve AFTER the PID,
        # invisibly to the back-calculation anti-windup; computing the factor
        # here (it depends only on inputs) and freezing the integrator while
        # the throttle is active (< 1.0) closes that windup hole (S1/dew-F2,
        # 2026-07-09): hours of throttled cooling no longer bank an integral
        # that would slam the valve open the moment the humidity clears.
        # K9 (2026-07-12): a back-calculation from the FINAL (throttled)
        # valve (I += u_final - u_raw per throttled cycle) was empirically
        # evaluated as a replacement for this freeze and REJECTED: any
        # tracking anti-windup enforces u_raw ~ u_final, which pins the
        # integral at ~0 for the whole throttle episode, so the post-release
        # catch-up was NOT faster (6 h full throttle -> +-0.3 K after 8.6 h
        # with either variant; partial throttle 9.5 h, worse) — the catch-up
        # time is a ki-speed property (I must legitimately rebuild 0 -> ~50
        # pp), not a windup artifact. See docs/DECISIONS.md §11.
        dew_factor = 1.0
        if mode is Mode.COOLING:
            dew_factor = self._cooling_throttle(inputs, room_dew, flags)
        # Snapshot for the out-of-band self-test start gate (dew_unsafe) and
        # the per-cycle cooling abort below.
        self._last_dew_factor = dew_factor
        # A running self-test in COOLING loses its dew headroom the moment
        # the throttle engages: abort, never "finish by force".
        if self._selftest.running and mode is Mode.COOLING and dew_factor < 1.0:
            self._abort_selftest()
        # S6 (2026-07-13): a latched no-flow alarm freezes the integrator —
        # the incident's wind-up against a non-responding plant. A running
        # self-test freezes too: the PI must not wind against the forced
        # 100 % excursion for 25 min.
        freeze = (
            inputs.hp_active_for_ufh is False
            or dew_factor < 1.0
            or self._flow.no_flow_active
            or self._selftest.running
        )

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

        # -- Step 11b: cooling boost-hold floor (anti measurement-path ------
        # inversion, 2026-07-13). While the split is ENGAGED in cooling the
        # floor valve is never pulled BELOW the position it held the cycle the
        # split engaged. The split cooling the air drives the room error (and
        # the filtered trend) toward zero, which would otherwise retreat the
        # floor to 0 — but the floor is the base source and, since the split
        # only engaged because the floor alone could not cope, the floor must
        # keep discharging the high-mass slab. ``max`` lets the PI push the
        # valve HIGHER, never lower. The engagement witness is
        # ``_fast_entry_state`` (the direction the unit was ALREADY running in
        # at step entry): a one-cycle-delayed signal the slab's thermal mass
        # absorbs, so the very first boost cycle runs unheld. The snapshot is
        # the PREVIOUS active cycle's raw pre-throttle clamped valve, so the S2
        # dew throttle below still scales the held value (a dew factor of 0
        # still closes the valve) instead of being baked into the hold. Only
        # a genuine cooling split can leave the machine in the COOLING state
        # (NONE is forced OFF, a HEATER can never cool), so no explicit
        # fast-source-kind guard is needed here.
        boost_cooling_active = (
            mode is Mode.COOLING and self._fast_entry_state is FastSourceMode.COOLING
        )
        if boost_cooling_active:
            if self._boost_hold_pct is None:
                self._boost_hold_pct = self._last_raw_valve_pct
            valve = max(valve, self._boost_hold_pct)
        else:
            self._boost_hold_pct = None
        # Persist THIS cycle's raw pre-throttle clamped valve for the next
        # engage-edge snapshot (recorded BEFORE the S2 throttle so the dew
        # factor is never folded into a future hold).
        self._last_raw_valve_pct = max(0.0, min(100.0, raw_valve))

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
            room_dew_c=room_dew,
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
        (factor 0.0) and ``"s2_throttle"`` is flagged. The flag was renamed
        from ``"s2_condensation"`` (2026-07-12, B7): that name now belongs
        exclusively to the independent hard-safety rule in
        :mod:`~tortoise_ufh.core.safety`, so the panel can tell the graduated
        local throttle from the hard backstop.

        A STALE humidity reading (held 60-120 min, K7 2026-07-12) pads the
        effective dew point by ``inputs.humidity_stale_frac`` times
        :data:`_STALE_RH_DEW_PAD_K` (linear in the age, D5 2026-07-12) and
        flags ``"rh_stale_gated"`` — the throttle keeps working on the last
        known moisture level priced up continuously with its age (up to
        +1 K at 120 min) instead of slamming to the conservative full stop,
        so a slow-reporting RH sensor can neither limit-cycle the cooling
        nor step the throttle discontinuously at the 60-min edge.

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
            if "s2_throttle" not in flags:
                flags.append("s2_throttle")
            return 0.0
        effective_dew = room_dew
        if inputs.humidity_stale_frac > 0.0:
            effective_dew += _STALE_RH_DEW_PAD_K * inputs.humidity_stale_frac
            if "rh_stale_gated" not in flags:
                flags.append("rh_stale_gated")
        t_supply_min = min(supplies)
        factor = cooling_throttle_factor(
            t_supply_min,
            effective_dew,
            margin=cfg.dew_margin_k,
            ramp=cfg.dew_ramp_k,
        )
        if factor == 0.0 and "s2_throttle" not in flags:
            flags.append("s2_throttle")
        return factor

    def _coordinate_fast_source(
        self,
        *,
        inputs: RoomInputs,
        error_c: float,
        room_dew_c: float | None,
        flags: list[str],
    ) -> FastSourceCommand:
        """Decide the fast-source command for HEATING/COOLING (step 14).

        Enforces anti priority-inversion: the valve is the base and is never
        lowered because of the split; the split only *adds* boost above the
        boost offset and releases once inside the comfort (deadband) band.
        Honours the three-state direction machine (C6): a global mode change
        while the split is held by min-ON keeps re-emitting the REMEMBERED
        direction and can only reverse through OFF with the full min-OFF. The
        boost target is offset by ``config.fast_target_offset_k`` in the boost
        direction (S12; a tuning knob since 2026-07-13, ``0`` disables) so the
        split's ceiling-mounted sensor does not throttle the unit before the
        boost is delivered.

        Quiet hours (B1, 2026-07-12): when ``inputs.fast_source_allowed`` is
        ``False`` the request is forced OFF and ``"fast_source_quiet_hours"``
        is flagged — an idle unit does not engage, a running unit stops
        through the normal min-ON dwell path. The safety layer downstream
        (:meth:`_apply_safety`) may still force the unit ON in an S3/S4
        emergency: room safety outranks acoustic comfort.

        Dry assist (opt-in, 2026-07-16, DECISIONS §24): in COOLING a SPLIT may
        also be called by HUMIDITY — the floor cools only sensibly and a
        rising dew point simultaneously steals its capacity (the safe dew
        point lifts the cooling water), so the split's latent capacity is the
        only dehumidifier in the system. When ``config.dry_enabled``, the room
        is at or above the setpoint (``error_c <= 0`` — engage side) and the
        room dew point exceeds ``config.dry_dew_max_c`` (release at
        ``- DRY_HYSTERESIS_K``, or when the room is overcooled past the
        deadband — the temperature gate is a full deadband-wide hysteresis,
        2026-07-17), the machine is engaged exactly like a boost but the
        emitted command mode is ``DRY`` (target ``None`` — dry mode
        self-regulates).
        The machine itself stays three-state: DRY is a PRESENTATION of the
        COOLING state, so dwells, group arbitration and the S3/S4 forces work
        unchanged, and a temperature boost pre-empts DRY -> COOLING in the
        same cycle without an OFF cycle (same refrigerant side). A dry run
        does NOT lower the temperature-boost engage threshold: temperature
        hysteresis stays keyed to a temperature-commanded run.

        Args:
            inputs: The room's raw inputs.
            error_c: ``setpoint - room_temp`` [K] (heating convention).
            room_dew_c: The room dew point [degC], or ``None`` without
                temperature + humidity (feeds the dry-assist trigger).
            flags: Mutable flag list (appended in place).

        Returns:
            The :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        if inputs.fast_source_kind is FastSourceKind.NONE:
            return self._fast.force_off()

        quiet = not inputs.fast_source_allowed
        if quiet and "fast_source_quiet_hours" not in flags:
            flags.append("fast_source_quiet_hours")

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
            return self._fast.force_off()

        # Was the previous EMITTED command a dry run? Temperature and humidity
        # carry SEPARATE hysteresis states: a dry run must not lower the
        # temperature engage threshold to the deadband (anti-inversion), and a
        # temperature run must not inherit the dry release band.
        last_dry = (
            self._fast.state is not FastSourceMode.OFF
            and self._fast.last_command_mode is FastSourceMode.DRY
        )
        temp_want = (not quiet) and self._fast.want(
            demand, engaged=(self._fast.state is fs_mode and not last_dry)
        )
        dry_threshold = self._config.dry_dew_max_c - (
            DRY_HYSTERESIS_K if last_dry else 0.0
        )
        # Temperature gate with a REAL hysteresis (2026-07-17, owner-observed
        # short-cycling): a split blows cold air in dry too, so with the single
        # `error < deadband` threshold a satisfied room flapped
        # dry -> overcool -> pause -> dry with a period of just the dwell
        # times. Engage only when the room is AT or ABOVE the setpoint; a
        # running dry may then cool it down to the deadband before the
        # overcool release — a full band of hysteresis, mirroring the boost's
        # engage-at-offset / release-in-band shape.
        dry_temp_ok = error_c < self._config.deadband_c if last_dry else error_c <= 0.0
        dry_want = (
            self._config.dry_enabled
            and inputs.mode is Mode.COOLING
            and inputs.fast_source_kind is FastSourceKind.SPLIT
            and not quiet
            and room_dew_c is not None
            and dry_temp_ok
            and room_dew_c > dry_threshold
        )

        offset_k = self._config.fast_target_offset_k
        decision = self._fast.decide(
            want_on=temp_want or dry_want,
            fs_mode=fs_mode,
            target_heating=inputs.setpoint_c + offset_k,
            target_cooling=inputs.setpoint_c - offset_k,
            flags=flags,
        )
        if (
            inputs.mode is Mode.COOLING
            and decision.on
            and decision.mode is FastSourceMode.COOLING
            and not temp_want
            and (dry_want or last_dry)
        ):
            # Humidity, not temperature, holds the split: present the COOLING
            # state as a DRY command. Target None — splits self-regulate in
            # dry and mostly ignore/reject a temperature there. ``last_dry``
            # keeps the presentation through a min-ON blocked tail (the dew
            # released but the dwell has not elapsed): the machine re-emits
            # its remembered COOLING, and staying in dry is gentler than
            # flipping a satisfied room to cool-at-target for the remainder.
            # The whole branch is gated on the CURRENT mode being COOLING —
            # without it a global flip to HEATING mid-dry would keep writing
            # "dry" through the blocked tail (review finding, 2026-07-16).
            decision = FastSourceCommand(
                on=True, mode=FastSourceMode.DRY, target_temperature_c=None
            )
            if "dry_assist" not in flags:
                flags.append("dry_assist")
        return decision

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
                self._fast.force_on(FastSourceMode.HEATING, target)
                if has_split
                else self._fast.force_off()
            )
        elif SafetyAction.EMERGENCY_COOL in actions:
            valve = 0.0  # air-side only; never open the floor without S2 cover
            # Only a SPLIT can cool; a HEATER must never be told to cool.
            if inputs.fast_source_kind is FastSourceKind.SPLIT:
                fast = self._fast.force_on(FastSourceMode.COOLING, target)
            else:
                fast = self._fast.force_off()
                if inputs.fast_source_kind is FastSourceKind.HEATER:
                    flags = tuple(dict.fromkeys((*flags, "fast_source_cannot_cool")))
        elif close_valve:
            # CLOSE_VALVE (S1/S2) is a WATER-side action only (K3,
            # 2026-07-12): the valve is parked, but the AIR-side decision
            # from the normal coordination (step 14 / transitional) stands.
            # An S1 floor-overheat must not kill a wanted heat boost, and an
            # S2 condensation stop must not kill the split — the one source
            # that can still cool safely (it has its own condensate tray)
            # exactly when it is humid. This also removes the dwell-clock
            # sawtooth and the flapping min-runtime flag the per-cycle
            # force-off used to cause. Mode.OFF / sensor-lost paths still
            # force the fast source off upstream (their result already
            # carries an OFF command), so nothing changes there.
            valve = 0.0
            fast = result.fast_source
        else:
            # FALLBACK_HP_CURVE (S5 watchdog) alone: NEUTRAL position, not a
            # hard close (amendment 2026-07-09, S6) — "defer to the heat-pump
            # native curve" means a passive baseline: the heating valve floor
            # in HEATING (keeps the house tempered by the HP curve), 0 in
            # COOLING (chilled water without fresh data is never safe).
            valve = self._config.valve_floor_pct if inputs.mode is Mode.HEATING else 0.0
            fast = self._fast.force_off()

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
        # Kept for the group arbiter (K4): the per-room deadband scales the
        # "error beyond the comfort band" demand strength.
        self._configs: dict[str, ControllerConfig] = dict(configs)
        # Incumbent bookkeeping of the group arbiter (K2, 2026-07-12): the
        # direction that last ran (or last won a conflict) per group, so a
        # challenger must beat the incumbent by the fixed hysteresis margin
        # even across all-OFF gaps. Cleared by reset().
        self._group_last_winner: dict[str, FastSourceMode] = {}

    def step(
        self,
        inputs: dict[str, RoomInputs],
        *,
        dt_seconds: float = 300.0,
        global_supply_temperature_c: float | None = None,
    ) -> BuildingOutputs:
        """Run every room controller and compute the global safe dew point.

        The global safe dew point is ``max_i(T_dew_i) + 2 K`` over rooms that
        are in COOLING with ``cooling_enabled`` and a usable humidity reading;
        it is ``None`` when no room is eligible. It never *lowers* on any single
        room — it is a maximum plus a fixed safety margin.

        S6 (2026-07-13): before dispatching, the building-level circulation
        gate is computed from the RAW inputs (see
        :meth:`_circulation_evident`) and injected into every room's inputs
        via ``dataclasses.replace`` — the ``step(inputs, *, dt_seconds)``
        room contract stays verbatim.

        Args:
            inputs: Per-room :class:`~tortoise_ufh.models.RoomInputs` keyed by
                room name.
            dt_seconds: Elapsed time since the previous step [s]. Must be > 0.
            global_supply_temperature_c: Optional manifold-bar supply probe
                [degC] feeding the S6 circulation gate; ``None`` when not
                configured (additive kw-only parameter, 2026-07-13).

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
        circulation = self._circulation_evident(inputs, global_supply_temperature_c)

        for name, raw_inputs in inputs.items():
            room_inputs = replace(raw_inputs, circulation_evident=circulation)
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
                    controller, exc, room_inputs.mode, room_inputs.room_temperature_c
                )

            dew = self._eligible_dew_point(room_inputs)
            if dew is not None:
                dew_points.append(dew)

        # K4 (2026-07-12): one direction per shared outdoor unit — arbitrate
        # conflicting fast-source commands within each multisplit group.
        self._arbitrate_fast_groups(inputs, rooms)

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
        self._group_last_winner.clear()

    def invalidate_trends(self) -> None:
        """Invalidate every room's filtered trend after a control-cycle gap.

        Adapter hook (R2-F6, 2026-07-12): called when the measured elapsed
        time hit the adapter's dt clamp (900 s), so the next raw dT/dt sample
        would divide a longer-than-``dt`` temperature delta by the clamped
        interval and inflate the trend for 2-3 EMA cycles.
        """
        for controller in self._controllers.values():
            controller.invalidate_trend()

    def notify_fast_source_farewell(self, room_name: str) -> None:
        """Synchronise one room's machine with a farewell OFF (K10).

        Adapter hook: see :meth:`RoomController.notify_fast_source_farewell`.
        Unknown room names are ignored (the farewell may race a room removal).

        Args:
            room_name: The room whose fast source was parked out-of-band.
        """
        controller = self._controllers.get(room_name)
        if controller is not None:
            controller.notify_fast_source_farewell()

    def begin_actuation_test(self, room_name: str, *, duration_s: float) -> str | None:
        """Start one room's actuation self-test (S6/C; adapter hook).

        Args:
            room_name: The room to test.
            duration_s: Test duration [s] (> 0).

        Returns:
            ``None`` when the test started, else the refusal reason (see
            :meth:`RoomController.begin_actuation_test`) or
            ``"unknown_room"``.
        """
        controller = self._controllers.get(room_name)
        if controller is None:
            return "unknown_room"
        return controller.begin_actuation_test(duration_s)

    def cancel_actuation_test(self, room_name: str) -> None:
        """Cancel one room's running actuation self-test (adapter hook).

        Unknown room names are ignored.

        Args:
            room_name: The room whose test to cancel.
        """
        controller = self._controllers.get(room_name)
        if controller is not None:
            controller.cancel_actuation_test()

    # -- internal helpers ---------------------------------------------------

    @staticmethod
    def _circulation_evident(
        inputs: dict[str, RoomInputs],
        global_supply_temperature_c: float | None,
    ) -> bool | None:
        """Compute the S6 building-level circulation gate from RAW inputs.

        ``True`` when (a) ANY loop in the building carries both probes with
        ``|supply - return|`` at or above
        :data:`~tortoise_ufh.core.flow_watchdog.CIRCULATION_DELTA_K` —
        deliberately including the inspected room's own loop (a post-valve
        dead loop shows ~0 delta-T and contributes nothing, while a
        manifold-bar pair with a large delta-T proves a live source) — or
        (b) the optional global supply probe reads source-side of the mean
        available room temperature by
        :data:`~tortoise_ufh.core.flow_watchdog.GLOBAL_SUPPLY_MARGIN_K`
        (heating above / cooling below, using the building's single active
        direction). The global-probe path is SUSPENDED (contributes neither
        ``True`` nor ``judged``) while any room reports
        ``hp_active_for_ufh is False``: during DHW/defrost a manifold probe
        can keep reading source-side while every UFH loop is legitimately
        starved, so trusting it would accumulate no-flow windows on healthy
        loops. ``False`` when evidence sources exist but none indicates
        circulation (pump provably idle). ``None`` when nothing can judge —
        the per-room watchdogs then HOLD.

        Args:
            inputs: Per-room raw inputs of this cycle.
            global_supply_temperature_c: Optional manifold supply probe
                [degC].

        Returns:
            ``True`` / ``False`` / ``None`` as described.
        """
        judged = False
        for room_inputs in inputs.values():
            for loop in room_inputs.loops:
                supply = loop.supply_temperature_c
                ret = loop.return_temperature_c
                if supply is None or ret is None:
                    continue
                judged = True
                if abs(supply - ret) >= CIRCULATION_DELTA_K:
                    return True
        hp_diverted = any(ri.hp_active_for_ufh is False for ri in inputs.values())
        if global_supply_temperature_c is not None and not hp_diverted:
            temps = [
                ri.room_temperature_c
                for ri in inputs.values()
                if ri.room_temperature_c is not None
            ]
            active_modes = {
                ri.mode
                for ri in inputs.values()
                if ri.mode in (Mode.HEATING, Mode.COOLING)
            }
            if temps and len(active_modes) == 1:
                judged = True
                t_mean = sum(temps) / len(temps)
                mode = next(iter(active_modes))
                if (
                    mode is Mode.HEATING
                    and global_supply_temperature_c >= t_mean + GLOBAL_SUPPLY_MARGIN_K
                ) or (
                    mode is Mode.COOLING
                    and global_supply_temperature_c <= t_mean - GLOBAL_SUPPLY_MARGIN_K
                ):
                    return True
        return False if judged else None

    def _arbitrate_fast_groups(
        self,
        inputs: dict[str, RoomInputs],
        rooms: dict[str, RoomOutputs],
    ) -> None:
        """Enforce ONE fast-source direction per multisplit group (K4).

        Indoor units sharing an outdoor unit (``RoomInputs.fast_source_group``)
        must never be commanded to heat and cool in the same cycle — a mode
        conflict locks the aggregate. For every group whose emitted ON
        commands disagree on direction this cycle:

        1. A room whose machine is ON and still inside its min-ON dwell (or
           held ON by an S3/S4 emergency) PINS the group to its direction —
           the arbiter never breaks a min-ON or overrides an emergency.
        2. With no pinned direction, the INCUMBENT direction (K2,
           2026-07-12) defends its seat: the incumbent is the direction of a
           unit that was already running when this step began, falling back
           to the group's last winning direction when every unit re-engaged
           from OFF. The challenging direction takes over only when its best
           comfort-band excess ``max(0, |error| - deadband)`` exceeds the
           incumbent's best excess by more than
           :data:`_GROUP_CHALLENGER_HYSTERESIS_K` — sensor noise on two
           persistent opposite demands can no longer ping-pong the aggregate.
        3. With no incumbent either (first-ever conflict of a fresh group),
           the direction of the room with the largest comfort-band excess
           wins.
        4. Every losing ON room is rewritten via
           :meth:`RoomController.resolve_group_conflict` (fast OFF + the
           ``"fast_source_group_conflict"`` flag); its machine passes through
           an honest min-OFF before it may re-engage.
        5. Pathological double-pin (two rooms min-ON-locked in opposite
           directions, only reachable by adopting an inconsistent physical
           state at startup) overrides nobody — every conflicting room is
           flagged and the situation resolves itself when a dwell elapses.

        Rooms without a group, without a fast source, or with an unknown
        controller are untouched; TRANSITIONAL rooms take part like any
        other (their per-room error sign is exactly the routine conflict
        source). Mutates ``rooms`` in place.

        Args:
            inputs: Per-room inputs of this cycle (group membership, errors).
            rooms: Per-room outputs of this cycle (mutated in place).
        """
        groups: dict[str, list[str]] = {}
        for name, room_inputs in inputs.items():
            if (
                room_inputs.fast_source_group
                and room_inputs.fast_source_kind is not FastSourceKind.NONE
                and name in self._controllers
                and name in rooms
            ):
                groups.setdefault(room_inputs.fast_source_group, []).append(name)

        for group, members in groups.items():
            on_rooms = [n for n in members if rooms[n].fast_source.on]
            # Directions compare refrigerant-side (§24): a DRY command counts
            # as the cooling side, so DRY + COOLING coexist on one aggregate
            # and only DRY vs HEATING is a real conflict.
            directions = {direction_of(rooms[n].fast_source.mode) for n in on_rooms}
            if len(directions) <= 1:
                # No conflict; a single running direction still claims the
                # incumbency (K2) so a later challenger faces the hysteresis.
                if len(directions) == 1:
                    winner_dir = next(iter(directions))
                    if winner_dir is not None:
                        self._group_last_winner[group] = winner_dir
                continue
            pinned = {
                n
                for n in on_rooms
                if self._controllers[n].fast_source_locked_on
                or self._is_safety_forced(rooms[n])
            }
            pinned_dirs = {direction_of(rooms[n].fast_source.mode) for n in pinned}
            if len(pinned_dirs) == 1:
                winner = next(iter(pinned_dirs))
            elif pinned_dirs:
                # Double-pin: flag every conflicting room, override none.
                for n in on_rooms:
                    rooms[n] = self._flag_group_conflict(rooms[n])
                continue
            else:
                winner = self._arbitrate_by_excess(group, inputs, rooms, on_rooms)
            if winner is not None:
                self._group_last_winner[group] = winner
            for n in on_rooms:
                if direction_of(rooms[n].fast_source.mode) is not winner:
                    rooms[n] = self._controllers[n].resolve_group_conflict(rooms[n])

    def _arbitrate_by_excess(
        self,
        group: str,
        inputs: dict[str, RoomInputs],
        rooms: dict[str, RoomOutputs],
        on_rooms: list[str],
    ) -> FastSourceMode:
        """Pick a conflicted group's direction: incumbent hysteresis + excess.

        K2 (2026-07-12): the incumbent direction is the one a unit was
        ALREADY physically running in at step entry
        (:attr:`RoomController.fast_source_entry_direction`); when every
        conflicting unit re-engaged from OFF this cycle, the group's stored
        last winner inherits the incumbency. The challenger wins only when
        its best comfort-band excess exceeds the incumbent side's best excess
        by more than :data:`_GROUP_CHALLENGER_HYSTERESIS_K`; with no
        incumbent at all the largest excess wins outright (the tie-break is
        deliberately the strongest single room, not the side head-count —
        one room 3 K outside its band outweighs two rooms 0.5 K outside).

        Args:
            group: The group key (for the stored last winner).
            inputs: Per-room inputs of this cycle.
            rooms: Per-room outputs of this cycle.
            on_rooms: Names of the group's rooms with an emitted ON command.

        Returns:
            The winning :class:`~tortoise_ufh.models.FastSourceMode`.
        """
        best_excess: dict[FastSourceMode, float] = {}
        for n in on_rooms:
            # Refrigerant-side normalisation (§24): a DRY room's claim counts
            # toward the cooling side. Its band excess is ~0 (the room is in
            # band by definition of a dry run), so a dry-only side is the
            # weakest possible claimant and loses to any temperature demand.
            mode = direction_of(rooms[n].fast_source.mode)
            if mode is None:
                continue
            excess = self._band_excess_k(inputs[n], n)
            if excess > best_excess.get(mode, -1.0):
                best_excess[mode] = excess
        entry_dirs = {
            self._controllers[n].fast_source_entry_direction
            for n in on_rooms
            if self._controllers[n].fast_source_entry_direction
            is direction_of(rooms[n].fast_source.mode)
        } - {FastSourceMode.OFF}
        incumbent: FastSourceMode | None = None
        if len(entry_dirs) == 1:
            incumbent = next(iter(entry_dirs))
        else:
            # Nobody (or both sides) already running: the stored last winner
            # inherits the incumbency when it is one of the candidates.
            stored = self._group_last_winner.get(group)
            if stored in best_excess:
                incumbent = stored
        challenger = max(
            (mode for mode in best_excess if mode is not incumbent),
            key=lambda mode: best_excess[mode],
        )
        if incumbent is None:
            return challenger
        if (
            best_excess[challenger]
            > best_excess[incumbent] + _GROUP_CHALLENGER_HYSTERESIS_K
        ):
            return challenger
        return incumbent

    def _band_excess_k(self, room_inputs: RoomInputs, name: str) -> float:
        """Return a room's comfort-band excess ``max(0, |error| - deadband)``.

        The group arbiter's demand strength [K]: how far the room sits
        OUTSIDE its comfort band. Rooms without a temperature read as 0.

        Args:
            room_inputs: The room's inputs this cycle.
            name: The room name (for its deadband).

        Returns:
            The band excess [K] (>= 0).
        """
        t_room = room_inputs.room_temperature_c
        if t_room is None:
            return 0.0
        deadband = self._configs[name].deadband_c
        return max(0.0, abs(room_inputs.setpoint_c - t_room) - deadband)

    @staticmethod
    def _is_safety_forced(outputs: RoomOutputs) -> bool:
        """Whether a room's ON command comes from an S3/S4 emergency force-on.

        Args:
            outputs: The room's outputs this cycle.

        Returns:
            ``True`` when an emergency rule pinned the fast source ON.
        """
        flags = outputs.report.flags
        return "s3_emergency_heat" in flags or "s4_emergency_cool" in flags

    @staticmethod
    def _flag_group_conflict(outputs: RoomOutputs) -> RoomOutputs:
        """Merge the group-conflict flag into a room's report (no override).

        Used only on the pathological double-pin path where the arbiter
        cannot force anyone off without breaking a min-ON.

        Args:
            outputs: The room's outputs this cycle.

        Returns:
            The outputs with ``"fast_source_group_conflict"`` merged in.
        """
        flags = tuple(
            dict.fromkeys((*outputs.report.flags, "fast_source_group_conflict"))
        )
        return replace(outputs, report=replace(outputs.report, flags=flags))

    @staticmethod
    def _eligible_dew_point(room_inputs: RoomInputs) -> float | None:
        """Return a room's dew point if it is eligible for the global maximum.

        Eligibility is decided by :func:`classify_dew_eligibility` (the same
        classifier that fills ``RoomReport.dew_excluded_reason``): a ``None``
        reason means COOLING mode, ``cooling_enabled`` and usable temperature +
        humidity — one logic, two consumers.

        A STALE humidity reading (K7, 2026-07-12) pads the room's
        contribution by ``humidity_stale_frac`` times
        :data:`_STALE_RH_DEW_PAD_K` (linear in the age, D5 2026-07-12), so
        the heat pump's supply floor prices the staleness in exactly like the
        local throttle does.

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
        dew = dew_point(t_room, rh)
        if room_inputs.humidity_stale_frac > 0.0:
            dew += _STALE_RH_DEW_PAD_K * room_inputs.humidity_stale_frac
        return dew

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
        report = _passive_report(
            error_c=None,
            trend=None,
            room_dew=None,
            i_term=0.0,
            raw_valve_pct=0.0,
            saturated=False,
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
        mode: Mode,
        room_temperature_c: float | None = None,
    ) -> RoomOutputs:
        """Build a safe-degraded output after a room controller raised.

        Forces the fast source OFF and flags ``"controller_error"``. The valve
        is MODE-AWARE (K5, 2026-07-12), symmetric with the sensor-lost safe
        degrade: HEATING holds the controller's last valve position (warm
        water is bounded by the heat-pump curve), while COOLING /
        TRANSITIONAL / OFF drive it to 0 — a crashed controller computes
        neither condensation defence, so a held-open valve would pass
        unprotected chilled water indefinitely (the last bypass of the dew-F1
        invariant).

        Args:
            controller: The room controller that raised.
            exc: The exception raised.
            mode: The room's operating mode this cycle (drives the valve rule).
            room_temperature_c: Measured room temperature [degC] echoed into the
                report, or ``None`` when unavailable.

        Returns:
            A safe-degraded, fast-OFF :class:`~tortoise_ufh.models.RoomOutputs`.
        """
        if mode is Mode.HEATING:
            valve = controller.last_valve_pct
            valve_txt = f"Zawor trzyma {valve:.0f}%"
        else:
            valve = 0.0
            valve_txt = "Zawor 0% (tryb bez grzania)"
        report = _passive_report(
            error_c=None,
            trend=None,
            room_dew=None,
            i_term=0.0,
            raw_valve_pct=valve,
            saturated=False,
            flags=("controller_error",),
            explanation=(
                f"Blad regulatora pokoju '{controller.name}': {exc}. "
                f"{valve_txt}, split OFF."
            ),
            room_temperature_c=room_temperature_c,
        )
        return RoomOutputs(
            valve_position_pct=valve,
            fast_source=FastSourceCommand(on=False, mode=FastSourceMode.OFF),
            report=report,
        )
