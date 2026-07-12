"""Constants for the Tortoise-UFH Home Assistant integration.

This is the HA *adapter* constants module (the ``custom_components`` side). It is
the single authoritative ``CONF_*`` key vocabulary shared by the config flow,
coordinator, and entity platforms — mirror of the pump-ahead pattern. Unlike the
pure core (``custom_components/tortoise_ufh/core/const.py``, which never imports
Home Assistant), this module may import ``homeassistant`` for the
:class:`~homeassistant.const.Platform` enum only.

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
    Platform.SELECT,
]

# ---------------------------------------------------------------------------
# Configuration keys — global (config-flow step 1 / options flow)
# ---------------------------------------------------------------------------

CONF_HOME_SETPOINT: str = "home_setpoint"
"""Global whole-home comfort setpoint, degrees Celsius. Room target = this + offset."""

CONF_ENTITY_MODE: str = "entity_mode"
"""Global mode input entity (select/input_select): heating/transitional/cooling/off."""

CONF_CONTROLLER: str = "controller"
"""Serialised global :class:`~core.config.ControllerConfig` knobs, stored under
``entry.data`` (wizard result) and overlaid by ``entry.options`` (options flow /
``set_tuning``). Defined here — not in ``config_flow.py`` — so runtime modules
(coordinator, websocket) never import the config wizard for one string key."""

CONF_LIVE_CONTROL: str = "live_control"
"""LEGACY per-room shadow->live toggle map (options), keyed by room name.

Retired in favour of the canonical three-state :data:`CONF_ROOM_STATE` map. Kept
only so :func:`async_migrate_entry` can read a v1 entry's value while translating
it to the new state map; never written by the current code.
"""

# ---------------------------------------------------------------------------
# Per-room control state (the canonical three-state OFF / SHADOW / LIVE)
# ---------------------------------------------------------------------------

CONF_ROOM_STATE: str = "room_state"
"""Options map ``{room_name: state}`` — the single source of truth for a room's
control participation. ``state`` is one of :data:`ROOM_STATES`."""

CONF_ROOM_TUNING: str = "room_tuning"
"""Options map ``{room_name: {field: value}}`` of *sparse* per-room controller
overrides. Only fields a room deliberately overrides are stored; every other
field falls back to the global :data:`CONF_CONTROLLER` tuning. An
empty override dict for a room means "back to global" and is pruned entirely."""

ROOM_STATE_OFF: str = "off"
"""Room does not participate in control at all (core sees ``Mode.OFF``)."""

ROOM_STATE_SHADOW: str = "shadow"
"""Room is computed and reported but no commands are written (dry-run)."""

ROOM_STATE_LIVE: str = "live"
"""Room is computed, reported and its commands are written to the actuators."""

ROOM_STATES: list[str] = [ROOM_STATE_OFF, ROOM_STATE_SHADOW, ROOM_STATE_LIVE]
"""Accepted per-room control states (off / shadow / live)."""

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

CONF_FAST_SOURCE_GROUP: str = "fast_source_group"
"""Optional multisplit outdoor-unit group key (K4, 2026-07-12).

Rooms whose fast sources share one physical outdoor unit should carry the
same non-empty group string (any generic label, e.g. ``"outdoor_unit_a"``);
the core then arbitrates ONE direction per group each cycle. Empty (default)
= an independent unit, no arbitration.
"""

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

DEFAULT_ROOM_STATE: str = ROOM_STATE_SHADOW
"""New / unknown rooms start safely in shadow (dry-run) mode: a room the
coordinator has never seen a persisted state for is observed, not driven.
"""

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
# Advanced controller-knob specs — single source of truth
# ---------------------------------------------------------------------------

CONTROLLER_NUMBER_KNOBS: tuple[tuple[str, float, float, float], ...] = (
    ("kp", 0.0, 50.0, 0.1),
    # NOTE: HA's NumberSelector requires step >= 0.001; the retuned default
    # ki=0.0015 stays representable via direct (box) input.
    ("ki", 0.0, 1.0, 0.001),
    ("kt", 0.0, 50.0, 0.1),
    ("deadband_c", 0.0, 5.0, 0.1),
    ("valve_floor_pct", 0.0, 100.0, 1.0),
    ("boost_offset_c", 0.0, 10.0, 0.1),
    ("fast_min_on_minutes", 0.0, 60.0, 1.0),
    ("fast_min_off_minutes", 0.0, 60.0, 1.0),
    ("dew_margin_k", 0.0, 10.0, 0.1),
    ("dew_ramp_k", 0.1, 10.0, 0.1),
    ("ff_neutral_c", -30.0, 40.0, 0.5),
    ("ff_gain_pct_per_k", 0.0, 10.0, 0.1),
    ("ff_max_pct", 0.0, 100.0, 1.0),
)
"""Numeric :class:`~tortoise_ufh.config.ControllerConfig` fields exposed as
advanced knobs, each as ``(field, min, max, step)``.

Single authoritative source for the config flow (``algorithm`` / ``settings``
steps), the ``get_tuning`` / ``set_tuning`` websocket range validation, and — via
the ``get_tuning`` payload — the panel's Tuning tab, so the ranges are never
duplicated in JavaScript.

Note: the derivative gain ``kd`` is deliberately not exposed — Aneks §8.3 forbids
a derivative-on-error term, so ``ControllerConfig.kd`` stays at its ``0.0``
default. The non-tuning bookkeeping fields ``cycle_seconds`` /
``valve_write_threshold_pct`` are likewise not user-facing.
"""

CONTROLLER_BOOL_KNOB: str = "outdoor_ff_enabled"
"""Boolean :class:`~tortoise_ufh.config.ControllerConfig` knob: outdoor-temperature
feedforward. Exposed alongside :data:`CONTROLLER_NUMBER_KNOBS`."""

CONTROLLER_KNOB_UNITS: dict[str, str] = {
    "kp": "%/K",
    "ki": "%/(K·s)",
    "kt": "%/(K/h)",
    "deadband_c": "K",
    "valve_floor_pct": "%",
    "boost_offset_c": "K",
    "fast_min_on_minutes": "min",
    "fast_min_off_minutes": "min",
    "dew_margin_k": "K",
    "dew_ramp_k": "K",
    "ff_neutral_c": "°C",
    "ff_gain_pct_per_k": "%/K",
    "ff_max_pct": "%",
    CONTROLLER_BOOL_KNOB: "",
}
"""Display unit per controller knob (empty for the boolean knob). Surfaced in the
``get_tuning`` payload so the panel labels each stepper with its unit."""

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
