"""Integration tests for the Tortoise-UFH per-room diagnostic binary sensors.

Asserts the four per-room binary sensors exist for both configured rooms, that
they read ``off`` while every source is fresh, and that losing a room's
temperature sensor (once past the stale-cache window) flips that room's
``sensor_lost`` problem sensor on while the coordinator safe-degrades the room:
valve frozen at its last position and the fast source forced off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.tortoise_ufh.const import DOMAIN, ENTITY_STALE_MAX_SECONDS

pytestmark = pytest.mark.ha

_ROOMS = ("Salon", "Lazienka")
_KEYS = (
    "sensor_lost",
    "output_saturated",
    "s2_condensation_active",
    "live_control",
)


def _entity_id(hass: HomeAssistant, entry_id: str, room: str, key: str) -> str | None:
    """Resolve a per-room binary sensor's entity id from its stable unique id."""
    registry = er.async_get(hass)
    unique_id = f"{entry_id}_{room.lower().replace(' ', '_')}_{key}"
    return registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)


async def test_per_room_binary_sensors_exist(
    hass: HomeAssistant, setup_integration
) -> None:
    """All four diagnostic binary sensors are created for both rooms."""
    entry = setup_integration
    for room in _ROOMS:
        for key in _KEYS:
            entity_id = _entity_id(hass, entry.entry_id, room, key)
            assert entity_id is not None, f"missing {room} {key}"
            assert hass.states.get(entity_id) is not None


async def test_all_fresh_sensor_lost_off(
    hass: HomeAssistant, setup_integration
) -> None:
    """With every source fresh, no room reports a lost sensor."""
    entry = setup_integration
    for room in _ROOMS:
        entity_id = _entity_id(hass, entry.entry_id, room, "sensor_lost")
        assert entity_id is not None
        assert hass.states.get(entity_id).state == "off"
        runtime = entry.runtime_data.coordinator.data.rooms[room]
        assert "sensor_lost" not in runtime.report.flags


async def test_salon_sensor_loss_turns_on_and_safe_degrades(
    hass: HomeAssistant, setup_integration
) -> None:
    """Losing Salon's temperature sensor flips sensor_lost on and safe-degrades.

    The room valve is frozen at its last commanded position and the split is
    forced off, per the locked safe-degrade decision; Lazienka is untouched.
    """
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    before = coordinator.data.rooms["Salon"]
    held_valve = before.outputs.valve_position_pct
    assert "sensor_lost" not in before.report.flags

    # A freshly-unavailable reading is masked by the short stale cache by
    # design, so backdate the cached sample past the stale window; only then
    # does an unavailable sensor actually degrade the room.
    cached_value, _ = coordinator._entity_cache["sensor.salon_temp"]
    coordinator._entity_cache["sensor.salon_temp"] = (
        cached_value,
        datetime.now(UTC) - timedelta(seconds=ENTITY_STALE_MAX_SECONDS + 60),
    )
    hass.states.async_remove("sensor.salon_temp")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    salon = coordinator.data.rooms["Salon"]
    assert "sensor_lost" in salon.report.flags
    assert salon.outputs.valve_position_pct == held_valve  # valve frozen
    assert salon.outputs.fast_source.on is False  # split forced off

    salon_lost = _entity_id(hass, entry.entry_id, "Salon", "sensor_lost")
    assert hass.states.get(salon_lost).state == "on"

    # Lazienka's sensor is still live, so it must not degrade.
    laz_lost = _entity_id(hass, entry.entry_id, "Lazienka", "sensor_lost")
    assert hass.states.get(laz_lost).state == "off"
    assert "sensor_lost" not in coordinator.data.rooms["Lazienka"].report.flags
