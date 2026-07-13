"""Hydraulic no-flow watchdog (rule S6) and the actuation self-test.

Motivated by the 2026-07 production incident (issue #4): after a power-event
reboot both valve controllers accepted targets without executing them while
the valve entities ECHOED the commanded position back as feedback — so every
data-path validation (age gates, plausibility gates, ``valve_mismatch``) was
blind for hours. The loop supply/return water probes are an independent
PHYSICAL witness of actuation; this module turns them into two per-loop
detections and one manual self-test:

* ``loop_no_flow`` — the valve has been commanded open (>=
  ``flow_open_threshold_pct``) for at least ``flow_response_window_min`` while
  the loop shows **no hydraulic signature** (``|T_supply - T_return|`` below
  ``flow_epsilon_k`` AND no probe displacement toward the source since the
  window opened), although circulation in the system is plausible.
* ``loop_stuck_open`` — the valve is commanded (essentially) closed yet the
  loop keeps a persistent source-side signature: a clear delta-T **and** the
  RETURN probe sitting on the source side of the room temperature. The
  return-vs-room condition realises "the return probe is the more reliable
  witness": a supply probe on the manifold bar BEFORE the valve shows a large
  delta-T without any flow, but its return then rests near the slab/room
  temperature — no false alarm.
* :class:`ActuationSelfTest` — a deliberate, MANUAL valve excursion (100 %
  for 20-30 min) whose verdict reuses the same displacement-based flow
  evidence, run per room via the ``tortoise_ufh.test_actuation`` service.

Deliberately **not** a :class:`~tortoise_ufh.core.safety.SafetyRule`: the rule
needs per-loop windows and reference temperatures rather than a scalar with
hysteresis, so it lives beside the safety evaluator as its own stateful layer
owned by :class:`~tortoise_ufh.core.controller.RoomController`. The watchdog
NEVER reads the valve entity feedback (``LoopInput.valve_position_pct``) —
that channel proved capable of lying end-to-end — and NEVER moves a valve:
the reaction is flags + an integrator freeze only.

This module is pure Python (stdlib only) and MUST NOT import
``homeassistant``.

Units:
    * Temperatures: degrees Celsius (``_c``); differences/margins: kelvin
      (``_k``).
    * Valve commands: percent 0..100 (``_pct``).
    * Times: seconds (``_s``) internally; the response window is configured
      in minutes (``flow_response_window_min``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import Mode

if TYPE_CHECKING:
    from .config import ControllerConfig
    from .models import LoopInput, RoomInputs

__all__ = [
    "CIRCULATION_DELTA_K",
    "GLOBAL_SUPPLY_MARGIN_K",
    "ActuationSelfTest",
    "FlowWatchdog",
    "LoopFlowMonitor",
]


# --- Module constants (deliberately NOT tuning knobs — K2-style fixed) -------

_FLOW_DELTA_EVIDENCE_K: float = 0.5
"""Probe displacement toward the source since the window opened that counts
as flow evidence [K]. Displacement from a captured REFERENCE, not a new
trend estimator: the watchdog asks "did the water move toward the source at
all since the valve opened" — a deterministic question over the whole
response window, with no EMA warm-up state (the 15-min trend EMA is tuned
for room-sensor noise, a different problem)."""

_FLOW_CLOSED_CMD_PCT: float = 5.0
"""Command at or below which a loop counts as "commanded closed" for the
stuck-open detection [%]. Matches the actuator write threshold so the
sub-threshold command tails observed in the incident (e.g. a 0.8 % residue)
are still guarded."""

CIRCULATION_DELTA_K: float = 1.0
"""``|T_supply - T_return|`` of ANY loop in the building that proves a live
circulating source [K]. Deliberately includes the inspected room's own loop:
a post-valve dead loop shows delta-T ~ 0 and contributes nothing, while a
manifold-bar probe pair with a large delta-T proves the source is alive.
Public: consumed by ``BuildingController._circulation_evident``."""

GLOBAL_SUPPLY_MARGIN_K: float = 3.0
"""Margin by which the optional GLOBAL supply probe must sit on the source
side of the mean room temperature to prove circulation [K] (heating:
``gs >= mean + 3``; cooling: ``gs <= mean - 3``).
Public: consumed by ``BuildingController._circulation_evident``."""

_STUCK_RETURN_ROOM_MARGIN_K: float = 1.0
"""Margin by which the RETURN probe must sit on the source side of the room
temperature for the stuck-open signature [K]. This is what makes the return
probe the decisive witness — a manifold supply probe alone cannot raise the
stuck-open flag."""

_SELFTEST_DELTA_K: float = 1.0
"""Displacement threshold for the actuation self-test verdict [K]. Raised
above :data:`_FLOW_DELTA_EVIDENCE_K` on purpose: a deliberate 100 %
excursion for 20-30 min must leave a CLEAR hydraulic signature."""

_LOOP_STATUSES: tuple[str, ...] = ("ok", "no_flow", "stuck_open", "inactive")
"""Closed vocabulary of :attr:`LoopFlowMonitor.status` /
``RoomReport.loop_flow_status`` values."""

_TEST_LOOP_RESULTS: tuple[str, ...] = ("passed", "failed", "untested")
"""Closed vocabulary of per-loop self-test verdicts."""

_TEST_STATUSES: tuple[str, ...] = ("running", "passed", "failed", "aborted")
"""Closed vocabulary of ``RoomReport.actuation_test_status`` values."""


def _flow_evidence(
    *,
    mode: Mode,
    supply_c: float,
    return_c: float,
    ref_supply_c: float,
    ref_return_c: float,
    epsilon_k: float,
    displacement_k: float,
) -> bool:
    """Return whether the loop shows ANY evidence that water is flowing.

    Shared predicate of the S6 no-flow window and the actuation self-test.
    Evidence is any of (deliberately erring toward "no alarm" — a single
    noisy sample resets the window, which is the safe direction):

    1. ``|supply - return| >= epsilon_k`` — the classic through-flow
       signature.
    2. The RETURN probe displaced toward the source since the reference was
       captured (heating: warmer; cooling: colder) by at least
       ``displacement_k``.
    3. The SUPPLY probe displaced likewise — a weaker witness (the probe may
       sit on the manifold bar before the valve), which is why it only ever
       counts AS evidence of flow, i.e. in the direction safe from false
       alarms.

    Args:
        mode: Active room mode (HEATING or COOLING) — flips the "toward the
            source" direction.
        supply_c: Current loop supply temperature [degC].
        return_c: Current loop return temperature [degC].
        ref_supply_c: Supply temperature captured when the window opened
            [degC].
        ref_return_c: Return temperature captured when the window opened
            [degC].
        epsilon_k: Minimum supply-return difference counting as flow [K].
        displacement_k: Minimum probe displacement toward the source [K].

    Returns:
        ``True`` when any of the three witnesses reports flow.
    """
    if abs(supply_c - return_c) >= epsilon_k:
        return True
    if mode is Mode.HEATING:
        return (
            return_c - ref_return_c >= displacement_k
            or supply_c - ref_supply_c >= displacement_k
        )
    return (
        ref_return_c - return_c >= displacement_k
        or ref_supply_c - supply_c >= displacement_k
    )


class LoopFlowMonitor:
    """Window machine of ONE loop's no-flow / stuck-open detection.

    Stateful: accumulates the no-flow and stuck-open windows across cycles,
    holds the reference temperatures captured when the no-flow window opened,
    and remembers the last computed status. One instance per loop, owned by
    :class:`FlowWatchdog`.
    """

    def __init__(self) -> None:
        """Initialise an idle monitor (no windows, ``"inactive"`` status)."""
        self._no_flow_elapsed_s: float = 0.0
        self._stuck_elapsed_s: float = 0.0
        self._ref_supply_c: float | None = None
        self._ref_return_c: float | None = None
        self._no_flow_active: bool = False
        self._stuck_active: bool = False
        self._status: str = "inactive"

    # -- properties ----------------------------------------------------------

    @property
    def status(self) -> str:
        """Last computed loop status (one of :data:`_LOOP_STATUSES`)."""
        return self._status

    @property
    def no_flow_active(self) -> bool:
        """Whether the loop's no-flow window has elapsed (flag latched)."""
        return self._no_flow_active

    @property
    def stuck_open_active(self) -> bool:
        """Whether the loop's stuck-open window has elapsed (flag latched)."""
        return self._stuck_active

    # -- public API -----------------------------------------------------------

    def restart(self) -> None:
        """Reset windows, references and flags (status back to inactive).

        Called on controller reset and after an actuation self-test ends —
        the deliberate excursion moved the loop temperatures, so the pre-test
        references would fake displacement evidence (or its absence).
        """
        self._no_flow_elapsed_s = 0.0
        self._stuck_elapsed_s = 0.0
        self._ref_supply_c = None
        self._ref_return_c = None
        self._no_flow_active = False
        self._stuck_active = False
        self._status = "inactive"

    def update(
        self,
        *,
        mode: Mode,
        commanded_pct: float | None,
        supply_c: float | None,
        return_c: float | None,
        room_temperature_c: float | None,
        circulation_evident: bool | None,
        dt_seconds: float,
        epsilon_k: float,
        open_threshold_pct: float,
        window_s: float,
    ) -> str:
        """Advance the loop's windows by one control cycle.

        Args:
            mode: The room's operating mode this cycle.
            commanded_pct: The valve position the CORE emitted on the
                previous cycle [%], or ``None`` before the first emission.
                Never the valve-entity feedback — the whole point of S6 is
                that the feedback channel can lie.
            supply_c: The loop's supply probe [degC], or ``None``.
            return_c: The loop's return probe [degC], or ``None``.
            room_temperature_c: The room temperature [degC], or ``None`` —
                used by the stuck-open return-vs-room condition (a missing
                room temperature degrades that condition to delta-T only).
            circulation_evident: The building-level circulation gate:
                ``True`` = a live source is proven, ``False`` = provably
                idle, ``None`` = unknown. Anything but ``True`` HOLDS the
                no-flow window (no accumulation, no reset). The stuck-open
                window is deliberately NOT gated — a persistent loop
                signature is itself proof that water moves.
            dt_seconds: Elapsed time this cycle [s].
            epsilon_k: ``flow_epsilon_k`` [K].
            open_threshold_pct: ``flow_open_threshold_pct`` [%].
            window_s: ``flow_response_window_min`` converted to seconds.

        Returns:
            The loop status after this cycle (one of
            :data:`_LOOP_STATUSES`).
        """
        if mode not in (Mode.HEATING, Mode.COOLING) or commanded_pct is None:
            self.restart()
            return self._status
        if supply_c is None or return_c is None:
            # Stale/missing probes: HOLD everything (a transiently stale
            # probe must not reset a 40-min window) but report inactive.
            self._status = "inactive"
            return self._status

        if commanded_pct >= open_threshold_pct:
            self._stuck_elapsed_s = 0.0
            self._stuck_active = False
            self._update_no_flow(
                mode=mode,
                supply_c=supply_c,
                return_c=return_c,
                circulation_evident=circulation_evident,
                dt_seconds=dt_seconds,
                epsilon_k=epsilon_k,
                window_s=window_s,
            )
            if self._no_flow_active:
                self._status = "no_flow"
            elif circulation_evident is True:
                self._status = "ok"
            else:
                # Gate unknown / pump provably idle: the watchdog is paused,
                # the panel shows an em dash.
                self._status = "inactive"
        elif commanded_pct <= _FLOW_CLOSED_CMD_PCT:
            self._reset_no_flow()
            self._update_stuck(
                mode=mode,
                supply_c=supply_c,
                return_c=return_c,
                room_temperature_c=room_temperature_c,
                dt_seconds=dt_seconds,
                epsilon_k=epsilon_k,
                window_s=window_s,
            )
            self._status = "stuck_open" if self._stuck_active else "ok"
        else:
            # Middle zone: neither open enough to expect a response nor
            # closed enough to expect silence — full reset of both windows.
            self._reset_no_flow()
            self._stuck_elapsed_s = 0.0
            self._stuck_active = False
            self._status = "ok"
        return self._status

    # -- internal -------------------------------------------------------------

    def _reset_no_flow(self) -> None:
        """Reset the no-flow window, references and flag."""
        self._no_flow_elapsed_s = 0.0
        self._ref_supply_c = None
        self._ref_return_c = None
        self._no_flow_active = False

    def _update_no_flow(
        self,
        *,
        mode: Mode,
        supply_c: float,
        return_c: float,
        circulation_evident: bool | None,
        dt_seconds: float,
        epsilon_k: float,
        window_s: float,
    ) -> None:
        """Advance the no-flow window (command is at/above the threshold)."""
        if self._ref_supply_c is None or self._ref_return_c is None:
            self._ref_supply_c = supply_c
            self._ref_return_c = return_c
        evidence = _flow_evidence(
            mode=mode,
            supply_c=supply_c,
            return_c=return_c,
            ref_supply_c=self._ref_supply_c,
            ref_return_c=self._ref_return_c,
            epsilon_k=epsilon_k,
            displacement_k=_FLOW_DELTA_EVIDENCE_K,
        )
        if evidence:
            # Physical evidence is decisive: clear immediately, refresh the
            # references so a later stall is measured from here. No extra
            # hysteresis needed — the >= 30 min window is the anti-flap and
            # a noisy "evidence" sample errs toward NO alarm.
            self._no_flow_elapsed_s = 0.0
            self._ref_supply_c = supply_c
            self._ref_return_c = return_c
            self._no_flow_active = False
            return
        if circulation_evident is True:
            self._no_flow_elapsed_s += dt_seconds
        # circulation None/False: HOLD (no accumulation, no reset).
        if self._no_flow_elapsed_s >= window_s:
            self._no_flow_active = True

    def _update_stuck(
        self,
        *,
        mode: Mode,
        supply_c: float,
        return_c: float,
        room_temperature_c: float | None,
        dt_seconds: float,
        epsilon_k: float,
        window_s: float,
    ) -> None:
        """Advance the stuck-open window (command is essentially closed)."""
        signature = abs(supply_c - return_c) >= epsilon_k
        if signature and room_temperature_c is not None:
            if mode is Mode.HEATING:
                signature = return_c >= room_temperature_c + _STUCK_RETURN_ROOM_MARGIN_K
            else:
                signature = return_c <= room_temperature_c - _STUCK_RETURN_ROOM_MARGIN_K
        if signature:
            self._stuck_elapsed_s += dt_seconds
        else:
            self._stuck_elapsed_s = 0.0
            self._stuck_active = False
        if self._stuck_elapsed_s >= window_s:
            self._stuck_active = True


