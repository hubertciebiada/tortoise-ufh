"""Direct unit tests of :class:`FastSourceMachine` edges the controller hides.

The controller-level suites (``test_fast_source.py``,
``test_controller_manual_hold.py``) drive the machine through whole 300-s
cycles, where these edges never show:

* the settle counter ACCUMULATES across short steps (the 2-s debounced
  recompute), and adoption starts exactly at half a cycle;
* the direction adopted for a running unit whose feedback names no single
  direction (COOLING only for a SPLIT in COOLING mode);
* a stopped unit reporting ``dry`` is not a dry-assist run;
* the engage/release thresholds of :meth:`FastSourceMachine.want` are strict.

Units: temperatures in degC; ``dt`` in seconds; dwells / hold in minutes
(config). This module never imports ``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.fast_source import FastSourceMachine
from custom_components.tortoise_ufh.core.models import (
    FastSourceKind,
    FastSourceMode,
    Mode,
    RoomInputs,
)
from tests.unit.conftest import make_inputs

pytestmark = pytest.mark.unit

_CFG = ControllerConfig(cycle_seconds=300.0, fast_manual_hold_minutes=60.0)
"""Half a cycle = 150 s settle window; a 60-min hold."""


def _feedback(
    on: bool,
    hvac: str | None,
    *,
    mode: Mode = Mode.HEATING,
    kind: FastSourceKind = FastSourceKind.SPLIT,
) -> RoomInputs:
    """Room inputs carrying only what :meth:`FastSourceMachine.sync` reads."""
    return make_inputs(
        mode=mode,
        fast_source_kind=kind,
        fast_source_on=on,
        fast_source_hvac_mode=hvac,
    )


def _running_heat_machine() -> FastSourceMachine:
    """A machine synced to a running heating split, our ON/HEATING recorded."""
    machine = FastSourceMachine(_CFG)
    machine.sync(_feedback(True, "heat"))
    machine.note_command(True, FastSourceMode.HEATING)
    return machine


class TestSettleWindow:
    """§28 settle rule: adoption only >= half a cycle after OUR command change."""

    def test_short_steps_accumulate_into_the_settle_window(self) -> None:
        """Two 100-s steps add up to 200 s >= 150 s: the second touch adopts."""
        machine = _running_heat_machine()
        machine.tick(100.0)
        machine.sync(_feedback(False, "off"))
        assert machine.mismatch is True
        assert machine.manual_hold_active is False

        machine.note_command(True, FastSourceMode.HEATING)  # same pair: no reset
        machine.tick(100.0)
        machine.sync(_feedback(False, "off"))
        assert machine.manual_hold_active is True
        assert machine.state is FastSourceMode.OFF
        assert machine.mismatch is False

    def test_adoption_starts_exactly_at_half_a_cycle(self) -> None:
        """At exactly 150 s since our command change a touch is adopted."""
        machine = _running_heat_machine()
        machine.tick(150.0)
        machine.sync(_feedback(False, "off"))
        assert machine.manual_hold_active is True
        assert machine.state is FastSourceMode.OFF

    def test_our_own_command_change_restarts_the_window(self) -> None:
        """A changed emitted pair zeroes the settle counter again."""
        machine = _running_heat_machine()
        machine.tick(300.0)
        machine.note_command(False, FastSourceMode.OFF)  # we switch it off
        machine.tick(100.0)
        machine.sync(_feedback(True, "heat"))  # unit has not caught up yet
        assert machine.mismatch is True
        assert machine.manual_hold_active is False

    def test_hold_length_follows_the_knob(self) -> None:
        """The adopted hold lasts ``fast_manual_hold_minutes``, then counts down."""
        machine = _running_heat_machine()
        machine.tick(150.0)
        machine.sync(_feedback(False, "off"))
        assert machine.manual_hold_remaining_s == pytest.approx(3600.0)
        machine.tick(600.0)
        assert machine.manual_hold_remaining_s == pytest.approx(3000.0)


class TestRunningDirection:
    """Direction adopted for a running unit with no single-direction report."""

    def test_split_in_cooling_mode_is_adopted_as_cooling(self) -> None:
        """An ambiguous running split in COOLING mode is adopted as COOLING."""
        machine = FastSourceMachine(_CFG)
        machine.sync(_feedback(True, "auto", mode=Mode.COOLING))
        assert machine.state is FastSourceMode.COOLING

    def test_split_in_heating_mode_is_adopted_as_heating(self) -> None:
        """An ambiguous running split in HEATING mode is adopted as HEATING."""
        machine = FastSourceMachine(_CFG)
        machine.sync(_feedback(True, "auto", mode=Mode.HEATING))
        assert machine.state is FastSourceMode.HEATING

    def test_heater_in_cooling_mode_is_adopted_as_heating(self) -> None:
        """A running HEATER can never cool, whatever the global mode."""
        machine = FastSourceMachine(_CFG)
        machine.sync(
            _feedback(True, None, mode=Mode.COOLING, kind=FastSourceKind.HEATER)
        )
        assert machine.state is FastSourceMode.HEATING

    def test_heater_reporting_cool_is_adopted_as_heating(self) -> None:
        """A HEATER's "cool" feedback is ignored: it can only heat."""
        machine = FastSourceMachine(_CFG)
        machine.sync(
            _feedback(True, "cool", mode=Mode.COOLING, kind=FastSourceKind.HEATER)
        )
        assert machine.state is FastSourceMode.HEATING


