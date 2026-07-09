"""Core black-box I/O contract for the tortoise-ufh controller.

This module defines the frozen, pure-Python dataclasses and enums that form
the external contract of the per-room UFH controller. It has **no** runtime
dependencies beyond the standard library and MUST NOT import ``homeassistant``.

Units (repo-wide, non-negotiable):
    * Temperatures: degrees Celsius (``_c``).
    * Valve / actuator position: percent 0..100 (``_pct``), float.
    * Humidity: relative percent 0..100 (``_pct``).
    * Trend: kelvin per hour (``_c_per_h``).
    * Dew-point margins / throttle factors: dimensionless 0..1 where noted.

Every value type is ``@dataclass(frozen=True)`` and validates its invariants in
``__post_init__`` (raising :class:`ValueError`). The result types
(:class:`RoomReport`, :class:`FastSourceCommand`, :class:`RoomOutputs`,
:class:`BuildingOutputs`) expose ``to_dict()`` producing plain JSON-serializable
structures (``dict``/``list``/``str``/``float``/``bool``/``None``) with enums
rendered as their ``.value`` and ``None`` preserved. The HA websocket layer and
the frontend panel consume those dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Mode(Enum):
    """Operating mode of a room's controller."""

    HEATING = "heating"
    TRANSITIONAL = "transitional"  # valves parked; only fast source, bidirectional
    COOLING = "cooling"
    OFF = "off"


class FastSourceKind(Enum):
    """Kind of fast auxiliary source available to a room."""

    NONE = "none"
    SPLIT = "split"
    HEATER = "heater"  # supported for the future; behaves like heating-only split


class FastSourceMode(Enum):
    """Commanded direction of the fast source."""

    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"


@dataclass(frozen=True)
class LoopInput:
    """One UFH loop's raw water probes + valve feedback.

    Attributes:
        valve_position_pct: Current actuator position feedback in percent
            (0..100), or ``None`` if the reading is unavailable.
        supply_temperature_c: Loop supply water temperature in degrees Celsius,
            or ``None`` if unavailable.
        return_temperature_c: Loop return water temperature in degrees Celsius,
            or ``None`` if unavailable.

    Raises:
        ValueError: If ``valve_position_pct`` is outside the 0..100 range.
    """

    valve_position_pct: float | None  # current actuator position (feedback), 0..100
    supply_temperature_c: float | None
    return_temperature_c: float | None

    def __post_init__(self) -> None:
        """Validate the valve feedback range (percent)."""
        if self.valve_position_pct is not None and not (
            0.0 <= self.valve_position_pct <= 100.0
        ):
            msg = (
                "valve_position_pct must be in [0, 100] %, got "
                f"{self.valve_position_pct}"
            )
            raise ValueError(msg)


@dataclass(frozen=True)
class RoomInputs:
    """Raw inputs for ONE room's black-box controller.

    Values may be ``None`` to represent a missing sensor; the controller
    degrades safely in that case.

    Attributes:
        mode: The room's operating :class:`Mode`.
        setpoint_c: Target temperature in degrees Celsius, already equal to
            ``home_setpoint + room_offset``.
        room_temperature_c: Measured room air temperature in degrees Celsius,
            or ``None`` if the sensor is lost.
        humidity_pct: Relative humidity in percent (0..100); required for cooled
            rooms to compute the dew point. ``None`` if unavailable.
        outdoor_temperature_c: Outdoor temperature in degrees Celsius, or
            ``None`` if unavailable (used for optional feedforward).
        loops: The room's UFH loops (water probes + valve feedback).
        fast_source_kind: Kind of fast auxiliary source available.
        fast_source_on: Current on/off feedback of the fast source, or ``None``.
        hp_active_for_ufh: ``False`` while the heat pump is unavailable for UFH
            (DHW / defrost) so the integrator is frozen; ``None`` if unknown.
        cooling_enabled: Whether this room participates in cooling.

    Raises:
        ValueError: If ``humidity_pct`` is outside the 0..100 range.
    """

    mode: Mode
    setpoint_c: float  # already = home_setpoint + room_offset
    room_temperature_c: float | None
    humidity_pct: float | None = None  # required for cooled rooms (dew point)
    outdoor_temperature_c: float | None = None
    loops: tuple[LoopInput, ...] = ()
    fast_source_kind: FastSourceKind = FastSourceKind.NONE
    fast_source_on: bool | None = None  # current state feedback
    hp_active_for_ufh: bool | None = None  # False during DHW/defrost -> freeze
    cooling_enabled: bool = True  # per-room "udzial w chlodzeniu"

    def __post_init__(self) -> None:
        """Validate the humidity range (percent)."""
        if self.humidity_pct is not None and not (0.0 <= self.humidity_pct <= 100.0):
            msg = f"humidity_pct must be in [0, 100] %, got {self.humidity_pct}"
            raise ValueError(msg)


