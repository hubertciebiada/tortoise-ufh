"""Unit tests for the safety / safe-degrade rules of :mod:`tortoise_ufh.controller`.

Exercises the :class:`~tortoise_ufh.controller.RoomController` and the
whole-building :class:`~tortoise_ufh.controller.BuildingController` safety
behaviour against the frozen black-box contract:

* Missing room temperature safe-degrade (hold last valve, fast source OFF,
  ``sensor_lost`` flag, PID not run); in COOLING the valve parks at 0 instead
  of freezing (C2), without poisoning the heating hold memory.
* The safety override keeps controller state honest (S5): an S1 trip never
  poisons the sensor-lost hold, and a forced-ON split syncs the dwell machine.
* Closing the valve never silences the air-side source (S7): S1 + S3 close
  the water side while the split keeps heating the air.
* The S5 watchdog (stale adapter age) drives the neutral position: the
  heating valve floor, or 0 in COOLING.
* The building output counts rooms currently flagged ``sensor_lost``
  (safety-F13).

Units: temperatures / setpoints in degC; valve in percent (0..100);
``dt_seconds`` in seconds; ``last_update_age_minutes`` in minutes. This
module never imports ``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import (
    BuildingController,
    RoomController,
)
from custom_components.tortoise_ufh.core.models import (
    FastSourceKind,
    FastSourceMode,
    LoopInput,
    Mode,
    RoomInputs,
)
from tests.unit.conftest import make_inputs


class TestSensorLost:
    """Safe-degrade behaviour when the room temperature sensor is lost."""

    @pytest.mark.unit
    def test_missing_temp_holds_last_valve_and_flags_sensor_lost(self) -> None:
        """A lost room sensor holds the last valve, forces fast OFF, flags loss."""
        controller = RoomController(ControllerConfig(), name="salon")
        # First a normal heating step establishes a non-trivial valve position.
        warm = controller.step(make_inputs(room_temperature_c=15.0), dt_seconds=300.0)
        held = warm.valve_position_pct
        assert held > ControllerConfig().valve_floor_pct

        lost = controller.step(make_inputs(room_temperature_c=None), dt_seconds=300.0)
        assert lost.valve_position_pct == held
        assert lost.fast_source.on is False
        assert lost.fast_source.mode is FastSourceMode.OFF
        assert "sensor_lost" in lost.report.flags
        assert lost.report.error_c is None
        assert lost.report.integrator_frozen is True

    @pytest.mark.unit
    def test_missing_temp_first_step_holds_valve_floor(self) -> None:
        """With no prior step, the held valve defaults to the heating floor."""
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        out = controller.step(make_inputs(room_temperature_c=None), dt_seconds=300.0)
        assert out.valve_position_pct == cfg.valve_floor_pct
        assert "sensor_lost" in out.report.flags

    @pytest.mark.unit
    def test_missing_temp_turns_running_fast_source_off(self) -> None:
        """A lost sensor forces an already-running split OFF (safety)."""
        controller = RoomController(ControllerConfig(), name="salon")
        engaged = controller.step(
            make_inputs(
                room_temperature_c=18.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert engaged.fast_source.on is True

        lost = controller.step(
            make_inputs(
                room_temperature_c=None,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert lost.fast_source.on is False


class TestSensorLostCooling:
    """C2 (2026-07-09): sensor loss in COOLING closes the valve, never freezes."""

    def _cooling_inputs(
        self, temp: float | None, *, humidity: float | None = 50.0
    ) -> RoomInputs:
        """Cooling inputs with a supply probe far above the dew point."""
        return make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=temp,
            humidity_pct=humidity,
            loops=(LoopInput(None, 20.0, None),),
        )

    @pytest.mark.unit
    def test_missing_temp_in_cooling_closes_valve(self) -> None:
        """A seeded cooling room with an open valve parks at 0 on sensor loss."""
        controller = RoomController(ControllerConfig(), name="salon")
        # Warm room (27 > 24 setpoint) opens the cooling valve.
        open_step = controller.step(self._cooling_inputs(27.0), dt_seconds=300.0)
        assert open_step.valve_position_pct > 0.0

        lost = controller.step(self._cooling_inputs(None), dt_seconds=300.0)
        assert lost.valve_position_pct == 0.0
        assert lost.fast_source.on is False
        assert "sensor_lost" in lost.report.flags

    @pytest.mark.unit
    def test_missing_temp_cooling_cold_start_is_zero(self) -> None:
        """With no prior live step, a cooling room parks at 0 on sensor loss."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(self._cooling_inputs(None), dt_seconds=300.0)
        assert out.valve_position_pct == 0.0
        assert "sensor_lost" in out.report.flags

    @pytest.mark.unit
    def test_cooling_loss_does_not_poison_heating_hold(self) -> None:
        """The cooling 0-park leaves the heating freeze memory untouched."""
        controller = RoomController(ControllerConfig(), name="salon")
        # Healthy heating step establishes a hold position.
        warm = controller.step(make_inputs(room_temperature_c=15.0), dt_seconds=300.0)
        held = warm.valve_position_pct
        assert held > 0.0
        # Sensor loss in cooling parks at 0 ...
        lost_cool = controller.step(self._cooling_inputs(None), dt_seconds=300.0)
        assert lost_cool.valve_position_pct == 0.0
        # ... but a later heating-mode loss still freezes the healthy position.
        lost_heat = controller.step(
            make_inputs(room_temperature_c=None), dt_seconds=300.0
        )
        assert lost_heat.valve_position_pct == pytest.approx(held)


