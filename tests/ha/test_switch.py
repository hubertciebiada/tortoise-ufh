"""Switch-platform tests: kill-switch and per-room live-control toggles.

Exercises the two writable control flags exposed as ``switch`` entities and the
locked decisions behind them (BUILD_SPEC / prd-control-brain.md §8):

* a single global kill-switch plus one live-control switch per configured room;
* turning the kill-switch on engages ``coordinator.get_kill_switch()`` and
  persists ``CONF_KILL_SWITCH`` into ``entry.options``;
* turning a room live-control on flips ``coordinator.get_live_control(room)`` and
  persists it under the ``CONF_LIVE_CONTROL`` map;
* both survive an ``async_reload`` because a freshly rebuilt coordinator restores
  them from the persisted options.
"""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tortoise_ufh.const import (
    CONF_KILL_SWITCH,
    CONF_LIVE_CONTROL,
    DOMAIN,
)

pytestmark = pytest.mark.ha


def _kill_switch_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Resolve the kill-switch entity id from its stable unique id."""
    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_kill_switch"
    entity_id = registry.async_get_entity_id("switch", DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


def _live_control_entity_id(
    hass: HomeAssistant, entry: MockConfigEntry, room: str
) -> str:
    """Resolve a room's live-control entity id from its stable unique id."""
    registry = er.async_get(hass)
    safe_room = room.lower().replace(" ", "_")
    unique_id = f"{entry.entry_id}_{safe_room}_live_control"
    entity_id = registry.async_get_entity_id("switch", DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


async def _turn(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    """Call switch.turn_on/turn_off and let the ensuing reload settle."""
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_switch_entities_exist(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One global kill-switch plus one live-control per room, all off initially."""
    entry = setup_integration

    kill_id = _kill_switch_entity_id(hass, entry)
    salon_id = _live_control_entity_id(hass, entry, "Salon")
    lazienka_id = _live_control_entity_id(hass, entry, "Lazienka")

    # Distinct entities: one kill-switch, two per-room live controls.
    assert len({kill_id, salon_id, lazienka_id}) == 3

    # New rooms start in shadow and the kill-switch starts disengaged.
    assert hass.states.get(kill_id).state == STATE_OFF
    assert hass.states.get(salon_id).state == STATE_OFF
    assert hass.states.get(lazienka_id).state == STATE_OFF

    coordinator = entry.runtime_data.coordinator
    assert coordinator.get_kill_switch() is False
    assert coordinator.get_live_control("Salon") is False
    assert coordinator.get_live_control("Lazienka") is False


async def test_kill_switch_turn_on_sets_flag_and_persists(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Kill-switch on => coordinator engaged and CONF_KILL_SWITCH persisted."""
    entry = setup_integration
    kill_id = _kill_switch_entity_id(hass, entry)

    await _turn(hass, kill_id, on=True)

    # The toggle persists options, which reloads the entry: read the rebuilt
    # coordinator, not the stale reference.
    assert entry.runtime_data.coordinator.get_kill_switch() is True
    assert entry.options.get(CONF_KILL_SWITCH) is True
    assert hass.states.get(kill_id).state == STATE_ON


async def test_live_control_turn_on_flips_and_persists(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Room live-control on => that room flips live and only it is persisted."""
    entry = setup_integration
    salon_id = _live_control_entity_id(hass, entry, "Salon")

    await _turn(hass, salon_id, on=True)

    coordinator = entry.runtime_data.coordinator
    assert coordinator.get_live_control("Salon") is True
    # The other room is untouched by a per-room toggle.
    assert coordinator.get_live_control("Lazienka") is False

    live_map = entry.options.get(CONF_LIVE_CONTROL)
    assert live_map == {"Salon": True}
    assert hass.states.get(salon_id).state == STATE_ON
    assert (
        hass.states.get(_live_control_entity_id(hass, entry, "Lazienka")).state
        == STATE_OFF
    )


async def test_persisted_flags_survive_reload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A fresh coordinator restores both flags from persisted options on reload."""
    entry = setup_integration
    kill_id = _kill_switch_entity_id(hass, entry)
    salon_id = _live_control_entity_id(hass, entry, "Salon")

    await _turn(hass, kill_id, on=True)
    await _turn(hass, salon_id, on=True)

    before = entry.runtime_data.coordinator

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    after = entry.runtime_data.coordinator
    # A genuinely rebuilt coordinator, seeded purely from entry.options.
    assert after is not before
    assert after.get_kill_switch() is True
    assert after.get_live_control("Salon") is True
    assert after.get_live_control("Lazienka") is False

    assert hass.states.get(kill_id).state == STATE_ON
    assert hass.states.get(salon_id).state == STATE_ON
