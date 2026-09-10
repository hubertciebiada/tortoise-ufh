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

Manual hold (2026-09-04, DECISIONS §28): a SETTLED physical feedback that
disagrees with the previously emitted command is USER INTENT, not a fault.
"Settled" means our own EMITTED command pair has not changed for at least
half a ``cycle_seconds`` — in practice the second regular cycle after our
change (a 2-s debounced recompute or a few-second climate-integration lag
must not be mistaken for a touch; the threshold sits well clear of both and
of the jittery real cycle length). The machine adopts the physical state and,
for ``fast_manual_hold_minutes``, the controller only mirrors it
(:meth:`FastSourceMachine.mirror`) — nothing is written, so the adapter's
re-assert cannot stomp on the user's choice, and a unit the user set to cool
is never flipped straight to heat. The hold counts down in :meth:`tick`; the
safety forces (:meth:`FastSourceMachine.force_on` /
:meth:`FastSourceMachine.force_off`) end it and leave the mismatch flag as
an honest trace. Knob ``0`` keeps the legacy behaviour (mismatch flag only).
What the adapter REALLY wrote (a demoted DRY) is reported through
:meth:`FastSourceMachine.note_written`, which feeds the divergence check but
never the settle counter.

Blind commands (2026-09-10, DECISIONS §28 note): a command emitted in a step
whose :meth:`FastSourceMachine.sync` saw NO feedback (the climate entity
unavailable — typically the first cycle after a Home Assistant restart) cannot
have reached the unit, because the adapter writes nothing to an unavailable
entity. It is therefore NOT recorded as the reference the next feedback is
compared against, and it does not restart the settle counter either. The first
visible feedback of a machine is always adopted as the truth (the S4
first-sync rule), however many blind commands preceded it — and not only after
a restart: ANY gap in the feedback un-syncs the machine, so the unit found when
it reappears is adopted afresh (conservative dwell seed, no hold), never judged
against a command it may not have received. A manual hold survives the gap: a
safety force the unit cannot see does not end it.

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
    "DRY_HYSTERESIS_K",
    "FAST_TARGET_OFFSET_K",
    "FastSourceMachine",
    "direction_of",
    "window_allows",
]

MINUTES_PER_DAY: int = 24 * 60
"""Minutes in one day — the domain of :func:`window_allows` arguments."""


def window_allows(minute_of_day: int, start_minute: int, end_minute: int) -> bool:
    """Whether a minute-of-day falls inside an allowed-hours window (B1).

    Pure window arithmetic for the per-room fast-source quiet hours: the
    window is the range in which the fast source MAY run. A normal window
    (``start < end``) allows ``start <= t < end``; a window crossing midnight
    (``start > end``, e.g. 22:00-07:00) allows ``t >= start or t < end``.
    The core never reads a wall clock — the ADAPTER computes the local
    minute-of-day and calls this; it lives here only so the midnight/edge
    cases are covered by pure unit tests without Home Assistant.

    ``start == end`` is rejected by the adapter's config validation and is
    deterministically treated as an EMPTY window (nothing allowed) — a
    degenerate all-day window would silently disable the feature instead.

    Args:
        minute_of_day: Local minute of the day, ``0..1439``.
        start_minute: Window start (inclusive), minutes after midnight.
        end_minute: Window end (exclusive), minutes after midnight.

    Returns:
        ``True`` when the fast source is allowed to run at that minute.

    Raises:
        ValueError: If any argument is outside ``[0, 1439]``.
    """
    for label, value in (
        ("minute_of_day", minute_of_day),
        ("start_minute", start_minute),
        ("end_minute", end_minute),
    ):
        if not 0 <= value < MINUTES_PER_DAY:
            msg = f"{label} must be in [0, {MINUTES_PER_DAY - 1}], got {value}"
            raise ValueError(msg)
    if start_minute == end_minute:
        return False
    if start_minute < end_minute:
        return start_minute <= minute_of_day < end_minute
    return minute_of_day >= start_minute or minute_of_day < end_minute


