"""Three-state fast-source (split/heater) direction machine.

Extracted verbatim from :mod:`tortoise_ufh.controller` (2026-07-10) so the
most bug-prone stateful piece of the room controller — the OFF / HEATING /
COOLING direction machine with its min ON/OFF dwell clock and the physical
feedback reconciliation (S4) — is a self-contained, unit-testable class.
:class:`~tortoise_ufh.controller.RoomController` owns exactly one
:class:`FastSourceMachine` and delegates to it; the mode→demand mapping and
the HEATER-cannot-cool rule stay in the controller because they depend on the
full :class:`~tortoise_ufh.models.RoomInputs`.

The machine's contract (C6, 2026-07-09): the split DIRECTION is part of the
state — a HEATING<->COOLING flip is only reachable through OFF with the full
min-OFF dwell, and a hold (min-ON not yet elapsed) re-emits the REMEMBERED
direction, never a freshly computed one. The dwell clock is advanced by
:meth:`tick` exactly once per control step (fix 2026-07-10); the decision
methods only RESET it on ON<->OFF edges.

This module is pure Python (stdlib + sibling core modules only) and MUST NOT
import ``homeassistant``.

Units:
    * Temperatures / targets: degrees Celsius (``_c``).
    * Demands / offsets expressed in kelvin (``_k`` / ``_c`` as a delta).
    * Dwell times: configured in minutes, tracked in seconds internally.
"""

from __future__ import annotations

from .config import ControllerConfig
from .models import (
    FastSourceCommand,
    FastSourceKind,
    FastSourceMode,
    Mode,
    RoomInputs,
)

__all__ = [
    "FAST_TARGET_OFFSET_K",
    "FastSourceMachine",
]


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


