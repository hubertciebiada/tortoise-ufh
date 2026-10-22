"""HA tests for the opt-in heating compressor kick (issue #34, 2026-10-22).

Drives the real coordinator with the heat-pump link's heating setpoint,
compressor-frequency and outlet entities wired, and asserts what it writes to
``number.set_value`` (intercepted with ``async_mock_service``, which never
updates the entity state — the tests apply a write to the pump entity by
hand where a real pump would).

With the default tuning and the conftest outdoor temperature of 5 degC the
heating curve is ``26 + 0.5 * (15 - 5) = 31 degC``; the pump entity starts at
31 degC (in sync), so only the kick can step it. Every cycle is fed a 300 s
dt, so the 15-min idle threshold is crossed on the 4th idle cycle.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.tortoise_ufh.const import (
    CONF_CONTROLLER,
    CONF_ENTITY_HP_COMPRESSOR_FREQ,
    CONF_ENTITY_HP_HEATING_SETPOINT,
    CONF_ENTITY_HP_OUTLET_TEMP,
    CONF_HEAT_PUMP,
    DOMAIN,
    ROOM_STATE_LIVE,
)
from custom_components.tortoise_ufh.core.models import Mode

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

pytestmark = pytest.mark.ha

_HEAT = "number.hp_heat_request"
_FREQ = "sensor.hp_compressor_freq"
_OUTLET = "sensor.hp_outlet_temp"
_TEMP_ATTRS = {"unit_of_measurement": "°C", "device_class": "temperature"}
_NUMBER_ATTRS = {"min": 20.0, "max": 55.0, "step": 1.0}
_KICK_ON = {"heat_kick_enabled": True}


async def _setup(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    *,
    controller: dict[str, Any] | None = None,
    pump_c: float = 31.0,
    outlet_c: float = 27.0,
) -> tuple[Any, list[ServiceCall]]:
    """Set up an entry with the gate inputs wired; return (coordinator, calls).

    The Salon room is LIVE and the mode is HEATING, so the link may write;
    the compressor starts idle.
    """
    hass.states.async_set(_HEAT, str(pump_c), _NUMBER_ATTRS)
    hass.states.async_set(_FREQ, "0", {})
    hass.states.async_set(_OUTLET, str(outlet_c), _TEMP_ATTRS)
    options: dict[str, Any] = {
        CONF_HEAT_PUMP: {
            CONF_ENTITY_HP_HEATING_SETPOINT: _HEAT,
            CONF_ENTITY_HP_COMPRESSOR_FREQ: _FREQ,
            CONF_ENTITY_HP_OUTLET_TEMP: _OUTLET,
        }
    }
    if controller:
        options[CONF_CONTROLLER] = controller
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data,
        options=options,
        title="Tortoise-UFH",
        unique_id="50.5_19.5",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    calls = async_mock_service(hass, "number", "set_value")
    coordinator._mode = Mode.HEATING
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    return coordinator, calls


async def _refresh(hass: HomeAssistant, coordinator: Any) -> dict[str, Any]:
    """Run one 300 s cycle and return the ``heat_pump.heating`` payload."""
    coordinator._last_step_monotonic = time.monotonic() - 300.0
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    hp = coordinator.data.heat_pump
    assert hp is not None and hp.heating is not None
    return dict(hp.heating)


def _heat_writes(calls: list[ServiceCall]) -> list[float]:
    """The values written to the heating setpoint entity, in order."""
    return [c.data["value"] for c in calls if c.data["entity_id"] == _HEAT]


def _sensor(hass: HomeAssistant, coordinator: Any, key: str) -> str:
    """The state of a global diagnostic sensor by its key."""
    entry_id = coordinator.config_entry.entry_id
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry_id}_{key}"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


async def _idle_until_kick(
    hass: HomeAssistant, coordinator: Any, calls: list[ServiceCall]
) -> dict[str, Any]:
    """Three idle cycles without a write, then the fired cycle's payload."""
    for _ in range(3):
        heating = await _refresh(hass, coordinator)
        assert heating["kick"]["reason"] == "idle_wait"
        assert heating["gate"]["reason"] == "in_sync"
    assert _heat_writes(calls) == []
    return await _refresh(hass, coordinator)


