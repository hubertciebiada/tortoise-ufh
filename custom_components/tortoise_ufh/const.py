"""Constants for the Tortoise-UFH Home Assistant integration.

This is the HA *adapter* constants module (the ``custom_components`` side). It is
the single authoritative ``CONF_*`` key vocabulary shared by the config flow,
coordinator, and entity platforms — mirror of the pump-ahead pattern. Unlike the
pure core (``tortoise_ufh/const.py``, which never imports Home Assistant), this
module may import ``homeassistant`` for the :class:`~homeassistant.const.Platform`
enum only.

It holds only module-level scalars, string keys, option lists, and unit-hint
sets — no control logic and no value/config/result dataclasses (those live in the
frozen dataclasses of the core ``models.py`` / ``config.py``).

Units contract (repo-wide):
    * Temperatures in degrees Celsius (``_c``); temperature offsets in K.
    * Valve position and relative humidity in percent (0..100).
    * Time in minutes for coordinator/watchdog bookkeeping; seconds for staleness.

The room-scoped ``fast_source_kind`` and global mode option strings below mirror
the ``.value`` of the core enums ``FastSourceKind`` and ``Mode`` verbatim (kept as
plain literals here to avoid coupling the HA config vocabulary to a core import).
"""

from __future__ import annotations

from homeassistant.const import Platform

# ---------------------------------------------------------------------------
# Integration identity
# ---------------------------------------------------------------------------

DOMAIN: str = "tortoise_ufh"

PLATFORMS: list[Platform] = [
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]

# ---------------------------------------------------------------------------
# Configuration keys — global (config-flow step 1 / options flow)
# ---------------------------------------------------------------------------

CONF_HOME_SETPOINT: str = "home_setpoint"
"""Global whole-home comfort setpoint, degrees Celsius. Room target = this + offset."""

CONF_ENTITY_MODE: str = "entity_mode"
"""Global mode input entity (select/input_select): heating/transitional/cooling/off."""

CONF_KILL_SWITCH: str = "kill_switch"
"""Master kill-switch flag. When engaged the module emits NO commands (compute-only)."""

CONF_LIVE_CONTROL: str = "live_control"
"""Per-room shadow->live toggle map (options), keyed by room name. False = shadow."""

# ---------------------------------------------------------------------------
# Configuration keys — rooms (config-flow step 2)
# ---------------------------------------------------------------------------

CONF_ROOMS: str = "rooms"
"""List-of-dicts of per-room configuration stored under ``entry.data``."""

CONF_ADD_ANOTHER: str = "add_another"
"""Wizard flag: loop back and add another room."""

CONF_ROOM_NAME: str = "room_name"
CONF_ROOM_AREA: str = "room_area"
"""Room floor area, m^2 (used by the simulator / nominal loop-power hints)."""

# ---------------------------------------------------------------------------
# Configuration keys — per-room entity mapping + flags (config-flow step 3)
# ---------------------------------------------------------------------------

CONF_ENTITY_TEMP_ROOM: str = "entity_temp_room"
"""Room-temperature source entity (sensor, device_class temperature)."""

CONF_ENTITY_HUMIDITY: str = "entity_humidity"
"""Relative-humidity source entity (humidity sensor). Cooled rooms only."""

CONF_ENTITY_TEMP_OUTDOOR: str = "entity_temp_outdoor"
"""Optional outdoor-temperature source entity (sensor, device_class temperature)."""

CONF_ENTITY_VALVES: str = "entity_valves"
"""List of valve actuator entities (``number`` or ``valve`` domain). A room/zone
may have 1..n loops."""

CONF_ENTITY_SUPPLY: str = "entity_supply"
"""List of supply-water temperature sensors, one per loop (sensor, temperature)."""

CONF_ENTITY_RETURN: str = "entity_return"
"""List of return-water temperature sensors, one per loop (sensor, temperature)."""

CONF_ENTITY_FAST_SOURCE: str = "entity_fast_source"
"""Fast-source (split/heater) entity (climate domain). Present when kind != none."""

