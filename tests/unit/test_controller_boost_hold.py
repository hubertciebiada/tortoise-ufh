"""Unit tests for the cooling boost-hold floor (2026-07-13, P2).

While a room is in :class:`~tortoise_ufh.models.Mode.COOLING` and its split is
ENGAGED, the floor valve must not retreat below the position it held the cycle
the split engaged (anti measurement-path priority inversion): the split cooling
the air drives the room error toward zero, which would otherwise starve the
base floor source to 0 exactly when the slab most needs discharging. The hold
is a ``max`` floor (the PI may push HIGHER), is snapshotted from the previous
active cycle's raw pre-throttle valve, and is applied BEFORE the S2 dew
throttle (so a dew factor of 0 still closes the valve) and BELOW the hard
safety layer (sensor-lost still parks at 0). HEATING is untouched.

Only #1 (the ``max`` floor) was implemented; the plan's optional #2 (freeze the
integrator during boost) and #3 (suppress trend damping during boost) were
dropped as empirically unnecessary — the ``max`` floor already dominates the
PI/trend, and the digital-twin reproduction showed no post-release integrator
collapse to justify #2 — so their T2/T3 tests are intentionally absent.

Units: temperatures / setpoints / dew points degC; valve percent 0..100;
``dt_seconds`` seconds. This module never imports ``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import RoomController
from custom_components.tortoise_ufh.core.dew_point import dew_point
from custom_components.tortoise_ufh.core.models import (
    FastSourceKind,
    FastSourceMode,
    LoopInput,
    Mode,
)
from tests.unit.conftest import make_inputs

_DT = 300.0


def _cool(
    *,
    room: float,
    setpoint: float = 24.0,
    humidity: float = 50.0,
    supply: float = 20.0,
    on: bool | None = None,
):
    """Build a COOLING RoomInputs carrying a split and one supply-probed loop."""
    loops = (LoopInput(None, supply, None),)
    return make_inputs(
        mode=Mode.COOLING,
        setpoint_c=setpoint,
        room_temperature_c=room,
        humidity_pct=humidity,
        loops=loops,
        fast_source_kind=FastSourceKind.SPLIT,
        fast_source_on=on,
    )


def _engage(controller: RoomController, **kw: float) -> float:
    """Run the engaging cycle (split OFF->COOLING) and return its valve %.

    On this first cycle the machine transitions OFF->COOLING at step 14, so the
    step-entry witness is still OFF and the hold is NOT yet armed: the emitted
    valve equals the raw (pre-throttle, clamped) valve, i.e. exactly the value
    the next cycle snapshots as the hold floor.
    """
    out = controller.step(_cool(room=27.0, **kw), dt_seconds=_DT)
    assert out.fast_source.on is True
    assert out.fast_source.mode is FastSourceMode.COOLING
    return out.valve_position_pct


class TestBoostHoldFloor:
    """#1: the floor valve is held at the engage-cycle snapshot during boost."""

    @pytest.mark.unit
    def test_valve_held_at_snapshot_despite_negative_cooling_error(self) -> None:
        """T1: engaged + room cooled below setpoint -> valve >= snapshot.

        The split overcools the air (cooling error goes negative, so the raw PI
        valve collapses toward 0), but the min-ON dwell keeps the split ENGAGED
        and the boost-hold pins the floor at the engage-cycle snapshot.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        snapshot = _engage(controller)
        assert snapshot > 15.0, "the engage cycle should open the floor materially"

        # Next cycle: the split is engaged at step entry; the room has been
        # cooled below the setpoint so the cooling PI wants the valve at 0.
        out = controller.step(_cool(room=23.5), dt_seconds=_DT)
        assert out.report.error_c > 0.0, "room below setpoint (cooling wants to close)"
        assert out.report.raw_valve_pct < snapshot, "raw PI valve collapsed"
        assert out.report.dew_throttle_factor == pytest.approx(1.0)
        assert out.valve_position_pct == pytest.approx(snapshot), (
            "the boost-hold must pin the floor at the engage-cycle snapshot"
        )

    @pytest.mark.unit
    def test_pi_may_push_higher_than_snapshot(self) -> None:
        """The hold is a floor, not a clamp: a hotter room lifts the valve."""
        controller = RoomController(ControllerConfig(), name="salon")
        snapshot = _engage(controller)
        # Room even hotter than the engage cycle -> PI demands MORE than the
        # snapshot, so max() lets it through.
        out = controller.step(_cool(room=29.0), dt_seconds=_DT)
        assert out.valve_position_pct > snapshot
        assert out.valve_position_pct == pytest.approx(out.report.raw_valve_pct)


class TestBoostHoldThrottleOrdering:
    """T4: the S2 dew throttle multiplies the HELD valve (dew factor 0 -> 0)."""

    @pytest.mark.unit
    def test_half_throttle_scales_the_held_valve(self) -> None:
        """dew factor 0.5 -> valve == 0.5 * max(pid, hold) == 0.5 * snapshot."""
        controller = RoomController(ControllerConfig(), name="salon")
        snapshot = _engage(controller)
        # Overcool so the raw PI valve is below the snapshot (hold dominates),
        # and place the coldest supply one kelvin above the dew point so the
        # graduated throttle (margin=ramp=2 -> factor = gap/2) reads 0.5.
        room = 23.5
        humidity = 50.0
        supply = dew_point(room, humidity) + 1.0
        out = controller.step(
            _cool(room=room, humidity=humidity, supply=supply), dt_seconds=_DT
        )
        assert out.report.dew_throttle_factor == pytest.approx(0.5, abs=1e-3)
        assert out.report.raw_valve_pct < snapshot
        assert out.valve_position_pct == pytest.approx(0.5 * snapshot, rel=1e-3)

    @pytest.mark.unit
    def test_dew_point_supply_closes_the_held_valve(self) -> None:
        """dew factor 0 (supply AT the dew point) -> the held valve closes to 0."""
        controller = RoomController(ControllerConfig(), name="salon")
        _engage(controller)
        room = 23.5
        humidity = 50.0
        supply = dew_point(room, humidity)  # gap 0 -> factor 0
        out = controller.step(
            _cool(room=room, humidity=humidity, supply=supply), dt_seconds=_DT
        )
        assert out.report.dew_throttle_factor == pytest.approx(0.0)
        assert out.valve_position_pct == pytest.approx(0.0)
        assert "s2_throttle" in out.report.flags


class TestBoostHoldSafetyWins:
    """T5: the hard safety layer overrides the hold (sensor-lost parks at 0)."""

    @pytest.mark.unit
    def test_sensor_lost_during_boost_parks_valve_at_zero(self) -> None:
        """A lost room sensor in cooling parks the valve at 0 despite the hold."""
        controller = RoomController(ControllerConfig(), name="salon")
        _engage(controller)
        out = controller.step(_cool(room=None), dt_seconds=_DT)  # type: ignore[arg-type]
        assert out.valve_position_pct == pytest.approx(0.0)
        assert "sensor_lost" in out.report.flags
        assert out.fast_source.on is False


class TestBoostHoldHeatingUntouched:
    """T6: HEATING with an engaged split is byte-identical (no hold applies)."""

    @pytest.mark.unit
    def test_heating_valve_identical_with_and_without_split(self) -> None:
        """The presence of an engaged heating split does not perturb the valve.

        The boost-hold is gated on ``Mode.COOLING``, so a heating room's valve
        must be exactly the PI/heating-floor result whether or not a split is
        engaged, and the hold state must never arm.
        """
        cfg = ControllerConfig()
        with_split = RoomController(cfg, name="a")
        no_split = RoomController(cfg, name="b")
        for _ in range(4):
            hs = with_split.step(
                make_inputs(
                    mode=Mode.HEATING,
                    setpoint_c=24.0,
                    room_temperature_c=20.0,
                    fast_source_kind=FastSourceKind.SPLIT,
                ),
                dt_seconds=_DT,
            )
            ns = no_split.step(
                make_inputs(
                    mode=Mode.HEATING,
                    setpoint_c=24.0,
                    room_temperature_c=20.0,
                    fast_source_kind=FastSourceKind.NONE,
                ),
                dt_seconds=_DT,
            )
        assert hs.fast_source.on is True, "the heating split should be engaged"
        assert hs.valve_position_pct == pytest.approx(ns.valve_position_pct)
        assert with_split._boost_hold_pct is None  # noqa: SLF001


class TestBoostHoldSnapshotLifecycle:
    """T7: release clears the snapshot; a fresh engage takes a new one."""

    @pytest.mark.unit
    def test_release_clears_and_reengage_resnapshots(self) -> None:
        """The hold snapshot is per-engagement, not persisted across a release."""
        controller = RoomController(ControllerConfig(), name="salon")
        snapshot1 = _engage(controller)
        # Cycle 2: engaged, hold armed at snapshot1.
        controller.step(_cool(room=23.5), dt_seconds=_DT)
        assert controller._boost_hold_pct == pytest.approx(snapshot1)  # noqa: SLF001

        # Release: keep the demand inside the band until the min-ON dwell
        # elapses and the split actually turns OFF.
        released = None
        for _ in range(6):
            out = controller.step(_cool(room=23.5), dt_seconds=_DT)
            if not out.fast_source.on:
                released = out
                break
        assert released is not None, "the split never released"
        # One inactive/idle cooling cycle past the release voids the snapshot.
        controller.step(_cool(room=23.8), dt_seconds=_DT)
        assert controller._boost_hold_pct is None  # noqa: SLF001

        # A fresh, hotter engagement snapshots a NEW (larger) floor.
        snapshot2 = _engage(controller)
        controller.step(_cool(room=26.0), dt_seconds=_DT)
        assert controller._boost_hold_pct == pytest.approx(snapshot2)  # noqa: SLF001
        assert controller._boost_hold_pct != pytest.approx(snapshot1)  # noqa: SLF001