class FlowWatchdog:
    """Per-room aggregation of one :class:`LoopFlowMonitor` per loop.

    Owned by :class:`~tortoise_ufh.core.controller.RoomController`; updated
    at the START of every control step (before the PID runs, so the
    integrator-freeze verdict is already known). The monitor list is resized
    lazily to the number of loops the room reports.
    """

    def __init__(self, config: ControllerConfig) -> None:
        """Initialise an empty watchdog.

        Args:
            config: The room's tuning (reads ``flow_epsilon_k``,
                ``flow_open_threshold_pct``, ``flow_response_window_min``).
        """
        self._config = config
        self._monitors: list[LoopFlowMonitor] = []

    # -- properties ----------------------------------------------------------

    @property
    def no_flow_active(self) -> bool:
        """Whether ANY loop currently latches the no-flow flag."""
        return any(m.no_flow_active for m in self._monitors)

    @property
    def stuck_open_active(self) -> bool:
        """Whether ANY loop currently latches the stuck-open flag."""
        return any(m.stuck_open_active for m in self._monitors)

    @property
    def loop_statuses(self) -> tuple[str, ...]:
        """Per-loop statuses, aligned with the room's ``inputs.loops``."""
        return tuple(m.status for m in self._monitors)

    # -- public API -----------------------------------------------------------

    def update(
        self,
        inputs: RoomInputs,
        *,
        commanded_pct: float | None,
        dt_seconds: float,
        paused: bool = False,
    ) -> None:
        """Advance every loop monitor by one control cycle.

        Args:
            inputs: The room's raw inputs this cycle (loops, mode, room
                temperature and the injected ``circulation_evident`` gate).
            commanded_pct: The valve position the core emitted on the
                PREVIOUS cycle [%] (never the entity feedback), or ``None``.
            dt_seconds: Elapsed time this cycle [s].
            paused: ``True`` while an actuation self-test runs — the
                deliberate excursion would reset every window's references,
                so the whole watchdog HOLDS.
        """
        self._resize(len(inputs.loops))
        if paused:
            return
        for monitor, loop in zip(self._monitors, inputs.loops, strict=True):
            monitor.update(
                mode=inputs.mode,
                commanded_pct=commanded_pct,
                supply_c=loop.supply_temperature_c,
                return_c=loop.return_temperature_c,
                room_temperature_c=inputs.room_temperature_c,
                circulation_evident=inputs.circulation_evident,
                dt_seconds=dt_seconds,
                epsilon_k=self._config.flow_epsilon_k,
                open_threshold_pct=self._config.flow_open_threshold_pct,
                window_s=self._config.flow_response_window_min * 60.0,
            )

    def restart_windows(self) -> None:
        """Restart every loop's windows (after a self-test / on reset)."""
        for monitor in self._monitors:
            monitor.restart()

    def reset(self) -> None:
        """Drop all monitors (full controller reset)."""
        self._monitors.clear()

    # -- internal -------------------------------------------------------------

    def _resize(self, n_loops: int) -> None:
        """Match the monitor list to the room's current loop count."""
        while len(self._monitors) < n_loops:
            self._monitors.append(LoopFlowMonitor())
        if len(self._monitors) > n_loops:
            del self._monitors[n_loops:]