class TestFirstSyncDry:
    """A unit found in ``dry`` keeps the dry-assist band only while RUNNING."""

    def test_running_dry_unit_records_dry(self) -> None:
        """A running unit reporting ``dry`` is recorded as dry-assisting."""
        machine = FastSourceMachine(_CFG)
        machine.sync(_feedback(True, "dry", mode=Mode.COOLING))
        assert machine.last_command_mode is FastSourceMode.DRY

    def test_stopped_unit_reporting_dry_is_not_dry_assist(self) -> None:
        """A stopped unit whose last HVAC mode reads ``dry`` records nothing."""
        machine = FastSourceMachine(_CFG)
        machine.sync(_feedback(False, "dry", mode=Mode.COOLING))
        assert machine.state is FastSourceMode.OFF
        assert machine.last_command_mode is None


class TestWantThresholds:
    """Engage strictly above the boost offset, release at the deadband."""

    def test_engage_needs_more_than_the_boost_offset(self) -> None:
        """A demand exactly at ``boost_offset_c`` does not engage."""
        machine = FastSourceMachine(_CFG)
        assert machine.want(_CFG.boost_offset_c, engaged=False) is False
        assert machine.want(_CFG.boost_offset_c + 0.01, engaged=False) is True

    def test_release_at_the_deadband(self) -> None:
        """An engaged unit releases once demand is back at ``deadband_c``."""
        machine = FastSourceMachine(_CFG)
        assert machine.want(_CFG.deadband_c, engaged=True) is False
        assert machine.want(_CFG.deadband_c + 0.01, engaged=True) is True


_DWELL_CFG = ControllerConfig(fast_min_on_minutes=30.0, fast_min_off_minutes=31.0)
"""Asymmetric dwells (30/31 min) so the lock side is observable."""


def _decide_off(machine: FastSourceMachine, flags: list[str]) -> None:
    """One ``decide`` call that wants nothing (stays/returns OFF)."""
    machine.decide(
        want_on=False,
        fs_mode=FastSourceMode.HEATING,
        target_heating=24.0,
        target_cooling=22.0,
        flags=flags,
    )


class TestDecideMinRuntimeFlag:
    """The min-runtime flag is appended at most once per flag list."""

    def test_blocked_engage_appends_the_flag_once(self) -> None:
        """Two blocked OFF->ON decisions share ONE flag entry."""
        machine = FastSourceMachine(_DWELL_CFG)
        machine.sync(_feedback(False, "off"))  # adopt OFF, timer seeded to 0
        flags: list[str] = []
        for _ in range(2):
            cmd = machine.decide(
                want_on=True,
                fs_mode=FastSourceMode.HEATING,
                target_heating=24.0,
                target_cooling=22.0,
                flags=flags,
            )
            assert cmd.on is False
        assert flags == ["fast_source_min_runtime"]

    def test_blocked_release_appends_the_flag_once(self) -> None:
        """Two blocked running->OFF decisions share ONE flag entry."""
        machine = FastSourceMachine(_DWELL_CFG)
        machine.sync(_feedback(True, "heat"))  # adopt HEATING, timer seeded to 0
        flags: list[str] = []
        for _ in range(2):
            cmd = machine.decide(
                want_on=False,
                fs_mode=FastSourceMode.HEATING,
                target_heating=24.0,
                target_cooling=22.0,
                flags=flags,
            )
            # Blocked by min-ON: the remembered direction is re-emitted.
            assert cmd.on is True
            assert cmd.mode is FastSourceMode.HEATING
        assert flags == ["fast_source_min_runtime"]


