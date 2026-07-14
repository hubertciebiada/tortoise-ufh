"""Tortoise-UFH pure-core library: flat re-export hub.

This package is the hardware-agnostic control "brain" for a high-thermal-mass
underfloor-heating house. It is pure Python (numpy/scipy + stdlib) and MUST
NEVER import ``homeassistant``; the Home Assistant adapter lives in
``custom_components/tortoise_ufh`` and imports *from* this package.

Importing :mod:`tortoise_ufh` re-exports every public class, function and enum
of the core submodules so callers can write ``from tortoise_ufh import
BuildingController`` without knowing the module layout. Only ``numpy`` and
``scipy`` are required at import time.

Units are SI-with-house-conventions throughout: temperatures in degrees Celsius
(degC), power in watts (W), valve position as a percentage 0..100, thermal
resistance in K/W, heat capacity in J/K, irradiance in W/m^2, relative humidity
as a percentage 0..100, and time in seconds (real-time cycle / RC model) or
minutes (simulation horizons).
"""

from __future__ import annotations

from .building_profiles import (
    BUILDING_PROFILES,
    MODERN_BUNGALOW_ROOMS,
    heavy_construction,
    leaky_old_house,
    modern_bungalow,
    thin_screed,
    well_insulated,
)
from .config import (
    BuildingConfig,
    ControllerConfig,
    Orientation,
    RoomConfig,
    SimScenario,
    WindowConfig,
)
from .const import (
    DEFAULT_DT_COOLING,
    DEFAULT_DT_HEATING,
    DEFAULT_HOME_SETPOINT_C,
    DEW_MARGIN_DEFAULT_K,
    K_PEX,
    T_FLOOR_MAX_C,
    VALID_IRRADIANCE_UNITS,
    VALID_PERCENT_UNITS,
    VALID_POWER_UNITS,
    VALID_TEMP_UNITS,
)
from .controller import BuildingController, RoomController
from .dew_point import (
    condensation_margin,
    cooling_throttle_factor,
    dew_point,
    dew_point_simplified,
)
from .fast_source import FastSourceMachine, window_allows
from .hp_link import (
    HEATING_SUPPLY_MAX_C,
    HEATING_SUPPLY_MIN_C,
    HEISHAMON_MODE_OPTIONS,
    cooling_setpoint_c,
    dhw_option,
    direction_option,
    heating_curve,
    round_to_step_c,
)
from .metrics import (
    SimMetrics,
    assert_comfort,
    assert_floor_temp_safe,
    assert_no_condensation,
    assert_no_freezing,
    assert_no_prolonged_cold,
)
from .models import (
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
from .pid import PIDController
from .rc_model import ModelOrder, RCModel, RCParams
from .safety import (
    SafetyAction,
    SafetyEvaluator,
    SafetyRule,
    SafetyRuleResult,
    SensorSnapshot,
)
from .scenarios import (
    SCENARIO_LIBRARY,
    cold_snap,
    hot_july_floor_cooling,
    night_setback,
    sensor_dropout,
    solar_overshoot,
    split_boost,
    spring_transition,
    steady_heating,
)
from .sensor_noise import SensorNoise
from .simulation_log import SimRecord, SimulationLog

# ``SimulatedRoom`` is defined in :mod:`tortoise_ufh.simulator` (the canonical
# BuildingSimulator bridge listed in the BUILD_SPEC) and re-exported here.
from .simulator import BuildingSimulator, HeatPumpMode, SimulatedRoom
from .trend import TrendEstimator
from .ufh_loop import LoopGeometry, loop_power, loop_power_with_valve
from .weather import (
    ChannelProfile,
    ProfileKind,
    SyntheticWeather,
    WeatherPoint,
    WeatherSource,
)
from .weather_comp import CoolingCompCurve, WeatherCompCurve

__version__ = "0.12.0"

__all__ = [
    "BUILDING_PROFILES",
    "DEFAULT_DT_COOLING",
    "DEFAULT_DT_HEATING",
    "DEFAULT_HOME_SETPOINT_C",
    "DEW_MARGIN_DEFAULT_K",
    "HEATING_SUPPLY_MAX_C",
    "HEATING_SUPPLY_MIN_C",
    "HEISHAMON_MODE_OPTIONS",
    "K_PEX",
    "MODERN_BUNGALOW_ROOMS",
    "SCENARIO_LIBRARY",
    "T_FLOOR_MAX_C",
    "VALID_IRRADIANCE_UNITS",
    "VALID_PERCENT_UNITS",
    "VALID_POWER_UNITS",
    "VALID_TEMP_UNITS",
    "BuildingConfig",
    "BuildingController",
    "BuildingOutputs",
    "BuildingSimulator",
    "ChannelProfile",
    "ControllerConfig",
    "CoolingCompCurve",
    "FastSourceCommand",
    "FastSourceKind",
    "FastSourceMachine",
    "FastSourceMode",
    "HeatPumpMode",
    "LoopGeometry",
    "LoopInput",
    "Mode",
    "ModelOrder",
    "Orientation",
    "PIDController",
    "ProfileKind",
    "RCModel",
    "RCParams",
    "RoomConfig",
    "RoomController",
    "RoomInputs",
    "RoomOutputs",
    "RoomReport",
    "SafetyAction",
    "SafetyEvaluator",
    "SafetyRule",
    "SafetyRuleResult",
    "SensorNoise",
    "SensorSnapshot",
    "SimMetrics",
    "SimRecord",
    "SimScenario",
    "SimulatedRoom",
    "SimulationLog",
    "SyntheticWeather",
    "TrendEstimator",
    "WeatherCompCurve",
    "WeatherPoint",
    "WeatherSource",
    "WindowConfig",
    "__version__",
    "assert_comfort",
    "assert_floor_temp_safe",
    "assert_no_condensation",
    "assert_no_freezing",
    "assert_no_prolonged_cold",
    "cold_snap",
    "condensation_margin",
    "cooling_setpoint_c",
    "cooling_throttle_factor",
    "dew_point",
    "dew_point_simplified",
    "dhw_option",
    "direction_option",
    "heating_curve",
    "heavy_construction",
    "hot_july_floor_cooling",
    "leaky_old_house",
    "loop_power",
    "loop_power_with_valve",
    "modern_bungalow",
    "night_setback",
    "round_to_step_c",
    "sensor_dropout",
    "solar_overshoot",
    "split_boost",
    "spring_transition",
    "steady_heating",
    "thin_screed",
    "well_insulated",
    "window_allows",
]