class TestSafetyOverrideStateSync:
    """S5 (2026-07-09): the safety override keeps controller state honest."""

    @pytest.mark.unit
    def test_safety_close_does_not_poison_sensor_lost_hold(self) -> None:
        """An S1 trip must not leave 0 % as the sensor-lost freeze position."""
        controller = RoomController(ControllerConfig(), name="salon")
        hot_supply = (LoopInput(None, 45.0, None),)
        ok_supply = (LoopInput(None, 30.0, None),)

        # Healthy heating regulation (supply fine) establishes the hold.
        healthy = controller.step(
            make_inputs(room_temperature_c=15.0, loops=ok_supply), dt_seconds=300.0
        )
        assert healthy.valve_position_pct > 0.0

        # S1 floor-overheat trips: the OUTPUT closes the valve ...
        tripped = controller.step(
            make_inputs(room_temperature_c=15.0, loops=hot_supply), dt_seconds=300.0
        )
        assert "s1_floor_overheat" in tripped.report.flags
        assert tripped.valve_position_pct == 0.0
        # ... but the healthy hold memory survives the override.
        assert controller.last_valve_pct > 0.0
        held = controller.last_valve_pct

        # Supply recovers (S1 clears below 38) and the sensor is lost: the
        # freeze holds the healthy position, not the emergency 0.
        lost = controller.step(
            make_inputs(room_temperature_c=None, loops=ok_supply), dt_seconds=300.0
        )
        assert lost.valve_position_pct == pytest.approx(held)
        assert lost.valve_position_pct > 0.0

    @pytest.mark.unit
    def test_safety_force_on_syncs_split_machine(self) -> None:
        """S3 force-ON registers in the dwell machine: no instant OFF later."""
        controller = RoomController(ControllerConfig(), name="salon")
        # Frost trip (room 4 degC): emergency heat forces the split ON.
        frozen = controller.step(
            make_inputs(room_temperature_c=4.0, fast_source_kind=FastSourceKind.SPLIT),
            dt_seconds=300.0,
        )
        assert "s3_emergency_heat" in frozen.report.flags
        assert frozen.fast_source.on is True
        assert frozen.fast_source.mode is FastSourceMode.HEATING
        # The machine is synced: the min-ON lock is armed for the report.
        assert frozen.report.fast_dwell_remaining_s == pytest.approx(600.0)

        # The room recovers into the comfort band (S3 cleared, no demand): the
        # min-ON dwell keeps the just-started compressor running instead of an
        # abrupt OFF two seconds after the safety releases.
        recovered = controller.step(
            make_inputs(room_temperature_c=21.0, fast_source_kind=FastSourceKind.SPLIT),
            dt_seconds=300.0,
        )
        assert recovered.fast_source.on is True
        assert "fast_source_min_runtime" in recovered.report.flags