class TestDwellLockSurfaces:
    """The dwell lock surfaced by decide/mirror/force_on follows the state."""

    def test_idle_lock_uses_the_min_off(self) -> None:
        """An OFF machine's lock is min-OFF (31 min), never min-ON (30)."""
        machine = FastSourceMachine(_DWELL_CFG)
        machine.sync(_feedback(False, "off"))  # timer seeded to 0
        machine.tick(60.0)
        _decide_off(machine, [])
        assert machine.dwell_remaining_s == pytest.approx(31.0 * 60.0 - 60.0)
        machine.mirror()
        assert machine.dwell_remaining_s == pytest.approx(31.0 * 60.0 - 60.0)

    def test_lock_clears_exactly_at_zero(self) -> None:
        """A lock remainder of exactly 0 s reads as None (elapsed), not 0.0."""
        machine = FastSourceMachine(_DWELL_CFG)
        machine.sync(_feedback(False, "off"))
        machine.tick(31.0 * 60.0)
        _decide_off(machine, [])
        assert machine.dwell_remaining_s is None
        machine.mirror()
        assert machine.dwell_remaining_s is None
        # Same boundary on the running side through force_on.
        running = FastSourceMachine(_DWELL_CFG)
        running.sync(_feedback(True, "heat"))  # HEATING, timer 0
        running.tick(30.0 * 60.0)
        running.force_on(FastSourceMode.HEATING, 24.0)  # no state change
        assert running.dwell_remaining_s is None

    def test_sub_second_lock_remainder_is_reported(self) -> None:
        """A 0.5 s lock remainder is surfaced, not rounded away to None."""
        machine = FastSourceMachine(_DWELL_CFG)
        machine.sync(_feedback(False, "off"))
        machine.tick(31.0 * 60.0 - 0.5)
        _decide_off(machine, [])
        assert machine.dwell_remaining_s == pytest.approx(0.5)
        machine.mirror()
        assert machine.dwell_remaining_s == pytest.approx(0.5)
        running = FastSourceMachine(_DWELL_CFG)
        running.sync(_feedback(True, "heat"))
        running.tick(30.0 * 60.0 - 0.5)
        running.force_on(FastSourceMode.HEATING, 24.0)
        assert running.dwell_remaining_s == pytest.approx(0.5)


class TestForceCommands:
    """force_on / force_off: command payload and dwell-timer bookkeeping."""

    def test_force_on_carries_the_target(self) -> None:
        """The forced command reports the room target it was given."""
        machine = FastSourceMachine(_DWELL_CFG)
        machine.sync(_feedback(True, "heat"))
        cmd = machine.force_on(FastSourceMode.HEATING, 24.5)
        assert cmd.on is True
        assert cmd.mode is FastSourceMode.HEATING
        assert cmd.target_temperature_c == pytest.approx(24.5)

    def test_force_on_dwell_counts_down_from_the_current_timer(self) -> None:
        """Without a state change the min-ON lock keeps counting from the
        timer: ``min_on * 60 - timer``, never plus."""
        machine = FastSourceMachine(_DWELL_CFG)
        machine.sync(_feedback(True, "heat"))  # HEATING, timer 0
        machine.tick(100.0)
        machine.force_on(FastSourceMode.HEATING, 24.0)  # no state change
        assert machine.dwell_remaining_s == pytest.approx(30.0 * 60.0 - 100.0)

    def test_force_on_restarts_the_timer_on_a_state_change(self) -> None:
        """An OFF->ON force re-seeds the dwell clock to exactly zero."""
        machine = FastSourceMachine(_DWELL_CFG)
        machine.sync(_feedback(False, "off"))
        machine.force_on(FastSourceMode.HEATING, 24.0)
        machine.tick(5.0)
        assert machine.timer_s == pytest.approx(5.0)

    def test_force_off_restarts_the_timer_on_the_edge(self) -> None:
        """An ON->OFF force re-seeds the dwell clock to exactly zero."""
        machine = FastSourceMachine(_DWELL_CFG)
        machine.sync(_feedback(True, "heat"))
        cmd = machine.force_off()
        assert cmd.on is False
        assert cmd.mode is FastSourceMode.OFF
        assert cmd.target_temperature_c is None
        machine.tick(5.0)
        assert machine.timer_s == pytest.approx(5.0)


class TestDivergenceGuard:
    """_diverges: no recorded command means no divergence."""

    def test_first_sync_without_a_command_is_never_a_divergence(self) -> None:
        """A changed feedback BEFORE any recorded command is not adopted and
        not flagged: there is no reference to disagree with yet."""
        machine = FastSourceMachine(_CFG)
        machine.sync(_feedback(False, "off"))  # first sync: adopt OFF
        machine.sync(_feedback(True, "cool"))  # no command recorded in between
        assert machine.mismatch is False
        assert machine.manual_hold_active is False
        assert machine.state is FastSourceMode.OFF