class ActuationSelfTest:
    """State machine of the manual per-room actuation self-test.

    Lives in the core (not the adapter) because the verdict needs the same
    hydraulic-evidence predicate as S6, multi-cycle state (the core has no
    wall clock — time is the accumulated ``dt_seconds``, like the fast-source
    dwell), and digital-twin testability. Owned by
    :class:`~tortoise_ufh.core.controller.RoomController`, which drives the
    valve excursion (100 %) BEFORE the safety override so S1/S2 always win,
    freezes the integrator while the test runs, and aborts on any condition
    that would invalidate the measurement.
    """

    def __init__(self) -> None:
        """Initialise an idle self-test (no result yet)."""
        self._running: bool = False
        self._elapsed_s: float = 0.0
        self._duration_s: float = 0.0
        self._mode: Mode = Mode.HEATING
        self._refs: tuple[tuple[float, float] | None, ...] = ()
        self._result: str | None = None
        self._loop_results: tuple[str, ...] = ()

    # -- properties ----------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether a test is currently running."""
        return self._running

    @property
    def mode(self) -> Mode:
        """The mode the running/last test was started in."""
        return self._mode

    @property
    def report_status(self) -> str | None:
        """The ``RoomReport.actuation_test_status`` value for this cycle."""
        if self._running:
            return "running"
        return self._result

    @property
    def remaining_min(self) -> float | None:
        """Minutes remaining of a running test, or ``None`` when idle."""
        if not self._running:
            return None
        return max(0.0, (self._duration_s - self._elapsed_s) / 60.0)

    @property
    def failed(self) -> bool:
        """Whether the LAST completed test failed (sticky until re-run)."""
        return self._result == "failed"

    @property
    def loop_results(self) -> tuple[str, ...]:
        """Per-loop verdicts of the last completed test (empty when none)."""
        return self._loop_results

    # -- public API -----------------------------------------------------------

    def begin(
        self,
        *,
        duration_s: float,
        loops: tuple[LoopInput, ...],
        mode: Mode,
    ) -> str | None:
        """Start a test, capturing per-loop probe references.

        The mode/probe/dew preconditions are validated by
        :meth:`RoomController.begin_actuation_test` (which holds the last
        inputs snapshot); this method only rejects an impossible geometry.

        Args:
            duration_s: Test duration [s] (> 0).
            loops: The room's loops as of the LAST control cycle — their
                probe values become the displacement references.
            mode: The active mode (HEATING or COOLING).

        Returns:
            ``None`` on a successful start, or ``"no_probes"`` when no loop
            carries both probes.

        Raises:
            ValueError: If ``duration_s`` is not positive.
        """
        if duration_s <= 0:
            msg = f"duration_s must be > 0, got {duration_s}"
            raise ValueError(msg)
        refs: list[tuple[float, float] | None] = []
        for loop in loops:
            if (
                loop.supply_temperature_c is not None
                and loop.return_temperature_c is not None
            ):
                refs.append((loop.supply_temperature_c, loop.return_temperature_c))
            else:
                refs.append(None)
        if not any(ref is not None for ref in refs):
            return "no_probes"
        self._running = True
        self._elapsed_s = 0.0
        self._duration_s = duration_s
        self._mode = mode
        self._refs = tuple(refs)
        self._result = None
        self._loop_results = ()
        return None

    def abort(self) -> None:
        """Abort a running test (sensor loss / mode change / safety)."""
        if not self._running:
            return
        self._running = False
        self._result = "aborted"
        self._loop_results = ()

    def cancel(self) -> None:
        """Cancel a running test on the user's request (same as abort)."""
        self.abort()

    def advance(
        self,
        dt_seconds: float,
        loops: tuple[LoopInput, ...],
        *,
        epsilon_k: float,
    ) -> bool:
        """Accumulate test time and, at the end, compute the verdict.

        Args:
            dt_seconds: Elapsed time this cycle [s].
            loops: The room's loops this cycle (current probe values).
            epsilon_k: The room's ``flow_epsilon_k`` [K].

        Returns:
            ``True`` when the test completed this cycle.
        """
        if not self._running:
            return False
        self._elapsed_s += dt_seconds
        if self._elapsed_s < self._duration_s:
            return False
        results: list[str] = []
        all_passed = True
        for i, ref in enumerate(self._refs):
            loop = loops[i] if i < len(loops) else None
            if (
                ref is None
                or loop is None
                or loop.supply_temperature_c is None
                or loop.return_temperature_c is None
            ):
                results.append("untested")
                continue
            passed = _flow_evidence(
                mode=self._mode,
                supply_c=loop.supply_temperature_c,
                return_c=loop.return_temperature_c,
                ref_supply_c=ref[0],
                ref_return_c=ref[1],
                epsilon_k=epsilon_k,
                displacement_k=_SELFTEST_DELTA_K,
            )
            results.append("passed" if passed else "failed")
            if not passed:
                all_passed = False
        measured = [r for r in results if r != "untested"]
        self._running = False
        self._result = "passed" if measured and all_passed else "failed"
        self._loop_results = tuple(results)
        return True

    def reset(self) -> None:
        """Clear all state, including the last result."""
        self._running = False
        self._elapsed_s = 0.0
        self._duration_s = 0.0
        self._refs = ()
        self._result = None
        self._loop_results = ()