CONF_FAST_SOURCE_KIND: str = "fast_source_kind"
"""Fast-source kind: one of :data:`FAST_SOURCE_KINDS`."""

CONF_ROOM_OFFSET: str = "room_offset"
"""Per-room offset from the global home setpoint, K. Room target = home + offset."""

CONF_PARTICIPATES: str = "participates"
"""Whether the room participates in control at all (``udzial``)."""

CONF_COOLING_ENABLED: str = "cooling_enabled"
"""Whether the room participates in floor cooling (``udzial w chlodzeniu``)."""

# ---------------------------------------------------------------------------
# Closed option sets (mirror core enum ``.value`` strings verbatim)
# ---------------------------------------------------------------------------

FAST_SOURCE_KIND_NONE: str = "none"
FAST_SOURCE_KIND_SPLIT: str = "split"
FAST_SOURCE_KIND_HEATER: str = "heater"
FAST_SOURCE_KINDS: list[str] = [
    FAST_SOURCE_KIND_NONE,
    FAST_SOURCE_KIND_SPLIT,
    FAST_SOURCE_KIND_HEATER,
]
"""Accepted fast-source kinds; mirrors core ``FastSourceKind`` values."""

MODE_HEATING: str = "heating"
MODE_TRANSITIONAL: str = "transitional"
MODE_COOLING: str = "cooling"
MODE_OFF: str = "off"
MODE_OPTIONS: list[str] = [MODE_HEATING, MODE_TRANSITIONAL, MODE_COOLING, MODE_OFF]
"""Accepted global mode strings; mirrors core ``Mode`` values."""

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_HOME_SETPOINT_C: float = 21.0
"""Default whole-home comfort setpoint, degrees Celsius."""

DEFAULT_ROOM_OFFSET_C: float = 0.0
"""Default per-room offset from the home setpoint, K."""

DEFAULT_FAST_SOURCE_KIND: str = FAST_SOURCE_KIND_NONE
DEFAULT_MODE: str = MODE_HEATING
DEFAULT_PARTICIPATES: bool = True
DEFAULT_COOLING_ENABLED: bool = True
DEFAULT_LIVE_CONTROL: bool = False
"""New rooms start in shadow (dry-run) mode until explicitly promoted to live."""

DEFAULT_KILL_SWITCH: bool = False

# ---------------------------------------------------------------------------
# Writable number-entity ranges (home setpoint + per-room offset)
# ---------------------------------------------------------------------------

HOME_SETPOINT_MIN_C: float = 5.0
HOME_SETPOINT_MAX_C: float = 30.0
HOME_SETPOINT_STEP_C: float = 0.5

ROOM_OFFSET_MIN_C: float = -5.0
ROOM_OFFSET_MAX_C: float = 5.0
ROOM_OFFSET_STEP_C: float = 0.5

# ---------------------------------------------------------------------------
# Coordinator / watchdog timing
# ---------------------------------------------------------------------------

UPDATE_INTERVAL_MINUTES: int = 5
"""Control cycle period, minutes (coordinator poll + core ``step``)."""

ENTITY_STALE_MAX_SECONDS: int = 300
"""Max age of a cached source-entity value before it is treated as missing, seconds."""

WATCHDOG_TIMEOUT_MINUTES: int = 15
"""No fresh data beyond this -> watchdog fault / alarm in the report, minutes."""

WATCHDOG_RECOVERY_MINUTES: int = 5
"""Sustained fresh data for this long clears the watchdog fault, minutes."""

# ---------------------------------------------------------------------------
# Unit-hint sets (hardware-agnostic validation; accepted source-entity units)
# ---------------------------------------------------------------------------

VALID_TEMP_UNITS: set[str] = {"°C", "C"}
"""Accepted unit strings for a temperature source entity (degrees Celsius)."""

VALID_PERCENT_UNITS: set[str] = {"%"}
"""Accepted unit strings for percent quantities (valve position, humidity)."""

VALID_POWER_UNITS: set[str] = {"W"}
"""Accepted unit strings for a power source entity (watts)."""
