"""Switch entities for the Tortoise-UFH integration.

Exposes two kinds of writable toggle, both backed by the coordinator's
single-source-of-truth runtime flags:

1. **Global kill-switch** (one per config entry). When ``on`` the coordinator
   emits *no* commands to any actuator (compute-and-report only). This is the
   master safety cut-out.
2. **Per-room live-control** (one per configured room). ``on`` = *live* (the
   coordinator writes this room's valve/fast-source commands); ``off`` =
   *shadow* (the room is computed and reported but nothing is written).

Toggling a switch does two things: it updates the coordinator's in-memory flag
immediately (via its ``@callback`` setters, so the running control loop and the
panel see the change at once) and it persists the new value into
``entry.options`` so the choice survives a Home Assistant restart. Because the
integration reloads the config entry whenever its options change (see
``__init__._async_update_listener``), the persisted write also re-synchronises a
freshly rebuilt coordinator from the same options.

Units: none of these entities carry a physical quantity; they are boolean
control flags. (Temperatures elsewhere in the integration are in degrees
Celsius, valve position in percent 0..100.)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_KILL_SWITCH, CONF_LIVE_CONTROL, CONF_ROOM_NAME, CONF_ROOMS
from .coordinator import TortoiseUfhCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import TortoiseUfhConfigEntry


# ---------------------------------------------------------------------------
# Entity description
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TortoiseUfhSwitchEntityDescription(SwitchEntityDescription):
    """Description of a Tortoise-UFH switch, driven by coordinator callbacks.

    Attributes:
        is_on_fn: Reads the current boolean state from the coordinator. Receives
            the coordinator and the room name (``None`` for a global switch).
        set_fn: Applies a new boolean state to the coordinator's in-memory flag
            (immediate runtime effect). Receives the coordinator, the room name
            (``None`` for a global switch) and the new value.
        per_room: ``True`` for a per-room switch (persisted under
            :data:`~.const.CONF_LIVE_CONTROL`), ``False`` for the global
            kill-switch (persisted under :data:`~.const.CONF_KILL_SWITCH`).

    Raises:
        ValueError: If ``key`` is empty or ``is_on_fn`` / ``set_fn`` is not
            callable.
    """

    is_on_fn: Callable[[TortoiseUfhCoordinator, str | None], bool]
    set_fn: Callable[[TortoiseUfhCoordinator, str | None, bool], None]
    per_room: bool = False

    def __post_init__(self) -> None:
        """Validate the description's key and callables."""
        if not self.key:
            msg = "switch description key must be a non-empty string"
            raise ValueError(msg)
        if not callable(self.is_on_fn):
            msg = f"is_on_fn must be callable for switch {self.key!r}"
            raise ValueError(msg)
        if not callable(self.set_fn):
            msg = f"set_fn must be callable for switch {self.key!r}"
            raise ValueError(msg)


# The global kill-switch: on => coordinator emits no commands.
KILL_SWITCH: TortoiseUfhSwitchEntityDescription = TortoiseUfhSwitchEntityDescription(
    key="kill_switch",
    translation_key="kill_switch",
    icon="mdi:cancel",
    entity_category=EntityCategory.CONFIG,
    per_room=False,
    is_on_fn=lambda coordinator, _room: coordinator.get_kill_switch(),
    set_fn=lambda coordinator, _room, value: coordinator.set_kill_switch(value),
)

