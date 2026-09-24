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
from dataclasses import replace
from typing import Any

import pytest

from custom_components.tortoise_ufh.core.config import (
    BuildingConfig,
    ControllerConfig,
    Orientation,
    RoomConfig,
    SimScenario,
    WindowConfig,
)
from custom_components.tortoise_ufh.core.models import (
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
from custom_components.tortoise_ufh.core.rc_model import RCParams
from custom_components.tortoise_ufh.core.scenarios import steady_heating
from tests.unit.conftest import make_inputs

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

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [0.4, 3.1])
    def test_flicker_band_out_of_range_rejected(self, value: float) -> None:
        """A flicker band outside [0.5, 3.0] raises ValueError (issue #7)."""
        with pytest.raises(ValueError, match=r"hp_flicker_band_k must be in"):
            ControllerConfig(hp_flicker_band_k=value)

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [4.9, 121.0])
    def test_flicker_stuck_minutes_out_of_range_rejected(self, value: float) -> None:
        """A flicker stuck time outside [5, 120] raises ValueError (issue #7)."""
        with pytest.raises(ValueError, match=r"hp_flicker_stuck_minutes must be in"):
            ControllerConfig(hp_flicker_stuck_minutes=value)

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [4.9, 121.0])
    def test_flicker_min_off_minutes_out_of_range_rejected(self, value: float) -> None:
        """A flicker cooldown outside [5, 120] raises ValueError (issue #7)."""
        with pytest.raises(ValueError, match=r"hp_flicker_min_off_minutes must be in"):
            ControllerConfig(hp_flicker_min_off_minutes=value)

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [0.9, 6.1])
    def test_flicker_max_starts_out_of_range_rejected(self, value: float) -> None:
        """A flicker max-starts cap outside [1, 6] raises ValueError (issue #7)."""
        with pytest.raises(ValueError, match=r"hp_flicker_max_starts_per_h must be in"):
            ControllerConfig(hp_flicker_max_starts_per_h=value)

    @pytest.mark.unit
    def test_flicker_knob_bounds_are_inclusive(self) -> None:
        """The flicker knob range endpoints are accepted (issue #7)."""
        cfg = ControllerConfig(
            hp_flicker_band_k=0.5,
            hp_flicker_stuck_minutes=120.0,
            hp_flicker_min_off_minutes=5.0,
            hp_flicker_max_starts_per_h=6.0,
        )
        assert cfg.hp_flicker_band_k == 0.5


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


# ---------------------------------------------------------------------------
# Mutation-survivor pins (group g3): inclusive boundaries + exact messages
#
# The classes below pin behaviour that surviving mutmut mutants proved was
# unpinned: inclusive vs. exclusive validation endpoints, sub-unit acceptance
# of ``> 0`` knobs, the exact ``ValueError`` message texts of the documented
# validation contract, and the exact key set of the ``to_dict`` serializers.
# ---------------------------------------------------------------------------


