"""Integration tests for the loop water-probe health path (issue #16).

Covers the adapter half of the probe-health feature end to end: the
:class:`~custom_components.tortoise_ufh.readers.SourceReader` water sample gate
(DS18B20 sentinels, jump confirmation, the short hold), the coordinator
hiding a flagged probe from the core (with the manifold main-supply fallback),
the ``probe_drift`` room flag, the ``probe_fault`` binary sensor and its
attributes, the flowing supply-vs-main check, the persisted baselines and the
``reset_probe_baselines`` service.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tortoise_ufh.const import (
    CONF_MANIFOLDS,
    DOMAIN,
    ROOM_STATE_LIVE,
)
from custom_components.tortoise_ufh.services import SERVICE_RESET_PROBE_BASELINES

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.ha

_TEMP_ATTRS = {"unit_of_measurement": "°C", "device_class": "temperature"}
_SUPPLY = "sensor.salon_supply"
_MAIN_SUPPLY = "sensor.east_main_supply"
_MAIN_RETURN = "sensor.east_main_return"


def _coordinator(entry: MockConfigEntry) -> Any:
    return entry.runtime_data.coordinator


async def _refresh(hass: HomeAssistant, coordinator: Any) -> None:
    await coordinator.async_refresh()
    await hass.async_block_till_done()


def _salon_loop(coordinator: Any) -> Any:
    room_cfg = coordinator._room_configs[coordinator._room_names.index("Salon")]
    return coordinator._build_loops(room_cfg)[0]


def _manifold() -> dict[str, Any]:
    return {
        "manifold_id": "east",
        "name": "East",
        "circuits": 4,
        "side": "left",
        "entity_supply_main": _MAIN_SUPPLY,
        "entity_return_main": _MAIN_RETURN,
        "loops": [
            {"position": 1, "entity_valve": "number.salon_valve", "label": ""},
            {"position": 2, "entity_valve": "number.lazienka_valve", "label": ""},
        ],
    }


def _probe_fault_entity(hass: HomeAssistant, entry_id: str, room: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry_id}_{room.lower()}_probe_fault"
    )
    assert entity_id is not None
    return entity_id


def _stored_drift(probe: str) -> dict[str, Any]:
    return {
        "version": 1,
        "data": {
            "clock_s": 1000.0,
            "probes": {
                probe: {
                    "baseline": [0.3, 0.3, 0.3],
                    "last_admit_s": 0.0,
                    "last_deviation_k": 5.3,
                    "drift_streak": 2,
                    "ok_streak": 0,
                    "drift": True,
                }
            },
        },
    }


# ---------------------------------------------------------------------------
# Sample gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentinel", ["85.0", "-127.0", "70.5", "-0.5"])
async def test_sentinel_sample_never_reaches_s1(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sentinel: str
) -> None:
    """A DS18B20 85.0 degC power-on sample does not trip S1 (acceptance 1)."""
    coordinator = _coordinator(setup_integration)
    hass.states.async_set(_SUPPLY, sentinel, _TEMP_ATTRS)
    await _refresh(hass, coordinator)

    assert "s1_floor_overheat" not in coordinator.data.rooms["Salon"].report.flags
    # The last accepted value bridges the rejected sample.
    assert _salon_loop(coordinator).supply_temperature_c == pytest.approx(30.0)


async def test_rejected_sample_is_held_only_briefly(
    hass: HomeAssistant, setup_integration: MockConfigEntry, freezer: Any
) -> None:
    """After the short hold a still-implausible probe reads as missing."""
    coordinator = _coordinator(setup_integration)
    freezer.tick(timedelta(seconds=601))
    hass.states.async_set(_SUPPLY, "85.0", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    assert _salon_loop(coordinator).supply_temperature_c is None


async def test_jump_needs_confirmation(
    hass: HomeAssistant, setup_integration: MockConfigEntry, freezer: Any
) -> None:
    """A >15 K step is held once and accepted on a consistent later sample."""
    coordinator = _coordinator(setup_integration)
    reader = coordinator._reader
    hass.states.async_set(_SUPPLY, "50.0", _TEMP_ATTRS)
    assert reader.read_water_temperature(_SUPPLY) == pytest.approx(30.0)
    freezer.tick(timedelta(seconds=10))
    hass.states.async_set(_SUPPLY, "50.5", _TEMP_ATTRS)
    assert reader.read_water_temperature(_SUPPLY) == pytest.approx(30.0)
    freezer.tick(timedelta(seconds=300))
    hass.states.async_set(_SUPPLY, "50.2", _TEMP_ATTRS)
    assert reader.read_water_temperature(_SUPPLY) == pytest.approx(50.2)
    # A normal step is accepted at once.
    hass.states.async_set(_SUPPLY, "40.0", _TEMP_ATTRS)
    assert reader.read_water_temperature(_SUPPLY) == pytest.approx(40.0)
    assert reader.read_water_temperature(None) is None


async def test_unavailable_probe_reads_missing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, freezer: Any
) -> None:
    coordinator = _coordinator(setup_integration)
    freezer.tick(timedelta(seconds=601))
    hass.states.async_set(_SUPPLY, "unavailable", _TEMP_ATTRS)
    assert coordinator._reader.read_water_temperature(_SUPPLY) is None


# ---------------------------------------------------------------------------
# Flagged probe: hidden, fallback, flag, entity, service, persistence
# ---------------------------------------------------------------------------


async def test_flagged_supply_falls_back_to_main_supply(
    hass: HomeAssistant,
    register_sources: None,
    entry_data: dict[str, Any],
    hass_storage: dict[str, Any],
) -> None:
    """A drift-flagged supply is hidden and replaced by the manifold main."""
    hass.states.async_set(_MAIN_SUPPLY, "31.0", _TEMP_ATTRS)
    hass.states.async_set(_MAIN_RETURN, "27.0", _TEMP_ATTRS)
    hass.states.async_set("sensor.salon_return", "26.0", _TEMP_ATTRS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data, CONF_MANIFOLDS: [_manifold()]},
        options={},
        title="Tortoise-UFH",
        unique_id="50.5_19.5",
    )
    entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.probe_health.{entry.entry_id}"] = _stored_drift(_SUPPLY)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(entry)

    loop = _salon_loop(coordinator)
    assert loop.supply_temperature_c == pytest.approx(31.0)
    assert loop.return_temperature_c == pytest.approx(26.0)
    data = coordinator.data
    assert "probe_drift" in data.rooms["Salon"].report.flags
    assert "probe_drift" not in data.rooms["Lazienka"].report.flags
    assert data.probe_health[_SUPPLY]["flagged"] is True
    assert data.probe_health[_SUPPLY]["reasons"] == ["still_water_drift"]

    salon = hass.states.get(_probe_fault_entity(hass, entry.entry_id, "Salon"))
    assert salon is not None
    assert salon.state == "on"
    probes = salon.attributes["probes"]
    assert set(probes) == {_SUPPLY, "sensor.salon_return"}
    assert probes[_SUPPLY]["baseline_k"] == pytest.approx(0.3)
    lazienka = hass.states.get(_probe_fault_entity(hass, entry.entry_id, "Lazienka"))
    assert lazienka is not None
    assert lazienka.state == "off"

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_PROBE_BASELINES,
        {"entity_id": [_SUPPLY]},
        blocking=True,
    )
    await _refresh(hass, coordinator)
    assert "probe_drift" not in coordinator.data.rooms["Salon"].report.flags
    assert _salon_loop(coordinator).supply_temperature_c == pytest.approx(30.0)


async def test_flagged_return_and_unassigned_supply_read_missing(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """Without a manifold or global probe a flagged supply has no fallback."""
    coordinator = _coordinator(setup_integration)
    coordinator._probes._monitor = type(coordinator._probes._monitor).from_dict(
        {
            "probes": {
                _SUPPLY: {"baseline": [0.0, 0.0, 0.0], "drift": True},
                "sensor.salon_return": {"baseline": [0.0, 0.0, 0.0], "drift": True},
            }
        }
    )
    await _refresh(hass, coordinator)
    loop = _salon_loop(coordinator)
    assert loop.supply_temperature_c is None
    assert loop.return_temperature_c is None
    assert "probe_drift" in coordinator.data.rooms["Salon"].report.flags

    # The whole-home reset clears every probe.
    await hass.services.async_call(
        DOMAIN, SERVICE_RESET_PROBE_BASELINES, {}, blocking=True
    )
    await _refresh(hass, coordinator)
    assert "probe_drift" not in coordinator.data.rooms["Salon"].report.flags


async def test_baselines_persist_across_unload(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """The learned state is flushed to its own Store on unload."""
    entry = setup_integration
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[f"{DOMAIN}.probe_health.{entry.entry_id}"]["data"]
    assert set(stored["probes"]) == {
        _SUPPLY,
        "sensor.salon_return",
        "sensor.lazienka_supply",
        "sensor.lazienka_return",
    }


async def test_flowing_supply_far_from_main_is_flagged(
    hass: HomeAssistant,
    register_sources: None,
    entry_data: dict[str, Any],
    freezer: Any,
) -> None:
    """An open loop whose supply sits 4 K off the main supply is flagged."""
    hass.states.async_set(_MAIN_SUPPLY, "28.2", _TEMP_ATTRS)
    hass.states.async_set(_MAIN_RETURN, "25.0", _TEMP_ATTRS)
    hass.states.async_set(_SUPPLY, "32.2", _TEMP_ATTRS)
    hass.states.async_set("number.salon_valve", "60", {"unit_of_measurement": "%"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data, CONF_MANIFOLDS: [_manifold()]},
        options={},
        title="Tortoise-UFH",
        unique_id="50.5_19.5",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(entry)
    # Salon stays OFF: its valve position comes from the actuator feedback.
    assert coordinator.get_room_state("Salon") != ROOM_STATE_LIVE
    for _ in range(7):
        freezer.tick(timedelta(minutes=5))
        await _refresh(hass, coordinator)
    assert coordinator.data.probe_health[_SUPPLY]["reasons"] == ["supply_vs_main"]
    assert "probe_drift" in coordinator.data.rooms["Salon"].report.flags
    # Hidden from the core: the main supply stands in.
    assert _salon_loop(coordinator).supply_temperature_c == pytest.approx(28.2)


async def test_failed_probe_update_falls_back_to_gated_reads(
    hass: HomeAssistant, setup_integration: MockConfigEntry, monkeypatch: Any
) -> None:
    """A probe-health failure never breaks the cycle or serves stale reads."""
    coordinator = _coordinator(setup_integration)

    def _boom(**_: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(coordinator._probes, "update", _boom)
    hass.states.async_set(_SUPPLY, "31.5", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    assert coordinator.data.algorithm_status == "running"
    assert _salon_loop(coordinator).supply_temperature_c == pytest.approx(31.5)
