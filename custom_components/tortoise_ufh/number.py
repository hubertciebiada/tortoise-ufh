"""Writable number entities for the Tortoise-UFH integration.

Two kinds of user-facing setpoint controls, both backed by the coordinator's
authoritative setpoint state (there is no separate storage here):

* ONE global **home temperature** [degrees Celsius], range 5..30, step 0.5,
  read via :meth:`TortoiseUfhCoordinator.get_home_temperature` and written via
  :meth:`TortoiseUfhCoordinator.set_home_temperature`.
* ONE per-room **offset** [K] from the home temperature, range -5..+5, step 0.5,
  read via :meth:`TortoiseUfhCoordinator.get_room_offset` and written via
  :meth:`TortoiseUfhCoordinator.set_room_offset`.

The room's effective target is ``home_temperature + room_offset`` [degrees
Celsius]; the coordinator setters rebroadcast immediately so every entity sees
the new target before the next 5-minute refresh. These controls are the setpoint
source of truth exposed to the user.

Units: home temperature in degrees Celsius; offset in kelvin (a temperature
difference); step in the same unit as the value.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ROOM_NAME,
    CONF_ROOMS,
    HOME_SETPOINT_MAX_C,
    HOME_SETPOINT_MIN_C,
    HOME_SETPOINT_STEP_C,
    ROOM_OFFSET_MAX_C,
    ROOM_OFFSET_MIN_C,
    ROOM_OFFSET_STEP_C,
)
from .coordinator import TortoiseUfhCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import TortoiseUfhConfigEntry


# ---------------------------------------------------------------------------
# Entity description (frozen, self-validating)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TortoiseUfhNumberEntityDescription(NumberEntityDescription):
    """Describes one writable Tortoise-UFH number entity.

    Extends the Home Assistant :class:`NumberEntityDescription` with the getter
    and setter that bind the entity to the coordinator's setpoint state.

    Attributes:
        value_fn: Reads the current value from the coordinator. Receives the
            coordinator and the room name (``None`` for global entities); returns
            the value in the description's unit (degrees Celsius or kelvin).
        set_fn: Writes a new value through the coordinator. Receives the
            coordinator, the room name (``None`` for global entities) and the new
            value in the description's unit.
        is_global: ``True`` for a single building-wide entity (room name
            ``None``); ``False`` for one entity per room.

    Raises:
        ValueError: If the min/max/step bounds are missing, non-finite, or
            inconsistent (``native_min_value >= native_max_value`` or
            ``native_step <= 0``).
    """

    value_fn: Callable[[TortoiseUfhCoordinator, str | None], float]
    set_fn: Callable[[TortoiseUfhCoordinator, str | None, float], None]
    is_global: bool = False

    def __post_init__(self) -> None:
        """Validate the numeric bounds baked into this description."""
        lo = self.native_min_value
        hi = self.native_max_value
        step = self.native_step
        if lo is None or hi is None or step is None:
            msg = (
                "native_min_value, native_max_value and native_step are required, "
                f"got min={lo}, max={hi}, step={step}"
            )
            raise ValueError(msg)
        if not (math.isfinite(lo) and math.isfinite(hi) and math.isfinite(step)):
            msg = f"native bounds must be finite, got min={lo}, max={hi}, step={step}"
            raise ValueError(msg)
        if lo >= hi:
            msg = f"native_min_value ({lo}) must be < native_max_value ({hi})"
            raise ValueError(msg)
        if step <= 0.0:
            msg = f"native_step must be positive, got {step}"
            raise ValueError(msg)


# The single global home-temperature control [degrees Celsius].
HOME_TEMPERATURE_DESCRIPTION: TortoiseUfhNumberEntityDescription = (
    TortoiseUfhNumberEntityDescription(
        key="home_temperature",
        translation_key="home_temperature",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=HOME_SETPOINT_MIN_C,
        native_max_value=HOME_SETPOINT_MAX_C,
        native_step=HOME_SETPOINT_STEP_C,
        mode=NumberMode.BOX,
        is_global=True,
        value_fn=lambda coordinator, _room: coordinator.get_home_temperature(),
        set_fn=lambda coordinator, _room, value: coordinator.set_home_temperature(
            value
        ),
    )
)

# The per-room offset control [K] from the home temperature.
ROOM_OFFSET_DESCRIPTION: TortoiseUfhNumberEntityDescription = (
    TortoiseUfhNumberEntityDescription(
        key="offset",
        translation_key="offset",
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        entity_category=EntityCategory.CONFIG,
        native_min_value=ROOM_OFFSET_MIN_C,
        native_max_value=ROOM_OFFSET_MAX_C,
        native_step=ROOM_OFFSET_STEP_C,
        mode=NumberMode.BOX,
        is_global=False,
        value_fn=lambda coordinator, room: (
            coordinator.get_room_offset(room) if room is not None else 0.0
        ),
        set_fn=lambda coordinator, room, value: (
            coordinator.set_room_offset(room, value) if room is not None else None
        ),
    )
)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TortoiseUfhConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tortoise-UFH number entities from a config entry.

    Builds one global home-temperature entity plus one offset entity per
    configured room (from ``entry.data[CONF_ROOMS]``).

    Args:
        hass: The Home Assistant instance (unused; entities read the
            coordinator).
        entry: The config entry holding the coordinator and room configs.
        async_add_entities: Callback to register the created entities.
    """
    coordinator = entry.runtime_data.coordinator

    entities: list[TortoiseUfhNumberEntity] = [
        TortoiseUfhNumberEntity(
            coordinator=coordinator,
            description=HOME_TEMPERATURE_DESCRIPTION,
            entry_id=entry.entry_id,
            room_name=None,
        )
    ]

    rooms: list[dict[str, Any]] = list(entry.data.get(CONF_ROOMS, []) or [])
    for room_cfg in rooms:
        room_name = str(room_cfg[CONF_ROOM_NAME])
        entities.append(
            TortoiseUfhNumberEntity(
                coordinator=coordinator,
                description=ROOM_OFFSET_DESCRIPTION,
                entry_id=entry.entry_id,
                room_name=room_name,
            )
        )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Number entity
