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

legacy-migration-only: read exclusively by :func:`async_migrate_entry` while
translating a v1 entry to the state map; never written by the current code.
"""

# ---------------------------------------------------------------------------
# Per-room control state (the canonical two-state OFF / LIVE)
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

ROOM_STATE_LIVE: str = "live"
"""Room is computed, reported and its commands are written to the actuators."""

ROOM_STATE_SHADOW: str = "shadow"
"""LEGACY dry-run state, removed 2026-07-12 (v0.7.0; DECISIONS §13).

legacy-migration-only: the literal is read (and transiently written by the
v1->v2 block) exclusively by :func:`async_migrate_entry` — the v2->v3 block
converts every ``"shadow"`` to :data:`ROOM_STATE_OFF`. Never written by the
current runtime code and deliberately absent from :data:`ROOM_STATES`.
"""

ROOM_STATES: list[str] = [ROOM_STATE_OFF, ROOM_STATE_LIVE]
"""Accepted per-room control states (off / live)."""

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

CONF_FAST_WINDOW_START: str = "fast_source_window_start"
"""Per-room quiet-hours window START, ``"HH:MM"`` local time (B1, 2026-07-12).

The window is when the fast source MAY run (e.g. ``"07:00"``-``"22:00"``);
outside it the split does not engage (quiet hours). May cross midnight
(start > end). Empty / absent together with the end = no restriction.
"""

CONF_FAST_WINDOW_END: str = "fast_source_window_end"
"""Per-room quiet-hours window END, ``"HH:MM"`` local time (B1, 2026-07-12).

See :data:`CONF_FAST_WINDOW_START`; the pair is all-or-nothing (validated in
the config flow, error ``quiet_window_invalid``).
"""

CONF_ROOM_OFFSET: str = "room_offset"
"""Per-room offset from the global home setpoint, K. Room target = home + offset."""

CONF_PARTICIPATES: str = "participates"
"""LEGACY v1 per-room participation flag (``udzial``).

legacy-migration-only: read exclusively by :func:`async_migrate_entry` (v1->v2);
never written by the current code. Participation is now derived from the
control state (``participates := state != off``).
"""

CONF_COOLING_ENABLED: str = "cooling_enabled"
"""Whether the room participates in floor cooling (``udzial w chlodzeniu``)."""

# ---------------------------------------------------------------------------
# Configuration keys — OPTIONAL heat-pump link (B2, 2026-07-12; options flow)
# ---------------------------------------------------------------------------

CONF_HEAT_PUMP: str = "heat_pump"
"""Options dict of the opt-in heat-pump link (``entry.options``). All keys are
optional; an absent/empty section keeps Tortoise entirely away from the pump
(the pre-0.8.0 behaviour). See ``prd-control-brain.md`` §8.13."""

CONF_ENTITY_HP_MODE: str = "entity_hp_mode"
"""Pump-mode select entity (HeishaMon-style options, e.g. ``"Heat only"`` /
``"Heat+DHW"``). Tortoise synchronises only the DIRECTION and always preserves
the ``+DHW`` flag (owned by the external DHW automation)."""

CONF_ENTITY_HP_HEATING_SETPOINT: str = "entity_hp_heating_setpoint"
"""Optional heating-water setpoint ``number`` entity. Unset (the default) the
pump follows its own weather curve — the owner's default, fully supported."""

CONF_ENTITY_HP_COOLING_SETPOINT: str = "entity_hp_cooling_setpoint"
"""Optional cooling-water setpoint ``number`` entity (HeishaMon-style
``z1_cool_request_temp``). The ONLY cooling write path; when set and the home
cools, Tortoise writes ``max(cooling_supply_base_c, global safe dew point)``."""

CONF_ENTITY_HP_ACTIVE: str = "entity_hp_active"
"""Optional "pump serves the UFH" entity (binary_sensor / switch /
input_boolean / sensor). ``off`` during DHW/defrost freezes every room's
integrator (``RoomInputs.hp_active_for_ufh``). Moved here from the
coordinator's dead zone (B2, 2026-07-12); the legacy per-room key of the same
name is still honoured as an override."""