class FastSourceMachine:
    """Stateful OFF / HEATING / COOLING machine for one room's fast source.

    Owns the direction state, the dwell clock, and the physical-feedback
    bookkeeping (S4). One instance per
    :class:`~tortoise_ufh.controller.RoomController`; every method body is the
    verbatim translocation of the controller's former ``_sync_fast_state`` /
    ``_want_fast`` / ``_decide_fast_source`` / ``_force_fast_on`` /
    ``_force_fast_off`` logic (2026-07-10), so the numeric behaviour — and the
    order of floating-point operations — is unchanged.

    Typical usage (one control step)::

        machine.sync(inputs)          # step 0: reconcile with the hardware
        machine.tick(dt_seconds)      # advance the dwell clock ONCE
        want = machine.want(demand, engaged=machine.state is fs_mode)
        command = machine.decide(want_on=want, fs_mode=fs_mode, ...)
    """

    def __init__(self, config: ControllerConfig) -> None:
        """Initialise the machine in the OFF state with an unlocked timer.

        Args:
            config: The room's tuning knobs; the machine reads ``deadband_c``,
                ``boost_offset_c``, ``fast_min_on_minutes`` and
                ``fast_min_off_minutes``.
        """
        self._config = config
        # The DIRECTION is part of the state — OFF / HEATING / COOLING (C6).
        self._state: FastSourceMode = FastSourceMode.OFF
        self._timer_s: float = _INITIAL_FAST_TIMER_S
        # Physical-feedback bookkeeping (S4): the first observed
        # ``fast_source_on`` wins over the cold machine (conservative timer
        # seed); later divergence raises the ``fast_source_mismatch`` flag.
        self._synced: bool = False
        self._mismatch: bool = False
        self._prev_cmd_on: bool | None = None
        # Seconds remaining on the min ON/OFF dwell lock (None = unlocked / no
        # fast source). Recomputed each cycle by the decision methods and
        # surfaced in the report for the panel's assist timer.
        self._dwell_remaining_s: float | None = None

    # -- properties -----------------------------------------------------------

    @property
    def state(self) -> FastSourceMode:
        """Current machine direction (OFF / HEATING / COOLING), read-only."""
        return self._state

    @property
    def timer_s(self) -> float:
        """Seconds accumulated in the current ON/OFF state (read-only)."""
        return self._timer_s

    @property
    def mismatch(self) -> bool:
        """Whether this cycle's physical feedback disagreed with the command."""
        return self._mismatch

    @property
    def dwell_remaining_s(self) -> float | None:
        """Seconds left on the min ON/OFF lock, or ``None`` once elapsed."""
        return self._dwell_remaining_s

    # -- public API -----------------------------------------------------------

    def sync(self, inputs: RoomInputs) -> None:
        """Reconcile the machine with the physical unit (S4, step 0).

        On the FIRST cycle that carries a physical ``fast_source_on`` feedback
        AND the machine has not emitted any command yet
        (``_prev_cmd_on is None``), the physical state wins: a running
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
        mismatch flag; the machine stays the owner and the adapter's periodic
        re-assert converges the hardware back.

        Args:
            inputs: The room's raw inputs for this cycle.
        """
        self._mismatch = False
        if inputs.fast_source_kind is FastSourceKind.NONE:
            return
        physical = inputs.fast_source_on
        if physical is None:
            return
        if not self._synced:
            self._synced = True
            if self._prev_cmd_on is None:
                # No command emitted yet — adopt the physical state.
                if physical and self._state is FastSourceMode.OFF:
                    self._state = (
                        FastSourceMode.COOLING
                        if (
                            inputs.mode is Mode.COOLING
                            and inputs.fast_source_kind is FastSourceKind.SPLIT
                        )
                        else FastSourceMode.HEATING
                    )
                elif not physical:
                    self._state = FastSourceMode.OFF
                # Conservative seed: a full dwell from now, whatever the state.
                self._timer_s = 0.0
                return
            # The machine already owns the unit; a late first feedback falls
            # through to the regular mismatch check below.
        if self._prev_cmd_on is not None and physical is not self._prev_cmd_on:
            self._mismatch = True

    def tick(self, dt_seconds: float) -> None:
        """Advance the dwell clock by ``dt_seconds`` — exactly once per step.

        The caller (:meth:`RoomController.step`) accumulates dt here EXACTLY
        ONCE per control cycle (fix 2026-07-10); the decision methods below
        only RESET the timer on ON<->OFF edges. Previously every fast-source
        decision added dt itself and a safety override in the same step added
        it AGAIN, so the min-OFF wait under an active S1 elapsed twice as fast
        as wall-clock time.

        Args:
            dt_seconds: Elapsed time since the previous control step [s].
        """
        self._timer_s += dt_seconds

    def note_command(self, on: bool) -> None:
        """Record this cycle's emitted on-state for the next S4 comparison.

        Args:
            on: The ``on`` field of the command actually emitted this cycle.
        """
        self._prev_cmd_on = on

    def want(self, demand: float, *, engaged: bool) -> bool:
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

    def decide(
        self,
        *,
        want_on: bool,
        fs_mode: FastSourceMode,
        target_heating: float,
        target_cooling: float,
        flags: list[str],
    ) -> FastSourceCommand:
        """Advance the three-state machine (C6, 2026-07-09).

        The machine state is OFF / HEATING / COOLING. The dwell clock is
        advanced once per step by :meth:`tick` (fix 2026-07-10); this method
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
        current = self._state
        if current is FastSourceMode.OFF:
            if want_on:
                if self._timer_s >= cfg.fast_min_off_minutes * 60.0:
                    self._state = fs_mode
                    self._timer_s = 0.0
                elif "fast_source_min_runtime" not in flags:
                    flags.append("fast_source_min_runtime")
        else:
            keep_running = want_on and fs_mode is current
            if not keep_running:
                # OFF requested, or a direction flip: both mean "stop first".
                if self._timer_s >= cfg.fast_min_on_minutes * 60.0:
                    self._state = FastSourceMode.OFF
                    self._timer_s = 0.0
                elif "fast_source_min_runtime" not in flags:
                    flags.append("fast_source_min_runtime")
        # Remaining lock on the CURRENT state: min ON while running (cannot turn
        # off yet), min OFF while idle (cannot turn on yet). None once elapsed.
        state = self._state
        min_lock_min = (
            cfg.fast_min_off_minutes
            if state is FastSourceMode.OFF
            else cfg.fast_min_on_minutes
        )
        remaining = min_lock_min * 60.0 - self._timer_s
        self._dwell_remaining_s = remaining if remaining > 0.0 else None
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

    def force_on(self, fs_mode: FastSourceMode, target: float) -> FastSourceCommand:
        """Force the fast source ON immediately (safety S3/S4 override).

        Unlike building the command directly, this keeps the machine in sync
        (S5, 2026-07-09): the machine state is set to the commanded direction
        and the dwell timer restarted on any state change, so releasing the
        safety override later hands a *running* machine back to the normal
        min-ON dwell logic instead of instantly stopping a compressor that
        just started. This is the ONE deliberate exception to the
        change-direction-through-OFF rule: a hard S3/S4 emergency outranks
        compressor hygiene (and S3-in-summer / S4-in-winter cannot co-occur
        with the opposite direction in practice).

        Args:
            fs_mode: Direction to command (HEATING or COOLING).
            target: Room target temperature [degC] for the split.

        Returns:
            An ON :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        if self._state is not fs_mode:
            self._state = fs_mode
            self._timer_s = 0.0
        remaining = self._config.fast_min_on_minutes * 60.0 - self._timer_s
        self._dwell_remaining_s = remaining if remaining > 0.0 else None
        return FastSourceCommand(on=True, mode=fs_mode, target_temperature_c=target)

    def force_off(self) -> FastSourceCommand:
        """Force the fast source OFF immediately (safety / OFF mode).

        Bypasses the min ON timer because a lost sensor or an explicit OFF is a
        safety condition. Resets the dwell timer on the ON->OFF edge so
        re-engaging respects the min OFF dwell afterwards; on subsequent
        already-OFF cycles the timer keeps growing via the single per-step
        accumulation in :meth:`tick` (fast-F6, 2026-07-09; single-accumulation
        fix 2026-07-10), so a long sensor-lost or OFF stretch counts toward the
        min-OFF wait instead of restarting it on recovery.

        Returns:
            An OFF :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        if self._state is not FastSourceMode.OFF:
            self._state = FastSourceMode.OFF
            self._timer_s = 0.0
        # A forced OFF is a safety / OFF-mode condition, not a normal dwell gate:
        # the panel shows no assist timer here (the min-runtime flag conveys any
        # block instead).
        self._dwell_remaining_s = None
        return FastSourceCommand(
            on=False, mode=FastSourceMode.OFF, target_temperature_c=None
        )

    def reset(self) -> None:
        """Clear all machine state (direction, dwell clock, S4 bookkeeping)."""
        self._state = FastSourceMode.OFF
        self._timer_s = _INITIAL_FAST_TIMER_S
        self._synced = False
        self._mismatch = False
        self._prev_cmd_on = None
        self._dwell_remaining_s = None