@dataclass(frozen=True)
class FastSourceCommand:
    """Command emitted for a room's fast source (split / heater).

    Attributes:
        on: Whether the fast source should be running.
        mode: Commanded direction (:class:`FastSourceMode`).
        target_temperature_c: Target temperature in degrees Celsius the split
            should self-regulate to, or ``None`` when off.

    Raises:
        ValueError: If ``on`` is ``True`` while ``mode`` is
            :attr:`FastSourceMode.OFF` (a contradictory command).
    """

    on: bool
    mode: FastSourceMode = FastSourceMode.OFF
    target_temperature_c: float | None = None

    def __post_init__(self) -> None:
        """Validate mode/on consistency."""
        if self.on and self.mode is FastSourceMode.OFF:
            msg = "FastSourceCommand cannot be on with mode OFF"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (enum -> value, ``None`` preserved).

        Returns:
            Plain ``dict`` with ``on`` (bool), ``mode`` (str) and
            ``target_temperature_c`` (float or ``None``).
        """
        return {
            "on": self.on,
            "mode": self.mode.value,
            "target_temperature_c": self.target_temperature_c,
        }


@dataclass(frozen=True)
class RoomReport:
    """Under-the-hood decision report for one room (human + AI readable).

    All temperatures are in degrees Celsius, valves in percent (0..100), the
    trend in kelvin per hour, and ``dew_throttle_factor`` is dimensionless in
    [0, 1].

    Attributes:
        error_c: ``setpoint - room_temp`` (heating sign convention), or ``None``.
        trend_c_per_h: Measured ``dT_room/dt`` in K/h, or ``None``.
        room_dew_point_c: Room dew-point temperature, or ``None``.
        p_term: Proportional contribution to the valve (percent).
        i_term: Integral contribution to the valve (percent).
        trend_term: Trend-damping contribution to the valve (percent).
        feedforward_term: Feedforward contribution to the valve (percent).
        raw_valve_pct: Valve percent before clamps/floors/safety.
        valve_floor_applied: Whether the heating valve floor was applied.
        saturated: Whether the final valve hit a 0/100 bound.
        dew_throttle_factor: Cooling throttle in [0, 1] (1.0 = open,
            0.0 = fully throttled).
        integrator_frozen: Whether the PID integrator was frozen this step.
        flags: Diagnostic flag strings (e.g. ``"sensor_lost"``).
        explanation: Short "what & why" text.
        room_temperature_c: Measured room air temperature [degC] echoed from the
            room inputs, or ``None`` when the sensor is lost. Surfaced so the
            panel shows the true measurement instead of reconstructing it from
            ``setpoint - error_c`` (which can transiently disagree with the
            broadcast setpoint). Additive, non-breaking field (defaults to
            ``None`` for compatibility with older report constructors).
        dew_excluded_reason: Why this room is excluded from the global safe
            dew-point maximum, or ``None`` when the room IS eligible (COOLING,
            ``cooling_enabled`` and usable temperature + humidity). One of
            ``"not_cooling_mode"``, ``"cooling_disabled"``, ``"no_temperature"``,
            ``"no_humidity"``. Additive, non-breaking field (defaults to
            ``None``); the panel uses it to explain a missing safe dew point.
        fast_dwell_remaining_s: Seconds remaining until the fast source may
            change on/off state under its min ON/OFF dwell lock, or ``None`` when
            the lock has already elapsed or there is no fast source. Additive,
            non-breaking field (defaults to ``None``); the panel renders it as
            "unlocks in ~N min".

    Raises:
        ValueError: If ``dew_throttle_factor`` is outside [0, 1].
    """

    error_c: float | None  # setpoint - room_temp (heating sign convention)
    trend_c_per_h: float | None  # measured dT_room/dt
    room_dew_point_c: float | None
    p_term: float
    i_term: float
    trend_term: float
    feedforward_term: float
    raw_valve_pct: float  # before clamps/floors/safety
    valve_floor_applied: bool
    saturated: bool
    dew_throttle_factor: float  # 1.0 = open, 0.0 = fully throttled (cooling)
    integrator_frozen: bool
    flags: tuple[str, ...] = ()  # e.g. "sensor_lost","s2_condensation"
    explanation: str = ""  # short "what & why" text
    room_temperature_c: float | None = None  # echoed measurement, None if lost
    dew_excluded_reason: str | None = None  # why excluded from safe dew point
    fast_dwell_remaining_s: float | None = None  # min ON/OFF lock remaining [s]

    def __post_init__(self) -> None:
        """Validate the dew-point throttle factor range."""
        if not (0.0 <= self.dew_throttle_factor <= 1.0):
            msg = (
                f"dew_throttle_factor must be in [0, 1], got {self.dew_throttle_factor}"
            )
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (``None`` preserved, flags -> list).

        Returns:
            Plain ``dict`` of primitives; ``flags`` becomes a ``list[str]``.
        """
        return {
            "error_c": self.error_c,
            "trend_c_per_h": self.trend_c_per_h,
            "room_dew_point_c": self.room_dew_point_c,
            "p_term": self.p_term,
            "i_term": self.i_term,
            "trend_term": self.trend_term,
            "feedforward_term": self.feedforward_term,
            "raw_valve_pct": self.raw_valve_pct,
            "valve_floor_applied": self.valve_floor_applied,
            "saturated": self.saturated,
            "dew_throttle_factor": self.dew_throttle_factor,
            "integrator_frozen": self.integrator_frozen,
            "flags": list(self.flags),
            "explanation": self.explanation,
            "room_temperature_c": self.room_temperature_c,
            "dew_excluded_reason": self.dew_excluded_reason,
            "fast_dwell_remaining_s": self.fast_dwell_remaining_s,
        }


