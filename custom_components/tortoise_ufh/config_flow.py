"""Config flow for the Tortoise-UFH integration.

Implements the multi-step setup wizard (``VERSION = 1``) and the options flow.

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

from tortoise_ufh.config import ControllerConfig

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
    CONF_KILL_SWITCH,
    CONF_LIVE_CONTROL,
    CONF_PARTICIPATES,
    CONF_ROOM_AREA,
    CONF_ROOM_NAME,
    CONF_ROOM_OFFSET,
    CONF_ROOMS,
    DEFAULT_COOLING_ENABLED,
    DEFAULT_FAST_SOURCE_KIND,
    DEFAULT_HOME_SETPOINT_C,
    DEFAULT_KILL_SWITCH,
    DEFAULT_LIVE_CONTROL,
    DEFAULT_PARTICIPATES,
    DEFAULT_ROOM_OFFSET_C,
    DOMAIN,
    FAST_SOURCE_KIND_NONE,
    FAST_SOURCE_KINDS,
    VALID_PERCENT_UNITS,
    VALID_TEMP_UNITS,
)
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

# ---------------------------------------------------------------------------
# Advanced controller-knob NumberSelector specs: (field, min, max, step)
# ---------------------------------------------------------------------------

_CONTROLLER_NUMBER_KNOBS: tuple[tuple[str, float, float, float], ...] = (
    ("kp", 0.0, 50.0, 0.1),
    ("ki", 0.0, 1.0, 0.001),
    ("kt", 0.0, 50.0, 0.1),
    ("deadband_c", 0.0, 5.0, 0.1),
    ("valve_floor_pct", 0.0, 100.0, 1.0),
    ("boost_offset_c", 0.0, 10.0, 0.1),
    ("fast_min_on_minutes", 0.0, 60.0, 1.0),
    ("fast_min_off_minutes", 0.0, 60.0, 1.0),
    ("dew_margin_k", 0.0, 10.0, 0.1),
    ("dew_ramp_k", 0.1, 10.0, 0.1),
)
"""Numeric :class:`ControllerConfig` fields exposed as advanced knobs.

Units: ``kp`` in %/K, ``ki`` in %/(K*s), ``kt`` in %/(K/h), ``*_c`` / ``*_k``
in kelvin, ``valve_floor_pct`` in percent, ``*_minutes`` in minutes.