# -- Cooling setpoint-flicker entities (issue #7, 2026-07-15) ----------------
# All OPTIONAL, stored inside the CONF_HEAT_PUMP options dict. See
# ``core/hp_link.py::SetpointFlicker`` and ``docs/DECISIONS.md`` §21.

CONF_ENTITY_HP_RETURN_TEMP: str = "entity_hp_return_temp"
"""Optional pump inlet/return water temperature sensor (issue #7). The MAIN
flicker signal: the return parks near ``cool-setpoint + 3 K`` through idles.
Live example: ``sensor.panasonic_heat_pump_main_main_inlet_temp``."""

CONF_ENTITY_HP_COMPRESSOR_FREQ: str = "entity_hp_compressor_freq"
"""Optional compressor frequency sensor [Hz], ``0`` = off (issue #7). Used for
idle detection (compressor OFF is the idle signal). Live example:
``sensor.panasonic_heat_pump_main_compressor_freq``."""

CONF_ENTITY_HP_OUTLET_TEMP: str = "entity_hp_outlet_temp"
"""Optional pump outlet/supply water temperature sensor (issue #7). DIAGNOSTIC
ONLY in v1 — read and surfaced in the heat-pump runtime / panel, no logic
consumes it (mapped for a future supply-side condensation guard). Live
example: ``sensor.panasonic_heat_pump_main_main_outlet_temp``."""

CONF_ENTITY_GLOBAL_SUPPLY: str = "entity_global_supply"
"""Optional GLOBAL manifold supply-temperature probe (sensor, temperature;
top-level ``entry.options`` key set in options -> settings; S6, 2026-07-13).
Feeds the S6 hydraulic watchdog's circulation gate: when it reads clearly
source-side of the mean room temperature, circulation is proven even while
every per-loop probe pair sits post-valve. Unset = the gate relies on the
per-loop delta-T witnesses only."""

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

