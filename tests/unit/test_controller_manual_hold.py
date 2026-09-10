"""Manual-hold replays of the owner's live episodes through ``RoomController``.

DECISIONS §28 (2026-09-04): in TRANSITIONAL a split started by hand from the
remote (target 21, room setpoint 23) in a room whose temperature sensor IS the
split's own sensor read 2 K below the setpoint within ~13 min; the direction
machine was OFF (it never commanded anything), so it flipped the physically
cooling unit straight to HEATING with no OFF dwell. This module replays that
scenario and its mirror image end to end:

* a manual cool is adopted and MIRRORED for the hold — no heating command,
  flag ``fast_source_manual``, no ``fast_source_mismatch``; after the hold the
  normal logic resumes from the adopted state and heating is reachable only
  through OFF and the full min-OFF dwell;
* a unit the controller was heating that the user switched OFF stays OFF for
  the hold, then re-engages honestly;
* the hard safety layer (sensor lost, S3) ends the hold and its command
  carries no manual flag, so the adapter writes it — and the cancelled hold
  leaves ``fast_source_mismatch`` as an honest trace;
* the settle rule: a divergence less than half a cycle after OUR OWN command
  change (the 2-s debounced recompute, a few-second climate-integration lag)
  only flags ``fast_source_mismatch`` — adoption follows at the second
  regular cycle, even one that came in short of 300 s — while a touch on a
  long-idle machine is adopted at the very next cycle.

The physical feedback is modelled the way a real unit behaves: it reports the
USER's state for the touch cycles and afterwards obeys whatever the controller
emitted the previous cycle (one cycle of lag).

Units: temperatures in degC; ``dt_seconds`` in seconds; dwells / hold in
minutes (config). This module never imports ``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import RoomController
from custom_components.tortoise_ufh.core.models import (
    FastSourceCommand,
    FastSourceKind,
    FastSourceMode,
    Mode,
    RoomInputs,
)
from tests.unit.conftest import make_inputs

DT: float = 300.0
"""One 5-minute control cycle [s]."""

_CFG = ControllerConfig(
    boost_offset_c=1.5,
    deadband_c=0.3,
    fast_min_on_minutes=30.0,
    fast_min_off_minutes=31.0,
    fast_manual_hold_minutes=60.0,
)
"""The owner's live tuning of the affected rooms."""

_HVAC_BY_MODE: dict[FastSourceMode, str] = {
    FastSourceMode.HEATING: "heat",
    FastSourceMode.COOLING: "cool",
    FastSourceMode.DRY: "dry",
}


def _inputs(temp: float | None, *, on: bool, hvac: str) -> RoomInputs:
    """TRANSITIONAL split room (setpoint 23) with a physical split feedback."""
    return make_inputs(
        mode=Mode.TRANSITIONAL,
        setpoint_c=23.0,
        room_temperature_c=temp,
        fast_source_kind=FastSourceKind.SPLIT,
        fast_source_on=on,
        fast_source_hvac_mode=hvac,
    )


def _echo(command: FastSourceCommand) -> tuple[bool, str]:
    """Physical feedback of a unit that OBEYED ``command`` (one cycle later)."""
    if not command.on:
        return (False, "off")
    return (True, _HVAC_BY_MODE[command.mode])