Note: the derivative gain ``kd`` is deliberately not exposed — Aneks §8.3
forbids a derivative-on-error term, so ``ControllerConfig.kd`` stays at its
``0.0`` default.
"""

_CONTROLLER_BOOL_KNOB: str = "outdoor_ff_enabled"
"""Boolean :class:`ControllerConfig` field: outdoor-temperature feedforward."""


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
    for field_name, low, high, step in _CONTROLLER_NUMBER_KNOBS:
        default_val = float(getattr(defaults, field_name))
        schema[vol.Optional(field_name, default=default_val)] = NumberSelector(
            NumberSelectorConfig(
                min=low, max=high, step=step, mode=NumberSelectorMode.BOX
            )
        )
    schema[vol.Optional(_CONTROLLER_BOOL_KNOB, default=defaults.outdoor_ff_enabled)] = (
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
        for field_name, _low, _high, _step in _CONTROLLER_NUMBER_KNOBS
        if field_name in user_input
    }
    if _CONTROLLER_BOOL_KNOB in user_input:
        kwargs[_CONTROLLER_BOOL_KNOB] = bool(user_input[_CONTROLLER_BOOL_KNOB])
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


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class TortoiseUfhConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step setup wizard for Tortoise-UFH."""

    VERSION = 1

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
                _validate_entities(validator, valves),
                _validate_entities(
                    validator,
                    supply,
                    valid_units=VALID_TEMP_UNITS,
                    device_class="temperature",
                ),
                _validate_entities(
                    validator,
                    returns,
                    valid_units=VALID_TEMP_UNITS,
                    device_class="temperature",
                ),
                _validate_entities(validator, [fast_source] if fast_source else []),
            ]
            if is_first and outdoor:
                checks.append(
                    _validate_entities(
                        validator,
                        [outdoor],
                        valid_units=VALID_TEMP_UNITS,
                        device_class="temperature",
                    )
                )
            first_error = next((key for key in checks if key is not None), None)
            if first_error is not None:
                errors["base"] = first_error

            if not valves:
                errors["base"] = "valve_required"

            if room.get(CONF_COOLING_ENABLED) and not humidity:
                errors["base"] = "humidity_required"

            if not errors:
                room[CONF_ENTITY_TEMP_ROOM] = temp_room
                room[CONF_ENTITY_HUMIDITY] = humidity
                room[CONF_ENTITY_VALVES] = valves
                room[CONF_ENTITY_SUPPLY] = supply
                room[CONF_ENTITY_RETURN] = returns
                room[CONF_ROOM_OFFSET] = DEFAULT_ROOM_OFFSET_C
                room[CONF_PARTICIPATES] = DEFAULT_PARTICIPATES
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

        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_ENTITY_TEMP_ROOM): EntitySelector(
                EntitySelectorConfig(domain=["sensor"], device_class=["temperature"])
            ),
            vol.Optional(CONF_ENTITY_HUMIDITY): EntitySelector(
                EntitySelectorConfig(domain=["sensor"], device_class=["humidity"])
            ),
            vol.Required(CONF_ENTITY_VALVES): EntitySelector(
                EntitySelectorConfig(domain=["number"], multiple=True)
            ),
            vol.Optional(CONF_ENTITY_SUPPLY): EntitySelector(
                EntitySelectorConfig(
                    domain=["sensor"],
                    device_class=["temperature"],
                    multiple=True,
                )
            ),
            vol.Optional(CONF_ENTITY_RETURN): EntitySelector(
                EntitySelectorConfig(
                    domain=["sensor"],
                    device_class=["temperature"],
                    multiple=True,
                )
            ),
        }
        if room.get(CONF_HAS_FAST_SOURCE):
            schema_dict[vol.Optional(CONF_ENTITY_FAST_SOURCE)] = EntitySelector(
                EntitySelectorConfig(domain=["climate"])
            )
        if is_first:
            schema_dict[vol.Optional(CONF_ENTITY_TEMP_OUTDOOR)] = EntitySelector(
                EntitySelectorConfig(domain=["sensor"], device_class=["temperature"])
            )
            schema_dict[vol.Optional(CONF_ENTITY_MODE)] = EntitySelector(
                EntitySelectorConfig(domain=["select", "input_select"])
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
    """Options flow: per-room live control, kill switch, advanced knobs.

    A single ``init`` step renders one live-control :class:`BooleanSelector` per
    room (the shadow -> live transition), the global kill-switch toggle, and the
    advanced :class:`ControllerConfig` knobs (defaults sourced from the entry's
    current controller options / data). Saving reloads the entry via the
    integration's update listener.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the single options step."""
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
                live_control: dict[str, bool] = {}
                for idx, room_cfg in enumerate(rooms):
                    room_name = str(room_cfg[CONF_ROOM_NAME])
                    key = self._live_key(idx)
                    live_control[room_name] = bool(user_input.get(key, False))
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_LIVE_CONTROL: live_control,
                        CONF_KILL_SWITCH: bool(
                            user_input.get(CONF_KILL_SWITCH, DEFAULT_KILL_SWITCH)
                        ),
                        CONF_CONTROLLER: asdict(controller),
                    },
                )

        current_live: dict[str, Any] = entry.options.get(CONF_LIVE_CONTROL, {})
        schema_dict: dict[Any, Any] = {}
        for idx, room_cfg in enumerate(rooms):
            room_name = str(room_cfg[CONF_ROOM_NAME])
            default_live = bool(current_live.get(room_name, DEFAULT_LIVE_CONTROL))
            schema_dict[vol.Optional(self._live_key(idx), default=default_live)] = (
                BooleanSelector()
            )
        schema_dict[
            vol.Optional(
                CONF_KILL_SWITCH,
                default=bool(entry.options.get(CONF_KILL_SWITCH, DEFAULT_KILL_SWITCH)),
            )
        ] = BooleanSelector()
        schema_dict.update(_controller_schema_dict(current_defaults))

        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema_dict), errors=errors
        )

    @staticmethod
    def _live_key(index: int) -> str:
        """Return the per-room live-control form key.

        Keyed on the room's positional index (not a lossy slug of its name) so
        the key is guaranteed unique even when two rooms have names that would
        collapse to the same slug (e.g. ``"Kids Room"`` / ``"Kids_Room"``).

        Args:
            index: The room's position in the ``CONF_ROOMS`` list.

        Returns:
            A per-room form key, e.g. ``"enable_live_control_0"``.
        """
        return f"enable_live_control_{index}"

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
