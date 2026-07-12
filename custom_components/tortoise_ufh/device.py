"""Device registry helpers for the Tortoise-UFH integration.

Every entity of the integration belongs to one of two device kinds:

* ONE **hub device** per config entry (identifier ``(DOMAIN, entry_id)``) that
  groups the building-wide entities (home temperature, global safe dew point,
  algorithm / watchdog status, last update).
* ONE **room device** per configured room (identifier
  ``(DOMAIN, f"{entry_id}_{room_slug}")``) that groups that room's controls and
  diagnostics and can be assigned to a Home Assistant area. Room devices link
  back to the hub via ``via_device``.

The room identifier reuses the frozen ``safe_room`` slug convention
(``room_name.lower().replace(" ", "_")``) that every per-room entity
``unique_id`` is built from, so devices and entities always agree on the room
key. Units: none — this module carries registry metadata only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

MANUFACTURER: str = "Tortoise-UFH"
"""Manufacturer string shown on every Tortoise-UFH device."""

HUB_MODEL: str = "UFH controller"
"""Model string of the per-entry hub device."""

ROOM_MODEL: str = "Room zone"
"""Model string of a per-room zone device."""

HUB_NAME: str = "Tortoise-UFH"
"""Display name of the per-entry hub device."""


def room_slug(room_name: str) -> str:
    """Return the frozen registry slug for a room name.

    The same transformation every per-room entity ``unique_id`` uses
    (``lower`` + spaces to underscores), so device identifiers and entity
    unique ids never disagree on the room key.

    Args:
        room_name: The configured room name (e.g. ``"Salon"``).

    Returns:
        The slugged room key (e.g. ``"salon"``).
    """
    return room_name.lower().replace(" ", "_")


def hub_device_info(entry_id: str) -> DeviceInfo:
    """Return the :class:`DeviceInfo` of the per-entry hub device.

    Args:
        entry_id: The config entry id owning the hub.

    Returns:
        The hub device info (identifier ``(DOMAIN, entry_id)``).
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=HUB_NAME,
        manufacturer=MANUFACTURER,
        model=HUB_MODEL,
    )


def register_hub_device(hass: HomeAssistant, entry_id: str) -> None:
    """Create the per-entry hub device in the device registry (K11).

    Called from ``async_setup_entry`` BEFORE the entity platforms are
    forwarded: the room devices reference the hub via ``via_device``, and
    since HA 2025.x adding an entity whose ``via_device`` points at a device
    that does not exist in the registry yet logs a "will stop working"
    deprecation. Registering the hub first makes the reference always valid.
    Idempotent (``async_get_or_create``).

    Args:
        hass: The Home Assistant instance.
        entry_id: The config entry id owning the hub.
    """
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, entry_id)},
        name=HUB_NAME,
        manufacturer=MANUFACTURER,
        model=HUB_MODEL,
    )


def room_device_info(entry_id: str, room_name: str) -> DeviceInfo:
    """Return the :class:`DeviceInfo` of a per-room zone device.

    Args:
        entry_id: The config entry id owning the room.
        room_name: The configured room name (used verbatim as the device name).

    Returns:
        The room device info (identifier ``(DOMAIN, f"{entry_id}_{slug}")``),
        linked to the hub device via ``via_device``.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{room_slug(room_name)}")},
        name=room_name,
        manufacturer=MANUFACTURER,
        model=ROOM_MODEL,
        via_device=(DOMAIN, entry_id),
    )
