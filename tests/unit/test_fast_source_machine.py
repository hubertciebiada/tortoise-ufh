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