class TestSafetyValveVsAirSource:
    """S7 (2026-07-09): closing the valve never silences the air-side source."""

    @pytest.mark.unit
    def test_s1_with_s3_keeps_split_heating(self) -> None:
        """Frost + overheated water: valve closed, split still heats the air."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                room_temperature_c=4.0,
                loops=(LoopInput(None, 45.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert "s1_floor_overheat" in out.report.flags
        assert "s3_emergency_heat" in out.report.flags
        # Water side: S1 wins, the valve is closed.
        assert out.valve_position_pct == 0.0
        # Air side: S3 wins, the split heats.
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.HEATING

    @pytest.mark.unit
    def test_s1_alone_still_forces_split_off(self) -> None:
        """S1 without an emergency keeps the fast source released."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                room_temperature_c=20.0,
                loops=(LoopInput(None, 45.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert "s1_floor_overheat" in out.report.flags
        assert out.valve_position_pct == 0.0
        assert out.fast_source.on is False

    @pytest.mark.unit
    def test_s3_alone_opens_valve_fully(self) -> None:
        """S3 without a CLOSE_VALVE rule still opens the floor fully."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                room_temperature_c=4.0,
                loops=(LoopInput(None, 30.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert "s3_emergency_heat" in out.report.flags
        assert out.valve_position_pct == 100.0
        assert out.fast_source.on is True


class TestWatchdogNeutral:
    """S6 (2026-07-09): S5 fed by the adapter age, action = neutral position."""

    @pytest.mark.unit
    def test_stale_age_drives_neutral_position_heating(self) -> None:
        """Age > 15 min trips S5: heating parks at the valve floor."""
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        # Healthy cycle establishes a high valve (cold room).
        busy = controller.step(make_inputs(room_temperature_c=17.0), dt_seconds=300.0)
        assert busy.valve_position_pct > cfg.valve_floor_pct

        stale = RoomInputs(
            mode=Mode.HEATING,
            setpoint_c=21.0,
            room_temperature_c=17.0,
            last_update_age_minutes=20.0,
        )
        out = controller.step(stale, dt_seconds=300.0)
        assert "s5_watchdog" in out.report.flags
        assert out.valve_position_pct == pytest.approx(cfg.valve_floor_pct)
        assert out.fast_source.on is False

    @pytest.mark.unit
    def test_stale_age_closes_valve_cooling(self) -> None:
        """In COOLING the S5 neutral position is 0 (no blind chilled water)."""
        controller = RoomController(ControllerConfig(), name="salon")
        stale = RoomInputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=27.0,
            humidity_pct=45.0,
            loops=(LoopInput(None, 22.0, None),),
            last_update_age_minutes=20.0,
        )
        out = controller.step(stale, dt_seconds=300.0)
        assert "s5_watchdog" in out.report.flags
        assert out.valve_position_pct == 0.0

    @pytest.mark.unit
    def test_fresh_age_keeps_s5_quiet(self) -> None:
        """The default age 0.0 never trips S5 (compat for old callers)."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(make_inputs(room_temperature_c=19.0), dt_seconds=300.0)
        assert "s5_watchdog" not in out.report.flags


class TestSensorLostRoomsCounter:
    """safety-F13 (2026-07-09): building-level degraded-rooms counter."""

    @pytest.mark.unit
    def test_counts_sensor_lost_rooms(self) -> None:
        """The building output counts rooms currently flagged sensor_lost."""
        building = BuildingController(
            {"a": ControllerConfig(), "b": ControllerConfig()}
        )
        outputs = building.step(
            {
                "a": make_inputs(room_temperature_c=None),
                "b": make_inputs(room_temperature_c=20.0),
            },
            dt_seconds=300.0,
        )
        assert outputs.sensor_lost_rooms == 1
        assert outputs.to_dict()["sensor_lost_rooms"] == 1

        outputs = building.step(
            {
                "a": make_inputs(room_temperature_c=20.0),
                "b": make_inputs(room_temperature_c=20.0),
            },
            dt_seconds=300.0,
        )
        assert outputs.sensor_lost_rooms == 0
