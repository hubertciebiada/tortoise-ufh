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

v6 (issue #31, 2026-10-22; DECISIONS §35) reduces the global tuning — once a
FULL ``ControllerConfig`` snapshot in ``data`` and ``options`` — to sparse
overrides in ``options``: old shipped defaults, values equal to the current
default and non-user-facing fields are dropped.

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
    CONF_CONTROLLER,
    CONF_LIVE_CONTROL,
    CONF_PARTICIPATES,
    CONF_ROOM_NAME,
    CONF_ROOM_STATE,
    CONF_ROOM_TUNING,
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

    assert entry.version == 6
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

    assert entry.version == 6
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

    assert entry.version == 6
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

    assert entry.version == 6
    assert CONF_ROOM_STATE not in entry.options
    assert entry.options.get("unrelated") is True


async def test_migrate_v3_drops_the_retired_mode_entity(hass: HomeAssistant) -> None:
    """v3 -> v4 (DECISIONS §27) purges the external mode-entity key from data.

    The mode is owned by the integration (the home-mode select) since v0.19.0,
    so the legacy ``entity_mode`` pointer must not linger in ``.storage``. Every
    other data key survives untouched.
    """
    entry = _entry(hass, rooms=[{CONF_ROOM_NAME: "Salon"}], version=3)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "entity_mode": "input_select.home_mode"}
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 6
    assert "entity_mode" not in entry.data
    assert entry.data[CONF_ROOMS] == [{CONF_ROOM_NAME: "Salon"}]


async def test_migrate_v3_without_a_mode_entity_just_bumps(
    hass: HomeAssistant,
) -> None:
    """A v3 entry that never had a mode entity migrates on the version alone."""
    entry = _entry(hass, rooms=[{CONF_ROOM_NAME: "Salon"}], version=3)
    data_before = dict(entry.data)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 6
    assert dict(entry.data) == data_before


async def test_v4_to_v5_strips_the_retired_flow_watchdog_keys(
    hass: HomeAssistant,
) -> None:
    """v4 -> v5 drops the removed S6 tuning keys everywhere (DECISIONS §34).

    The keys go from the global tuning in data and options and from every
    per-room override; an override left empty is dropped, other knobs stay.
    """
    entry = _entry(
        hass,
        rooms=[{CONF_ROOM_NAME: "Salon"}, {CONF_ROOM_NAME: "Toaleta"}],
        options={
            CONF_CONTROLLER: {
                "kp": 12.0,
                "flow_epsilon_k": 0.4,
                "flow_open_threshold_pct": 15.0,
                "flow_response_window_min": 55.0,
            },
            CONF_ROOM_TUNING: {
                "Toaleta": {"flow_epsilon_k": 1.0},
                "Salon": {"ki": 0.5, "flow_response_window_min": 60.0},
            },
        },
        version=4,
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_CONTROLLER: {"flow_epsilon_k": 0.3}}
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 6
    # v5 -> v6 then folds the (emptied) data copy into the sparse options.
    assert CONF_CONTROLLER not in entry.data
    assert entry.options[CONF_CONTROLLER] == {"kp": 12.0}
    assert entry.options[CONF_ROOM_TUNING] == {"Salon": {"ki": 0.5}}


def _full_snapshot(**changes: Any) -> dict[str, Any]:
    """A full v5-era ``asdict(ControllerConfig())`` snapshot with ``changes``."""
    from dataclasses import asdict

    from custom_components.tortoise_ufh.core.config import ControllerConfig

    return {**asdict(ControllerConfig()), **changes}