DEFAULT_ROOM_STATE: str = ROOM_STATE_OFF
"""New / unknown rooms start safely OFF: a room the coordinator has never seen
a persisted state for writes nothing until the user deliberately switches it
to ``live``.
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
    # Lower bound 3 min (K2, 2026-07-12): zero dwells let a conflicted
    # multisplit group flip direction EVERY cycle; 3 min is the floor of
    # sane compressor hygiene, the core itself still accepts 0 for tests.
    ("fast_min_on_minutes", 3.0, 60.0, 1.0),
    ("fast_min_off_minutes", 3.0, 60.0, 1.0),
    # A knob since 2026-07-13 (owner request): the S12 boost overdrive of the
    # split target beyond the room setpoint; 0 disables (plain setpoint).
    ("fast_target_offset_k", 0.0, 3.0, 0.5),
    # Dry assist (2026-07-16, §24): room dew point above which a split is
    # engaged in DRY; default 17 from the owner's stale-air observation.
    ("dry_dew_max_c", 12.0, 22.0, 0.5),
    # Lower bound 0.5 K (D4, 2026-07-12): margin 0 degenerates the local
    # dew-point throttle ramp into a hard on/off step at the dew point.
    ("dew_margin_k", 0.5, 10.0, 0.1),
    ("dew_ramp_k", 0.1, 10.0, 0.1),
    ("ff_neutral_c", -30.0, 40.0, 0.5),
    ("ff_gain_pct_per_k", 0.0, 10.0, 0.1),
    ("ff_max_pct", 0.0, 100.0, 1.0),
    # Heat-pump water setpoints (B2, 2026-07-12): building-level knobs read
    # only by the opt-in heat-pump link; see HP_GLOBAL_ONLY_KNOBS below.
    ("cooling_supply_base_c", 10.0, 25.0, 0.5),
    ("heating_supply_base_c", 20.0, 40.0, 0.5),
    ("heating_supply_slope", 0.0, 2.0, 0.1),
    # Cooling setpoint-flicker timing (issue #7, 2026-07-15): global-only knobs
    # read by the opt-in SetpointFlicker; see HP_GLOBAL_ONLY_KNOBS below.
    ("hp_flicker_band_k", 0.5, 3.0, 0.1),
    # Demand gate (2026-07-16, DECISIONS §23): min loop-weighted valve opening
    # of the calling rooms before a start may be forced. The static max here is
    # a loose fallback — every UI surface overrides it with the REAL ceiling
    # (total configured loops x 100) via tuning.flicker_open_max_pct.
    ("hp_flicker_min_open_pct", 100.0, 10000.0, 25.0),
    ("hp_flicker_stuck_minutes", 5.0, 120.0, 1.0),
    ("hp_flicker_min_off_minutes", 5.0, 120.0, 1.0),
    ("hp_flicker_max_starts_per_h", 1.0, 6.0, 1.0),
    # S6 hydraulic no-flow watchdog (2026-07-13, issue #4). The response
    # window's UI minimum is 30 min — the slab is slow and shorter windows
    # would flap (the CORE accepts any positive value so simulation tests
    # can run short windows); 1440 min effectively disables the watchdog.
    ("flow_epsilon_k", 0.1, 3.0, 0.1),
    ("flow_open_threshold_pct", 5.0, 100.0, 1.0),
    ("flow_response_window_min", 30.0, 1440.0, 5.0),
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

CONTROLLER_BOOL_KNOBS: tuple[str, ...] = (
    "outdoor_ff_enabled",
    "dry_enabled",
    "hp_flicker_enabled",
)
"""Boolean :class:`~tortoise_ufh.config.ControllerConfig` knobs, exposed
alongside :data:`CONTROLLER_NUMBER_KNOBS`: the outdoor-temperature feedforward
switch, the opt-in dry assist (humidity-triggered split DRY; 2026-07-16, §24)
and the opt-in cooling setpoint-flicker master switch (issue #7,
2026-07-15 — each on/off is a tuning knob so it renders as a
toggle in its panel group and the options-flow settings step)."""

CONTROLLER_KNOB_UNITS: dict[str, str] = {
    "kp": "%/K",
    "ki": "%/(K·s)",
    "kt": "%/(K/h)",
    "deadband_c": "K",
    "valve_floor_pct": "%",
    "boost_offset_c": "K",
    "fast_min_on_minutes": "min",
    "fast_min_off_minutes": "min",
    "fast_target_offset_k": "K",
    "dry_dew_max_c": "°C",
    "dry_enabled": "",
    "dew_margin_k": "K",
    "dew_ramp_k": "K",
    "ff_neutral_c": "°C",
    "ff_gain_pct_per_k": "%/K",
    "ff_max_pct": "%",
    "cooling_supply_base_c": "°C",
    "heating_supply_base_c": "°C",
    "heating_supply_slope": "K/K",
    "hp_flicker_band_k": "K",
    "hp_flicker_min_open_pct": "%",
    "hp_flicker_stuck_minutes": "min",
    "hp_flicker_min_off_minutes": "min",
    "hp_flicker_max_starts_per_h": "1/h",
    "flow_epsilon_k": "K",
    "flow_open_threshold_pct": "%",
    "flow_response_window_min": "min",
    "outdoor_ff_enabled": "",
    "hp_flicker_enabled": "",
}
"""Display unit per controller knob (empty for the boolean knob). Surfaced in the
``get_tuning`` payload so the panel labels each stepper with its unit."""

HP_GLOBAL_ONLY_KNOBS: frozenset[str] = frozenset(
    {
        "cooling_supply_base_c",
        "heating_supply_base_c",
        "heating_supply_slope",
        "hp_flicker_enabled",
        "hp_flicker_band_k",
        "hp_flicker_min_open_pct",
        "hp_flicker_stuck_minutes",
        "hp_flicker_min_off_minutes",
        "hp_flicker_max_starts_per_h",
    }
)
"""Knobs that exist ONLY in the global tuning scope (B2, 2026-07-12; extended
with the flicker timing, issue #7 2026-07-15).

The heat-pump water setpoints and the setpoint-flicker timing are
building-level physics — a per-room override could be set but would have zero
effect, so it is rejected outright: ``coerce_tuning_values`` raises for a room
scope and the panel does not render the group outside the Global scope."""

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
