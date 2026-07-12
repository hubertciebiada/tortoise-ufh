"""Select entities for the Tortoise-UFH integration.

Exposes one per-room **control-state** select (``off`` / ``live``),
backed by the coordinator's single-source-of-truth control-state map:

* ``off``  — the room does not participate in control (the core sees
  ``Mode.OFF``; the valve is held and the fast source idled). Nothing is
  written, so the physical actuators stay untouched.
* ``live`` — the room is computed, reported *and* its valve / fast-source
  commands are written to the actuators.

Selecting an option updates the coordinator's in-memory state immediately (via
its ``@callback`` setter, so the running control loop and the panel see the
change at once) and persists the new value into ``entry.options`` so the choice
survives a Home Assistant restart. A control-state-only options change does not
reload the config entry (see ``coordinator.options_require_reload`` and
``__init__._async_update_listener``), so the PID integrator is preserved.

This native select replaces the retired per-room ``live_control`` switch and the
global kill-switch: a whole-home stop is now "every room off". (The former
third state ``shadow`` — compute but never write — was removed 2026-07-12,
v0.7.0; see docs/DECISIONS.md §13. Migration maps it to ``off``.)

Units: none — this entity carries a closed set of control-state strings, not a
physical quantity. (Temperatures elsewhere are in degrees Celsius, valve position
in percent 0..100.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ROOM_NAME, CONF_ROOMS, ROOM_STATES
from .coordinator import TortoiseUfhCoordinator
from .device import room_device_info, room_slug

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import TortoiseUfhConfigEntry


# The per-room control-state select: off / live.
CONTROL_STATE_DESCRIPTION: SelectEntityDescription = SelectEntityDescription(
    key="control_state",
    translation_key="control_state",
    icon="mdi:cog-play-outline",
    entity_category=EntityCategory.CONFIG,
)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TortoiseUfhConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tortoise-UFH select entities from a config entry.

    Creates one control-state select per configured room (rooms are read from
    ``entry.data`` so a room appears even if it produced no output on the first
    cycle).

    Args:
        hass: The Home Assistant instance (unused; entities read the
            coordinator).
        entry: The config entry being set up.
        async_add_entities: Callback to register the created entities.
    """
    coordinator: TortoiseUfhCoordinator = entry.runtime_data.coordinator

    entities: list[TortoiseUfhControlStateSelect] = []
    rooms: Any = entry.data.get(CONF_ROOMS, [])
    for room_cfg in rooms:
        room_name = str(room_cfg[CONF_ROOM_NAME])
        entities.append(
            TortoiseUfhControlStateSelect(
                coordinator=coordinator,
                description=CONTROL_STATE_DESCRIPTION,
                entry_id=entry.entry_id,
                room_name=room_name,
            )
        )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Select entity
# ---------------------------------------------------------------------------


class TortoiseUfhControlStateSelect(
    CoordinatorEntity[TortoiseUfhCoordinator], SelectEntity
):
    """A per-room control-state select (``off`` / ``live``).

    Reads its current option from the coordinator's control-state map and, on
    selection, updates that state in memory (immediate effect) and persists the
    value to ``entry.options`` (survives restart).
    """

    _attr_has_entity_name = True
    _attr_options = ROOM_STATES
    entity_description: SelectEntityDescription

    def __init__(
        self,
        coordinator: TortoiseUfhCoordinator,
        description: SelectEntityDescription,
        entry_id: str,
        room_name: str,
    ) -> None:
        """Initialise the control-state select entity.

        Args:
            coordinator: The Tortoise-UFH data-update coordinator.
            description: The select description (the control-state control).
            entry_id: Config-entry id, used to build a stable ``unique_id``.
            room_name: The room this select controls.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._room_name = room_name

        slug = room_slug(room_name)
        self._attr_unique_id = f"{entry_id}_{slug}_{description.key}"
        self._attr_device_info = room_device_info(entry_id, room_name)

    @property
    def current_option(self) -> str:
        """Return the room's current control state (``off`` / ``live``)."""
        return self.coordinator.get_room_state(self._room_name)

    async def async_select_option(self, option: str) -> None:
        """Apply a new control state through the coordinator and refresh state.

        Args:
            option: The new control state, one of :data:`ROOM_STATES`.
        """
        self.coordinator.set_room_state(self._room_name, option)
        self.async_write_ha_state()