@dataclass(frozen=True)
class RoomOutputs:
    """The per-room result: the two commands + the report.

    Attributes:
        valve_position_pct: Final valve position in percent (0..100).
        fast_source: The fast-source command.
        report: The under-the-hood decision report.

    Raises:
        ValueError: If ``valve_position_pct`` is outside the 0..100 range.
    """

    valve_position_pct: float  # 0..100, final
    fast_source: FastSourceCommand
    report: RoomReport

    def __post_init__(self) -> None:
        """Validate the final valve range (percent)."""
        if not (0.0 <= self.valve_position_pct <= 100.0):
            msg = (
                "valve_position_pct must be in [0, 100] %, got "
                f"{self.valve_position_pct}"
            )
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of the room result.

        Returns:
            Plain ``dict`` with ``valve_position_pct`` (float),
            ``fast_source`` (dict) and ``report`` (dict).
        """
        return {
            "valve_position_pct": self.valve_position_pct,
            "fast_source": self.fast_source.to_dict(),
            "report": self.report.to_dict(),
        }


@dataclass(frozen=True)
class BuildingOutputs:
    """The whole-building result: per-room outputs + the global dew point.

    Attributes:
        rooms: Per-room outputs keyed by room name.
        global_safe_dew_point_c: ``max_over_cooled(T_dew) + 2 K`` in degrees
            Celsius, or ``None`` if there is no eligible cooled/humid room.
    """

    rooms: dict[str, RoomOutputs]
    global_safe_dew_point_c: float | None  # max_over_cooled(T_dew)+2K, or None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of the building result.

        Returns:
            Plain ``dict`` with ``rooms`` (dict of room dicts) and
            ``global_safe_dew_point_c`` (float or ``None``).
        """
        return {
            "rooms": {name: out.to_dict() for name, out in self.rooms.items()},
            "global_safe_dew_point_c": self.global_safe_dew_point_c,
        }