class TestControllerConfigBoundaryPins:
    """``ControllerConfig`` inclusive boundaries and exact error messages."""

    @pytest.mark.unit
    def test_zero_deadband_allowed(self) -> None:
        """``deadband_c = 0`` is on the valid boundary (documented ``>= 0``)."""
        cfg = ControllerConfig(deadband_c=0.0)
        assert cfg.deadband_c == 0.0

    @pytest.mark.unit
    @pytest.mark.parametrize("floor", [0.0, 0.5, 100.0])
    def test_valve_floor_boundaries_allowed(self, floor: float) -> None:
        """``valve_floor_pct`` 0 and 100 are inclusive-valid endpoints."""
        cfg = ControllerConfig(valve_floor_pct=floor)
        assert cfg.valve_floor_pct == floor

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [-30.0, 40.0])
    def test_ff_neutral_inside_range_allowed(self, value: float) -> None:
        """``ff_neutral_c`` -30 and 40 are inclusive-valid endpoints."""
        cfg = ControllerConfig(ff_neutral_c=value)
        assert cfg.ff_neutral_c == value

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [-30.5, 40.5])
    def test_ff_neutral_out_of_range_exact_message(self, value: float) -> None:
        """``ff_neutral_c`` outside [-30, 40] raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(ff_neutral_c=value)
        assert str(exc_info.value) == (
            f"ff_neutral_c must be in [-30, 40], got {value}"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [0.0, 0.5])
    def test_ff_gain_zero_and_fraction_allowed(self, value: float) -> None:
        """``ff_gain_pct_per_k`` 0 and sub-1 values are valid (``>= 0``)."""
        cfg = ControllerConfig(ff_gain_pct_per_k=value)
        assert cfg.ff_gain_pct_per_k == value

    @pytest.mark.unit
    def test_ff_gain_negative_exact_message(self) -> None:
        """A negative feedforward gain raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(ff_gain_pct_per_k=-0.1)
        assert str(exc_info.value) == "ff_gain_pct_per_k must be >= 0, got -0.1"

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [0.0, 0.5, 100.0])
    def test_ff_max_boundaries_allowed(self, value: float) -> None:
        """``ff_max_pct`` 0 and 100 are inclusive-valid endpoints."""
        cfg = ControllerConfig(ff_max_pct=value)
        assert cfg.ff_max_pct == value

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [-0.1, 100.5])
    def test_ff_max_out_of_range_exact_message(self, value: float) -> None:
        """``ff_max_pct`` outside [0, 100] raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(ff_max_pct=value)
        assert str(exc_info.value) == f"ff_max_pct must be in [0, 100], got {value}"

    @pytest.mark.unit
    def test_boost_offset_not_above_deadband_exact_message(self) -> None:
        """``boost_offset_c <= deadband_c`` raises the hysteresis message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(deadband_c=0.0, boost_offset_c=0.0)
        assert str(exc_info.value) == (
            "boost_offset_c must be > deadband_c (the engage threshold must "
            "lie outside the comfort band, or the fast-source hysteresis "
            "inverts), got boost_offset_c=0.0 <= deadband_c=0.0"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [0.0, 3.0])
    def test_fast_target_offset_boundaries_allowed(self, value: float) -> None:
        """``fast_target_offset_k`` 0 and 3 are inclusive-valid endpoints."""
        cfg = ControllerConfig(fast_target_offset_k=value)
        assert cfg.fast_target_offset_k == value

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [-0.1, 3.1])
    def test_fast_target_offset_out_of_range_exact_message(self, value: float) -> None:
        """``fast_target_offset_k`` outside [0, 3] raises the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(fast_target_offset_k=value)
        assert str(exc_info.value) == (
            f"fast_target_offset_k must be in [0, 3], got {value}"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [12.0, 12.5, 22.0])
    def test_dry_dew_max_boundaries_allowed(self, value: float) -> None:
        """``dry_dew_max_c`` 12 and 22 are inclusive-valid endpoints."""
        cfg = ControllerConfig(dry_dew_max_c=value)
        assert cfg.dry_dew_max_c == value

    @pytest.mark.unit
    def test_dry_dew_max_out_of_range_exact_message(self) -> None:
        """``dry_dew_max_c`` above 22 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(dry_dew_max_c=22.5)
        assert str(exc_info.value) == "dry_dew_max_c must be in [12, 22], got 22.5"

    @pytest.mark.unit
    def test_fast_manual_hold_negative_exact_message(self) -> None:
        """A negative manual-hold duration raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(fast_manual_hold_minutes=-1.0)
        assert str(exc_info.value) == (
            "fast_manual_hold_minutes must be >= 0, got -1.0"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [0.0, 0.5])
    def test_dew_margin_zero_and_fraction_allowed(self, value: float) -> None:
        """``dew_margin_k`` 0 and sub-1 values are valid (``>= 0``)."""
        cfg = ControllerConfig(dew_margin_k=value)
        assert cfg.dew_margin_k == value

    @pytest.mark.unit
    def test_cooling_supply_base_exact_message(self) -> None:
        """``cooling_supply_base_c`` below 10 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(cooling_supply_base_c=9.9)
        assert str(exc_info.value) == (
            "cooling_supply_base_c must be in [10, 25], got 9.9"
        )

    @pytest.mark.unit
    def test_heating_supply_base_exact_message(self) -> None:
        """``heating_supply_base_c`` above 40 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(heating_supply_base_c=40.1)
        assert str(exc_info.value) == (
            "heating_supply_base_c must be in [20, 40], got 40.1"
        )

    @pytest.mark.unit
    def test_heating_supply_slope_exact_message(self) -> None:
        """``heating_supply_slope`` above 2 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(heating_supply_slope=2.1)
        assert str(exc_info.value) == "heating_supply_slope must be in [0, 2], got 2.1"

    @pytest.mark.unit
    def test_flicker_band_upper_boundary_allowed(self) -> None:
        """``hp_flicker_band_k = 3.0`` is the inclusive-valid upper bound."""
        cfg = ControllerConfig(hp_flicker_band_k=3.0)
        assert cfg.hp_flicker_band_k == 3.0

    @pytest.mark.unit
    def test_flicker_stuck_lower_boundary_allowed(self) -> None:
        """``hp_flicker_stuck_minutes = 5.0`` is the inclusive lower bound."""
        cfg = ControllerConfig(hp_flicker_stuck_minutes=5.0)
        assert cfg.hp_flicker_stuck_minutes == 5.0

    @pytest.mark.unit
    def test_flicker_stuck_exact_message(self) -> None:
        """A too-short flicker stuck time raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(hp_flicker_stuck_minutes=4.9)
        assert str(exc_info.value) == (
            "hp_flicker_stuck_minutes must be in [5, 120], got 4.9"
        )

    @pytest.mark.unit
    def test_flicker_min_off_upper_boundary_allowed(self) -> None:
        """``hp_flicker_min_off_minutes = 120.0`` is the inclusive upper bound."""
        cfg = ControllerConfig(hp_flicker_min_off_minutes=120.0)
        assert cfg.hp_flicker_min_off_minutes == 120.0

    @pytest.mark.unit
    def test_flicker_min_off_exact_message(self) -> None:
        """A too-long flicker cooldown raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(hp_flicker_min_off_minutes=121.0)
        assert str(exc_info.value) == (
            "hp_flicker_min_off_minutes must be in [5, 120], got 121.0"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [1.0, 1.5])
    def test_flicker_max_starts_lower_values_allowed(self, value: float) -> None:
        """``hp_flicker_max_starts_per_h`` 1 and sub-2 values are valid."""
        cfg = ControllerConfig(hp_flicker_max_starts_per_h=value)
        assert cfg.hp_flicker_max_starts_per_h == value

    @pytest.mark.unit
    def test_flicker_max_starts_exact_message(self) -> None:
        """A sub-1 flicker start cap raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(hp_flicker_max_starts_per_h=0.9)
        assert str(exc_info.value) == (
            "hp_flicker_max_starts_per_h must be in [1, 6], got 0.9"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [100.0, 10000.0])
    def test_flicker_min_open_boundaries_allowed(self, value: float) -> None:
        """``hp_flicker_min_open_pct`` 100 and 10000 are inclusive bounds."""
        cfg = ControllerConfig(hp_flicker_min_open_pct=value)
        assert cfg.hp_flicker_min_open_pct == value

    @pytest.mark.unit
    def test_flicker_min_open_exact_message(self) -> None:
        """A flicker demand gate above 10000 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(hp_flicker_min_open_pct=10000.5)
        assert str(exc_info.value) == (
            "hp_flicker_min_open_pct must be in [100, 10000], got 10000.5"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [0.0, 0.5, 100.0])
    def test_flow_open_threshold_boundaries_allowed(self, value: float) -> None:
        """``flow_open_threshold_pct`` 0 and 100 are inclusive endpoints (S6)."""
        cfg = ControllerConfig(flow_open_threshold_pct=value)
        assert cfg.flow_open_threshold_pct == value

    @pytest.mark.unit
    def test_flow_open_threshold_exact_message(self) -> None:
        """A flow-open threshold above 100 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(flow_open_threshold_pct=100.1)
        assert str(exc_info.value) == (
            "flow_open_threshold_pct must be in [0, 100], got 100.1"
        )

    @pytest.mark.unit
    def test_flow_response_window_sub_minute_allowed(self) -> None:
        """Any positive ``flow_response_window_min`` is valid (S6, sim use)."""
        cfg = ControllerConfig(flow_response_window_min=0.5)
        assert cfg.flow_response_window_min == 0.5

    @pytest.mark.unit
    def test_flow_response_window_exact_message(self) -> None:
        """A zero flow-response window raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(flow_response_window_min=0.0)
        assert str(exc_info.value) == "flow_response_window_min must be > 0, got 0.0"

    @pytest.mark.unit
    def test_cycle_seconds_sub_second_allowed(self) -> None:
        """Any positive ``cycle_seconds`` is valid (``> 0``)."""
        cfg = ControllerConfig(cycle_seconds=0.5)
        assert cfg.cycle_seconds == 0.5

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [0.0, 0.5])
    def test_valve_write_threshold_zero_and_fraction_allowed(
        self, value: float
    ) -> None:
        """``valve_write_threshold_pct`` 0 and sub-1 values are valid (>= 0)."""
        cfg = ControllerConfig(valve_write_threshold_pct=value)
        assert cfg.valve_write_threshold_pct == value