async def test_v5_to_v6_reduces_snapshots_to_overrides(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """v5 -> v6 keeps only genuine overrides of the global tuning (issue #31).

    The live install's case: a setup-time snapshot in data carrying the
    pre-§8 gains (kp 8 / ki 0.02 / kt 6) and the pre-K2b write threshold
    (2 pp). Old shipped defaults, values equal to today's default and
    non-user-facing fields are dropped from both data and options; a
    genuinely user-set value survives; per-room overrides are untouched.
    """
    entry = _entry(
        hass,
        rooms=[{CONF_ROOM_NAME: "Salon"}],
        options={
            CONF_CONTROLLER: _full_snapshot(
                kp=8.0,
                ki=0.02,
                kt=6.0,
                valve_write_threshold_pct=2.0,
                deadband_c=0.4,
            ),
            CONF_ROOM_TUNING: {"Salon": {"kp": 8.0}},
        },
        version=5,
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_CONTROLLER: _full_snapshot(
                kp=8.0, ki=0.02, kt=6.0, valve_write_threshold_pct=2.0
            ),
        },
    )

    with caplog.at_level("INFO"):
        assert await async_migrate_entry(hass, entry)

    assert entry.version == 6
    assert CONF_CONTROLLER not in entry.data
    assert entry.options[CONF_CONTROLLER] == {"deadband_c": 0.4}
    # A per-room override is already sparse and deliberate: kept as is.
    assert entry.options[CONF_ROOM_TUNING] == {"Salon": {"kp": 8.0}}
    assert "dropped stored defaults" in caplog.text
    assert "valve_write_threshold_pct" in caplog.text


async def test_v5_to_v6_keeps_user_set_values(hass: HomeAssistant) -> None:
    """Values that are neither a current nor an old default survive v5 -> v6.

    An options value wins over the data copy (the old ``data <- options``
    merge), and an all-default snapshot leaves no controller key at all.
    """
    entry = _entry(
        hass,
        rooms=[{CONF_ROOM_NAME: "Salon"}],
        options={CONF_CONTROLLER: {"kp": 16.0, "outdoor_ff_enabled": True}},
        version=5,
    )
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_CONTROLLER: _full_snapshot(kp=10.0, ki=0.003)},
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 6
    assert CONF_CONTROLLER not in entry.data
    assert entry.options[CONF_CONTROLLER] == {
        "kp": 16.0,
        "ki": 0.003,
        "outdoor_ff_enabled": True,
    }


async def test_v5_to_v6_all_defaults_leaves_no_tuning(hass: HomeAssistant) -> None:
    """A pure default snapshot migrates to no stored global tuning at all."""
    entry = _entry(
        hass,
        rooms=[{CONF_ROOM_NAME: "Salon"}],
        options={CONF_CONTROLLER: _full_snapshot()},
        version=5,
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_CONTROLLER: _full_snapshot()}
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 6
    assert CONF_CONTROLLER not in entry.data
    assert CONF_CONTROLLER not in entry.options


_PRE_ISSUE_32_DEFAULTS: dict[str, float] = {
    "boost_offset_c": 1.0,
    "fast_min_on_minutes": 10.0,
    "fast_min_off_minutes": 10.0,
    "fast_target_offset_k": 1.0,
    "dew_margin_k": 2.0,
    "cooling_supply_base_c": 18.0,
    "dry_dew_max_c": 17.0,
    "hp_flicker_band_k": 1.5,
    "hp_flicker_stuck_minutes": 10.0,
    "hp_flicker_min_off_minutes": 20.0,
    "hp_flicker_max_starts_per_h": 2.0,
    "hp_flicker_min_open_pct": 250.0,
}


async def test_v5_to_v6_drops_the_pre_issue_32_defaults(hass: HomeAssistant) -> None:
    """A snapshot frozen before issue #32 picks up the retuned defaults.

    Every default retuned by issue #32 is listed in
    ``SUPERSEDED_CONTROLLER_DEFAULTS``, so the old value — indistinguishable
    from a frozen default — is dropped; a value that never was a default
    (fast_min_off 20) survives as a genuine override.
    """
    entry = _entry(
        hass,
        rooms=[{CONF_ROOM_NAME: "Salon"}],
        options={
            CONF_CONTROLLER: _full_snapshot(
                **{**_PRE_ISSUE_32_DEFAULTS, "fast_min_off_minutes": 20.0}
            )
        },
        version=5,
    )
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_CONTROLLER: _full_snapshot(**_PRE_ISSUE_32_DEFAULTS)},
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 6
    assert CONF_CONTROLLER not in entry.data
    assert entry.options[CONF_CONTROLLER] == {"fast_min_off_minutes": 20.0}


async def test_migrate_rejects_newer_version(hass: HomeAssistant) -> None:
    """A version newer than the code knows about is refused (no downgrade)."""
    entry = _entry(hass, rooms=[{CONF_ROOM_NAME: "Salon"}])
    hass.config_entries.async_update_entry(entry, version=7)

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
    assert entry.version == 6

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
