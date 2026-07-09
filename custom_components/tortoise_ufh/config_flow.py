"""Config flow for the Tortoise-UFH integration.

Implements the multi-step setup wizard (``VERSION = 2``) and the options flow.

Wizard steps:
    1. ``user``       — home location (latitude / longitude, decimal degrees).
    2. ``rooms``      — one or more rooms (name, floor area, fast-source flag +
       kind, cooling participation); loops on itself while ``add_another``.
    3. ``entities``   — per-room source entity mapping (room temperature,
       humidity, valves, supply/return water probes, fast source) plus the two
       global entities (outdoor temperature, mode input) collected on the first
       room only.
    4. ``algorithm``  — advanced controller tuning knobs (all optional, defaults
       from the core :class:`~tortoise_ufh.config.ControllerConfig`).
    5. ``confirm``    — sets ``unique_id = f"{lat}_{lon}"`` and creates the entry.

Entity pickers use domain / device-class filtering; every selected entity is
validated for the correct ``unit_of_measurement`` via :class:`EntityValidator`
(hardware-agnostic: units only — degC, %, W — never a brand).

Persistence note: Home Assistant stores ``entry.data`` / ``entry.options`` as
JSON-serialisable dicts, so the flow collects values into the frozen, validated
value objects below (:class:`LocationInput`, :class:`RoomDefinition`, and the
core :class:`ControllerConfig`) and then serialises them to plain dicts.

Units:
    Latitude / longitude in decimal degrees; room area in square metres;
    temperatures in degrees Celsius; humidity / valve position in percent.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)

from .const import (
    CONF_ADD_ANOTHER,
    CONF_COOLING_ENABLED,
    CONF_ENTITY_FAST_SOURCE,
    CONF_ENTITY_HUMIDITY,
    CONF_ENTITY_MODE,
    CONF_ENTITY_RETURN,
    CONF_ENTITY_SUPPLY,
    CONF_ENTITY_TEMP_OUTDOOR,
    CONF_ENTITY_TEMP_ROOM,
    CONF_ENTITY_VALVES,
    CONF_FAST_SOURCE_KIND,
    CONF_HOME_SETPOINT,
    CONF_ROOM_AREA,
    CONF_ROOM_NAME,
    CONF_ROOM_OFFSET,
    CONF_ROOM_STATE,
    CONF_ROOMS,
    CONTROLLER_BOOL_KNOB,
    CONTROLLER_NUMBER_KNOBS,
    DEFAULT_COOLING_ENABLED,
    DEFAULT_FAST_SOURCE_KIND,
    DEFAULT_HOME_SETPOINT_C,
    DEFAULT_ROOM_OFFSET_C,
    DEFAULT_ROOM_STATE,
    DOMAIN,
    FAST_SOURCE_KIND_NONE,
    FAST_SOURCE_KINDS,
    ROOM_OFFSET_MAX_C,
    ROOM_OFFSET_MIN_C,
    ROOM_OFFSET_STEP_C,
    ROOM_STATES,
    VALID_PERCENT_UNITS,
    VALID_TEMP_UNITS,
)
from .core.config import ControllerConfig
from .entity_validator import EntityValidator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wizard-local configuration keys (not part of the shared const.py vocabulary)
# ---------------------------------------------------------------------------

CONF_HAS_FAST_SOURCE: str = "has_fast_source"
"""Per-room wizard flag: whether the room has a fast auxiliary source."""

CONF_CONTROLLER: str = "controller"
"""Serialised default :class:`ControllerConfig` knobs (``entry.data`` /
``entry.options``)."""

_ENTITY_UNAVAILABLE: str = "entity_unavailable"
"""Non-blocking validation error key (an entity may come online later)."""

CONF_SELECTED_ROOM: str = "selected_room"
"""Options-flow room-picker field key (edit-room / remove-room steps)."""

_SETPOINT_STORE_VERSION: int = 1
"""Schema version of the coordinator's per-entry setpoint :class:`Store`.