class TestRoomConfigBoundaryPins:
    """``RoomConfig`` inclusive boundaries and exact error messages."""

    @pytest.mark.unit
    def test_fractional_area_allowed(self) -> None:
        """Any positive ``area_m2`` is valid (``> 0``)."""
        room = _room("salon", area_m2=0.5)
        assert room.area_m2 == 0.5

    @pytest.mark.unit
    def test_empty_name_exact_message(self) -> None:
        """An empty room name raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            RoomConfig(name="", area_m2=20.0, params=_valid_params())
        assert str(exc_info.value) == "name must be a non-empty string"

    @pytest.mark.unit
    def test_kind_none_when_enabled_exact_message(self) -> None:
        """has_fast_source=True with kind NONE raises the exact message."""
        with pytest.raises(ValueError) as exc_info:
            RoomConfig(
                name="salon",
                area_m2=20.0,
                params=_valid_params(),
                has_fast_source=True,
                fast_source_kind=FastSourceKind.NONE,
                fast_source_power_w=2500.0,
            )
        assert str(exc_info.value) == (
            "fast_source_kind must not be NONE when has_fast_source=True (room 'salon')"
        )

    @pytest.mark.unit
    def test_kind_set_when_disabled_exact_message(self) -> None:
        """has_fast_source=False with a non-NONE kind raises exactly."""
        with pytest.raises(ValueError) as exc_info:
            RoomConfig(
                name="salon",
                area_m2=20.0,
                params=_valid_params(),
                has_fast_source=False,
                fast_source_kind=FastSourceKind.SPLIT,
            )
        assert str(exc_info.value) == (
            "fast_source_kind must be NONE when has_fast_source=False "
            "(room 'salon', got split)"
        )

    @pytest.mark.unit
    def test_fractional_fast_source_power_allowed(self) -> None:
        """Any positive ``fast_source_power_w`` is valid (``> 0``)."""
        room = RoomConfig(
            name="salon",
            area_m2=20.0,
            params=_valid_params(),
            has_fast_source=True,
            fast_source_kind=FastSourceKind.SPLIT,
            fast_source_power_w=0.5,
        )
        assert room.fast_source_power_w == 0.5

    @pytest.mark.unit
    def test_zero_power_when_enabled_exact_message(self) -> None:
        """Zero fast-source power with the source enabled raises exactly."""
        with pytest.raises(ValueError) as exc_info:
            RoomConfig(
                name="salon",
                area_m2=20.0,
                params=_valid_params(),
                has_fast_source=True,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_power_w=0.0,
            )
        assert str(exc_info.value) == (
            "fast_source_power_w must be > 0 when has_fast_source=True "
            "(room 'salon', got 0.0)"
        )


class TestBuildingConfigBoundaryPins:
    """``BuildingConfig`` inclusive boundaries and exact error messages."""

    @pytest.mark.unit
    def test_no_rooms_exact_message(self) -> None:
        """An empty room tuple raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            BuildingConfig(
                rooms=(), hp_max_power_w=4900.0, latitude=50.0, longitude=17.0
            )
        assert str(exc_info.value) == "rooms must contain at least 1 room"

    @pytest.mark.unit
    def test_duplicate_message_lists_only_duplicates(self) -> None:
        """The uniqueness error lists exactly the duplicated room names."""
        with pytest.raises(ValueError) as exc_info:
            BuildingConfig(
                rooms=(_room("kuchnia"), _room("kuchnia"), _room("salon")),
                hp_max_power_w=4900.0,
                latitude=50.0,
                longitude=17.0,
            )
        assert str(exc_info.value) == (
            "room names must be unique, duplicates: ['kuchnia']"
        )

    @pytest.mark.unit
    def test_sub_watt_hp_power_allowed(self) -> None:
        """Any positive ``hp_max_power_w`` is valid (``> 0``)."""
        building = BuildingConfig(
            rooms=(_room("salon"),),
            hp_max_power_w=0.5,
            latitude=50.0,
            longitude=17.0,
        )
        assert building.hp_max_power_w == 0.5

    @pytest.mark.unit
    @pytest.mark.parametrize("longitude", [-180.0, 180.0])
    def test_longitude_boundaries_allowed(self, longitude: float) -> None:
        """Longitude endpoints -180 and 180 are inclusive-valid."""
        building = BuildingConfig(
            rooms=(_room("salon"),),
            hp_max_power_w=4900.0,
            latitude=50.0,
            longitude=longitude,
        )
        assert building.longitude == longitude