_INITIAL_FAST_TIMER_S: float = 1.0e9
"""Initial fast-source dwell timer [s] used only while the physical fast-source
state is UNKNOWN (no ``fast_source_on`` feedback configured).

Seeded large so the very first ON/OFF transition is never blocked by the
minimum OFF/ON dwell time. The moment a physical on/off feedback is first
observed the machine re-seeds the timer conservatively to 0 (a full dwell must
elapse before any state change), so an HA restart/reload loop can never
short-cycle a compressor (amendment 2026-07-09, S4).
"""

_HVAC_MODE_TO_DIRECTION: dict[str, FastSourceMode] = {
    "heat": FastSourceMode.HEATING,
    "heating": FastSourceMode.HEATING,
    "cool": FastSourceMode.COOLING,
    "cooling": FastSourceMode.COOLING,
    # DRY is cooling-SIDE refrigerant-wise (dry assist, DECISIONS §24): a unit
    # reported in "dry" agrees with a commanded COOLING/DRY, conflicts with
    # HEATING.
    "dry": FastSourceMode.COOLING,
}
"""Map of raw HVAC-mode feedback strings to a fast-source DIRECTION (K4).

Only single-direction modes are mapped (``"dry"`` counts as the cooling side);
anything else (``"off"``, ``"auto"``, ``"heat_cool"``, ``"fan_only"``, vendor
strings) yields no direction and the reconciliation falls back to the plain
on/off check.
"""

DRY_HYSTERESIS_K: float = 1.0
"""Release hysteresis of the dry assist below ``dry_dew_max_c`` [K] (§24).

A dry run engages when the room dew point exceeds the knob and releases only
once it falls this far below it. Deliberately a constant, not a knob: RH
sensors are slow and noisy, and 1 K of dew-point hysteresis absorbs their
jitter without another parameter.
"""


def direction_of(mode: FastSourceMode | None) -> FastSourceMode | None:
    """Normalise a command mode to its refrigerant DIRECTION (§24).

    ``DRY`` is the cooling side (a split's dry mode runs the same circuit
    direction as cool); everything else maps to itself.

    Args:
        mode: A command mode, or ``None``.

    Returns:
        ``COOLING`` for ``DRY``, otherwise ``mode`` unchanged.
    """
    if mode is FastSourceMode.DRY:
        return FastSourceMode.COOLING
    return mode