# The per-room live-control switch: on => live (writes), off => shadow.
LIVE_CONTROL: TortoiseUfhSwitchEntityDescription = TortoiseUfhSwitchEntityDescription(
    key="live_control",
    translation_key="live_control",
    icon="mdi:play-circle-outline",
    entity_category=EntityCategory.CONFIG,
    per_room=True,
    is_on_fn=lambda coordinator, room: coordinator.get_live_control(room or ""),
    set_fn=lambda coordinator, room, value: coordinator.set_live_control(
        room or "", value
    ),
)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TortoiseUfhConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tortoise-UFH switch entities from a config entry.

    Creates one global kill-switch plus one live-control switch per configured
    room (rooms are read from ``entry.data`` so a room appears even if it
    produced no output on the first cycle).

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being set up.
        async_add_entities: Callback to register the created entities.
    """
    coordinator: TortoiseUfhCoordinator = entry.runtime_data.coordinator

    entities: list[TortoiseUfhSwitch] = [
        TortoiseUfhSwitch(
            coordinator=coordinator,
            description=KILL_SWITCH,
            entry_id=entry.entry_id,
            room_name=None,
        )
    ]

    rooms: Any = entry.data.get(CONF_ROOMS, [])
    for room_cfg in rooms:
        room_name = str(room_cfg[CONF_ROOM_NAME])
        entities.append(
            TortoiseUfhSwitch(
                coordinator=coordinator,
                description=LIVE_CONTROL,
                entry_id=entry.entry_id,
                room_name=room_name,
            )
        )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Switch entity
# ---------------------------------------------------------------------------


class TortoiseUfhSwitch(CoordinatorEntity[TortoiseUfhCoordinator], SwitchEntity):
    """A Tortoise-UFH control toggle (global kill-switch or per-room live control).

    Reads its state from the coordinator's runtime flags and, on toggle, updates
    that flag in memory (immediate effect) and persists the value to
    ``entry.options`` (survives restart).
    """

    _attr_has_entity_name = True
    entity_description: TortoiseUfhSwitchEntityDescription

    def __init__(
        self,
        coordinator: TortoiseUfhCoordinator,
        description: TortoiseUfhSwitchEntityDescription,
        entry_id: str,
        room_name: str | None,
    ) -> None:
        """Initialise the switch entity.

        Args:
            coordinator: The Tortoise-UFH data-update coordinator.
            description: The switch description (kill-switch or live-control).
            entry_id: Config-entry id, used to build a stable ``unique_id``.
            room_name: The room name for a per-room switch, or ``None`` for the
                global kill-switch.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._room_name = room_name

        if room_name is not None:
            safe_room = room_name.lower().replace(" ", "_")
            self._attr_unique_id = f"{entry_id}_{safe_room}_{description.key}"
            self._attr_name = f"{room_name} {description.key.replace('_', ' ')}"
        else:
            self._attr_unique_id = f"{entry_id}_{description.key}"
            self._attr_name = f"Tortoise-UFH {description.key.replace('_', ' ')}"

    @property
    def is_on(self) -> bool:
        """Return whether the switch is on.

        Returns:
            For the kill-switch: ``True`` when engaged (no commands emitted). For
            a live-control switch: ``True`` when the room is live (writes
            commands), ``False`` when in shadow mode.
        """
        return self.entity_description.is_on_fn(self.coordinator, self._room_name)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on.

        Args:
            **kwargs: Unused Home Assistant service keyword arguments.
        """
        self._apply(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off.

        Args:
            **kwargs: Unused Home Assistant service keyword arguments.
        """
        self._apply(False)

    @callback
    def _apply(self, value: bool) -> None:
        """Apply a new state: update the coordinator, refresh, and persist.

        Args:
            value: The new boolean state.
        """
        self.entity_description.set_fn(self.coordinator, self._room_name, value)
        self.async_write_ha_state()
        self._persist(value)

    @callback
    def _persist(self, value: bool) -> None:
        """Persist the new value into ``entry.options``.

        For a per-room switch the room's flag is updated inside the
        :data:`~.const.CONF_LIVE_CONTROL` map; for the global kill-switch the
        :data:`~.const.CONF_KILL_SWITCH` flag is set. Writing the options reloads
        the config entry, which re-synchronises a rebuilt coordinator from the
        same values.

        Args:
            value: The new boolean state to persist.
        """
        entry = self.coordinator.config_entry
        new_options: dict[str, Any] = dict(entry.options)
        if self.entity_description.per_room and self._room_name is not None:
            live_map: dict[str, bool] = dict(new_options.get(CONF_LIVE_CONTROL, {}))
            live_map[self._room_name] = value
            new_options[CONF_LIVE_CONTROL] = live_map
        else:
            new_options[CONF_KILL_SWITCH] = value
        self.hass.config_entries.async_update_entry(entry, options=new_options)