async def test_kick_fires_and_restores_while_running(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """15 min idle, outlet 4 K low: +1 K through the gate, back on the start."""
    coordinator, calls = await _setup(hass, entry_data, controller=_KICK_ON)
    heating = await _idle_until_kick(hass, coordinator, calls)

    kick = heating["kick"]
    assert kick["reason"] == "fired"
    assert kick["state"] == "kick"
    assert kick["kick_target_c"] == pytest.approx(32.0)
    assert kick["offset_k"] == pytest.approx(1.0)
    assert kick["deficit_k"] == pytest.approx(4.0)
    assert kick["enabled"] is True
    assert kick["idle_minutes"] == pytest.approx(15.0)
    assert kick["deficit_threshold_k"] == pytest.approx(3.5)
    assert kick["timeout_minutes"] == pytest.approx(5.0)
    assert heating["gate"]["reason"] == "write_increase_idle"
    assert heating["target_c"] == pytest.approx(32.0)
    assert _heat_writes(calls) == [32.0]
    assert _sensor(hass, coordinator, "hp_heating_kick") == "kick"

    # The pump applies the kick and the compressor starts: the normal target
    # is written back as an ordinary decrease while running.
    hass.states.async_set(_HEAT, "32.0", _NUMBER_ATTRS)
    hass.states.async_set(_FREQ, "38", {})
    hass.states.async_set(_OUTLET, "27.5", _TEMP_ATTRS)
    heating = await _refresh(hass, coordinator)
    assert heating["kick"]["reason"] == "started"
    assert heating["kick"]["last_result"] == "started"
    assert heating["kick"]["offset_k"] == pytest.approx(0.0)
    assert heating["gate"]["reason"] == "write_decrease_running"
    assert _heat_writes(calls) == [32.0, 31.0]
    assert _sensor(hass, coordinator, "hp_heating_kick") == "ready"
    assert _sensor(hass, coordinator, "hp_heating_kick_last") == "started"


async def test_restore_waits_for_the_decrease_rule(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """A start with a hot outlet: the restore is held until outlet < 31 + 2."""
    coordinator, calls = await _setup(hass, entry_data, controller=_KICK_ON)
    await _idle_until_kick(hass, coordinator, calls)
    hass.states.async_set(_HEAT, "32.0", _NUMBER_ATTRS)
    hass.states.async_set(_FREQ, "38", {})
    hass.states.async_set(_OUTLET, "33.0", _TEMP_ATTRS)
    heating = await _refresh(hass, coordinator)
    assert heating["kick"]["reason"] == "started"
    assert heating["gate"]["reason"] == "hold_decrease_running"
    assert _heat_writes(calls) == [32.0]

    hass.states.async_set(_OUTLET, "32.9", _TEMP_ATTRS)
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "write_decrease_running"
    assert _heat_writes(calls) == [32.0, 31.0]


async def test_timeout_stops_for_the_idle_period(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """No start within 5 min: offset removed, lower target held while idle."""
    coordinator, calls = await _setup(hass, entry_data, controller=_KICK_ON)
    await _idle_until_kick(hass, coordinator, calls)
    hass.states.async_set(_HEAT, "32.0", _NUMBER_ATTRS)

    heating = await _refresh(hass, coordinator)
    assert heating["kick"]["reason"] == "timeout"
    assert heating["kick"]["state"] == "done"
    assert heating["kick"]["last_result"] == "timeout"
    # Writing 31 now would be an idle decrease below the outlet... held.
    assert heating["gate"]["reason"] == "hold_decrease_idle"
    assert _sensor(hass, coordinator, "hp_heating_kick") == "done"
    assert _sensor(hass, coordinator, "hp_heating_kick_last") == "timeout"

    # The same idle period never kicks again, however long it lasts.
    for _ in range(6):
        heating = await _refresh(hass, coordinator)
        assert heating["kick"]["reason"] == "done"
    assert _heat_writes(calls) == [32.0]

    # Once the compressor runs, the restore is an ordinary decrease.
    hass.states.async_set(_FREQ, "30", {})
    heating = await _refresh(hass, coordinator)
    assert heating["kick"]["reason"] == "running"
    assert heating["gate"]["reason"] == "write_decrease_running"
    assert _heat_writes(calls) == [32.0, 31.0]


async def test_disabled_by_default(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """Without the knob nothing of the kick runs and nothing is written."""
    coordinator, calls = await _setup(hass, entry_data)
    for _ in range(8):
        heating = await _refresh(hass, coordinator)
        assert heating["kick"] is None
        assert heating["gate"]["reason"] == "in_sync"
    assert _heat_writes(calls) == []
    assert _sensor(hass, coordinator, "hp_heating_kick") == "off"
    assert _sensor(hass, coordinator, "hp_heating_kick_last") == "unknown"


async def test_hot_outlet_never_kicks(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """A DHW cycle shows a hot outlet: no deficit, no kick."""
    coordinator, calls = await _setup(
        hass, entry_data, controller=_KICK_ON, outlet_c=48.0
    )
    for _ in range(8):
        heating = await _refresh(hass, coordinator)
    assert heating["kick"]["reason"] == "no_deficit"
    assert _heat_writes(calls) == []


async def test_kick_cancelled_when_writes_stop(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """Every room off mid-kick: cancelled, nothing more written."""
    coordinator, calls = await _setup(hass, entry_data, controller=_KICK_ON)
    await _idle_until_kick(hass, coordinator, calls)
    hass.states.async_set(_HEAT, "32.0", _NUMBER_ATTRS)
    coordinator._room_states["Salon"] = "off"
    heating = await _refresh(hass, coordinator)
    assert heating["kick"]["reason"] == "cancelled"
    assert heating["kick"]["last_result"] == "cancelled"
    assert _heat_writes(calls) == [32.0]
    assert _sensor(hass, coordinator, "hp_heating_kick_last") == "cancelled"


async def test_kick_needs_the_gate(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """heating_gate_enabled off: the kick never acts."""
    coordinator, calls = await _setup(
        hass,
        entry_data,
        controller={"heat_kick_enabled": True, "heating_gate_enabled": False},
    )
    for _ in range(6):
        heating = await _refresh(hass, coordinator)
        assert heating["kick"]["reason"] == "gate_disabled"
    # The ungated path writes the in-sync target once (pre-#33 behaviour),
    # never the kick.
    assert 32.0 not in _heat_writes(calls)


async def test_get_config_exposes_the_kick_sensor(
    hass: HomeAssistant,
    register_sources: None,
    entry_data: dict[str, Any],
    hass_ws_client: Any,
) -> None:
    """The panel counts kicks from the hp_heating_kick sensor's history."""
    coordinator, _calls = await _setup(hass, entry_data, controller=_KICK_ON)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/get_config"})
    msg = await client.receive_json()
    assert msg["success"] is True
    entry_id = coordinator.config_entry.entry_id
    assert msg["result"]["hp_heating_kick_entity_id"] == er.async_get(
        hass
    ).async_get_entity_id("sensor", DOMAIN, f"{entry_id}_hp_heating_kick")
    assert msg["result"]["hp_heating_kick_entity_id"] is not None


async def test_diagnostics_dump_carries_the_kick(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """The diagnostics download includes the kick view."""
    from custom_components.tortoise_ufh.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    coordinator, calls = await _setup(hass, entry_data, controller=_KICK_ON)
    await _idle_until_kick(hass, coordinator, calls)
    dump = await async_get_config_entry_diagnostics(hass, coordinator.config_entry)
    kick = dump["coordinator"]["data"]["heat_pump"]["heating"]["kick"]
    assert kick["reason"] == "fired"
    assert kick["kicks_last_24h"] == 1