FAST_TARGET_OFFSET_K: float = 1.0
"""Split target offset from the room setpoint in HEATING/COOLING [K].

Since 2026-07-13 this is only the DEFAULT of the
:class:`~tortoise_ufh.core.config.ControllerConfig` ``fast_target_offset_k``
tuning knob (owner request: adjustable per room, ``0`` disables the
overdrive); the controller reads the knob, not this constant.

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
        if machine.manual_hold_active:              # §28: the user touched it
            command = machine.mirror()
        else:
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
        # seed); a later divergence is adopted as user intent under a manual
        # hold (§28) or, with the hold knob at 0, raises ``fast_source_mismatch``.
        self._synced: bool = False
        self._mismatch: bool = False
        # Whether the latest sync() saw a feedback (2026-09-10): a command
        # emitted while the unit is invisible never reached it (the adapter
        # writes nothing to an unavailable entity), so it is not recorded as
        # the reference below and does not touch the settle counter.
        self._unit_visible: bool = False
        # The on-state of the last command emitted while the unit was VISIBLE
        # — the reference the next feedback is compared against (S4).
        self._prev_cmd_on: bool | None = None
        # Direction of that same command (K4, 2026-07-12): lets the
        # reconciliation flag a unit physically running in the OPPOSITE
        # direction (multisplit standby / manual override), which the plain
        # on/off comparison is blind to.
        self._prev_cmd_mode: FastSourceMode | None = None
        # Mode of the last emitted (or written) command, visible or not — the
        # dry-assist hysteresis state behind ``last_command_mode`` (§24).
        self._last_cmd_mode: FastSourceMode | None = None
        # The (on, direction) pair the core last EMITTED while the unit was
        # visible (§28 settle rule) — distinct from the written pair above: a
        # degraded write reported via note_written() must not look like a
        # command change every cycle.
        self._last_emitted_pair: tuple[bool, FastSourceMode | None] | None = None
        # Seconds since that EMITTED (on, direction) command pair last CHANGED
        # (§28 settle rule): reset by note_command() on a change, advanced by
        # tick(). A divergence is adopted as user intent only once this has
        # reached half a cycle — a unit that has not caught up with OUR OWN
        # fresh command (2-s debounced recompute, slow climate integration)
        # is not a touch.
        self._since_cmd_change_s: float = 0.0
        # Seconds remaining on the min ON/OFF dwell lock (None = unlocked / no
        # fast source). Recomputed each cycle by the decision methods and
        # surfaced in the report for the panel's assist timer.
        self._dwell_remaining_s: float | None = None
        # Manual hold (§28): seconds left during which the controller only
        # MIRRORS the adopted physical state. 0 = no hold. Restarted on every
        # new divergence, counted down by tick(), ended by force_on/force_off.
        self._manual_hold_s: float = 0.0

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

    @property
    def last_command_mode(self) -> FastSourceMode | None:
        """Mode of the previously EMITTED command, or ``None`` before any (§24).

        Lets the controller distinguish "the machine runs because of
        temperature" (COOLING/HEATING) from "it runs because of humidity"
        (DRY) — the two demands carry separate hysteresis states. Recorded
        whether or not the unit was visible (blind commands only stay out of
        the S4 reference).
        """
        return self._last_cmd_mode

    @property
    def manual_hold_active(self) -> bool:
        """Whether a manual hold is running (§28): mirror, do not decide."""
        return self._manual_hold_s > 0.0

    @property
    def manual_hold_remaining_s(self) -> float:
        """Seconds left on the manual hold, ``0.0`` when none is active (§28)."""
        return self._manual_hold_s

    # -- public API -----------------------------------------------------------

    def sync(self, inputs: RoomInputs) -> None:
        """Reconcile the machine with the physical unit (S4, step 0).

        On the FIRST cycle that carries a physical ``fast_source_on`` feedback
        the physical state wins: a running unit is adopted as ON (the
        reported direction when unambiguous, else the global mode; COOLING is
        adopted only for a SPLIT — a HEATER can never cool), a stopped unit as
        OFF, and in BOTH cases the dwell timer is re-seeded to 0 so a full
        min-ON/min-OFF must elapse before the machine may change state — a
        restart/reload (every tuning change!) can therefore never short-cycle
        a compressor.

        This holds even when the machine emitted commands before the first
        feedback arrived (2026-09-10): those were emitted BLIND — the adapter
        writes nothing to an unavailable climate entity — so none of them
        reached the unit and the first reading is the truth, not a stale echo.
        The case is the first cycle after a Home Assistant restart: the
        rebuilt machine force-stops on a room sensor that is not up yet
        (often the split's own probe), and the retired "late first feedback"
        guard then read the split it had started itself before the restart as
        a manual touch. A cycle WITHOUT feedback on a synced machine un-syncs
        it (the reference is dropped), so after any gap — a Wi-Fi blip, a
        split that rebooted — the unit found is adopted by the same rule: the
        machine cannot know what reached the unit meanwhile. An ambiguous
        running report ("auto", "fan_only", ...) keeps the machine's own
        direction; a unit found drying keeps the dry-assist band.

        While visible, the machine records every emitted command as the
        reference for the next reconciliation (:meth:`note_command`); a blind
        cycle records nothing.

        On later cycles a feedback that disagrees with that recorded command
        is a DIVERGENCE. Since 2026-07-12 (K4) the comparison
        also sees the DIRECTION: a unit physically running in a
        single-direction HVAC mode opposite to the commanded one (multisplit
        standby, manual reversal) diverges even though the plain on/off
        feedback agrees. What a divergence does depends on
        ``fast_manual_hold_minutes`` (§28, 2026-09-04):

        * knob ``> 0`` (default 60) AND the emitted command pair has been
          stable for at least half a ``cycle_seconds`` (the settle rule): the
          divergence is USER INTENT — the machine ADOPTS the physical state
          (a running unit takes the reported direction when unambiguous,
          else the first-sync fallback; a stopped unit becomes OFF; the dwell
          timer restarts on any state change) and a manual hold of that many
          minutes starts — RESTARTED on every new divergence, so it is
          measured from the last touch. No mismatch flag is raised; the
          controller reports ``"fast_source_manual"`` and only mirrors the
          state until the hold elapses.
        * knob ``> 0`` but OUR OWN command changed less than half a cycle
          ago: the unit may simply not have caught up (the coordinator's
          debounced off-cycle recompute runs ~2 s after a write; a climate
          integration may report a few seconds late) — only the mismatch
          flag is set, nothing is adopted. Because the controller runs
          ``sync -> tick -> decide -> note_command``, the settle counter seen
          by ``sync`` lags one step behind: adoption after our own command
          change happens at the SECOND regular cycle — deterministically,
          since half a cycle is far from the jittery real cycle length (a
          full-cycle threshold was a knife edge: ``299.99 < 300`` slipped
          the adoption to the next cycle at random).
        * knob ``0``: legacy behaviour — only the mismatch flag is set, the
          machine stays the owner and the adapter's periodic re-assert
          converges the hardware back.

        Args:
            inputs: The room's raw inputs for this cycle.
        """
        self._mismatch = False
        self._unit_visible = False
        if inputs.fast_source_kind is FastSourceKind.NONE:
            return
        physical = inputs.fast_source_on
        if physical is None:
            # A gap in the feedback: nothing emitted until the unit is
            # visible again can be known to reach it, so drop the reference —
            # the next visible reading is adopted afresh (first-sync rule).
            self._synced = False
            self._prev_cmd_on = None
            self._prev_cmd_mode = None
            self._last_emitted_pair = None
            return
        self._unit_visible = True
        raw_hvac = (inputs.fast_source_hvac_mode or "").lower()
        reported = _HVAC_MODE_TO_DIRECTION.get(raw_hvac) if raw_hvac else None
        if not self._synced:
            # First visible feedback (start-up or after a gap) — adopt the
            # physical state. Any command emitted before it was blind, so a
            # running unit takes the reported HVAC direction when unambiguous
            # (K4); a HEATER can never cool. With an ambiguous report
            # ("auto", "fan_only", ...) the machine's own direction is the
            # better guess than the global-mode fallback (in TRANSITIONAL
            # that would be HEATING for a unit engaged to cool). A unit found
            # drying keeps the dry-assist release band (§24).
            self._synced = True
            if not physical:
                self._state = FastSourceMode.OFF
            elif reported is not None or self._state is FastSourceMode.OFF:
                self._state = self._running_direction(reported, inputs)
            if physical and raw_hvac == "dry":
                self._last_cmd_mode = FastSourceMode.DRY
            # Conservative seed: a full dwell from now, whatever the state.
            self._timer_s = 0.0
            return
        if not self._diverges(physical, reported):
            return
        if (
            self._config.fast_manual_hold_minutes <= 0.0
            or self._since_cmd_change_s < 0.5 * self._config.cycle_seconds
        ):
            # Legacy (knob 0), or our own command changed less than half a
            # cycle ago and the unit may not have caught up yet: flag only,
            # the machine stays the owner.
            self._mismatch = True
            return
        # Manual hold (§28): the user touched the unit — adopt what it is
        # physically doing and hold that for the configured time, measured
        # from THIS (latest) touch.
        adopted = (
            self._running_direction(reported, inputs)
            if physical
            else FastSourceMode.OFF
        )
        if adopted is not self._state:
            self._state = adopted
            self._timer_s = 0.0
        self._manual_hold_s = self._config.fast_manual_hold_minutes * 60.0

    def _diverges(self, physical: bool, reported: FastSourceMode | None) -> bool:
        """Whether a physical feedback disagrees with the last recorded command.

        The S4/K4 reconciliation condition, against the last command emitted
        while the unit was visible: the on/off state differs, or both are ON
        and the unit reports the OPPOSITE refrigerant direction. Directions are
        compared refrigerant-side (§24): a commanded DRY normalises to
        COOLING, so a unit reporting "dry" or "cool" while dry-assisting is
        NOT a divergence.

        Args:
            physical: The unit's on/off feedback this cycle.
            reported: The direction parsed from ``fast_source_hvac_mode``, or
                ``None`` when the feedback carries no single direction.

        Returns:
            ``True`` on a divergence; ``False`` before any command was
            recorded.
        """
        if self._prev_cmd_on is None:
            return False
        if physical is not self._prev_cmd_on:
            return True
        return (
            physical
            and self._prev_cmd_mode is not None
            and reported is not None
            and reported is not direction_of(self._prev_cmd_mode)
        )

    @staticmethod
    def _running_direction(
        reported: FastSourceMode | None, inputs: RoomInputs
    ) -> FastSourceMode:
        """Direction adopted for a unit that is physically RUNNING (S4/K4).

        The reported HVAC direction wins when unambiguous; otherwise the
        direction follows the global mode. A HEATER can never cool, whatever
        the feedback claims. Shared by the first-sync adoption and the
        manual-hold adoption (§28) so both apply one rule.

        Args:
            reported: The direction parsed from ``fast_source_hvac_mode``, or
                ``None`` when the feedback carries no single direction.
            inputs: The room's raw inputs (global mode, fast-source kind).

        Returns:
            ``HEATING`` or ``COOLING`` — never ``OFF``.
        """
        if reported is not None and (
            reported is FastSourceMode.HEATING
            or inputs.fast_source_kind is FastSourceKind.SPLIT
        ):
            return reported
        return (
            FastSourceMode.COOLING
            if (
                inputs.mode is Mode.COOLING
                and inputs.fast_source_kind is FastSourceKind.SPLIT
            )
            else FastSourceMode.HEATING
        )

    def tick(self, dt_seconds: float) -> None:
        """Advance the dwell clock by ``dt_seconds`` — exactly once per step.

        The caller (:meth:`RoomController.step`) accumulates dt here EXACTLY
        ONCE per control cycle (fix 2026-07-10); the decision methods below
        only RESET the timer on ON<->OFF edges. Previously every fast-source
        decision added dt itself and a safety override in the same step added
        it AGAIN, so the min-OFF wait under an active S1 elapsed twice as fast
        as wall-clock time.

        The manual hold (§28) counts down here too, once per step, and the
        settle counter (seconds since our own command pair last changed)
        advances.

        Args:
            dt_seconds: Elapsed time since the previous control step [s].
        """
        self._timer_s += dt_seconds
        self._since_cmd_change_s += dt_seconds
        if self._manual_hold_s > 0.0:
            self._manual_hold_s = max(0.0, self._manual_hold_s - dt_seconds)

    def mirror(self) -> FastSourceCommand:
        """Emit the command that MIRRORS the adopted state (manual hold, §28).

        Called by the controller instead of :meth:`want` / :meth:`decide`
        while :attr:`manual_hold_active`: the state is whatever the user set
        the unit to, so the command echoes it — ``on`` when the state is not
        OFF, ``mode`` = state, ``target_temperature_c`` = ``None`` (the unit
        runs at the USER's own setpoint, which the controller does not know;
        a fabricated target would show up in the panel and sensors as ours,
        and the adapter writes nothing during a hold anyway). Recording the
        command via :meth:`note_command` is what lets the next :meth:`sync`
        see agreement and NOT restart the hold. The dwell lock is surfaced
        exactly like :meth:`decide` does, so the panel timer stays honest
        about the min ON/OFF that still applies once the hold elapses.

        Returns:
            The mirrored :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        cfg = self._config
        state = self._state
        min_lock_min = (
            cfg.fast_min_off_minutes
            if state is FastSourceMode.OFF
            else cfg.fast_min_on_minutes
        )
        remaining = min_lock_min * 60.0 - self._timer_s
        self._dwell_remaining_s = remaining if remaining > 0.0 else None
        if state is FastSourceMode.OFF:
            return FastSourceCommand(
                on=False, mode=FastSourceMode.OFF, target_temperature_c=None
            )
        return FastSourceCommand(on=True, mode=state, target_temperature_c=None)

    def note_command(self, on: bool, mode: FastSourceMode | None = None) -> None:
        """Record this cycle's EMITTED command for the next S4 comparison.

        Also restarts the settle counter (§28) whenever the EMITTED
        ``(on, direction_of(mode))`` pair CHANGES versus the previously
        emitted one — a divergence right after our own command change is the
        unit catching up, not a touch. The comparison is against the last
        EMITTED pair, not the last written one: a degraded write reported via
        :meth:`note_written` every cycle (``dry_unsupported``) would otherwise
        read as a command change every cycle and the hold could never arm.

        Only a command emitted while the unit was VISIBLE in the latest
        :meth:`sync` is recorded (2026-09-10): a blind command was never
        written (the adapter skips an unavailable entity), so neither the
        reconciliation reference nor the settle counter may move — otherwise
        its first real delivery, cycles later, would look settled and the
        unit catching up with it would be adopted as a manual touch. The mode
        behind :attr:`last_command_mode` is recorded either way.

        Args:
            on: The ``on`` field of the command actually emitted this cycle.
            mode: The command's direction (K4, 2026-07-12) so the next
                reconciliation can also flag a DIRECTION divergence. ``None``
                (legacy callers) records the on-state only.
        """
        self._last_cmd_mode = mode
        if not self._unit_visible:
            return
        pair = (on, direction_of(mode))
        if pair != self._last_emitted_pair:
            self._since_cmd_change_s = 0.0
        self._last_emitted_pair = pair
        self._prev_cmd_on = on
        self._prev_cmd_mode = mode

    def note_written(self, on: bool, mode: FastSourceMode) -> None:
        """Record what the adapter REALLY wrote, overriding the emitted pair.

        Adapter hook (§28) for a degraded write: when the climate entity
        advertises no ``dry`` mode the adapter writes OFF instead of the
        core's DRY (``dry_unsupported``, §24). Only the pair the next
        :meth:`sync` compares the feedback against is replaced — the unit's
        OFF feedback then agrees with the written OFF instead of diverging
        from the emitted DRY. The settle counter is NOT touched: the core
        keeps emitting the same DRY, so nothing about OUR command changed,
        and a real touch on such a unit must still be adoptable. Like
        :meth:`note_command`, the reference only moves when the latest
        :meth:`sync` saw the unit.

        Args:
            on: The ``on`` state actually written.
            mode: The direction actually written (``OFF`` for a demoted DRY).
        """
        self._last_cmd_mode = mode
        if not self._unit_visible:
            return
        self._prev_cmd_on = on
        self._prev_cmd_mode = mode

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
        with the opposite direction in practice). It also ENDS a manual hold
        (§28): safety outranks the user's touch, and the forced command must
        be written. Cancelling an ACTIVE hold leaves an honest trace — the
        unit is diverged from what the safety layer writes, so the mismatch
        flag is raised for this cycle instead of silence. Not in a cycle that
        did not see the unit (:meth:`_end_hold`).

        Args:
            fs_mode: Direction to command (HEATING or COOLING).
            target: Room target temperature [degC] for the split.

        Returns:
            An ON :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        self._end_hold()
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
        min-OFF wait instead of restarting it on recovery. Also ENDS a manual
        hold (§28) — uniformly for every caller (safety, sensor lost, OFF
        mode, group arbitration, farewell): a forced OFF is written, and
        cancelling an ACTIVE hold raises the mismatch flag as an honest trace
        (the unit is diverged from what the safety layer writes). Not in a
        cycle that did not see the unit (:meth:`_end_hold`).

        Returns:
            An OFF :class:`~tortoise_ufh.models.FastSourceCommand`.
        """
        self._end_hold()
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

    def _end_hold(self) -> None:
        """End an active manual hold from a safety force (§28).

        A hold cancelled by ``force_on`` / ``force_off`` sets the mismatch
        flag for this cycle: the physical unit is in the USER's state while
        the safety layer is about to write something else, and that
        divergence must show in the report rather than vanish silently. An
        inactive hold leaves the flag alone.

        A force in a cycle whose :meth:`sync` did not see the unit
        (2026-09-10) leaves the hold running: nothing is written to an
        unreachable unit, and a Wi-Fi blip — which also reads as sensor lost
        when the room sensor is the split's own probe — must not end the
        user's manual cooling. The unit found after the gap is re-adopted
        (first-sync rule) and mirrored for the rest of the hold.
        """
        if self._manual_hold_s > 0.0 and self._unit_visible:
            self._mismatch = True
            self._manual_hold_s = 0.0

    def reset(self) -> None:
        """Clear all machine state (direction, dwell clock, S4, manual hold)."""
        self._state = FastSourceMode.OFF
        self._timer_s = _INITIAL_FAST_TIMER_S
        self._synced = False
        self._mismatch = False
        self._unit_visible = False
        self._prev_cmd_on = None
        self._prev_cmd_mode = None
        self._last_cmd_mode = None
        self._last_emitted_pair = None
        self._since_cmd_change_s = 0.0
        self._dwell_remaining_s = None
        self._manual_hold_s = 0.0