class TestSettleCounterEdges:
    """The settle counter restarts at exactly 0.0 on every command change."""

    def test_direction_change_restarts_the_settle_window(self) -> None:
        """HEATING -> DRY changes the emitted (on, direction) pair: a touch
        one cycle later is the unit catching up, not user intent."""
        machine = _running_heat_machine()  # emitted ON/HEATING, settle 0
        machine.tick(300.0)  # settle window spent
        machine.note_command(True, FastSourceMode.DRY)  # direction flip -> 0
        machine.sync(_feedback(False, "off"))
        assert machine.mismatch is True
        assert machine.manual_hold_active is False
        assert machine.state is FastSourceMode.HEATING

    def test_the_restarted_settle_window_counts_from_zero(self) -> None:
        """A touch 149.5 s (< half a cycle) after our own change only flags."""
        machine = _running_heat_machine()
        machine.note_command(False, FastSourceMode.OFF)  # pair change -> 0
        machine.tick(149.5)
        machine.sync(_feedback(True, "heat"))  # unit has not caught up yet
        assert machine.mismatch is True
        assert machine.manual_hold_active is False

    def test_reset_zeroes_the_settle_window(self) -> None:
        """After reset() a written reference still needs the full half cycle
        before a divergence can be adopted as a touch."""
        machine = _running_heat_machine()
        machine.reset()
        machine.sync(_feedback(True, "heat"))  # first sync: re-adopt, visible
        machine.note_written(True, FastSourceMode.HEATING)  # reference, no settle
        machine.tick(149.5)
        machine.sync(_feedback(False, "off"))
        assert machine.mismatch is True
        assert machine.manual_hold_active is False


class TestNoteWritten:
    """note_written(): replaces the S4 reference and the last command mode."""

    def test_written_mode_replaces_the_direction_reference(self) -> None:
        """After a written HEATING, a unit physically cooling DIVERGES."""
        machine = _running_heat_machine()
        machine.note_command(True, FastSourceMode.DRY)  # emitted DRY
        machine.note_written(True, FastSourceMode.HEATING)  # really wrote HEAT
        machine.sync(_feedback(True, "cool"))
        assert machine.mismatch is True

    def test_note_written_updates_the_last_command_mode(self) -> None:
        """The demoted mode is visible through ``last_command_mode``."""
        machine = _running_heat_machine()
        machine.note_written(False, FastSourceMode.OFF)
        assert machine.last_command_mode is FastSourceMode.OFF


class TestResetContract:
    """reset(): mismatch cleared, first-sync rule and blind commands restored."""

    def test_reset_clears_the_mismatch_flag(self) -> None:
        """A flagged divergence does not survive reset()."""
        machine = _running_heat_machine()
        machine.sync(_feedback(False, "off"))  # settle 0 -> flag only
        assert machine.mismatch is True
        machine.reset()
        assert machine.mismatch is False

    def test_reset_restores_the_first_sync_rule(self) -> None:
        """After reset() the first visible feedback is adopted as the truth,
        and a command noted while the unit was invisible is no reference."""
        machine = _running_heat_machine()
        machine.reset()
        machine.note_command(True, FastSourceMode.HEATING)  # blind: not recorded
        machine.sync(_feedback(True, "cool"))  # first sync: adopt COOLING
        assert machine.state is FastSourceMode.COOLING
        assert machine.mismatch is False
        machine.sync(_feedback(False, "off"))  # nothing recorded: no divergence
        assert machine.mismatch is False
        assert machine.manual_hold_active is False
        assert machine.state is FastSourceMode.COOLING


class TestManualHoldEdges:
    """Hold knob boundary and the sub-second end of a hold."""

    def _settled_heating(self, cfg: ControllerConfig) -> FastSourceMachine:
        """A machine synced to a running heating unit, command settled."""
        machine = FastSourceMachine(cfg)
        machine.sync(_feedback(True, "heat"))
        machine.note_command(True, FastSourceMode.HEATING)
        machine.tick(300.0)
        return machine

    def test_hold_knob_below_one_minute_still_holds(self) -> None:
        """``fast_manual_hold_minutes = 0.5`` is > 0: the hold path applies."""
        machine = self._settled_heating(ControllerConfig(fast_manual_hold_minutes=0.5))
        machine.sync(_feedback(False, "off"))
        assert machine.manual_hold_active is True
        assert machine.manual_hold_remaining_s == pytest.approx(30.0)
        assert machine.mismatch is False

    def test_sub_second_hold_counts_down_and_a_force_ends_it(self) -> None:
        """The hold counts down below one second, and a force still ends it."""
        machine = self._settled_heating(ControllerConfig(fast_manual_hold_minutes=0.5))
        machine.sync(_feedback(False, "off"))  # adopted OFF, hold armed 30 s
        machine.note_command(False, FastSourceMode.OFF)  # agreement from now
        machine.tick(29.5)
        machine.tick(0.1)
        assert machine.manual_hold_remaining_s == pytest.approx(0.4)
        machine.sync(_feedback(False, "off"))  # agreement; unit visible
        machine.force_off()
        assert machine.mismatch is True
        assert machine.manual_hold_active is False