class TestManualCoolInTransitional:
    """The child's bedroom, 08-18 / 08-22 / 08-24 and the 09-04 turbo-cool script."""

    @pytest.mark.unit
    def test_manual_cool_is_mirrored_then_released_through_off(self) -> None:
        """A remote-started cool is mirrored; heating only after OFF + min-OFF."""
        ctl = RoomController(_CFG, name="kids_room")
        # Cycles 1-2: idle — room at the setpoint, unit off (first sync adopts
        # OFF; the second cycle settles the OFF command for the §28 rule).
        for _ in range(2):
            idle = ctl.step(_inputs(23.0, on=False, hvac="off"), dt_seconds=DT)
            assert idle.fast_source.on is False

        # The user starts cooling from the remote (unit target 21). The room
        # sensor IS the split's sensor: it falls ~2 K over three cycles.
        touched = [
            ctl.step(_inputs(temp, on=True, hvac="cool"), dt_seconds=DT)
            for temp in (22.4, 21.7, 21.0)
        ]
        for out in touched:
            assert out.fast_source.on is True
            assert out.fast_source.mode is FastSourceMode.COOLING
            # No fabricated target: the unit runs at the USER's own setpoint
            # (nothing is written during the hold).
            assert out.fast_source.target_temperature_c is None
            assert "fast_source_manual" in out.report.flags
            assert "fast_source_mismatch" not in out.report.flags
            assert "fast_source_min_runtime" not in out.report.flags
        # Detected 15 min ago (3 cycles incl. the detection one): 45 min left.
        assert "reczne sterowanie (jeszcze 45 min)" in touched[-1].report.explanation

        # Keep stepping: the room stays 2 K cold, the unit obeys the previous
        # command (while mirrored that IS "still cooling").
        history = []
        prev = touched[-1].fast_source
        for _ in range(30):
            on, hvac = _echo(prev)
            out = ctl.step(_inputs(21.0, on=on, hvac=hvac), dt_seconds=DT)
            history.append(out)
            prev = out.fast_source

        # The flag shows on 3 + 8 = 11 steps = 55 min: tick() runs AFTER
        # sync() in the detecting step, so that step already counts 5 min of
        # the 60-min hold, and the 12th step's tick brings it to 0.
        manual = [
            i for i, o in enumerate(history) if "fast_source_manual" in o.report.flags
        ]
        assert manual == list(range(8))
        for out in history[:8]:
            assert out.fast_source.on is True
            assert out.fast_source.mode is FastSourceMode.COOLING

        # The hold elapsed: the normal logic resumes from the adopted COOLING
        # state — the room is 2 K below the setpoint, so cooling RELEASES
        # (min-ON long elapsed during the hold), the unit passes through OFF
        # and waits the full min-OFF before heating may engage. Never a direct
        # COOL -> HEAT flip.
        after = history[8:]
        assert after[0].fast_source.on is False
        assert after[0].fast_source.mode is FastSourceMode.OFF
        first_heat = next(
            i
            for i, o in enumerate(after)
            if o.fast_source.mode is FastSourceMode.HEATING
        )
        assert all(o.fast_source.on is False for o in after[:first_heat])
        assert first_heat * DT >= _CFG.fast_min_off_minutes * 60.0
        assert "fast_source_min_runtime" in after[1].report.flags
        assert after[first_heat].fast_source.on is True
        # A unit that obeys never diverges again: no flag of either kind.
        assert all("fast_source_manual" not in o.report.flags for o in after)
        assert all("fast_source_mismatch" not in o.report.flags for o in history)


class TestManualOffWhileHeating:
    """The mirror image: the user switches OFF a unit the controller heats."""

    @pytest.mark.unit
    def test_manual_off_holds_off_then_reengages(self) -> None:
        """The OFF is mirrored for the hold; no ON command until it elapses."""
        ctl = RoomController(_CFG, name="sypialnia")
        # Cold room (demand 2 K > boost 1.5), unit off: the first sync adopts
        # OFF with a conservative min-OFF seed, so the engage waits ~31 min.
        out = ctl.step(_inputs(21.0, on=False, hvac="off"), dt_seconds=DT)
        for _ in range(12):
            if out.fast_source.on:
                break
            out = ctl.step(_inputs(21.0, on=False, hvac="off"), dt_seconds=DT)
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.HEATING

        # The unit obeys for a cycle: agreement, no flags.
        agreed = ctl.step(_inputs(21.0, on=True, hvac="heat"), dt_seconds=DT)
        assert agreed.fast_source.on is True
        assert "fast_source_manual" not in agreed.report.flags

        # The user switches the unit OFF from the remote.
        off = ctl.step(_inputs(21.0, on=False, hvac="off"), dt_seconds=DT)
        assert off.fast_source.on is False
        assert off.fast_source.mode is FastSourceMode.OFF
        assert "fast_source_manual" in off.report.flags
        assert "fast_source_mismatch" not in off.report.flags
        assert "reczne sterowanie" in off.report.explanation

        # For the rest of the hold the room stays 2 K cold and the unit off:
        # no ON command is emitted (55 min after detection = 10 more cycles).
        held = [
            ctl.step(_inputs(21.0, on=False, hvac="off"), dt_seconds=DT)
            for _ in range(10)
        ]
        for out in held:
            assert out.fast_source.on is False
            assert "fast_source_manual" in out.report.flags

        # The hold elapsed: the demand is real and the OFF timer accumulated
        # during the hold (60 min > 31), so heating re-engages honestly.
        resumed = ctl.step(_inputs(21.0, on=False, hvac="off"), dt_seconds=DT)
        assert "fast_source_manual" not in resumed.report.flags
        assert resumed.fast_source.on is True
        assert resumed.fast_source.mode is FastSourceMode.HEATING


