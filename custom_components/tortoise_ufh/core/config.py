"""Immutable configuration dataclasses for the tortoise-ufh core.

Defines the frozen, ``__post_init__``-validated configuration hierarchy that
parameterises the per-room UFH controller and the offline building simulator.
This module is part of the pure core: it MUST NOT import ``homeassistant``. Its
only runtime dependencies are the standard library and the sibling core modules.

Hierarchy:
    ControllerConfig  — PID + trend + fast-source + dew-point tuning knobs.
    WindowConfig      — one window (orientation, area, solar transmittance).
    RoomConfig        — a single room: RC params, loops, fast source, windows.
    BuildingConfig    — the whole building: rooms, heat pump, location.
    SimScenario       — a full simulation scenario (building + weather + run).

Units (repo-wide, non-negotiable):
    * Temperatures / setpoints / offsets: degrees Celsius (``_c``).
    * Temperature margins / deadbands / ramps: kelvin (``_k`` / ``_c``).
    * Power: watts (``_w``); valve position: percent 0..100 (``_pct``).
    * Trend gain ``kt``: percent per (kelvin per hour).
    * Time: minutes (``_minutes``) for durations, seconds (``_seconds``) for the
      real-time / simulation control step.
    * Area: square metres (``_m2``); geographic latitude/longitude: degrees.

Every value type is ``@dataclass(frozen=True)`` and validates its invariants in
``__post_init__`` (raising :class:`ValueError` with a message assigned to a local
``msg`` first, per the repo ruff convention).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from .models import FastSourceKind, Mode

if TYPE_CHECKING:
    from .rc_model import RCParams
    from .ufh_loop import LoopGeometry
    from .weather import WeatherSource
    from .weather_comp import CoolingCompCurve, WeatherCompCurve


# ---------------------------------------------------------------------------
# ControllerConfig — per-room controller tuning knobs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControllerConfig:
    """Tuning parameters for one room's PID(+trend) UFH controller.

    All defaults are sensible starting values so a room controls out of the box
    with no manual tuning (advanced knobs are hidden in the HA options flow).

    Attributes:
        kp: Proportional gain, percent per kelvin of error [%/K] (>= 0).
        ki: Integral gain, percent per (kelvin*second) [%/(K*s)] (>= 0).
        kd: Derivative gain, percent per (kelvin/second) [%/(K/s)] (>= 0).
        kt: Trend-damping (anti-overshoot) gain, percent per (kelvin/hour)
            [%/(K/h)] (>= 0).
        deadband_c: Half-width of the no-action band around the setpoint [K]
            (>= 0).
        valve_floor_pct: Minimum valve opening when calling for heat [%]
            (in [0, 100]).
        outdoor_ff_enabled: Whether to add an outdoor-temperature feedforward
            baseline term to the valve command.
        ff_neutral_c: Outdoor temperature at which the feedforward term is
            zero [degC] (in [-30, 40]; control-F6, 2026-07-09 — previously a
            module constant).
        ff_gain_pct_per_k: Feedforward gain [%/K] of outdoor deviation from
            ``ff_neutral_c`` (>= 0).
        ff_max_pct: Upper clamp on the feedforward baseline term [%]
            (in [0, 100]).
        boost_offset_c: Absolute error beyond which the fast source engages [K]
            (>= 0 and > ``deadband_c``, or the engage/release hysteresis would
            invert — D2, 2026-07-09).
        fast_min_on_minutes: Minimum fast-source ON dwell time [min] (>= 0).
        fast_min_off_minutes: Minimum fast-source OFF dwell time [min] (>= 0).
        dew_margin_k: Local per-room condensation-protection margin [K] (>= 0).
        dew_ramp_k: Width over which the local dew-point throttle ramps from
            full-open to fully-throttled [K] (> 0).
        cycle_seconds: Nominal control-cycle period [s] (> 0, default 300 = 5
            minutes).
        valve_write_threshold_pct: Minimum valve change before a new value is
            written to the actuator [%] (>= 0).
    """

    # Defaults retuned 2026-07-09 (C1; DECISIONS §8) on the CALIBRATED digital
    # twin: the old ki=0.02 (Ti ~ 7 min) was an order of magnitude too
    # aggressive for a tau = 3-6 h slab and produced a measured +1.2 K
    # overshoot with a persistent +-0.6 K limit cycle. kp=14 / ki=0.0015
    # (Ti ~ 2.6 h) / kt=12 on the FILTERED trend: steady_heating overshoot
    # +0.18 K, 24-48 h tail 100 % inside +-0.3 K, ~1 pp/h valve travel.
    kp: float = 14.0
    ki: float = 0.0015
    kd: float = 0.0
    kt: float = 12.0
    deadband_c: float = 0.3
    valve_floor_pct: float = 15.0
    outdoor_ff_enabled: bool = False
    ff_neutral_c: float = 15.0
    ff_gain_pct_per_k: float = 1.0
    ff_max_pct: float = 20.0
    boost_offset_c: float = 1.0
    fast_min_on_minutes: float = 10.0
    fast_min_off_minutes: float = 10.0
    dew_margin_k: float = 2.0
    dew_ramp_k: float = 2.0
    cycle_seconds: float = 300.0
    valve_write_threshold_pct: float = 2.0

    def __post_init__(self) -> None:
        """Validate the controller tuning parameters.

        Raises:
            ValueError: If any gain is negative, ``valve_floor_pct`` is outside
                ``[0, 100]``, a margin/threshold is negative, ``dew_ramp_k`` or
                ``cycle_seconds`` is non-positive, or ``boost_offset_c`` does
                not exceed ``deadband_c``.
        """
        for gain_name, gain in (
            ("kp", self.kp),
            ("ki", self.ki),
            ("kd", self.kd),
            ("kt", self.kt),
        ):
            if gain < 0:
                msg = f"{gain_name} must be >= 0, got {gain}"
                raise ValueError(msg)
        if self.deadband_c < 0:
            msg = f"deadband_c must be >= 0, got {self.deadband_c}"
            raise ValueError(msg)
        if self.valve_floor_pct < 0 or self.valve_floor_pct > 100:
            msg = f"valve_floor_pct must be in [0, 100], got {self.valve_floor_pct}"
            raise ValueError(msg)
        if self.ff_neutral_c < -30.0 or self.ff_neutral_c > 40.0:
            msg = f"ff_neutral_c must be in [-30, 40], got {self.ff_neutral_c}"
            raise ValueError(msg)
        if self.ff_gain_pct_per_k < 0:
            msg = f"ff_gain_pct_per_k must be >= 0, got {self.ff_gain_pct_per_k}"
            raise ValueError(msg)
        if self.ff_max_pct < 0 or self.ff_max_pct > 100:
            msg = f"ff_max_pct must be in [0, 100], got {self.ff_max_pct}"
            raise ValueError(msg)
        if self.boost_offset_c < 0:
            msg = f"boost_offset_c must be >= 0, got {self.boost_offset_c}"
            raise ValueError(msg)
        if self.boost_offset_c <= self.deadband_c:
            msg = (
                "boost_offset_c must be > deadband_c (the engage threshold must "
                "lie outside the comfort band, or the fast-source hysteresis "
                f"inverts), got boost_offset_c={self.boost_offset_c} <= "
                f"deadband_c={self.deadband_c}"
            )
            raise ValueError(msg)
        if self.fast_min_on_minutes < 0:
            msg = f"fast_min_on_minutes must be >= 0, got {self.fast_min_on_minutes}"
            raise ValueError(msg)
        if self.fast_min_off_minutes < 0:
            msg = f"fast_min_off_minutes must be >= 0, got {self.fast_min_off_minutes}"
            raise ValueError(msg)
        if self.dew_margin_k < 0:
            msg = f"dew_margin_k must be >= 0, got {self.dew_margin_k}"
            raise ValueError(msg)
        if self.dew_ramp_k <= 0:
            msg = f"dew_ramp_k must be > 0, got {self.dew_ramp_k}"
            raise ValueError(msg)
        if self.cycle_seconds <= 0:
            msg = f"cycle_seconds must be > 0, got {self.cycle_seconds}"
            raise ValueError(msg)
        if self.valve_write_threshold_pct < 0:
            msg = (
                f"valve_write_threshold_pct must be >= 0, "
                f"got {self.valve_write_threshold_pct}"
            )
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# WindowConfig (+ Orientation) — solar-gain geometry for one window
# ---------------------------------------------------------------------------


class Orientation(Enum):
    """Cardinal orientation of a window's outward-facing normal.

    The ``azimuth_deg`` property returns the compass azimuth of the surface
    normal using the solar convention North=0, East=90, South=180, West=270.
    """

    NORTH = "north"
    EAST = "east"
    SOUTH = "south"
    WEST = "west"

    @property
    def azimuth_deg(self) -> float:
        """Compass azimuth of the surface normal [degrees].

        Returns:
            0.0 (North), 90.0 (East), 180.0 (South) or 270.0 (West).
        """
        return {
            Orientation.NORTH: 0.0,
            Orientation.EAST: 90.0,
            Orientation.SOUTH: 180.0,
            Orientation.WEST: 270.0,
        }[self]


@dataclass(frozen=True)
class WindowConfig:
    """A single window contributing to a room's solar gain.

    Attributes:
        orientation: Cardinal direction the window faces (:class:`Orientation`).
        area_m2: Glazed area [m^2] (must be > 0).
        g_value: Solar heat-gain (transmittance) coefficient, dimensionless
            (must be in ``(0, 1]``).
    """

    orientation: Orientation
    area_m2: float
    g_value: float

    def __post_init__(self) -> None:
        """Validate window geometry and transmittance.

        Raises:
            ValueError: If ``area_m2`` is non-positive or ``g_value`` is not in
                the half-open interval ``(0, 1]``.
        """
        if self.area_m2 <= 0:
            msg = f"area_m2 must be > 0, got {self.area_m2}"
            raise ValueError(msg)
        if self.g_value <= 0 or self.g_value > 1:
            msg = f"g_value must be in (0, 1], got {self.g_value}"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# RoomConfig — one room's configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoomConfig:
    """Configuration for a single room / control zone.

    A room may own several UFH loops that all receive one common valve
    position, and optionally a fast auxiliary source (split/heater).

    Attributes:
        name: Human-readable room identifier (must be non-empty).
        area_m2: Floor area [m^2] (must be > 0).
        params: RC thermal parameters — used by the simulator only.
        n_loops: Number of UFH loops in the zone (must be >= 1).
        has_fast_source: Whether the room has a fast auxiliary source.
        fast_source_kind: Kind of fast source (:class:`FastSourceKind`). Must be
            ``NONE`` when ``has_fast_source`` is ``False`` and a non-``NONE``
            kind when ``has_fast_source`` is ``True``.
        fast_source_power_w: Nominal fast-source power [W] (must be >= 0, and
            > 0 when ``has_fast_source`` is ``True``).
        cooling_enabled: Whether the room participates in floor cooling
            ("udzial w chlodzeniu").
        controller: Per-room controller tuning (:class:`ControllerConfig`).
        windows: Window configurations for solar-gain modelling.
        loop_geometry: Optional explicit loop geometry for the simulator's
            EN 1264 power calculation. ``None`` lets the simulator estimate it
            from ``area_m2``.
    """

    name: str
    area_m2: float
    params: RCParams
    n_loops: int = 1
    has_fast_source: bool = False
    fast_source_kind: FastSourceKind = FastSourceKind.NONE
    fast_source_power_w: float = 0.0
    cooling_enabled: bool = True
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    windows: tuple[WindowConfig, ...] = ()
    loop_geometry: LoopGeometry | None = None

    def __post_init__(self) -> None:
        """Validate the room configuration.

        Raises:
            ValueError: If ``name`` is empty, ``area_m2`` is non-positive,
                ``n_loops`` is below 1, ``fast_source_power_w`` is negative, or
                ``fast_source_kind`` is inconsistent with ``has_fast_source``.
        """
        if not self.name or not self.name.strip():
            msg = "name must be a non-empty string"
            raise ValueError(msg)
        if self.area_m2 <= 0:
            msg = f"area_m2 must be > 0, got {self.area_m2}"
            raise ValueError(msg)
        if self.n_loops < 1:
            msg = f"n_loops must be >= 1, got {self.n_loops}"
            raise ValueError(msg)
        if self.fast_source_power_w < 0:
            msg = f"fast_source_power_w must be >= 0, got {self.fast_source_power_w}"
            raise ValueError(msg)
        # -- Fast-source kind vs. has_fast_source consistency ----------------
        if self.has_fast_source and self.fast_source_kind is FastSourceKind.NONE:
            msg = (
                "fast_source_kind must not be NONE when has_fast_source=True "
                f"(room '{self.name}')"
            )
            raise ValueError(msg)
        if (
            not self.has_fast_source
            and self.fast_source_kind is not FastSourceKind.NONE
        ):
            msg = (
                "fast_source_kind must be NONE when has_fast_source=False "
                f"(room '{self.name}', got {self.fast_source_kind.value})"
            )
            raise ValueError(msg)
        if self.has_fast_source and self.fast_source_power_w <= 0:
            msg = (
                "fast_source_power_w must be > 0 when has_fast_source=True "
                f"(room '{self.name}', got {self.fast_source_power_w})"
            )
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# BuildingConfig — whole-building configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildingConfig:
    """Configuration for a building containing one or more rooms.

    Attributes:
        rooms: Room configurations (must contain >= 1 room, unique names).
        hp_max_power_w: Heat-pump maximum thermal power [W] (must be > 0).
        latitude: Geographic latitude [degrees], in ``[-90, 90]``.
        longitude: Geographic longitude [degrees], in ``[-180, 180]``.
        home_setpoint_c: Global home target temperature [degC] (default 21.0).
            Per-room setpoints are this value plus a per-room offset.
    """

    rooms: tuple[RoomConfig, ...]
    hp_max_power_w: float
    latitude: float
    longitude: float
    home_setpoint_c: float = 21.0

    def __post_init__(self) -> None:
        """Validate the building configuration.

        Raises:
            ValueError: If there are no rooms, room names are not unique,
                ``hp_max_power_w`` is non-positive, or latitude/longitude are
                out of range.
        """
        if len(self.rooms) == 0:
            msg = "rooms must contain at least 1 room"
            raise ValueError(msg)
        names = [r.name for r in self.rooms]
        if len(names) != len(set(names)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            msg = f"room names must be unique, duplicates: {duplicates}"
            raise ValueError(msg)
        if self.hp_max_power_w <= 0:
            msg = f"hp_max_power_w must be > 0, got {self.hp_max_power_w}"
            raise ValueError(msg)
        if self.latitude < -90 or self.latitude > 90:
            msg = f"latitude must be in [-90, 90], got {self.latitude}"
            raise ValueError(msg)
        if self.longitude < -180 or self.longitude > 180:
            msg = f"longitude must be in [-180, 180], got {self.longitude}"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# SimScenario — full offline simulation scenario
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimScenario:
    """A full simulation scenario composing building, weather, and run knobs.

    Attributes:
        name: Human-readable scenario identifier (must be non-empty).
        building: Building configuration with rooms and heat pump.
        weather: Weather data source (any :class:`WeatherSource` implementation).
        duration_minutes: Total simulation duration [min] (must be > 0).
        mode: Global operating mode for the run (:class:`Mode`, default
            ``HEATING``).
        dt_seconds: Simulation time step [s] (must be > 0, default 60.0).
        sensor_noise_std: Sensor-noise standard deviation [K] (must be >= 0).
        description: Human-readable description for reporting (default "").
        room_offsets: Per-room setpoint offsets from ``home_setpoint_c`` [K],
            keyed by room name. Keys must match rooms in ``building``; rooms not
            listed use a zero offset. Empty by default.
        weather_comp: Optional heating weather-compensation curve for the
            twin's supply temperature (2026-07-09; ``None`` = the simulator's
            constant fallback).
        cooling_comp: Optional cooling weather-compensation curve (``None`` =
            fallback constant).
        initial_temperature_c: Optional initial temperature for every room's
            thermal nodes [degC] (2026-07-09). ``None`` keeps the RC model's
            20 degC reset default — summer scenarios pass a summer-like value
            so the run does not start with a cold-house artifact.
    """

    name: str
    building: BuildingConfig
    weather: WeatherSource
    duration_minutes: int
    mode: Mode = Mode.HEATING
    dt_seconds: float = 60.0
    sensor_noise_std: float = 0.0
    description: str = ""
    room_offsets: dict[str, float] = field(default_factory=dict)
    weather_comp: WeatherCompCurve | None = None
    cooling_comp: CoolingCompCurve | None = None
    initial_temperature_c: float | None = None

    def __post_init__(self) -> None:
        """Validate the scenario parameters.

        Raises:
            ValueError: If ``name`` is empty, ``duration_minutes`` or
                ``dt_seconds`` is non-positive, ``sensor_noise_std`` is negative,
                or ``room_offsets`` references an unknown room name.
        """
        if not self.name or not self.name.strip():
            msg = "name must be a non-empty string"
            raise ValueError(msg)
        if self.duration_minutes <= 0:
            msg = f"duration_minutes must be > 0, got {self.duration_minutes}"
            raise ValueError(msg)
        if self.dt_seconds <= 0:
            msg = f"dt_seconds must be > 0, got {self.dt_seconds}"
            raise ValueError(msg)
        if self.sensor_noise_std < 0:
            msg = f"sensor_noise_std must be >= 0, got {self.sensor_noise_std}"
            raise ValueError(msg)
        if self.initial_temperature_c is not None and not (
            0.0 <= self.initial_temperature_c <= 35.0
        ):
            msg = (
                "initial_temperature_c must be in [0, 35] when set, got "
                f"{self.initial_temperature_c}"
            )
            raise ValueError(msg)
        if self.room_offsets:
            known = {r.name for r in self.building.rooms}
            unknown = sorted(set(self.room_offsets) - known)
            if unknown:
                msg = f"room_offsets references unknown room names: {unknown}"
                raise ValueError(msg)