class TestWindowConfigBoundaryPins:
    """``WindowConfig`` inclusive boundaries."""

    @pytest.mark.unit
    def test_fractional_area_allowed(self) -> None:
        """Any positive glazed ``area_m2`` is valid (``> 0``)."""
        window = WindowConfig(orientation=Orientation.SOUTH, area_m2=0.5, g_value=0.6)
        assert window.area_m2 == 0.5

    @pytest.mark.unit
    def test_g_value_one_allowed(self) -> None:
        """``g_value = 1`` is the inclusive upper bound of ``(0, 1]``."""
        window = WindowConfig(orientation=Orientation.SOUTH, area_m2=3.0, g_value=1.0)
        assert window.g_value == 1.0


def _scenario(**overrides: Any) -> SimScenario:
    """Rebuild the ``steady_heating`` scenario with field overrides.

    Args:
        overrides: ``SimScenario`` fields to replace before re-validation.

    Returns:
        A re-validated :class:`SimScenario`.
    """
    return replace(steady_heating(), **overrides)


class TestSimScenarioBoundaryPins:
    """``SimScenario.__post_init__`` boundaries and exact error messages."""

    @pytest.mark.unit
    def test_whitespace_name_rejected(self) -> None:
        """A whitespace-only scenario name is rejected like an empty one."""
        with pytest.raises(ValueError, match="non-empty"):
            _scenario(name="   ")

    @pytest.mark.unit
    def test_empty_name_exact_message(self) -> None:
        """An empty scenario name raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            _scenario(name="")
        assert str(exc_info.value) == "name must be a non-empty string"

    @pytest.mark.unit
    def test_zero_duration_exact_message(self) -> None:
        """``duration_minutes = 0`` is non-positive and raises exactly."""
        with pytest.raises(ValueError) as exc_info:
            _scenario(duration_minutes=0)
        assert str(exc_info.value) == "duration_minutes must be > 0, got 0"

    @pytest.mark.unit
    def test_one_minute_duration_allowed(self) -> None:
        """``duration_minutes = 1`` is a valid positive duration."""
        scenario = _scenario(duration_minutes=1)
        assert scenario.duration_minutes == 1

    @pytest.mark.unit
    def test_zero_dt_exact_message(self) -> None:
        """``dt_seconds = 0`` is non-positive and raises exactly."""
        with pytest.raises(ValueError) as exc_info:
            _scenario(dt_seconds=0.0)
        assert str(exc_info.value) == "dt_seconds must be > 0, got 0.0"

    @pytest.mark.unit
    def test_fractional_dt_allowed(self) -> None:
        """Any positive ``dt_seconds`` is valid (``> 0``)."""
        scenario = _scenario(dt_seconds=0.5)
        assert scenario.dt_seconds == 0.5

    @pytest.mark.unit
    def test_negative_sensor_noise_exact_message(self) -> None:
        """A negative ``sensor_noise_std`` raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            _scenario(sensor_noise_std=-0.1)
        assert str(exc_info.value) == "sensor_noise_std must be >= 0, got -0.1"

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [0.0, 0.5, 20.0, 35.0])
    def test_initial_temperature_boundaries_allowed(self, value: float) -> None:
        """``initial_temperature_c`` inside [0, 35] (inclusive) is valid."""
        scenario = _scenario(initial_temperature_c=value)
        assert scenario.initial_temperature_c == value

    @pytest.mark.unit
    def test_initial_temperature_out_of_range_exact_message(self) -> None:
        """``initial_temperature_c`` above 35 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            _scenario(initial_temperature_c=35.5)
        assert str(exc_info.value) == (
            "initial_temperature_c must be in [0, 35] when set, got 35.5"
        )

    @pytest.mark.unit
    def test_unknown_room_offset_exact_message(self) -> None:
        """``room_offsets`` with an unknown room raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            _scenario(room_offsets={"no_such_room": 1.0})
        assert str(exc_info.value) == (
            "room_offsets references unknown room names: ['no_such_room']"
        )

    @pytest.mark.unit
    def test_schedule_non_increasing_exact_message(self) -> None:
        """Equal schedule minutes raise the strictly-increasing message."""
        with pytest.raises(ValueError) as exc_info:
            _scenario(setpoint_schedule=((0.0, 21.0), (0.0, 19.0)))
        assert str(exc_info.value) == (
            "setpoint_schedule minutes must be strictly increasing, got 0.0 after 0.0"
        )

    @pytest.mark.unit
    def test_schedule_setpoint_boundaries_allowed(self) -> None:
        """Schedule setpoints 0 and 35 are inclusive-valid endpoints."""
        scenario = _scenario(setpoint_schedule=((0.0, 0.0), (60.0, 0.5), (120.0, 35.0)))
        assert len(scenario.setpoint_schedule) == 3

    @pytest.mark.unit
    def test_schedule_setpoint_out_of_range_exact_message(self) -> None:
        """A schedule setpoint above 35 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            _scenario(setpoint_schedule=((0.0, 35.5),))
        assert str(exc_info.value) == (
            "setpoint_schedule setpoints must be in [0, 35] degC, got 35.5"
        )


class TestFastSourceCommandContract:
    """``FastSourceCommand`` validation message and ``to_dict`` key contract."""

    @pytest.mark.unit
    def test_on_with_mode_off_exact_message(self) -> None:
        """An ON command with mode OFF raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            FastSourceCommand(on=True, mode=FastSourceMode.OFF)
        assert str(exc_info.value) == "FastSourceCommand cannot be on with mode OFF"

    @pytest.mark.unit
    def test_to_dict_keys_exact(self) -> None:
        """The serialized dict has exactly the documented key set."""
        cmd = FastSourceCommand(
            on=True, mode=FastSourceMode.HEATING, target_temperature_c=21.0
        )
        payload = cmd.to_dict()
        assert payload["on"] is True
        assert set(payload) == {"on", "mode", "target_temperature_c"}


