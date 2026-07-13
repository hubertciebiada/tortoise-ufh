"""Per-room diagnostic binary sensors for the Tortoise-UFH integration.

Publishes four per-room binary sensors, all read strictly from the
coordinator's typed :class:`~.coordinator.CoordinatorData`:

* ``sensor_lost`` (device class ``PROBLEM``) — the room temperature reading was
  missing this cycle, so the controller safe-degraded (held valve, split off).
* ``output_saturated`` — the computed valve position hit a 0 % or 100 % bound.
* ``s2_condensation_active`` (device class ``PROBLEM``) — the per-room S2
  dew-point protection fully throttled floor cooling to avoid condensation.
* ``flow_fault`` (device class ``PROBLEM``) — the S6 hydraulic no-flow
  watchdog latched ``loop_no_flow`` or ``loop_stuck_open`` on any of the
  room's loops (2026-07-13, issue #4): the valve command and the loop's
  physical water behaviour disagree.

The former ``live_control`` binary sensor was retired in v0.5.0 — it merely
mirrored ``control_state == "live"`` and is fully covered by the per-room
control-state select; orphaned registry entries are purged on setup
(``__init__._async_purge_retired_entities``).

Every sensor is ``EntityCategory.DIAGNOSTIC`` and read-only, joins its room's
device and is named via ``translation_key``. The entities are
description-driven: each :class:`TortoiseUfhBinarySensorEntityDescription`
carries a ``value_fn`` that maps a room's
:class:`~.coordinator.RoomRuntime` to a boolean (or ``None`` when the room is
absent from the current payload).

Units: no physical units — every value is a boolean flag.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ROOM_NAME, CONF_ROOMS
from .coordinator import RoomRuntime, TortoiseUfhCoordinator
from .device import room_device_info, room_slug

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

# Core report flag strings surfaced as binary sensors (mirrors the strings the
# core RoomController emits in ``RoomReport.flags``).
_FLAG_SENSOR_LOST = "sensor_lost"
_FLAG_S2_CONDENSATION = "s2_condensation"
# The graduated local throttle's full-stop flag, split from the hard rule
# 2026-07-12 (B7): the "condensation protection active" entity keeps firing
# on EITHER layer, exactly like it did when both shared one flag string.
_FLAG_S2_THROTTLE = "s2_throttle"
# S6 hydraulic watchdog flags (2026-07-13, issue #4): the loop probes
# contradict the valve command in either direction.
_FLAG_LOOP_NO_FLOW = "loop_no_flow"
_FLAG_LOOP_STUCK_OPEN = "loop_stuck_open"


# ---------------------------------------------------------------------------
# Binary-sensor descriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TortoiseUfhBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Binary-sensor description with a boolean value extractor.

    Attributes:
        value_fn: Maps a room's :class:`~.coordinator.RoomRuntime` to the
            sensor's boolean state (``True`` = flag active / on).
    """

    value_fn: Callable[[RoomRuntime], bool]


def _sensor_lost(runtime: RoomRuntime) -> bool:
    """Return whether the room reported a lost temperature sensor.

    Args:
        runtime: The room's runtime payload.

    Returns:
        ``True`` when ``"sensor_lost"`` is present in the report flags.
    """
    return _FLAG_SENSOR_LOST in runtime.report.flags


def _output_saturated(runtime: RoomRuntime) -> bool:
    """Return whether the room's valve output hit a 0/100 % bound.

    Args:
        runtime: The room's runtime payload.

    Returns:
        The report's ``saturated`` flag.
    """
    return runtime.report.saturated


def _s2_condensation_active(runtime: RoomRuntime) -> bool:
    """Return whether per-room S2 dew-point protection fully throttled cooling.

    Covers BOTH protection layers (flag split 2026-07-12, B7): the graduated
    local throttle's full stop (``"s2_throttle"``) and the independent hard
    safety rule (``"s2_condensation"``) — either means the room's cooling is
    condensation-stopped, which is what this PROBLEM entity always reported.

    Args:
        runtime: The room's runtime payload.

    Returns:
        ``True`` when either S2 flag is present in the report flags.
    """
    flags = runtime.report.flags
    return _FLAG_S2_CONDENSATION in flags or _FLAG_S2_THROTTLE in flags


