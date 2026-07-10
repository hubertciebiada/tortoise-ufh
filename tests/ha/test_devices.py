"""Device-registry and entity-naming tests for the Tortoise-UFH integration.

Pins the v0.5.0 device/naming contract:

* every configured room gets ONE device (identifier
  ``(DOMAIN, f"{entry_id}_{slug}")``, name = room name, model "Room zone")
  linked via ``via_device`` to the per-entry hub device (identifier
  ``(DOMAIN, entry_id)``, model "UFH controller");
* per-room entities attach to their room device, global entities to the hub;
* entity names come from ``has_entity_name`` + ``translation_key`` (no
  hard-coded ``_attr_name``), so the PL name translations finally apply;
* ``unique_id`` formats are UNCHANGED, so an upgraded installation keeps its
  entity ids (pinned with a prefabricated registry entry);
* the retired per-room ``live_control`` binary sensor is purged from the
  registry on setup.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tortoise_ufh.const import DOMAIN
from custom_components.tortoise_ufh.device import (
    HUB_MODEL,
    MANUFACTURER,
    ROOM_MODEL,
    room_slug,
)

pytestmark = pytest.mark.ha

_ROOMS = ("Salon", "Lazienka")


def _room_device(
    hass: HomeAssistant, entry_id: str, room: str
) -> dr.DeviceEntry | None:
    """Resolve a room's device entry from its stable identifier."""
    registry = dr.async_get(hass)
    return registry.async_get_device(
        identifiers={(DOMAIN, f"{entry_id}_{room_slug(room)}")}
    )


async def test_room_devices_created_with_hub_link(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Each room gets a zone device named after it, linked to the hub device."""
    entry = setup_integration
    registry = dr.async_get(hass)

    hub = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert hub is not None
    assert hub.manufacturer == MANUFACTURER
    assert hub.model == HUB_MODEL

    for room in _ROOMS:
        device = _room_device(hass, entry.entry_id, room)
        assert device is not None, f"missing device for {room}"
        assert device.name == room
        assert device.manufacturer == MANUFACTURER
        assert device.model == ROOM_MODEL
        # Room devices hang off the hub (via_device).
        assert device.via_device_id == hub.id


async def test_entities_attached_to_their_devices(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Per-room entities join the room device; global entities join the hub."""
    entry = setup_integration
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    hub = device_registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert hub is not None

    per_room = (
        ("select", "control_state"),
        ("number", "offset"),
        ("sensor", "recommended_valve"),
        ("sensor", "i_term"),
        ("binary_sensor", "sensor_lost"),
    )
    for room in _ROOMS:
        device = _room_device(hass, entry.entry_id, room)
        assert device is not None
        for platform, key in per_room:
            unique_id = f"{entry.entry_id}_{room_slug(room)}_{key}"
            entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
            assert entity_id is not None, f"missing {room} {platform}.{key}"
            reg_entry = entity_registry.async_get(entity_id)
            assert reg_entry is not None
            assert reg_entry.device_id == device.id

    for platform, key in (
        ("number", "home_temperature"),
        ("sensor", "global_safe_dew_point"),
        ("sensor", "algorithm_status"),
        ("sensor", "watchdog_status"),
        ("sensor", "last_update"),
    ):
        entity_id = entity_registry.async_get_entity_id(
            platform, DOMAIN, f"{entry.entry_id}_{key}"
        )
        assert entity_id is not None, f"missing global {platform}.{key}"
        reg_entry = entity_registry.async_get(entity_id)
        assert reg_entry is not None
        assert reg_entry.device_id == hub.id


async def test_entity_names_come_from_translation_key(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Names use has_entity_name + translation_key, so translations apply."""
    entry = setup_integration
    entity_registry = er.async_get(hass)

    checks = (
        # (platform, unique_id suffix, expected translated EN name)
        ("select", "salon_control_state", "Control state"),
        ("number", "salon_offset", "Setpoint offset"),
        ("sensor", "salon_i_term", "Integral term (valve %)"),
        ("binary_sensor", "salon_sensor_lost", "Sensor lost"),
        ("number", "home_temperature", "Home temperature"),
        ("sensor", "global_safe_dew_point", "Global safe dew point"),
    )
    for platform, suffix, expected_name in checks:
        entity_id = entity_registry.async_get_entity_id(
            platform, DOMAIN, f"{entry.entry_id}_{suffix}"
        )
        assert entity_id is not None, f"missing {platform} {suffix}"
        reg_entry = entity_registry.async_get(entity_id)
        assert reg_entry is not None
        # No hard-coded name: HA composes "<device name> <translated name>".
        assert reg_entry.has_entity_name is True
        assert reg_entry.translation_key == suffix.removeprefix("salon_")
        state = hass.states.get(entity_id)
        assert state is not None
        device_name = "Salon" if suffix.startswith("salon_") else "Tortoise-UFH"
        assert state.attributes["friendly_name"] == f"{device_name} {expected_name}"


async def test_upgraded_install_keeps_entity_ids(
    hass: HomeAssistant, register_sources: None, mock_entry: MockConfigEntry
) -> None:
    """An entity registered under the frozen unique_id keeps its entity_id.

    Simulates an upgrade: a pre-v0.5.0 install has registry entries keyed by
    the (unchanged) unique ids, with entity ids derived from the old
    ``_attr_name``. After setup the platform must adopt those entries — same
    entity_id, now attached to the new room device — instead of minting new
    device+name-derived ids.
    """
    entity_registry = er.async_get(hass)
    legacy = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{mock_entry.entry_id}_salon_i_term",
        suggested_object_id="salon_i_term",
        config_entry=mock_entry,
    )
    assert legacy.entity_id == "sensor.salon_i_term"

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    adopted_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{mock_entry.entry_id}_salon_i_term"
    )
    assert adopted_id == "sensor.salon_i_term"
    reg_entry = entity_registry.async_get(adopted_id)
    assert reg_entry is not None
    # The adopted entry gained the Salon room device.
    device = _room_device(hass, mock_entry.entry_id, "Salon")
    assert device is not None
    assert reg_entry.device_id == device.id
    assert hass.states.get(adopted_id) is not None


async def test_retired_live_control_purged_on_setup(
    hass: HomeAssistant, register_sources: None, mock_entry: MockConfigEntry
) -> None:
    """An orphaned live_control binary sensor is swept from the registry."""
    entity_registry = er.async_get(hass)
    stale = entity_registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{mock_entry.entry_id}_salon_live_control",
        suggested_object_id="salon_live_control",
        config_entry=mock_entry,
    )
    assert entity_registry.async_get(stale.entity_id) is not None

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        entity_registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{mock_entry.entry_id}_salon_live_control"
        )
        is None
    )
