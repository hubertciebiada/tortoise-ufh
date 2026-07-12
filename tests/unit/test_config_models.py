"""Unit tests for the config and models dataclass contracts.

Covers two frozen-dataclass concerns of the pure core:

* ``config.py`` ``__post_init__`` validation — every configuration invariant
  must fail fast with :class:`ValueError`: non-positive floor area, duplicate
  room names, out-of-range latitude/longitude, and negative controller gains
  (plus adjacent bound checks).
* ``models.py`` ``to_dict()`` serialization — the result dataclasses must
  round-trip through :func:`json.dumps` (i.e. be JSON-serializable using only
  ``dict``/``list``/``str``/``float``/``bool``/``None``) with every enum mapped
  to its ``.value``.

Units are the repo-wide contract: temperatures/setpoints in degrees Celsius,
valve/humidity in percent (0..100), area in square metres, latitude/longitude
in degrees, controller gains as documented in :class:`ControllerConfig`.

This module is part of the pure-core test suite: it imports ONLY from
``tortoise_ufh`` and never from ``homeassistant``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from custom_components.tortoise_ufh.core.config import (
    BuildingConfig,
    ControllerConfig,
    Orientation,
    RoomConfig,
    WindowConfig,
)
from custom_components.tortoise_ufh.core.models import (
    BuildingOutputs,
    FastSourceCommand,
    FastSourceMode,
    Mode,
    RoomOutputs,
    RoomReport,
)
from custom_components.tortoise_ufh.core.rc_model import RCParams

# ---------------------------------------------------------------------------
# Helpers — minimal valid building blocks (no test fixtures assumed)
# ---------------------------------------------------------------------------


def _valid_params() -> RCParams:
    """Return a minimal validated SISO 3R3C :class:`RCParams`.

    Returns:
        A physically realistic ~20 m^2 UFH-room parameter set (K/W, J/K).
    """
    return RCParams(
        C_air=60_000.0,
        C_slab=3_250_000.0,
        R_sf=0.01,
        C_wall=1_500_000.0,
        R_wi=0.02,
        R_wo=0.03,
        R_ve=0.03,
        R_ins=0.01,
    )


def _room(name: str, *, area_m2: float = 20.0) -> RoomConfig:
    """Return a minimal validated :class:`RoomConfig`.

    Args:
        name: Room identifier.
        area_m2: Floor area in square metres.

    Returns:
        A validated single-loop, no-fast-source room configuration.
    """
    return RoomConfig(name=name, area_m2=area_m2, params=_valid_params())


def _report() -> RoomReport:
    """Return a fully-populated :class:`RoomReport` for serialization tests.

    Returns:
        A validated report with a non-empty flag tuple and explanation.
    """
    return RoomReport(
        error_c=-0.4,
        trend_c_per_h=0.3,
        room_dew_point_c=12.5,
        p_term=-3.2,
        i_term=1.1,
        trend_term=-1.8,
        feedforward_term=0.0,
        raw_valve_pct=34.0,
        valve_floor_applied=True,
        saturated=False,
        dew_throttle_factor=1.0,
        integrator_frozen=False,
        flags=("sensor_lost", "fast_source_min_runtime"),
        explanation="Grzanie, blad -0.4 K, trend +0.3 K/h. Zawor 34%.",
    )


# ---------------------------------------------------------------------------
# ControllerConfig validation
# ---------------------------------------------------------------------------


class TestControllerConfigValidation:
    """``ControllerConfig.__post_init__`` invariant checks."""

    @pytest.mark.unit
    def test_defaults_are_valid(self) -> None:
        """The frozen defaults construct without raising."""
        cfg = ControllerConfig()
        assert cfg.kp >= 0

    @pytest.mark.unit
    @pytest.mark.parametrize("gain", ["kp", "ki", "kd", "kt"])
    def test_negative_gain_rejected(self, gain: str) -> None:
        """A negative PID/trend gain raises ValueError naming that gain."""
        kwargs: dict[str, Any] = {gain: -0.1}
        with pytest.raises(ValueError, match=f"{gain} must be >= 0"):
            ControllerConfig(**kwargs)

    @pytest.mark.unit
    def test_zero_gains_allowed(self) -> None:
        """Zero gains are on the valid boundary (>= 0)."""
        cfg = ControllerConfig(kp=0.0, ki=0.0, kd=0.0, kt=0.0)
        assert cfg.ki == 0.0

    @pytest.mark.unit
    def test_negative_deadband_rejected(self) -> None:
        """A negative deadband half-width raises ValueError."""
        with pytest.raises(ValueError, match="deadband_c must be >= 0"):
            ControllerConfig(deadband_c=-0.1)

    @pytest.mark.unit
    @pytest.mark.parametrize("floor", [-1.0, 100.1])
    def test_valve_floor_out_of_range_rejected(self, floor: float) -> None:
        """A valve floor outside [0, 100] % raises ValueError."""
        with pytest.raises(ValueError, match=r"valve_floor_pct must be in \[0, 100\]"):
            ControllerConfig(valve_floor_pct=floor)

    @pytest.mark.unit
    def test_negative_boost_offset_rejected(self) -> None:
        """A negative fast-source boost offset raises ValueError."""
        with pytest.raises(ValueError, match="boost_offset_c must be >= 0"):
            ControllerConfig(boost_offset_c=-0.5)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "field_name", ["fast_min_on_minutes", "fast_min_off_minutes"]
    )
    def test_negative_dwell_time_rejected(self, field_name: str) -> None:
        """A negative fast-source dwell time raises ValueError."""
        kwargs: dict[str, Any] = {field_name: -1.0}
        with pytest.raises(ValueError, match=f"{field_name} must be >= 0"):
            ControllerConfig(**kwargs)

    @pytest.mark.unit
    def test_negative_dew_margin_rejected(self) -> None:
        """A negative dew-point margin raises ValueError."""
        with pytest.raises(ValueError, match="dew_margin_k must be >= 0"):
            ControllerConfig(dew_margin_k=-0.1)

    @pytest.mark.unit
    def test_nonpositive_dew_ramp_rejected(self) -> None:
        """A non-positive dew-point ramp width raises ValueError."""
        with pytest.raises(ValueError, match="dew_ramp_k must be > 0"):
            ControllerConfig(dew_ramp_k=0.0)

    @pytest.mark.unit
    def test_nonpositive_cycle_seconds_rejected(self) -> None:
        """A non-positive control-cycle period raises ValueError."""
        with pytest.raises(ValueError, match="cycle_seconds must be > 0"):
            ControllerConfig(cycle_seconds=0.0)

    @pytest.mark.unit
    def test_negative_write_threshold_rejected(self) -> None:
        """A negative valve-write threshold raises ValueError."""
        with pytest.raises(ValueError, match="valve_write_threshold_pct must be >= 0"):
            ControllerConfig(valve_write_threshold_pct=-1.0)


# ---------------------------------------------------------------------------
# WindowConfig validation
# ---------------------------------------------------------------------------


class TestWindowConfigValidation:
    """``WindowConfig.__post_init__`` invariant checks."""

    @pytest.mark.unit
    @pytest.mark.parametrize("area", [0.0, -3.0])
    def test_nonpositive_area_rejected(self, area: float) -> None:
        """A non-positive glazed area raises ValueError."""
        with pytest.raises(ValueError, match="area_m2 must be > 0"):
            WindowConfig(orientation=Orientation.SOUTH, area_m2=area, g_value=0.6)

    @pytest.mark.unit
    @pytest.mark.parametrize("g_value", [0.0, -0.1, 1.5])
    def test_g_value_out_of_range_rejected(self, g_value: float) -> None:
        """A g-value outside the half-open interval (0, 1] raises ValueError."""
        with pytest.raises(ValueError, match=r"g_value must be in \(0, 1\]"):
            WindowConfig(orientation=Orientation.SOUTH, area_m2=3.0, g_value=g_value)


# ---------------------------------------------------------------------------
# RoomConfig validation
# ---------------------------------------------------------------------------


class TestRoomConfigValidation:
    """``RoomConfig.__post_init__`` invariant checks."""

    @pytest.mark.unit
    def test_minimal_room_valid(self) -> None:
        """A minimal room constructs without raising."""
        room = _room("salon")
        assert room.name == "salon"

    @pytest.mark.unit
    @pytest.mark.parametrize("area", [0.0, -5.0])
    def test_nonpositive_area_rejected(self, area: float) -> None:
        """A non-positive floor area raises ValueError."""
        with pytest.raises(ValueError, match="area_m2 must be > 0"):
            _room("salon", area_m2=area)

    @pytest.mark.unit
    @pytest.mark.parametrize("name", ["", "   "])
    def test_empty_name_rejected(self, name: str) -> None:
        """An empty or whitespace-only room name raises ValueError."""
        with pytest.raises(ValueError, match="name must be a non-empty string"):
            RoomConfig(name=name, area_m2=20.0, params=_valid_params())

    @pytest.mark.unit
    def test_zero_loops_rejected(self) -> None:
        """Fewer than one UFH loop raises ValueError."""
        with pytest.raises(ValueError, match="n_loops must be >= 1"):
            RoomConfig(name="salon", area_m2=20.0, params=_valid_params(), n_loops=0)

    @pytest.mark.unit
    def test_negative_fast_source_power_rejected(self) -> None:
        """A negative fast-source power raises ValueError."""
        with pytest.raises(ValueError, match="fast_source_power_w must be >= 0"):
            RoomConfig(
                name="salon",
                area_m2=20.0,
                params=_valid_params(),
                fast_source_power_w=-100.0,
            )

    @pytest.mark.unit
    def test_fast_source_kind_none_when_enabled_rejected(self) -> None:
        """has_fast_source=True with kind NONE is inconsistent and rejected."""
        from custom_components.tortoise_ufh.core.models import FastSourceKind

        with pytest.raises(ValueError, match="must not be NONE when has_fast_source"):
            RoomConfig(
                name="salon",
                area_m2=20.0,
                params=_valid_params(),
                has_fast_source=True,
                fast_source_kind=FastSourceKind.NONE,
                fast_source_power_w=2500.0,
            )

    @pytest.mark.unit
    def test_fast_source_kind_set_when_disabled_rejected(self) -> None:
        """has_fast_source=False with a non-NONE kind is rejected."""
        from custom_components.tortoise_ufh.core.models import FastSourceKind

        with pytest.raises(ValueError, match="must be NONE when has_fast_source=False"):
            RoomConfig(
                name="salon",
                area_m2=20.0,
                params=_valid_params(),
                has_fast_source=False,
                fast_source_kind=FastSourceKind.SPLIT,
            )

    @pytest.mark.unit
    def test_fast_source_zero_power_when_enabled_rejected(self) -> None:
        """has_fast_source=True requires strictly positive power."""
        from custom_components.tortoise_ufh.core.models import FastSourceKind

        with pytest.raises(ValueError, match="fast_source_power_w must be > 0 when"):
            RoomConfig(
                name="salon",
                area_m2=20.0,
                params=_valid_params(),
                has_fast_source=True,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_power_w=0.0,
            )


# ---------------------------------------------------------------------------
# BuildingConfig validation
# ---------------------------------------------------------------------------


class TestBuildingConfigValidation:
    """``BuildingConfig.__post_init__`` invariant checks."""

    @pytest.mark.unit
    def test_minimal_building_valid(self) -> None:
        """A one-room building constructs without raising."""
        building = BuildingConfig(
            rooms=(_room("salon"),),
            hp_max_power_w=4900.0,
            latitude=50.5,
            longitude=19.5,
        )
        assert len(building.rooms) == 1

    @pytest.mark.unit
    def test_no_rooms_rejected(self) -> None:
        """An empty room tuple raises ValueError."""
        with pytest.raises(ValueError, match="rooms must contain at least 1 room"):
            BuildingConfig(
                rooms=(), hp_max_power_w=4900.0, latitude=50.0, longitude=17.0
            )

    @pytest.mark.unit
    def test_duplicate_room_names_rejected(self) -> None:
        """Two rooms with the same name raise ValueError listing the duplicate."""
        with pytest.raises(ValueError, match="room names must be unique"):
            BuildingConfig(
                rooms=(_room("salon"), _room("salon")),
                hp_max_power_w=4900.0,
                latitude=50.0,
                longitude=17.0,
            )

    @pytest.mark.unit
    def test_duplicate_name_reported_in_message(self) -> None:
        """The duplicate room name appears in the error message."""
        with pytest.raises(ValueError, match="kuchnia"):
            BuildingConfig(
                rooms=(_room("kuchnia"), _room("kuchnia"), _room("salon")),
                hp_max_power_w=4900.0,
                latitude=50.0,
                longitude=17.0,
            )

    @pytest.mark.unit
    @pytest.mark.parametrize("power", [0.0, -4900.0])
    def test_nonpositive_hp_power_rejected(self, power: float) -> None:
        """A non-positive heat-pump power raises ValueError."""
        with pytest.raises(ValueError, match="hp_max_power_w must be > 0"):
            BuildingConfig(
                rooms=(_room("salon"),),
                hp_max_power_w=power,
                latitude=50.0,
                longitude=17.0,
            )

    @pytest.mark.unit
    @pytest.mark.parametrize("latitude", [-90.1, 90.1, 200.0])
    def test_latitude_out_of_range_rejected(self, latitude: float) -> None:
        """A latitude outside [-90, 90] raises ValueError."""
        with pytest.raises(ValueError, match=r"latitude must be in \[-90, 90\]"):
            BuildingConfig(
                rooms=(_room("salon"),),
                hp_max_power_w=4900.0,
                latitude=latitude,
                longitude=17.0,
            )

    @pytest.mark.unit
    @pytest.mark.parametrize("longitude", [-180.1, 180.1, 360.0])
    def test_longitude_out_of_range_rejected(self, longitude: float) -> None:
        """A longitude outside [-180, 180] raises ValueError."""
        with pytest.raises(ValueError, match=r"longitude must be in \[-180, 180\]"):
            BuildingConfig(
                rooms=(_room("salon"),),
                hp_max_power_w=4900.0,
                latitude=50.0,
                longitude=longitude,
            )

    @pytest.mark.unit
    @pytest.mark.parametrize("latitude", [-90.0, 0.0, 90.0])
    def test_latitude_boundaries_allowed(self, latitude: float) -> None:
        """The latitude endpoints -90 and 90 are inclusive-valid."""
        building = BuildingConfig(
            rooms=(_room("salon"),),
            hp_max_power_w=4900.0,
            latitude=latitude,
            longitude=17.0,
        )
        assert building.latitude == latitude


# ---------------------------------------------------------------------------
# models.py — to_dict() JSON-serializability + enum -> .value
# ---------------------------------------------------------------------------


def _assert_json_roundtrip(payload: dict[str, Any]) -> dict[str, Any]:
    """Assert a dict survives a ``json.dumps``/``json.loads`` round-trip.

    Args:
        payload: The candidate JSON-serializable mapping.

    Returns:
        The parsed mapping (identical in content to ``payload``).
    """
    parsed: dict[str, Any] = json.loads(json.dumps(payload))
    assert parsed == payload
    return parsed


class TestFastSourceCommandToDict:
    """``FastSourceCommand.to_dict`` serialization and enum mapping."""

    @pytest.mark.unit
    def test_mode_maps_to_value(self) -> None:
        """The mode enum is rendered as its string ``.value``."""
        cmd = FastSourceCommand(
            on=True, mode=FastSourceMode.HEATING, target_temperature_c=21.0
        )
        payload = cmd.to_dict()
        assert payload["mode"] == "heating"
        assert payload["mode"] == FastSourceMode.HEATING.value

    @pytest.mark.unit
    def test_off_command_json_serializable(self) -> None:
        """An off command (target None) round-trips through JSON."""
        payload = FastSourceCommand(on=False).to_dict()
        parsed = _assert_json_roundtrip(payload)
        assert parsed["mode"] == "off"
        assert parsed["target_temperature_c"] is None

    @pytest.mark.unit
    def test_no_enum_objects_leak(self) -> None:
        """The serialized dict contains no raw Enum instances."""
        payload = FastSourceCommand(
            on=True, mode=FastSourceMode.COOLING, target_temperature_c=18.0
        ).to_dict()
        assert not any(isinstance(v, FastSourceMode) for v in payload.values())


class TestRoomReportToDict:
    """``RoomReport.to_dict`` serialization."""

    @pytest.mark.unit
    def test_flags_become_list(self) -> None:
        """The flags tuple is serialized as a JSON list."""
        payload = _report().to_dict()
        assert isinstance(payload["flags"], list)
        assert payload["flags"] == ["sensor_lost", "fast_source_min_runtime"]

    @pytest.mark.unit
    def test_report_json_serializable(self) -> None:
        """A fully-populated report round-trips through JSON."""
        _assert_json_roundtrip(_report().to_dict())

    @pytest.mark.unit
    def test_none_fields_preserved(self) -> None:
        """``None`` optional fields survive serialization as JSON null."""
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
            integrator_frozen=False,
        )
        parsed = _assert_json_roundtrip(report.to_dict())
        assert parsed["error_c"] is None
        assert parsed["trend_c_per_h"] is None
        assert parsed["room_dew_point_c"] is None


class TestRoomOutputsToDict:
    """``RoomOutputs.to_dict`` nested serialization."""

    @pytest.mark.unit
    def test_nested_structure_json_serializable(self) -> None:
        """A room result with nested command + report round-trips through JSON."""
        outputs = RoomOutputs(
            valve_position_pct=34.0,
            fast_source=FastSourceCommand(
                on=True, mode=FastSourceMode.HEATING, target_temperature_c=21.0
            ),
            report=_report(),
        )
        parsed = _assert_json_roundtrip(outputs.to_dict())
        assert parsed["valve_position_pct"] == 34.0
        assert parsed["fast_source"]["mode"] == "heating"
        assert parsed["report"]["flags"] == [
            "sensor_lost",
            "fast_source_min_runtime",
        ]


class TestBuildingOutputsToDict:
    """``BuildingOutputs.to_dict`` whole-building serialization."""

    @pytest.mark.unit
    def test_building_json_serializable(self) -> None:
        """A multi-room building result round-trips through JSON."""
        room_out = RoomOutputs(
            valve_position_pct=50.0,
            fast_source=FastSourceCommand(on=False),
            report=_report(),
        )
        outputs = BuildingOutputs(
            rooms={"salon": room_out, "kuchnia": room_out},
            global_safe_dew_point_c=14.5,
        )
        parsed = _assert_json_roundtrip(outputs.to_dict())
        assert set(parsed["rooms"]) == {"salon", "kuchnia"}
        assert parsed["global_safe_dew_point_c"] == 14.5

    @pytest.mark.unit
    def test_none_global_dew_point_preserved(self) -> None:
        """A ``None`` global dew point serializes to JSON null."""
        outputs = BuildingOutputs(rooms={}, global_safe_dew_point_c=None)
        parsed = _assert_json_roundtrip(outputs.to_dict())
        assert parsed["global_safe_dew_point_c"] is None
        assert parsed["rooms"] == {}


# ---------------------------------------------------------------------------
# Enum value contract (Mode / FastSourceMode)
# ---------------------------------------------------------------------------


class TestEnumValueContract:
    """The closed string-set enums expose the frozen ``.value`` mapping."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("member", "expected"),
        [
            (Mode.HEATING, "heating"),
            (Mode.TRANSITIONAL, "transitional"),
            (Mode.COOLING, "cooling"),
            (Mode.OFF, "off"),
        ],
    )
    def test_mode_values(self, member: Mode, expected: str) -> None:
        """Each :class:`Mode` member maps to its documented lowercase value."""
        assert member.value == expected

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("member", "expected"),
        [
            (FastSourceMode.OFF, "off"),
            (FastSourceMode.HEATING, "heating"),
            (FastSourceMode.COOLING, "cooling"),
        ],
    )
    def test_fast_source_mode_values(
        self, member: FastSourceMode, expected: str
    ) -> None:
        """Each :class:`FastSourceMode` member maps to its documented value."""
        assert member.value == expected