class TestLoopInputValidation:
    """``LoopInput.__post_init__`` valve-feedback range contract."""

    @pytest.mark.unit
    @pytest.mark.parametrize("position", [0.0, 100.0])
    def test_valve_position_boundaries_allowed(self, position: float) -> None:
        """Valve feedback 0 and 100 are inclusive-valid endpoints."""
        loop = LoopInput(
            valve_position_pct=position,
            supply_temperature_c=None,
            return_temperature_c=None,
        )
        assert loop.valve_position_pct == position

    @pytest.mark.unit
    def test_valve_position_above_100_exact_message(self) -> None:
        """Valve feedback above 100 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            LoopInput(
                valve_position_pct=100.5,
                supply_temperature_c=None,
                return_temperature_c=None,
            )
        assert str(exc_info.value) == (
            "valve_position_pct must be in [0, 100] %, got 100.5"
        )


class TestRoomInputsValidation:
    """``RoomInputs.__post_init__`` humidity/staleness range contract."""

    @pytest.mark.unit
    @pytest.mark.parametrize("humidity", [0.0, 100.0])
    def test_humidity_boundaries_allowed(self, humidity: float) -> None:
        """Humidity 0 and 100 are inclusive-valid endpoints."""
        inputs = make_inputs(humidity_pct=humidity)
        assert inputs.humidity_pct == humidity

    @pytest.mark.unit
    def test_humidity_above_100_exact_message(self) -> None:
        """Humidity above 100 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            make_inputs(humidity_pct=100.5)
        assert str(exc_info.value) == ("humidity_pct must be in [0, 100] %, got 100.5")

    @pytest.mark.unit
    def test_negative_age_exact_message(self) -> None:
        """A negative ``last_update_age_minutes`` raises the exact message."""
        with pytest.raises(ValueError) as exc_info:
            RoomInputs(
                mode=Mode.HEATING,
                setpoint_c=21.0,
                room_temperature_c=20.0,
                last_update_age_minutes=-0.5,
            )
        assert str(exc_info.value) == ("last_update_age_minutes must be >= 0, got -0.5")

    @pytest.mark.unit
    @pytest.mark.parametrize("frac", [0.0, 1.0])
    def test_stale_frac_boundaries_allowed(self, frac: float) -> None:
        """``humidity_stale_frac`` 0 and 1 are inclusive-valid endpoints."""
        inputs = make_inputs(humidity_stale_frac=frac)
        assert inputs.humidity_stale_frac == frac

    @pytest.mark.unit
    def test_stale_frac_above_1_exact_message(self) -> None:
        """``humidity_stale_frac`` above 1 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            make_inputs(humidity_stale_frac=1.5)
        assert str(exc_info.value) == "humidity_stale_frac must be in [0, 1], got 1.5"


class TestRoomOutputsValidation:
    """``RoomOutputs.__post_init__`` final-valve range contract."""

    @pytest.mark.unit
    @pytest.mark.parametrize("position", [0.0, 100.0])
    def test_valve_position_boundaries_allowed(self, position: float) -> None:
        """Final valve positions 0 and 100 are inclusive-valid endpoints."""
        outputs = RoomOutputs(
            valve_position_pct=position,
            fast_source=FastSourceCommand(on=False),
            report=_report(),
        )
        assert outputs.valve_position_pct == position

    @pytest.mark.unit
    def test_valve_position_above_100_exact_message(self) -> None:
        """A final valve position above 100 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            RoomOutputs(
                valve_position_pct=100.5,
                fast_source=FastSourceCommand(on=False),
                report=_report(),
            )
        assert str(exc_info.value) == (
            "valve_position_pct must be in [0, 100] %, got 100.5"
        )


