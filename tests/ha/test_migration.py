"""Config-entry migration tests: v1 -> v2 -> v3 (the control-state refactors).

The v1 schema carried a per-room ``participates`` flag (in ``entry.data``), a
per-room ``live_control`` map and a global ``kill_switch`` (both in
``entry.options``). v2 collapsed all three into a single canonical per-room
three-state map ``entry.options[CONF_ROOM_STATE]`` (``off`` / ``shadow`` /
``live``), with safety precedence: ``participates == False`` wins as ``off``
even when ``live_control`` was ``True``.

v3 (shadow removal, 2026-07-12; DECISIONS §13) reduces the map to the
two-state ``off`` / ``live``: every value that is not ``off`` or ``live``
(notably the retired ``shadow``, but also corrupted values) becomes ``off`` —
identical write behaviour, since neither shadow nor off ever wrote a command.

These tests pin every ``participates × live_control`` combination through the
FULL v1 -> v3 chain (one ``async_migrate_entry`` call), the standalone v2 -> v3
conversion, the removal of the legacy keys and the retired switch entities, and
that a real v1 entry still loads cleanly after being migrated by
:func:`homeassistant.config_entries.async_setup`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.tortoise_ufh import async_migrate_entry
from custom_components.tortoise_ufh.const import (
    CONF_LIVE_CONTROL,
    CONF_PARTICIPATES,
    CONF_ROOM_NAME,
    CONF_ROOM_STATE,
    CONF_ROOMS,
    DOMAIN,
    ROOM_STATE_LIVE,
    ROOM_STATE_OFF,
    ROOM_STATE_SHADOW,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

pytestmark = pytest.mark.ha

# Legacy option / entity key for the retired global kill-switch (v2 dropped the
# named constant; it survives here only to assert the migration purges it).
_LEGACY_KILL_SWITCH = "kill_switch"


def _entry(
    hass: HomeAssistant,
    *,
    rooms: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
    version: int = 1,
    unique_id: str = "50.5_19.5",
) -> MockConfigEntry:
    """Add a :class:`MockConfigEntry` of the given version to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LATITUDE: 50.5,
            CONF_LONGITUDE: 19.5,
            CONF_ROOMS: rooms,
        },
        options=options or {},
        version=version,
        title="Tortoise-UFH",
        unique_id=unique_id,
    )
    entry.add_to_hass(hass)
    return entry


# ``participates`` value in entry.data (``None`` == key absent, default True),
# ``live_control`` value in options (``None`` == key absent) -> expected state
# AFTER the full v1 -> v3 chain (the intermediate v2 "shadow" becomes "off").
_COMBOS: Iterable[tuple[bool | None, bool | None, str]] = (
    (False, True, ROOM_STATE_OFF),  # participates=False wins over live=True
    (False, False, ROOM_STATE_OFF),
    (False, None, ROOM_STATE_OFF),
    (True, True, ROOM_STATE_LIVE),
    (True, False, ROOM_STATE_OFF),  # v2 shadow -> v3 off
    (True, None, ROOM_STATE_OFF),  # live key absent -> shadow -> off
    (None, True, ROOM_STATE_LIVE),  # participates absent -> default True
    (None, False, ROOM_STATE_OFF),
    (None, None, ROOM_STATE_OFF),  # no options at all -> shadow -> off
)


@pytest.mark.parametrize(("participates", "live", "expected"), _COMBOS)
async def test_migrate_v1_chain_state_precedence(
    hass: HomeAssistant,
    participates: bool | None,
    live: bool | None,
    expected: str,
) -> None:
    """Every participates × live_control combo lands on the right v3 state.

    A single ``async_migrate_entry`` call must run BOTH blocks (v1 -> v2 -> v3).
    """
    room: dict[str, Any] = {CONF_ROOM_NAME: "Salon"}
    if participates is not None:
        room[CONF_PARTICIPATES] = participates
    options: dict[str, Any] = {}
    if live is not None:
        options[CONF_LIVE_CONTROL] = {"Salon": live}

    entry = _entry(hass, rooms=[room], options=options)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 3
    assert entry.options[CONF_ROOM_STATE] == {"Salon": expected}
    # No shadow literal may survive the chain.
    assert ROOM_STATE_SHADOW not in entry.options[CONF_ROOM_STATE].values()
    # Legacy keys are purged, not merely ignored.
    assert CONF_PARTICIPATES not in entry.data[CONF_ROOMS][0]
    assert CONF_LIVE_CONTROL not in entry.options
    assert _LEGACY_KILL_SWITCH not in entry.options


async def test_migrate_v1_no_options_defaults_every_room_to_off(
    hass: HomeAssistant,
) -> None:
    """A v1 entry with no options migrates every room to the safe off state."""
    rooms = [
        {CONF_ROOM_NAME: "Salon", CONF_PARTICIPATES: True},
        {CONF_ROOM_NAME: "Lazienka", CONF_PARTICIPATES: True},
    ]
    entry = _entry(hass, rooms=rooms, options={})

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 3
    assert entry.options[CONF_ROOM_STATE] == {
        "Salon": ROOM_STATE_OFF,
        "Lazienka": ROOM_STATE_OFF,
    }


