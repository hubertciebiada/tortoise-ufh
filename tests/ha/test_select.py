"""Select-platform tests: the per-room control-state select.

Exercises the single writable control surface that replaced the retired
kill-switch and per-room live-control switches (BUILD_SPEC / prd-control-brain.md
§8, RoomControlState refactor; reduced to the two-state ``off`` / ``live``
2026-07-12 — DECISIONS §13):

* one ``control_state`` select per configured room, options ``off`` / ``live``,
  each starting in the safe default ``off``;
* selecting an option flips ``coordinator.get_room_state(room)`` and persists the
  new value under ``CONF_ROOM_STATE`` in ``entry.options`` WITHOUT reloading the
  entry (the PID integrator survives — see ``options_require_reload``);
* the persisted state is restored by a freshly rebuilt coordinator on reload.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tortoise_ufh.const import (
    CONF_ROOM_STATE,
    DOMAIN,
    ROOM_STATE_LIVE,
    ROOM_STATE_OFF,
    ROOM_STATES,
)

pytestmark = pytest.mark.ha


def _control_state_entity_id(
    hass: HomeAssistant, entry: MockConfigEntry, room: str
) -> str:
    """Resolve a room's control-state select entity id from its unique id."""
    registry = er.async_get(hass)
    safe_room = room.lower().replace(" ", "_")
    unique_id = f"{entry.entry_id}_{safe_room}_control_state"
    entity_id = registry.async_get_entity_id("select", DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    """Call select.select_option and let the (non-reloading) update settle."""
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": option},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_select_entities_exist(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One control-state select per room, options off/live, all off."""
    entry = setup_integration

    salon_id = _control_state_entity_id(hass, entry, "Salon")
    lazienka_id = _control_state_entity_id(hass, entry, "Lazienka")

    # Two distinct per-room selects.
    assert salon_id != lazienka_id

    for entity_id in (salon_id, lazienka_id):
        state = hass.states.get(entity_id)
        assert state is not None
        # New rooms start in the safe off default (nothing is written).
        assert state.state == ROOM_STATE_OFF
        # Exactly the two-state option list (shadow removed in v0.7.0).
        assert state.attributes["options"] == ROOM_STATES
        assert state.attributes["options"] == [ROOM_STATE_OFF, ROOM_STATE_LIVE]

    coordinator = entry.runtime_data.coordinator
    assert coordinator.get_room_state("Salon") == ROOM_STATE_OFF
    assert coordinator.get_room_state("Lazienka") == ROOM_STATE_OFF


async def test_select_option_flips_state_and_persists_without_reload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Selecting ``live`` flips only that room and persists it (no reload)."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    # The promoted room starts writing on the next cycle: capture, not dispatch.
    async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")

    entry = setup_integration
    coordinator_before = entry.runtime_data.coordinator
    salon_id = _control_state_entity_id(hass, entry, "Salon")

    await _select(hass, salon_id, ROOM_STATE_LIVE)

    # A control-state-only change must NOT reload the entry (integrator kept).
    assert entry.runtime_data.coordinator is coordinator_before

    coordinator = entry.runtime_data.coordinator
    assert coordinator.get_room_state("Salon") == ROOM_STATE_LIVE
    # The other room is untouched by a per-room selection.
    assert coordinator.get_room_state("Lazienka") == ROOM_STATE_OFF

    assert hass.states.get(salon_id).state == ROOM_STATE_LIVE
    lazienka_id = _control_state_entity_id(hass, entry, "Lazienka")
    assert hass.states.get(lazienka_id).state == ROOM_STATE_OFF

    # Persisted under the canonical control-state map.
    state_map = entry.options.get(CONF_ROOM_STATE, {})
    assert state_map.get("Salon") == ROOM_STATE_LIVE


async def test_persisted_state_survives_reload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A fresh coordinator restores the control state from options on reload."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")

    entry = setup_integration
    salon_id = _control_state_entity_id(hass, entry, "Salon")

    await _select(hass, salon_id, ROOM_STATE_LIVE)
    before = entry.runtime_data.coordinator

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    after = entry.runtime_data.coordinator
    # A genuinely rebuilt coordinator, seeded purely from entry.options.
    assert after is not before
    assert after.get_room_state("Salon") == ROOM_STATE_LIVE
    assert after.get_room_state("Lazienka") == ROOM_STATE_OFF

    assert hass.states.get(salon_id).state == ROOM_STATE_LIVE