class TestRoomReportValidation:
    """``RoomReport.__post_init__`` range and vocabulary contract."""

    @pytest.mark.unit
    @pytest.mark.parametrize("factor", [0.0, 1.0])
    def test_dew_throttle_boundaries_allowed(self, factor: float) -> None:
        """``dew_throttle_factor`` 0 and 1 are inclusive-valid endpoints."""
        report = replace(_report(), dew_throttle_factor=factor)
        assert report.dew_throttle_factor == factor

    @pytest.mark.unit
    def test_dew_throttle_above_1_exact_message(self) -> None:
        """``dew_throttle_factor`` above 1 raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            replace(_report(), dew_throttle_factor=1.5)
        assert str(exc_info.value) == "dew_throttle_factor must be in [0, 1], got 1.5"

    @pytest.mark.unit
    def test_loop_flow_status_bad_entry_exact_message(self) -> None:
        """An unknown ``loop_flow_status`` entry raises the exact message."""
        with pytest.raises(ValueError) as exc_info:
            replace(_report(), loop_flow_status=("stuck",))
        assert str(exc_info.value) == (
            "loop_flow_status entries must be one of "
            "['inactive', 'no_flow', 'ok'], got 'stuck'"
        )

    @pytest.mark.unit
    def test_actuation_test_status_bad_exact_message(self) -> None:
        """An unknown ``actuation_test_status`` raises the exact message."""
        with pytest.raises(ValueError) as exc_info:
            replace(_report(), actuation_test_status="exploded")
        assert str(exc_info.value) == (
            "actuation_test_status must be one of "
            "['aborted', 'failed', 'passed', 'running'] or None, got 'exploded'"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("minutes", [0.0, 0.5])
    def test_actuation_remaining_zero_and_fraction_allowed(
        self, minutes: float
    ) -> None:
        """``actuation_test_remaining_min`` 0 and sub-1 values are valid."""
        report = replace(_report(), actuation_test_remaining_min=minutes)
        assert report.actuation_test_remaining_min == minutes

    @pytest.mark.unit
    def test_actuation_remaining_negative_exact_message(self) -> None:
        """A negative ``actuation_test_remaining_min`` raises exactly."""
        with pytest.raises(ValueError) as exc_info:
            replace(_report(), actuation_test_remaining_min=-0.5)
        assert str(exc_info.value) == (
            "actuation_test_remaining_min must be >= 0, got -0.5"
        )

    @pytest.mark.unit
    def test_actuation_test_loops_bad_entry_exact_message(self) -> None:
        """An unknown ``actuation_test_loops`` entry raises exactly."""
        with pytest.raises(ValueError) as exc_info:
            replace(_report(), actuation_test_loops=("bogus",))
        assert str(exc_info.value) == (
            "actuation_test_loops entries must be one of "
            "['failed', 'passed', 'untested'], got 'bogus'"
        )


class TestRoomReportToDictKeys:
    """``RoomReport.to_dict`` full key/value contract."""

    @pytest.mark.unit
    def test_to_dict_full_key_value_contract(self) -> None:
        """A fully-populated report serializes to exactly the documented dict."""
        report = RoomReport(
            error_c=-0.4,
            trend_c_per_h=0.3,
            room_dew_point_c=12.5,
            p_term=-3.2,
            i_term=1.1,
            trend_term=-1.8,
            feedforward_term=0.5,
            raw_valve_pct=34.0,
            valve_floor_applied=True,
            saturated=False,
            dew_throttle_factor=0.7,
            integrator_frozen=True,
            flags=("sensor_lost",),
            explanation="heating demand",
            room_temperature_c=21.4,
            dew_excluded_reason="no_humidity",
            fast_dwell_remaining_s=120.0,
            loop_flow_status=("ok",),
            actuation_test_status="running",
            actuation_test_remaining_min=3.0,
            actuation_test_loops=("passed",),
        )
        assert report.to_dict() == {
            "error_c": -0.4,
            "trend_c_per_h": 0.3,
            "room_dew_point_c": 12.5,
            "p_term": -3.2,
            "i_term": 1.1,
            "trend_term": -1.8,
            "feedforward_term": 0.5,
            "raw_valve_pct": 34.0,
            "valve_floor_applied": True,
            "saturated": False,
            "dew_throttle_factor": 0.7,
            "integrator_frozen": True,
            "flags": ["sensor_lost"],
            "explanation": "heating demand",
            "room_temperature_c": 21.4,
            "dew_excluded_reason": "no_humidity",
            "fast_dwell_remaining_s": 120.0,
            "loop_flow_status": ["ok"],
            "actuation_test_status": "running",
            "actuation_test_remaining_min": 3.0,
            "actuation_test_loops": ["passed"],
        }
