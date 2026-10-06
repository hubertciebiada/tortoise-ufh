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
    get_entry_device,
    room_slug,
)

pytestmark = pytest.mark.ha

_ROOMS = ("Salon", "Lazienka")


def _room_device(
    hass: HomeAssistant, entry_id: str, room: str
) -> dr.DeviceEntry | None:
    """Resolve a room's device entry from its stable identifier."""
    registry = dr.async_get(hass)
    return get_entry_device(
        registry, entry_id, (DOMAIN, f"{entry_id}_{room_slug(room)}")
    )


async def test_room_devices_created_with_hub_link(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Each room gets a zone device named after it, linked to the hub device."""
    entry = setup_integration
    registry = dr.async_get(hass)

    hub = get_entry_device(registry, entry.entry_id, (DOMAIN, entry.entry_id))
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
    hub = get_entry_device(device_registry, entry.entry_id, (DOMAIN, entry.entry_id))
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


async def test_hub_device_registered_before_platforms(
    hass: HomeAssistant, register_sources: None, mock_entry: MockConfigEntry
) -> None:
    """K11 (2026-07-12): the hub exists before any via_device references it.

    Room devices link to the hub via ``via_device``; since HA 2025.x adding
    an entity whose ``via_device`` target is missing from the registry logs a
    "will stop working" deprecation. ``async_setup_entry`` now registers the
    hub explicitly BEFORE forwarding the entity platforms, so the reference
    is always valid and no such warning is emitted during setup.
    """
    import logging

    class _ViaDeviceWarnings(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.WARNING)
            self.records: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if "via_device" in message or "via device" in message:
                self.records.append(message)

    handler = _ViaDeviceWarnings()
    logging.getLogger().addHandler(handler)
    try:
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
    finally:
        logging.getLogger().removeHandler(handler)

    registry = dr.async_get(hass)
    hub = get_entry_device(registry, mock_entry.entry_id, (DOMAIN, mock_entry.entry_id))
    assert hub is not None
    assert hub.model == HUB_MODEL
    for room in _ROOMS:
        device = _room_device(hass, mock_entry.entry_id, room)
        assert device is not None
        assert device.via_device_id == hub.id
    assert handler.records == []


async def test_room_entity_survives_entity_id_change(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Issue #23: renaming a room entity's entity_id keeps it live.

    An entity-ID change re-adds the entity from a core frame. On HA 2026.9+
    a room ``DeviceInfo`` still carrying the deprecated ``via_device`` makes
    the device registry raise there, so the entity vanished (no state under
    either id) until the entry was reloaded. Room devices now link to the hub
    via ``via_device_id`` wherever the core supports it.
    """
    entry = setup_integration
    entity_registry = er.async_get(hass)
    old_id = entity_registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_salon_sensor_lost"
    )
    assert old_id is not None
    new_id = "binary_sensor.salon_renamed_sensor_lost"

    entity_registry.async_update_entity(old_id, new_entity_id=new_id)
    await hass.async_block_till_done()

    assert hass.states.get(old_id) is None
    assert hass.states.get(new_id) is not None
    hub = get_entry_device(dr.async_get(hass), entry.entry_id, (DOMAIN, entry.entry_id))
    device = _room_device(hass, entry.entry_id, "Salon")
    assert hub is not None
    assert device is not None
    assert device.via_device_id == hub.id


async def test_room_device_info_links_hub_by_registry_id() -> None:
    """Issue #23: the room device links the hub by the core-supported key."""
    from custom_components.tortoise_ufh.device import (
        SUPPORTS_VIA_DEVICE_ID,
        room_device_info,
    )

    info = room_device_info("entry1", "Salon", "hub-device-id")
    if SUPPORTS_VIA_DEVICE_ID:
        assert info.get("via_device_id") == "hub-device-id"
        assert "via_device" not in info
    else:
        assert info.get("via_device") == (DOMAIN, "entry1")