def _flow_fault(runtime: RoomRuntime) -> bool:
    """Return whether the S6 hydraulic watchdog latched a flow fault (S6).

    Fires on EITHER detection (2026-07-13, issue #4): ``loop_no_flow`` (an
    open command with no hydraulic response — the frozen-actuator incident)
    or ``loop_stuck_open`` (a closed command with a persistent source-side
    signature). Both mean the physical valve and the command disagree, so a
    single PROBLEM entity per room is the automation hook for both.

    Args:
        runtime: The room's runtime payload.

    Returns:
        ``True`` when either S6 flag is present in the report flags.
    """
    flags = runtime.report.flags
    return _FLAG_LOOP_NO_FLOW in flags or _FLAG_LOOP_STUCK_OPEN in flags


# Per-room binary-sensor descriptions.
ROOM_BINARY_SENSORS: tuple[TortoiseUfhBinarySensorEntityDescription, ...] = (
    TortoiseUfhBinarySensorEntityDescription(
        key="sensor_lost",
        translation_key="sensor_lost",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_sensor_lost,
    ),
    TortoiseUfhBinarySensorEntityDescription(
        key="output_saturated",
        translation_key="output_saturated",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_output_saturated,
    ),
    TortoiseUfhBinarySensorEntityDescription(
        key="s2_condensation_active",
        translation_key="s2_condensation_active",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_s2_condensation_active,
    ),
    TortoiseUfhBinarySensorEntityDescription(
        key="flow_fault",
        translation_key="flow_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_flow_fault,
    ),
)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tortoise-UFH binary sensors from a config entry.

    One set of per-room diagnostic binary sensors is created for every room
    in the configured room list (not the coordinator's current payload), so
    rooms missing from the first payload — e.g. after an error cycle — still
    get sensors and render as unavailable until the coordinator recovers.

    Args:
        hass: The Home Assistant instance (unused; required by the platform).
        entry: The config entry whose ``runtime_data`` holds the coordinator.
        async_add_entities: Callback registering the new entities.
    """
    coordinator: TortoiseUfhCoordinator = entry.runtime_data.coordinator  # type: ignore[attr-defined]

    entities: list[TortoiseUfhBinarySensorEntity] = []
    rooms = list(entry.data.get(CONF_ROOMS, []) or [])
    for room_cfg in rooms:
        room_name = str(room_cfg[CONF_ROOM_NAME])
        entities.extend(
            TortoiseUfhBinarySensorEntity(
                coordinator=coordinator,
                description=description,
                entry_id=entry.entry_id,
                room_name=room_name,
            )
            for description in ROOM_BINARY_SENSORS
        )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Binary-sensor entity
# ---------------------------------------------------------------------------


class TortoiseUfhBinarySensorEntity(
    CoordinatorEntity[TortoiseUfhCoordinator], BinarySensorEntity
):
    """Read-only per-room diagnostic binary sensor for Tortoise-UFH."""

    entity_description: TortoiseUfhBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TortoiseUfhCoordinator,
        description: TortoiseUfhBinarySensorEntityDescription,
        entry_id: str,
        room_name: str,
    ) -> None:
        """Initialise a per-room binary sensor.

        Args:
            coordinator: The Tortoise-UFH data coordinator.
            description: The binary-sensor description with its ``value_fn``.
            entry_id: Config entry id, used to build a stable ``unique_id``.
            room_name: The room this sensor belongs to.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._room_name = room_name

        slug = room_slug(room_name)
        self._attr_unique_id = f"{entry_id}_{slug}_{description.key}"
        self._attr_device_info = room_device_info(entry_id, room_name)

    @property
    def is_on(self) -> bool | None:
        """Return the sensor state, or ``None`` when the room is absent.

        Returns:
            The boolean from the description's ``value_fn``, or ``None`` if the
            coordinator has no data yet or the room dropped out of the payload.
        """
        data = self.coordinator.data
        if data is None:
            return None
        runtime = data.rooms.get(self._room_name)
        if runtime is None:
            return None
        return self.entity_description.value_fn(runtime)