async def test_migrate_ignores_unknown_rooms_in_live_map(
    hass: HomeAssistant,
) -> None:
    """A live_control entry for a room that no longer exists is dropped."""
    rooms = [{CONF_ROOM_NAME: "Salon", CONF_PARTICIPATES: True}]
    options = {
        CONF_LIVE_CONTROL: {"Salon": False, "Ghost": True},
        _LEGACY_KILL_SWITCH: True,
    }
    entry = _entry(hass, rooms=rooms, options=options)

    assert await async_migrate_entry(hass, entry)

    # Only the real room appears in the state map; the phantom room is not.
    assert entry.options[CONF_ROOM_STATE] == {"Salon": ROOM_STATE_OFF}
    assert "Ghost" not in entry.options[CONF_ROOM_STATE]
    # Both legacy option keys are gone.
    assert CONF_LIVE_CONTROL not in entry.options
    assert _LEGACY_KILL_SWITCH not in entry.options


async def test_migrate_removes_retired_switch_entities(
    hass: HomeAssistant,
) -> None:
    """The retired kill-switch + per-room live-control switches are purged."""
    rooms = [{CONF_ROOM_NAME: "Salon", CONF_PARTICIPATES: True}]
    entry = _entry(hass, rooms=rooms, options={CONF_LIVE_CONTROL: {"Salon": True}})

    registry = er.async_get(hass)
    # Pre-seed the legacy switch entities under their frozen v1 unique ids.
    kill_entry = registry.async_get_or_create(
        "switch", DOMAIN, f"{entry.entry_id}_{_LEGACY_KILL_SWITCH}", config_entry=entry
    )
    live_entry = registry.async_get_or_create(
        "switch", DOMAIN, f"{entry.entry_id}_salon_live_control", config_entry=entry
    )
    assert registry.async_get(kill_entry.entity_id) is not None
    assert registry.async_get(live_entry.entity_id) is not None

    assert await async_migrate_entry(hass, entry)

    # Both stale switch entities are gone from the registry.
    assert registry.async_get(kill_entry.entity_id) is None
    assert registry.async_get(live_entry.entity_id) is None
    assert (
        registry.async_get_entity_id(
            "switch", DOMAIN, f"{entry.entry_id}_{_LEGACY_KILL_SWITCH}"
        )
        is None
    )


# -- v2 -> v3 (shadow removal, 2026-07-12) -----------------------------------


@pytest.mark.parametrize(
    ("v2_state", "expected"),
    [
        (ROOM_STATE_SHADOW, ROOM_STATE_OFF),
        (ROOM_STATE_OFF, ROOM_STATE_OFF),
        (ROOM_STATE_LIVE, ROOM_STATE_LIVE),
        ("garbage", ROOM_STATE_OFF),
    ],
)
async def test_migrate_v2_state_conversion(
    hass: HomeAssistant, v2_state: str, expected: str
) -> None:
    """v2 -> v3 maps shadow (and garbage) to off; off/live stay themselves."""
    entry = _entry(
        hass,
        rooms=[{CONF_ROOM_NAME: "Salon"}],
        options={CONF_ROOM_STATE: {"Salon": v2_state}},
        version=2,
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 3
    assert entry.options[CONF_ROOM_STATE] == {"Salon": expected}


async def test_migrate_v2_without_state_map_does_not_invent_one(
    hass: HomeAssistant,
) -> None:
    """A v2 entry lacking CONF_ROOM_STATE stays without it (coordinator defaults)."""
    entry = _entry(
        hass,
        rooms=[{CONF_ROOM_NAME: "Salon"}],
        options={"unrelated": True},
        version=2,
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 3
    assert CONF_ROOM_STATE not in entry.options
    assert entry.options.get("unrelated") is True


async def test_migrate_rejects_newer_version(hass: HomeAssistant) -> None:
    """A version newer than the code knows about is refused (no downgrade)."""
    entry = _entry(hass, rooms=[{CONF_ROOM_NAME: "Salon"}])
    hass.config_entries.async_update_entry(entry, version=4)

    assert await async_migrate_entry(hass, entry) is False


async def test_v1_entry_sets_up_and_migrates(
    hass: HomeAssistant,
    register_sources: None,
    entry_data: dict[str, Any],
) -> None:
    """A real v1 entry migrates through the chain and loads via async_setup.

    ``entry_data`` (from conftest) carries ``participates`` on both rooms; a
    seeded ``live_control`` map plus a stray ``kill_switch`` exercise the full
    legacy surface. Salon (live) stays live; Lazienka (no live_control entry)
    passes through the intermediate shadow and lands on off. Actuator services
    are mocked, so the first refresh's live writes are captured, not dispatched.
    """
    for domain, service in (
        ("number", "set_value"),
        ("climate", "set_hvac_mode"),
        ("climate", "set_temperature"),
    ):
        async_mock_service(hass, domain, service)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data,
        options={
            CONF_LIVE_CONTROL: {"Salon": True},
            _LEGACY_KILL_SWITCH: True,
        },
        version=1,
        title="Tortoise-UFH",
        unique_id="50.5_19.5",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The entry loaded on the migrated (v3) schema.
    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 3

    coordinator = entry.runtime_data.coordinator
    # participates=True + live=True -> live; the other room -> shadow -> off.
    assert coordinator.get_room_state("Salon") == ROOM_STATE_LIVE
    assert coordinator.get_room_state("Lazienka") == ROOM_STATE_OFF

    assert entry.options[CONF_ROOM_STATE] == {
        "Salon": ROOM_STATE_LIVE,
        "Lazienka": ROOM_STATE_OFF,
    }
    # Legacy keys are gone from options and every room dict.
    assert CONF_LIVE_CONTROL not in entry.options
    assert _LEGACY_KILL_SWITCH not in entry.options
    for room in entry.data[CONF_ROOMS]:
        assert CONF_PARTICIPATES not in room