class TestSetpointScheduleValidation:
    """Fail-fast validation of ``SimScenario.setpoint_schedule`` (K1, night setback)."""

    @staticmethod
    def _with_schedule(schedule: tuple[tuple[float, float], ...]) -> None:
        """Rebuild the steady_heating scenario with the given schedule."""
        from dataclasses import replace

        from custom_components.tortoise_ufh.core.scenarios import steady_heating

        replace(steady_heating(), setpoint_schedule=schedule)

    @pytest.mark.unit
    def test_valid_schedule_constructs(self) -> None:
        """A strictly increasing, in-range schedule is accepted."""
        self._with_schedule(((0.0, 21.0), (720.0, 19.0), (1440.0, 21.0)))

    @pytest.mark.unit
    def test_negative_minute_rejected(self) -> None:
        """A negative schedule minute raises ``ValueError``."""
        with pytest.raises(ValueError, match="must be >= 0"):
            self._with_schedule(((-5.0, 21.0),))

    @pytest.mark.unit
    def test_non_increasing_minutes_rejected(self) -> None:
        """Non-strictly-increasing minutes raise ``ValueError``."""
        with pytest.raises(ValueError, match="strictly increasing"):
            self._with_schedule(((0.0, 21.0), (0.0, 19.0)))

    @pytest.mark.unit
    def test_out_of_range_setpoint_rejected(self) -> None:
        """A schedule setpoint outside [0, 35] degC raises ``ValueError``."""
        with pytest.raises(ValueError, match=r"in \[0, 35\]"):
            self._with_schedule(((0.0, 40.0),))