class TestSafetyOutranksHold:
    """force_on / force_off end the hold; their command carries no manual flag.

    The cancelled hold is not silent: the unit is physically in the USER's
    state while the safety command is written, so the report carries
    ``fast_source_mismatch`` for that cycle.
    """

    def _held(self) -> RoomController:
        ctl = RoomController(_CFG, name="kids_room")
        for _ in range(2):  # adopt OFF, then settle the OFF command (§28)
            ctl.step(_inputs(23.0, on=False, hvac="off"), dt_seconds=DT)
        out = ctl.step(_inputs(22.4, on=True, hvac="cool"), dt_seconds=DT)
        assert "fast_source_manual" in out.report.flags
        return ctl

    @pytest.mark.unit
    def test_sensor_lost_ends_the_hold(self) -> None:
        """Sensor lost -> split OFF (uniform rule), no manual flag: written."""
        ctl = self._held()
        out = ctl.step(_inputs(None, on=True, hvac="cool"), dt_seconds=DT)
        assert "sensor_lost" in out.report.flags
        assert "fast_source_manual" not in out.report.flags
        assert "fast_source_mismatch" in out.report.flags
        assert out.fast_source.on is False

    @pytest.mark.unit
    def test_s3_emergency_heat_ends_the_hold(self) -> None:
        """A frost emergency forces HEATING over the user's cool: written."""
        ctl = self._held()
        out = ctl.step(_inputs(4.0, on=True, hvac="cool"), dt_seconds=DT)
        assert "s3_emergency_heat" in out.report.flags
        assert "fast_source_manual" not in out.report.flags
        assert "fast_source_mismatch" in out.report.flags
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.HEATING


class TestOwnCommandSettle:
    """§28 settle rule: our own fresh command is not a touch; an idle one is."""

    @pytest.mark.unit
    def test_stale_feedback_after_own_command_only_flags(self) -> None:
        """(a) Divergence right after OUR change: mismatch, no hold, no adoption.

        The controller runs ``sync -> tick -> decide -> note_command``, so the
        settle counter ``sync`` sees lags one step: adoption after our own
        change follows at the SECOND regular cycle (the 2-s debounced
        recompute is not one) — deterministically, even when that cycle comes
        in a hair short of 300 s (the real dt is jittery).
        """
        ctl = RoomController(_CFG, name="kids_room")
        # Cold room, unit off: the first sync adopts OFF with a conservative
        # min-OFF seed; heating engages once the 31-min dwell has elapsed.
        cold = _inputs(21.0, on=False, hvac="off")
        out = ctl.step(cold, dt_seconds=DT)
        for _ in range(12):
            if out.fast_source.on:
                break
            out = ctl.step(cold, dt_seconds=DT)
        assert out.fast_source.mode is FastSourceMode.HEATING  # step N

        # N+1: the debounced recompute 2 s after the write reads a climate
        # entity that has not published the new state yet.
        recompute = ctl.step(cold, dt_seconds=2.0)
        assert "fast_source_mismatch" in recompute.report.flags
        assert "fast_source_manual" not in recompute.report.flags
        assert recompute.fast_source.on is True  # state unchanged: still heats
        assert recompute.fast_source.mode is FastSourceMode.HEATING

        # N+2: one regular cycle later (it came in at 297 s) the unit STILL
        # reads off. The counter sync sees is 2 s (< half a cycle): flag only.
        settling = ctl.step(cold, dt_seconds=297.0)
        assert "fast_source_mismatch" in settling.report.flags
        assert "fast_source_manual" not in settling.report.flags
        assert settling.fast_source.on is True

        # N+3: the second regular cycle after our change — the counter sync
        # sees is 299 s (>= half a cycle, below a full one) and the unit
        # really is off — the user switched it off: adopted, held, mirrored
        # as OFF.
        adopted = ctl.step(cold, dt_seconds=DT)
        assert "fast_source_manual" in adopted.report.flags
        assert "fast_source_mismatch" not in adopted.report.flags
        assert adopted.fast_source.on is False
        assert adopted.fast_source.mode is FastSourceMode.OFF

    @pytest.mark.unit
    def test_touch_on_a_long_idle_machine_is_adopted_next_cycle(self) -> None:
        """(b) A machine that has emitted OFF for hours adopts a touch at once."""
        ctl = RoomController(_CFG, name="kids_room")
        idle = _inputs(23.0, on=False, hvac="off")
        for _ in range(24):  # two idle hours at the setpoint, unit off
            out = ctl.step(idle, dt_seconds=DT)
            assert out.fast_source.on is False
            assert "fast_source_mismatch" not in out.report.flags
        touched = ctl.step(_inputs(22.4, on=True, hvac="cool"), dt_seconds=DT)
        assert "fast_source_manual" in touched.report.flags
        assert "fast_source_mismatch" not in touched.report.flags
        assert touched.fast_source.on is True
        assert touched.fast_source.mode is FastSourceMode.COOLING
        assert "reczne sterowanie (jeszcze 55 min)" in touched.report.explanation