# ---------------------------------------------------------------------------


class TortoiseUfhNumberEntity(CoordinatorEntity[TortoiseUfhCoordinator], NumberEntity):
    """A writable Tortoise-UFH setpoint control (home temperature or offset)."""

    entity_description: TortoiseUfhNumberEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TortoiseUfhCoordinator,
        description: TortoiseUfhNumberEntityDescription,
        entry_id: str,
        room_name: str | None,
    ) -> None:
        """Initialise a writable number entity.

        Args:
            coordinator: The Tortoise-UFH data coordinator (setpoint source of
                truth).
            description: The entity description binding value/set functions and
                the numeric bounds.
            entry_id: The config entry id, used to build a stable ``unique_id``.
            room_name: The room name for a per-room entity, or ``None`` for the
                global home-temperature entity.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._room_name = room_name

        pretty_key = description.key.replace("_", " ")
        if room_name is None:
            self._attr_unique_id = f"{entry_id}_{description.key}"
            self._attr_name = f"Tortoise-UFH {pretty_key}"
        else:
            safe_room = room_name.lower().replace(" ", "_")
            self._attr_unique_id = f"{entry_id}_{safe_room}_{description.key}"
            self._attr_name = f"{room_name} {pretty_key}"

    @property
    def native_value(self) -> float:
        """Return the current value in the description's unit (degC or K)."""
        return self.entity_description.value_fn(self.coordinator, self._room_name)

    async def async_set_native_value(self, value: float) -> None:
        """Write a new value through the coordinator and refresh state.

        Args:
            value: The new value in the description's unit (degrees Celsius for
                the home temperature, kelvin for a room offset).
        """
        self.entity_description.set_fn(self.coordinator, self._room_name, value)
        self.async_write_ha_state()