class TestHeatPumpWaterKnobs:
    """B2 (2026-07-12): the global heat-pump water setpoints validate."""

    @pytest.mark.unit
    def test_defaults_valid(self) -> None:
        """The library defaults (18 / 26 / 0.5) construct cleanly."""
        cfg = ControllerConfig()
        assert cfg.cooling_supply_base_c == pytest.approx(18.0)
        assert cfg.heating_supply_base_c == pytest.approx(26.0)
        assert cfg.heating_supply_slope == pytest.approx(0.5)

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [9.9, 25.1, -5.0])
    def test_cooling_supply_base_out_of_range_rejected(self, value: float) -> None:
        """cooling_supply_base_c must sit in [10, 25]."""
        with pytest.raises(ValueError, match="cooling_supply_base_c"):
            ControllerConfig(cooling_supply_base_c=value)

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [19.9, 40.1])
    def test_heating_supply_base_out_of_range_rejected(self, value: float) -> None:
        """heating_supply_base_c must sit in [20, 40]."""
        with pytest.raises(ValueError, match="heating_supply_base_c"):
            ControllerConfig(heating_supply_base_c=value)

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [-0.1, 2.1])
    def test_heating_supply_slope_out_of_range_rejected(self, value: float) -> None:
        """heating_supply_slope must sit in [0, 2]."""
        with pytest.raises(ValueError, match="heating_supply_slope"):
            ControllerConfig(heating_supply_slope=value)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("cooling_supply_base_c", 10.0),
            ("cooling_supply_base_c", 25.0),
            ("heating_supply_base_c", 20.0),
            ("heating_supply_base_c", 40.0),
            ("heating_supply_slope", 0.0),
            ("heating_supply_slope", 2.0),
        ],
    )
    def test_boundaries_allowed(self, field_name: str, value: float) -> None:
        """The inclusive range boundaries construct cleanly."""
        cfg = ControllerConfig(**{field_name: value})
        assert getattr(cfg, field_name) == pytest.approx(value)