Mirrors ``coordinator._SETPOINT_STORE_VERSION``; kept in sync so the options
flow can prune a removed room's persisted offset from the same Store.
"""

_GLOBAL_UNIQUE_ID_KEYS: frozenset[str] = frozenset(
    {
        "home_temperature",
        "global_safe_dew_point",
        "algorithm_status",
        "last_update",
        "watchdog_status",
    }
)
"""Per-entry *global* entity unique-id suffixes (``f"{entry_id}_{key}"``, with no
room segment). Excluded from room-removal registry cleanup so a room whose slug
is a prefix of a global key (e.g. a room named "Home" vs ``home_temperature``)
can never delete a global entity. Mirrors the global entity-description keys of
the number / sensor platforms (``home_temperature`` is
``HOME_TEMPERATURE_DESCRIPTION.key`` in ``number.py`` — *not* the
``CONF_HOME_SETPOINT`` config key).
"""

# ---------------------------------------------------------------------------
# Frozen, validated value objects collected by the wizard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocationInput:
    """A validated home geographic location.

    Attributes:
        latitude: Geographic latitude [decimal degrees], in ``[-90, 90]``.
        longitude: Geographic longitude [decimal degrees], in ``[-180, 180]``.

    Raises:
        ValueError: If latitude or longitude is out of range.
    """

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        """Validate the latitude / longitude ranges."""
        if not -90.0 <= self.latitude <= 90.0:
            msg = f"latitude must be in [-90, 90], got {self.latitude}"
            raise ValueError(msg)
        if not -180.0 <= self.longitude <= 180.0:
            msg = f"longitude must be in [-180, 180], got {self.longitude}"
            raise ValueError(msg)

    @property
    def unique_id(self) -> str:
        """Return the config-entry unique id ``f"{lat}_{lon}"``."""
        return f"{self.latitude}_{self.longitude}"


@dataclass(frozen=True)
class RoomDefinition:
    """A validated per-room definition (name / area / fast source / cooling).

    Attributes:
        name: Non-empty, unique room identifier.
        area_m2: Floor area [m^2] (must be > 0).
        has_fast_source: Whether the room has a fast auxiliary source.
        fast_source_kind: One of :data:`FAST_SOURCE_KINDS`. Must be ``"none"``
            when ``has_fast_source`` is ``False`` and a non-``"none"`` kind when
            it is ``True``.
        cooling_enabled: Whether the room participates in floor cooling.

    Raises:
        ValueError: If the name is empty, the area is non-positive, or the
            fast-source kind is inconsistent with ``has_fast_source``.
    """

    name: str
    area_m2: float
    has_fast_source: bool
    fast_source_kind: str
    cooling_enabled: bool

    def __post_init__(self) -> None:
        """Validate the room definition invariants."""
        if not self.name.strip():
            msg = "room name must be a non-empty string"
            raise ValueError(msg)
        if self.area_m2 <= 0:
            msg = f"area_m2 must be > 0, got {self.area_m2}"
            raise ValueError(msg)
        if self.fast_source_kind not in FAST_SOURCE_KINDS:
            msg = (
                f"fast_source_kind must be one of {FAST_SOURCE_KINDS}, "
                f"got {self.fast_source_kind!r}"
            )
            raise ValueError(msg)
        if self.has_fast_source and self.fast_source_kind == FAST_SOURCE_KIND_NONE:
            msg = "fast_source_kind must not be 'none' when has_fast_source is True"
            raise ValueError(msg)
        if not self.has_fast_source and self.fast_source_kind != FAST_SOURCE_KIND_NONE:
            msg = "fast_source_kind must be 'none' when has_fast_source is False"
            raise ValueError(msg)

    def as_dict(self) -> dict[str, Any]:
        """Serialise to the room base dict stored under ``CONF_ROOMS``."""
        return {
            CONF_ROOM_NAME: self.name.strip(),
            CONF_ROOM_AREA: self.area_m2,
            CONF_HAS_FAST_SOURCE: self.has_fast_source,
            CONF_FAST_SOURCE_KIND: self.fast_source_kind,
            CONF_COOLING_ENABLED: self.cooling_enabled,
        }


# ---------------------------------------------------------------------------
# Shared schema / validation helpers
# ---------------------------------------------------------------------------


def _controller_schema_dict(defaults: ControllerConfig) -> dict[Any, Any]:
    """Build the advanced controller-knob schema fragment.

    Args:
        defaults: The :class:`ControllerConfig` supplying the field defaults.

    Returns:
        A voluptuous schema dict of optional number / boolean selectors, one per
        exposed :class:`ControllerConfig` field.
    """
    schema: dict[Any, Any] = {}
    for field_name, low, high, step in CONTROLLER_NUMBER_KNOBS:
        default_val = float(getattr(defaults, field_name))
        schema[vol.Optional(field_name, default=default_val)] = NumberSelector(
            NumberSelectorConfig(
                min=low, max=high, step=step, mode=NumberSelectorMode.BOX
            )
        )
    schema[vol.Optional(CONTROLLER_BOOL_KNOB, default=defaults.outdoor_ff_enabled)] = (
        BooleanSelector()
    )
    return schema


def _parse_controller(user_input: dict[str, Any]) -> ControllerConfig:
    """Build a validated :class:`ControllerConfig` from submitted knob values.

    Args:
        user_input: The submitted algorithm-step form values.

    Returns:
        The validated :class:`ControllerConfig`.

    Raises:
        ValueError: If any knob violates a :class:`ControllerConfig` invariant
            (propagated from its ``__post_init__``).
    """
    kwargs: dict[str, Any] = {
        field_name: float(user_input[field_name])
        for field_name, _low, _high, _step in CONTROLLER_NUMBER_KNOBS
        if field_name in user_input
    }
    if CONTROLLER_BOOL_KNOB in user_input:
        kwargs[CONTROLLER_BOOL_KNOB] = bool(user_input[CONTROLLER_BOOL_KNOB])
    return ControllerConfig(**kwargs)


def _as_entity_list(value: Any) -> list[str]:
    """Coerce a (possibly missing / scalar) selector value to a list of ids.

    Args:
        value: An ``EntitySelector`` value: ``None``, a single id, or a list.

    Returns:
        A list of entity ids (empty when nothing was selected).
    """
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _validate_entities(
    validator: EntityValidator,
    entity_ids: list[str],
    *,
    valid_units: set[str] | None = None,
    device_class: str | None = None,
) -> str | None:
    """Validate a list of entities; return the first blocking error key.

    Availability failures are treated as non-blocking (logged, not returned),
    because a configured entity may legitimately come online after setup.

    Args:
        validator: The entity validator bound to the live HA instance.
        entity_ids: Entity ids to validate.
        valid_units: Accepted unit strings, or ``None`` to skip the unit check.
        device_class: Required device class, or ``None`` to skip that check.

    Returns:
        The first blocking ``error_key``, or ``None`` when all entities pass.
    """
    for entity_id in entity_ids:
        result = validator.validate_entity(
            entity_id,
            valid_units=valid_units,
            expected_device_class=device_class,
        )
        if not result.valid and result.error_key != _ENTITY_UNAVAILABLE:
            if result.error_details:
                _LOGGER.warning("%s", result.error_details)
            return result.error_key
    return None


def _validate_valves(validator: EntityValidator, valve_ids: list[str]) -> str | None:
    """Validate the room's valve actuators, dispatching by domain.

    Runs the shared existence / availability check on every actuator (``number``
    or ``valve``; units are not required for either), then asserts that each
    ``valve``-domain entity can be driven to a position
    (``ValveEntityFeature.SET_POSITION``). ``number`` actuators, positioned via
    ``number.set_value``, need no such capability gate. Availability failures
    stay non-blocking.

    Args:
        validator: The entity validator bound to the live HA instance.
        valve_ids: The room's valve actuator entity ids.

    Returns:
        The first blocking ``error_key``, or ``None`` when all valves pass.
    """
    generic = _validate_entities(validator, valve_ids)
    if generic is not None:
        return generic
    for entity_id in valve_ids:
        result = validator.validate_valve_set_position(entity_id)
        if not result.valid and result.error_key != _ENTITY_UNAVAILABLE:
            if result.error_details:
                _LOGGER.warning("%s", result.error_details)
            return result.error_key
    return None


def _room_attributes_schema_dict(
    *,
    include_name: bool,
    include_offset: bool = True,
    defaults: dict[str, Any] | None = None,
) -> dict[Any, Any]:
    """Build the room-attributes schema fragment (name / area / offset / flags).

    Shared by the options flow's add-room and edit-room attribute steps so the
    room knobs are declared exactly once. The room name is included only when
    adding (a room name is immutable once created — rename is remove + add).

    Args:
        include_name: Whether to include the required room-name text field.
        include_offset: Whether to include the per-room setpoint-offset field.
            Included only when adding (to seed a brand-new room's offset). It is
            omitted when editing: for an existing room the offset is runtime
            state owned by the home-temperature / offset number entities and the
            coordinator's setpoint Store, which authoritatively override any
            value in ``entry.data`` on reload — so an editable field here would
            be silently reverted. Offset edits go through the number entity.
        defaults: An existing room dict used to pre-fill the fields when editing,
            or ``None`` to use the library defaults (the add case). When the
            ``has_fast_source`` flag is absent it is derived from
            ``fast_source_kind`` so a number-only / legacy room still pre-fills.

    Returns:
        A voluptuous schema dict (marker -> selector).
    """
    values = defaults or {}
    has_fast_default = bool(
        values.get(
            CONF_HAS_FAST_SOURCE,
            str(values.get(CONF_FAST_SOURCE_KIND, FAST_SOURCE_KIND_NONE))
            != FAST_SOURCE_KIND_NONE,
        )
    )
    schema: dict[Any, Any] = {}
    if include_name:
        schema[vol.Required(CONF_ROOM_NAME)] = TextSelector()
    schema[
        vol.Required(CONF_ROOM_AREA, default=float(values.get(CONF_ROOM_AREA, 20.0)))
    ] = NumberSelector(
        NumberSelectorConfig(min=1, max=1000, step=0.1, mode=NumberSelectorMode.BOX)
    )
    if include_offset:
        schema[
            vol.Required(
                CONF_ROOM_OFFSET,
                default=float(values.get(CONF_ROOM_OFFSET, DEFAULT_ROOM_OFFSET_C)),
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=ROOM_OFFSET_MIN_C,
                max=ROOM_OFFSET_MAX_C,
                step=ROOM_OFFSET_STEP_C,
                mode=NumberSelectorMode.BOX,
            )
        )
    schema[vol.Required(CONF_HAS_FAST_SOURCE, default=has_fast_default)] = (
        BooleanSelector()
    )
    schema[
        vol.Required(
            CONF_FAST_SOURCE_KIND,
            default=str(values.get(CONF_FAST_SOURCE_KIND, DEFAULT_FAST_SOURCE_KIND)),
        )
    ] = SelectSelector(SelectSelectorConfig(options=FAST_SOURCE_KINDS))
    schema[
        vol.Required(
            CONF_COOLING_ENABLED,
            default=bool(values.get(CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED)),
        )
    ] = BooleanSelector()
    return schema


def _entities_schema_dict(
    *,
    has_fast_source: bool,
    include_globals: bool,
    defaults: dict[str, Any] | None = None,
) -> dict[Any, Any]:
    """Build the per-room entity-mapping schema fragment.

    Shared by the setup wizard's ``entities`` step and the options flow's
    add/edit-room entity step, so the entity selectors, their domain / device
    class filters and their required/optional markers live in one place.

    Args:
        has_fast_source: Whether to include the fast-source climate picker.
        include_globals: Whether to append the two global pickers (outdoor
            temperature + mode input). ``True`` only on the wizard's first room;
            the options flow never re-collects the (already configured) globals.
        defaults: An existing room dict used to pre-fill the pickers when
            editing, or ``None`` to leave every field blank (add / first setup).

    Returns:
        A voluptuous schema dict (marker -> selector).
    """
    values = defaults or {}

    def marker(key: str, *, required: bool) -> Any:
        cls = vol.Required if required else vol.Optional
        suggested = values.get(key)
        if suggested:
            return cls(key, description={"suggested_value": suggested})
        return cls(key)

    schema: dict[Any, Any] = {
        marker(CONF_ENTITY_TEMP_ROOM, required=True): EntitySelector(
            EntitySelectorConfig(domain=["sensor"], device_class=["temperature"])
        ),
        marker(CONF_ENTITY_HUMIDITY, required=False): EntitySelector(
            EntitySelectorConfig(domain=["sensor"], device_class=["humidity"])
        ),
        marker(CONF_ENTITY_VALVES, required=True): EntitySelector(
            EntitySelectorConfig(domain=["number", "valve"], multiple=True)
        ),
        marker(CONF_ENTITY_SUPPLY, required=False): EntitySelector(
            EntitySelectorConfig(
                domain=["sensor"], device_class=["temperature"], multiple=True
            )
        ),
        marker(CONF_ENTITY_RETURN, required=False): EntitySelector(
            EntitySelectorConfig(
                domain=["sensor"], device_class=["temperature"], multiple=True
            )
        ),
    }
    if has_fast_source:
        schema[marker(CONF_ENTITY_FAST_SOURCE, required=False)] = EntitySelector(
            EntitySelectorConfig(domain=["climate"])
        )
    if include_globals:
        schema[marker(CONF_ENTITY_TEMP_OUTDOOR, required=False)] = EntitySelector(
            EntitySelectorConfig(domain=["sensor"], device_class=["temperature"])
        )
        schema[marker(CONF_ENTITY_MODE, required=False)] = EntitySelector(
            EntitySelectorConfig(domain=["select", "input_select"])
        )
    return schema


def _first_entity_error(
    validator: EntityValidator,
    *,
    temp_room: str,
    humidity: str,
    valves: list[str],
    supply: list[str],
    returns: list[str],
    fast_source: str,
    outdoor: str,
    cooling_enabled: bool,
) -> str | None:
    """Validate a room's mapped entities; return the first blocking error key.

    Mirrors the setup wizard's precedence: the per-entity unit / device-class
    checks are lowest priority, then the "at least one valve" gate, then the
    "cooled rooms need a humidity sensor" gate (highest), so a missing humidity
    sensor is always surfaced ahead of a unit mismatch.

    Args:
        validator: The entity validator bound to the live HA instance.
        temp_room: Room-temperature sensor id (required).
        humidity: Humidity sensor id, or ``""`` when none was selected.
        valves: Valve actuator ids (at least one is required).
        supply: Supply-water temperature sensor ids.
        returns: Return-water temperature sensor ids.
        fast_source: Fast-source climate id, or ``""`` when none.
        outdoor: Outdoor-temperature sensor id, or ``""`` to skip that check.
        cooling_enabled: Whether the room participates in floor cooling.

    Returns:
        The first blocking ``error_key``, or ``None`` when the mapping is valid.
    """
    checks: list[str | None] = [
        _validate_entities(
            validator,
            [temp_room],
            valid_units=VALID_TEMP_UNITS,
            device_class="temperature",
        ),
        _validate_entities(
            validator,
            [humidity] if humidity else [],
            valid_units=VALID_PERCENT_UNITS,
            device_class="humidity",
        ),
        _validate_valves(validator, valves),
        _validate_entities(
            validator, supply, valid_units=VALID_TEMP_UNITS, device_class="temperature"
        ),
        _validate_entities(
            validator, returns, valid_units=VALID_TEMP_UNITS, device_class="temperature"
        ),
        _validate_entities(validator, [fast_source] if fast_source else []),
    ]
    if outdoor:
        checks.append(
            _validate_entities(
                validator,
                [outdoor],
                valid_units=VALID_TEMP_UNITS,
                device_class="temperature",
            )
        )
    error = next((key for key in checks if key is not None), None)
    if not valves:
        error = "valve_required"
    if cooling_enabled and not humidity:
        error = "humidity_required"
    return error


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class TortoiseUfhConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step setup wizard for Tortoise-UFH."""

    VERSION = 2

    @staticmethod
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TortoiseUfhOptionsFlow:
        """Return the options-flow handler.

        Args:
            config_entry: The config entry being reconfigured.

        Returns:
            A fresh :class:`TortoiseUfhOptionsFlow`.
        """
        return TortoiseUfhOptionsFlow()

    def __init__(self) -> None:
        """Initialise mutable wizard state."""
        self._location: dict[str, Any] = {}
        self._rooms: list[dict[str, Any]] = []
        self._global: dict[str, Any] = {}
        self._controller: dict[str, Any] = asdict(ControllerConfig())
        self._entity_room_idx: int = 0

    # -- Step 1: location ---------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: home location (latitude / longitude, decimal degrees)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                location = LocationInput(
                    latitude=float(user_input[CONF_LATITUDE]),
                    longitude=float(user_input[CONF_LONGITUDE]),
                )
            except ValueError as err:
                _LOGGER.warning("Invalid location: %s", err)
                errors["base"] = "invalid_location"
            else:
                self._location = {
                    CONF_LATITUDE: location.latitude,
                    CONF_LONGITUDE: location.longitude,
                }
                return await self.async_step_rooms()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LATITUDE, default=self.hass.config.latitude
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=-90, max=90, step=0.001, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_LONGITUDE, default=self.hass.config.longitude
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=-180, max=180, step=0.001, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # -- Step 2: rooms ------------------------------------------------------

    async def async_step_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: define a room; loop while ``add_another`` is set."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = str(user_input.get(CONF_ROOM_NAME, "")).strip()
            has_fast = bool(user_input.get(CONF_HAS_FAST_SOURCE, False))
            raw_kind = str(
                user_input.get(CONF_FAST_SOURCE_KIND, DEFAULT_FAST_SOURCE_KIND)
            )
            # A room without a fast source is always kind "none".
            kind = raw_kind if has_fast else FAST_SOURCE_KIND_NONE
            add_another = bool(user_input.get(CONF_ADD_ANOTHER, False))

            if not name:
                errors["base"] = "empty_room_name"
            elif any(r[CONF_ROOM_NAME] == name for r in self._rooms):
                errors["base"] = "duplicate_room_name"

            room: RoomDefinition | None = None
            if not errors:
                try:
                    room = RoomDefinition(
                        name=name,
                        area_m2=float(user_input.get(CONF_ROOM_AREA, 0.0)),
                        has_fast_source=has_fast,
                        fast_source_kind=kind,
                        cooling_enabled=bool(
                            user_input.get(
                                CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED
                            )
                        ),
                    )
                except ValueError as err:
                    _LOGGER.warning("Invalid room definition: %s", err)
                    errors["base"] = "invalid_room"

            if not errors and room is not None:
                self._rooms.append(room.as_dict())
                if add_another:
                    return await self.async_step_rooms()
                self._entity_room_idx = 0
                return await self.async_step_entities()

        schema = vol.Schema(
            {
                vol.Required(CONF_ROOM_NAME): TextSelector(),
                vol.Required(CONF_ROOM_AREA, default=20.0): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=1000, step=0.1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_HAS_FAST_SOURCE, default=False): BooleanSelector(),
                vol.Required(
                    CONF_FAST_SOURCE_KIND, default=DEFAULT_FAST_SOURCE_KIND
                ): SelectSelector(SelectSelectorConfig(options=FAST_SOURCE_KINDS)),
                vol.Required(
                    CONF_COOLING_ENABLED, default=DEFAULT_COOLING_ENABLED
                ): BooleanSelector(),
                vol.Required(CONF_ADD_ANOTHER, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="rooms",
            data_schema=schema,
            errors=errors,
            description_placeholders={"num_rooms": str(len(self._rooms))},
        )

    # -- Step 3: per-room entity mapping ------------------------------------

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: map source entities for the current room.

        The two global entities (outdoor temperature and mode input) are shown
        only for the first room.
        """
        errors: dict[str, str] = {}
        room = self._rooms[self._entity_room_idx]
        room_name = str(room[CONF_ROOM_NAME])
        is_first = self._entity_room_idx == 0

        if user_input is not None:
            validator = EntityValidator(self.hass)
            temp_room = str(user_input.get(CONF_ENTITY_TEMP_ROOM, ""))
            humidity = str(user_input.get(CONF_ENTITY_HUMIDITY, ""))
            valves = _as_entity_list(user_input.get(CONF_ENTITY_VALVES))
            supply = _as_entity_list(user_input.get(CONF_ENTITY_SUPPLY))
            returns = _as_entity_list(user_input.get(CONF_ENTITY_RETURN))
            fast_source = str(user_input.get(CONF_ENTITY_FAST_SOURCE, ""))
            outdoor = str(user_input.get(CONF_ENTITY_TEMP_OUTDOOR, ""))
            mode_entity = str(user_input.get(CONF_ENTITY_MODE, ""))

            first_error = _first_entity_error(
                validator,
                temp_room=temp_room,
                humidity=humidity,
                valves=valves,
                supply=supply,
                returns=returns,
                fast_source=fast_source,
                outdoor=outdoor if is_first else "",
                cooling_enabled=bool(room.get(CONF_COOLING_ENABLED)),
            )
            if first_error is not None:
                errors["base"] = first_error

            if not errors:
                room[CONF_ENTITY_TEMP_ROOM] = temp_room
                room[CONF_ENTITY_HUMIDITY] = humidity
                room[CONF_ENTITY_VALVES] = valves
                room[CONF_ENTITY_SUPPLY] = supply
                room[CONF_ENTITY_RETURN] = returns
                room[CONF_ROOM_OFFSET] = DEFAULT_ROOM_OFFSET_C
                if room.get(CONF_HAS_FAST_SOURCE):
                    room[CONF_ENTITY_FAST_SOURCE] = fast_source
                if is_first:
                    self._global = {
                        CONF_ENTITY_TEMP_OUTDOOR: outdoor,
                        CONF_ENTITY_MODE: mode_entity,
                    }

                self._entity_room_idx += 1
                if self._entity_room_idx < len(self._rooms):
                    return await self.async_step_entities()
                return await self.async_step_algorithm()

        schema_dict = _entities_schema_dict(
            has_fast_source=bool(room.get(CONF_HAS_FAST_SOURCE)),
            include_globals=is_first,
        )

        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"room_name": room_name},
        )

    # -- Step 4: algorithm knobs --------------------------------------------

    async def async_step_algorithm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 4: advanced controller tuning (optional; core defaults)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                controller = _parse_controller(user_input)
            except ValueError as err:
                _LOGGER.warning("Invalid controller tuning: %s", err)
                errors["base"] = "invalid_controller"
            else:
                self._controller = asdict(controller)
                return await self.async_step_confirm()

        schema = vol.Schema(_controller_schema_dict(ControllerConfig()))
        return self.async_show_form(
            step_id="algorithm", data_schema=schema, errors=errors
        )

    # -- Step 5: confirm ----------------------------------------------------

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 5: set the unique id and create the config entry."""
        if user_input is not None:
            location = LocationInput(
                latitude=float(self._location[CONF_LATITUDE]),
                longitude=float(self._location[CONF_LONGITUDE]),
            )
            await self.async_set_unique_id(location.unique_id)
            self._abort_if_unique_id_configured()

            outdoor = self._global.get(CONF_ENTITY_TEMP_OUTDOOR, "")
            rooms: list[dict[str, Any]] = []
            for room in self._rooms:
                # The outdoor sensor is global; expose it to every room so the
                # coordinator (which reads it per room) sees it uniformly.
                rooms.append({**room, CONF_ENTITY_TEMP_OUTDOOR: outdoor})

            data: dict[str, Any] = {
                CONF_LATITUDE: location.latitude,
                CONF_LONGITUDE: location.longitude,
                CONF_HOME_SETPOINT: DEFAULT_HOME_SETPOINT_C,
                CONF_ENTITY_MODE: self._global.get(CONF_ENTITY_MODE, ""),
                CONF_ROOMS: rooms,
                CONF_CONTROLLER: self._controller,
            }
            return self.async_create_entry(title="Tortoise-UFH", data=data)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "num_rooms": str(len(self._rooms)),
                "latitude": str(self._location.get(CONF_LATITUDE, "")),
                "longitude": str(self._location.get(CONF_LONGITUDE, "")),
                "home_setpoint": str(DEFAULT_HOME_SETPOINT_C),
            },
        )


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


class TortoiseUfhOptionsFlow(OptionsFlow):
    """Options flow: a menu over room management and control settings.

    The entry point (:meth:`async_step_init`) is a menu with four leaves:

    * ``add_room`` — collect a new room's attributes then its source entities,
      append it to ``entry.data[CONF_ROOMS]`` and reload.
    * ``edit_room`` — pick an existing room, then edit its attributes (name
      immutable) and entity mapping in place.
    * ``remove_room`` — pick a room, remove it from ``entry.data[CONF_ROOMS]``,
      delete its orphaned entity-registry entries and prune its per-room state.
    * ``settings`` — per-room control-state selects (off / shadow / live) and the
      advanced :class:`ControllerConfig` knobs (the original options form).

    Room definitions live in ``entry.data`` (not ``entry.options``); the room
    leaves therefore persist through ``async_update_entry`` with a fresh
    ``CONF_ROOMS`` list, which fires the integration's update listener and
    reloads the entry (rebuilding the coordinator and re-syncing entities). Each
    leaf uses a single save/return point so exactly one reload is triggered.
    """

    # Room being added / edited, carried across a leaf's multiple steps.
    _pending_room: dict[str, Any]
    # Index of the room being edited in CONF_ROOMS, or None while adding.
    _pending_index: int | None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the top-level options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_room", "edit_room", "remove_room", "settings"],
        )

    # -- Leaf: control & tuning settings (the original options form) --------

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Per-room control-state selects and advanced controller knobs.

        Provides an emergency / automation-free surface for the per-room control
        state (off / shadow / live) alongside the advanced
        :class:`ControllerConfig` knobs; the primary control surface is the
        panel and the per-room ``select`` entities. Writes the state map to
        ``entry.options[CONF_ROOM_STATE]``.
        """
        entry = self.config_entry
        rooms: list[dict[str, Any]] = list(entry.data.get(CONF_ROOMS, []))
        errors: dict[str, str] = {}

        current_defaults = self._current_controller(entry)

        if user_input is not None:
            try:
                controller = _parse_controller(user_input)
            except ValueError as err:
                _LOGGER.warning("Invalid controller tuning: %s", err)
                errors["base"] = "invalid_controller"
            else:
                room_states: dict[str, str] = {}
                for idx, room_cfg in enumerate(rooms):
                    room_name = str(room_cfg[CONF_ROOM_NAME])
                    key = self._state_key(idx)
                    state = str(user_input.get(key, DEFAULT_ROOM_STATE))
                    room_states[room_name] = (
                        state if state in ROOM_STATES else DEFAULT_ROOM_STATE
                    )
                # Merge over the existing options so surfaces this form does not
                # manage (notably the sparse per-room tuning map CONF_ROOM_TUNING,
                # set from the panel) are preserved rather than wiped: an options
                # flow's async_create_entry REPLACES entry.options wholesale.
                return self.async_create_entry(
                    title="",
                    data={
                        **entry.options,
                        CONF_ROOM_STATE: room_states,
                        CONF_CONTROLLER: asdict(controller),
                    },
                )

        current_states: dict[str, Any] = entry.options.get(CONF_ROOM_STATE, {})
        schema_dict: dict[Any, Any] = {}
        for idx, room_cfg in enumerate(rooms):
            room_name = str(room_cfg[CONF_ROOM_NAME])
            default_state = str(current_states.get(room_name, DEFAULT_ROOM_STATE))
            if default_state not in ROOM_STATES:
                default_state = DEFAULT_ROOM_STATE
            schema_dict[vol.Optional(self._state_key(idx), default=default_state)] = (
                SelectSelector(
                    SelectSelectorConfig(
                        options=list(ROOM_STATES),
                        translation_key="control_state",
                    )
                )
            )
        schema_dict.update(_controller_schema_dict(current_defaults))

        return self.async_show_form(
            step_id="settings", data_schema=vol.Schema(schema_dict), errors=errors
        )

    # -- Leaf: add a room ---------------------------------------------------

    async def async_step_add_room(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect a new room's attributes, then advance to its entity mapping."""
        entry = self.config_entry
        rooms: list[dict[str, Any]] = list(entry.data.get(CONF_ROOMS, []))
        errors: dict[str, str] = {}

        if user_input is not None:
            name = str(user_input.get(CONF_ROOM_NAME, "")).strip()
            has_fast = bool(user_input.get(CONF_HAS_FAST_SOURCE, False))
            raw_kind = str(
                user_input.get(CONF_FAST_SOURCE_KIND, DEFAULT_FAST_SOURCE_KIND)
            )
            kind = raw_kind if has_fast else FAST_SOURCE_KIND_NONE

            if not name:
                errors["base"] = "empty_room_name"
            elif any(str(r[CONF_ROOM_NAME]) == name for r in rooms):
                errors["base"] = "duplicate_room_name"

            room: RoomDefinition | None = None
            if not errors:
                try:
                    room = RoomDefinition(
                        name=name,
                        area_m2=float(user_input.get(CONF_ROOM_AREA, 0.0)),
                        has_fast_source=has_fast,
                        fast_source_kind=kind,
                        cooling_enabled=bool(
                            user_input.get(
                                CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED
                            )
                        ),
                    )
                except ValueError as err:
                    _LOGGER.warning("Invalid room definition: %s", err)
                    errors["base"] = "invalid_room"

            if not errors and room is not None:
                self._pending_index = None
                self._pending_room = {
                    **room.as_dict(),
                    CONF_ROOM_OFFSET: float(
                        user_input.get(CONF_ROOM_OFFSET, DEFAULT_ROOM_OFFSET_C)
                    ),
                }
                return await self.async_step_room_entities()

        return self.async_show_form(
            step_id="add_room",
            data_schema=vol.Schema(_room_attributes_schema_dict(include_name=True)),
            errors=errors,
        )

    # -- Leaf: edit a room --------------------------------------------------

    async def async_step_edit_room(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick an existing room to edit."""
        entry = self.config_entry
        rooms: list[dict[str, Any]] = list(entry.data.get(CONF_ROOMS, []))
        if not rooms:
            return self.async_abort(reason="no_rooms")
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = str(user_input.get(CONF_SELECTED_ROOM, ""))
            index = next(
                (i for i, r in enumerate(rooms) if str(r[CONF_ROOM_NAME]) == selected),
                None,
            )
            if index is None:
                errors["base"] = "invalid_room"
            else:
                self._pending_index = index
                self._pending_room = dict(rooms[index])
                return await self.async_step_edit_room_attrs()

        names = [str(r[CONF_ROOM_NAME]) for r in rooms]
        return self.async_show_form(
            step_id="edit_room",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SELECTED_ROOM): SelectSelector(
                        SelectSelectorConfig(options=names)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_edit_room_attrs(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Edit the picked room's attributes (its name is immutable)."""
        room = self._pending_room
        room_name = str(room[CONF_ROOM_NAME])
        errors: dict[str, str] = {}

        if user_input is not None:
            has_fast = bool(user_input.get(CONF_HAS_FAST_SOURCE, False))
            raw_kind = str(
                user_input.get(CONF_FAST_SOURCE_KIND, DEFAULT_FAST_SOURCE_KIND)
            )
            kind = raw_kind if has_fast else FAST_SOURCE_KIND_NONE

            room_def: RoomDefinition | None = None
            try:
                room_def = RoomDefinition(
                    name=room_name,
                    area_m2=float(user_input.get(CONF_ROOM_AREA, 0.0)),
                    has_fast_source=has_fast,
                    fast_source_kind=kind,
                    cooling_enabled=bool(
                        user_input.get(CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED)
                    ),
                )
            except ValueError as err:
                _LOGGER.warning("Invalid room definition: %s", err)
                errors["base"] = "invalid_room"

            if not errors and room_def is not None:
                # The offset field is add-only (see _room_attributes_schema_dict):
                # ``**room`` carries the existing room's offset forward unchanged,
                # since it is runtime state owned by the number entity / Store.
                self._pending_room = {
                    **room,
                    **room_def.as_dict(),
                }
                return await self.async_step_room_entities()

        return self.async_show_form(
            step_id="edit_room_attrs",
            data_schema=vol.Schema(
                _room_attributes_schema_dict(
                    include_name=False, include_offset=False, defaults=room
                )
            ),
            errors=errors,
            description_placeholders={"room_name": room_name},
        )

    # -- Shared: entity mapping for the pending (added / edited) room -------

    async def async_step_room_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Map source entities for the pending room, then save and reload."""
        entry = self.config_entry
        room = self._pending_room
        room_name = str(room[CONF_ROOM_NAME])
        has_fast_source = bool(
            room.get(
                CONF_HAS_FAST_SOURCE,
                str(room.get(CONF_FAST_SOURCE_KIND, FAST_SOURCE_KIND_NONE))
                != FAST_SOURCE_KIND_NONE,
            )
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            validator = EntityValidator(self.hass)
            temp_room = str(user_input.get(CONF_ENTITY_TEMP_ROOM, ""))
            humidity = str(user_input.get(CONF_ENTITY_HUMIDITY, ""))
            valves = _as_entity_list(user_input.get(CONF_ENTITY_VALVES))
            supply = _as_entity_list(user_input.get(CONF_ENTITY_SUPPLY))
            returns = _as_entity_list(user_input.get(CONF_ENTITY_RETURN))
            fast_source = str(user_input.get(CONF_ENTITY_FAST_SOURCE, ""))

            error = _first_entity_error(
                validator,
                temp_room=temp_room,
                humidity=humidity,
                valves=valves,
                supply=supply,
                returns=returns,
                fast_source=fast_source,
                outdoor="",
                cooling_enabled=bool(room.get(CONF_COOLING_ENABLED)),
            )
            if error is not None:
                errors["base"] = error

            if not errors:
                updated = dict(room)
                updated[CONF_ENTITY_TEMP_ROOM] = temp_room
                updated[CONF_ENTITY_HUMIDITY] = humidity
                updated[CONF_ENTITY_VALVES] = valves
                updated[CONF_ENTITY_SUPPLY] = supply
                updated[CONF_ENTITY_RETURN] = returns
                updated[CONF_ENTITY_TEMP_OUTDOOR] = self._existing_outdoor(entry)
                if has_fast_source:
                    updated[CONF_ENTITY_FAST_SOURCE] = fast_source
                else:
                    updated.pop(CONF_ENTITY_FAST_SOURCE, None)

                rooms: list[dict[str, Any]] = list(entry.data.get(CONF_ROOMS, []))
                if self._pending_index is None:
                    rooms.append(updated)
                else:
                    rooms[self._pending_index] = updated
                return self._save_rooms(rooms)

        return self.async_show_form(
            step_id="room_entities",
            data_schema=vol.Schema(
                _entities_schema_dict(
                    has_fast_source=has_fast_source,
                    include_globals=False,
                    defaults=room,
                )
            ),
            errors=errors,
            description_placeholders={"room_name": room_name},
        )

    # -- Leaf: remove a room ------------------------------------------------

    async def async_step_remove_room(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick a room to remove; clean up its entities and per-room state."""
        entry = self.config_entry
        rooms: list[dict[str, Any]] = list(entry.data.get(CONF_ROOMS, []))
        if not rooms:
            return self.async_abort(reason="no_rooms")
        if len(rooms) <= 1:
            return self.async_abort(reason="cannot_remove_last_room")
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = str(user_input.get(CONF_SELECTED_ROOM, ""))
            index = next(
                (i for i, r in enumerate(rooms) if str(r[CONF_ROOM_NAME]) == selected),
                None,
            )
            if index is None:
                errors["base"] = "invalid_room"
            else:
                all_names = [str(r[CONF_ROOM_NAME]) for r in rooms]
                removed_name = all_names[index]
                self._async_cleanup_room_entities(removed_name, all_names)
                await self._async_prune_room_setpoint(removed_name)
                remaining = [r for i, r in enumerate(rooms) if i != index]
                room_state = {
                    name: str(value)
                    for name, value in entry.options.get(CONF_ROOM_STATE, {}).items()
                    if name != removed_name
                }
                new_options = {**entry.options, CONF_ROOM_STATE: room_state}
                return self._save_rooms(remaining, options=new_options)

        names = [str(r[CONF_ROOM_NAME]) for r in rooms]
        return self.async_show_form(
            step_id="remove_room",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SELECTED_ROOM): SelectSelector(
                        SelectSelectorConfig(options=names)
                    )
                }
            ),
            errors=errors,
        )

    # -- Internal: persistence + cleanup helpers ----------------------------

    def _save_rooms(
        self, rooms: list[dict[str, Any]], *, options: dict[str, Any] | None = None
    ) -> FlowResult:
        """Persist a replacement room list (single reload) and finish the flow.

        Writes the new ``CONF_ROOMS`` back to ``entry.data`` — and any changed
        options in the *same* ``async_update_entry`` call, so exactly one update
        (one reload) fires — then terminates the options flow re-affirming those
        options. Because the terminal write sets ``entry.options`` to a value it
        already holds, it is a no-op that does not trigger a second reload.

        Args:
            rooms: The full replacement room list for ``entry.data``.
            options: Replacement ``entry.options`` when a leaf also changed them
                (e.g. remove-room pruning ``live_control``); ``None`` keeps the
                current options unchanged.

        Returns:
            The terminal ``CREATE_ENTRY`` flow result.
        """
        entry = self.config_entry
        new_options = dict(entry.options) if options is None else options
        self.hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_ROOMS: rooms},
            options=new_options,
        )
        return self.async_create_entry(title="", data=new_options)

    @staticmethod
    def _existing_outdoor(entry: ConfigEntry) -> str:
        """Return the shared outdoor-temperature entity id, or ``""``.

        The outdoor sensor is a single global entity fanned out into every room
        dict; a newly added / edited room inherits the same id.

        Args:
            entry: The config entry holding the room list.

        Returns:
            The first non-empty ``entity_temp_outdoor`` across rooms, else ``""``.
        """
        for room_cfg in entry.data.get(CONF_ROOMS, []):
            outdoor = str(room_cfg.get(CONF_ENTITY_TEMP_OUTDOOR, "") or "")
            if outdoor:
                return outdoor
        return ""

    def _async_cleanup_room_entities(
        self, removed_name: str, all_room_names: list[str]
    ) -> None:
        """Delete the entry's entity-registry entries for a removed room.

        Attributes each of the entry's registry entries to the room whose slug
        (``name.lower().replace(" ", "_")``) is the longest ``"{slug}_"`` prefix
        of the unique id's per-room segment, and removes only those belonging to
        the removed room. This disambiguates slug-prefix collisions ("Salon" vs
        "Salon 2"); global entities are protected by :data:`_GLOBAL_UNIQUE_ID_KEYS`
        so a room named e.g. "Home" never deletes the global ``home_temperature``.

        Removing a currently-loaded entity's registry entry tears the live entity
        down cleanly; the reload that follows re-creates only the surviving rooms.

        Args:
            removed_name: The name of the room being removed.
            all_room_names: Every room name (removed + remaining) for longest-slug
                disambiguation.
        """
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        entry = self.config_entry
        entry_prefix = f"{entry.entry_id}_"
        removed_slug = removed_name.lower().replace(" ", "_")
        slugs = [name.lower().replace(" ", "_") for name in all_room_names]

        reg_entries = er.async_entries_for_config_entry(registry, entry.entry_id)
        for reg_entry in list(reg_entries):
            unique_id = reg_entry.unique_id or ""
            if not unique_id.startswith(entry_prefix):
                continue
            rest = unique_id[len(entry_prefix) :]
            if rest in _GLOBAL_UNIQUE_ID_KEYS:
                continue
            best_slug: str | None = None
            for slug in slugs:
                if rest.startswith(f"{slug}_") and (
                    best_slug is None or len(slug) > len(best_slug)
                ):
                    best_slug = slug
            if best_slug is not None and best_slug == removed_slug:
                registry.async_remove(reg_entry.entity_id)

    async def _async_prune_room_setpoint(self, removed_name: str) -> None:
        """Drop the removed room's offset from the private setpoint Store.

        The per-room offset persists in a coordinator-owned
        :class:`~homeassistant.helpers.storage.Store` keyed by room name. A
        rebuilt coordinator already ignores offsets for unknown rooms, so this is
        a best-effort tidy-up; any failure is logged and swallowed.

        Args:
            removed_name: The name of the room being removed.
        """
        from homeassistant.helpers.storage import Store

        entry = self.config_entry
        store: Store[dict[str, Any]] = Store(
            self.hass,
            _SETPOINT_STORE_VERSION,
            f"{DOMAIN}.setpoints.{entry.entry_id}",
        )
        try:
            stored = await store.async_load()
            if not stored:
                return
            offsets = stored.get(CONF_ROOM_OFFSET)
            if isinstance(offsets, dict) and removed_name in offsets:
                del offsets[removed_name]
                await store.async_save(stored)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to prune setpoint store for removed room %s",
                removed_name,
                exc_info=True,
            )

    @staticmethod
    def _state_key(index: int) -> str:
        """Return the per-room control-state form key.

        Keyed on the room's positional index (not a lossy slug of its name) so
        the key is guaranteed unique even when two rooms have names that would
        collapse to the same slug (e.g. ``"Kids Room"`` / ``"Kids_Room"``).

        Args:
            index: The room's position in the ``CONF_ROOMS`` list.

        Returns:
            A per-room form key, e.g. ``"room_state_0"``.
        """
        return f"room_state_{index}"

    @staticmethod
    def _current_controller(entry: ConfigEntry) -> ControllerConfig:
        """Resolve the current controller defaults for the options form.

        Merges the serialised knobs from ``entry.data`` (setup) with any override
        in ``entry.options``. Falls back to :class:`ControllerConfig` defaults if
        the stored values are absent or invalid.

        Args:
            entry: The config entry.

        Returns:
            The current :class:`ControllerConfig`.
        """
        merged: dict[str, Any] = {
            **entry.data.get(CONF_CONTROLLER, {}),
            **entry.options.get(CONF_CONTROLLER, {}),
        }
        try:
            return ControllerConfig(**merged)
        except (TypeError, ValueError):
            return ControllerConfig()
