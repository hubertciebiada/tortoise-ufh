"""HA tests for the heating-setpoint write gate (issue #33, 2026-10-22).

Drives the real coordinator through a config entry with the heat-pump link's
heating setpoint, compressor-frequency and outlet entities, and asserts what
it writes to ``number.set_value`` (intercepted with ``async_mock_service``,
which never updates the entity state — exactly like a pump that has not yet
applied, or rejected, the write).

With the default tuning and the conftest outdoor temperature of 5 degC the
heating curve is ``26 + 0.5 * (15 - 5) = 31 degC``; the pump entity starts
at 30 degC unless a test says otherwise.
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
    CONF_ENTITY_HP_RESTART_BAND,
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
_BAND = "number.hp_heat_on_delta"
_TEMP_ATTRS = {"unit_of_measurement": "°C", "device_class": "temperature"}
_NUMBER_ATTRS = {"min": 20.0, "max": 55.0, "step": 1.0}


async def _setup(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    *,
    pump_c: float = 30.0,
    freq_hz: float | None = 0.0,
    outlet_c: float | None = 29.0,
    wire_inputs: bool = True,
    band_k: float | None = None,
    controller: dict[str, Any] | None = None,
    number_attrs: dict[str, Any] | None = None,
) -> tuple[Any, list[ServiceCall]]:
    """Set up an entry with the heating gate wired; return (coordinator, calls).

    The Salon room is LIVE and the mode is HEATING, so the link may write.
    """
    hass.states.async_set(_HEAT, str(pump_c), number_attrs or _NUMBER_ATTRS)
    hp: dict[str, Any] = {CONF_ENTITY_HP_HEATING_SETPOINT: _HEAT}
    if wire_inputs:
        hp[CONF_ENTITY_HP_COMPRESSOR_FREQ] = _FREQ
        hp[CONF_ENTITY_HP_OUTLET_TEMP] = _OUTLET
        hass.states.async_set(
            _FREQ, "unavailable" if freq_hz is None else str(freq_hz), {}
        )
        hass.states.async_set(
            _OUTLET, "unavailable" if outlet_c is None else str(outlet_c), _TEMP_ATTRS
        )
    if band_k is not None:
        hp[CONF_ENTITY_HP_RESTART_BAND] = _BAND
        hass.states.async_set(_BAND, str(band_k), {})
    options: dict[str, Any] = {CONF_HEAT_PUMP: hp}
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
    """Run one cycle and return the ``heat_pump.heating`` payload."""
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    hp = coordinator.data.heat_pump
    assert hp is not None and hp.heating is not None
    return dict(hp.heating)


def _heat_writes(calls: list[ServiceCall]) -> list[float]:
    """The values written to the heating setpoint entity, in order."""
    return [c.data["value"] for c in calls if c.data["entity_id"] == _HEAT]


async def test_idle_increase_inside_restart_band_is_held(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """Idle, pump 30, outlet 29 >= 31 - 3: the 31 degC step is held."""
    coordinator, calls = await _setup(hass, entry_data, outlet_c=29.0)
    heating = await _refresh(hass, coordinator)

    assert _heat_writes(calls) == []
    assert heating["target_c"] == pytest.approx(31.0)
    assert heating["curve_c"] == pytest.approx(31.0)
    assert heating["pump_setpoint_c"] == pytest.approx(30.0)
    assert heating["held"] is True
    gate = heating["gate"]
    assert gate["reason"] == "hold_increase_idle"
    assert gate["gate_active"] is True
    assert gate["compressor_running"] is False
    assert gate["outlet_c"] == pytest.approx(29.0)
    assert gate["restart_band_k"] == pytest.approx(3.0)
    assert gate["restart_band_source"] == "knob"
    assert gate["enabled"] is True

    # Diagnostics sensor carries the reason.
    entry_id = coordinator.config_entry.entry_id
    sensor_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry_id}_hp_heating_gate"
    )
    assert sensor_id is not None
    state = hass.states.get(sensor_id)
    assert state is not None and state.state == "hold_increase_idle"

    # The outlet cools past the band: the held step is written at once.
    hass.states.async_set(_OUTLET, "27.5", _TEMP_ATTRS)
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "write_increase_idle"
    assert _heat_writes(calls) == [31.0]


async def test_running_increase_is_written_at_once(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """Compressor running: an increase is written even with a hot outlet."""
    coordinator, calls = await _setup(hass, entry_data, freq_hz=42.0, outlet_c=35.0)
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "write_increase_running"
    assert heating["gate"]["compressor_running"] is True
    assert _heat_writes(calls) == [31.0]


async def test_running_decrease_waits_for_outlet_below_target_plus_offset(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """Outdoor 15 -> target 26 below the pump's 30: the stop rule applies."""
    hass.states.async_set("sensor.outdoor_temp", "15.0", _TEMP_ATTRS)
    coordinator, calls = await _setup(hass, entry_data, freq_hz=30.0, outlet_c=28.0)
    heating = await _refresh(hass, coordinator)
    assert heating["target_c"] == pytest.approx(26.0)
    assert heating["gate"]["reason"] == "hold_decrease_running"
    assert _heat_writes(calls) == []

    hass.states.async_set(_OUTLET, "27.9", _TEMP_ATTRS)
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "write_decrease_running"
    assert _heat_writes(calls) == [26.0]


async def test_idle_decrease_waits_for_outlet_at_target(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """Idle: a decrease is written only once the outlet is >= the new target."""
    hass.states.async_set("sensor.outdoor_temp", "15.0", _TEMP_ATTRS)
    coordinator, calls = await _setup(hass, entry_data, outlet_c=25.0)
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "hold_decrease_idle"
    assert _heat_writes(calls) == []

    hass.states.async_set(_OUTLET, "26.0", _TEMP_ATTRS)
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "write_decrease_idle"
    assert _heat_writes(calls) == [26.0]


async def test_unchanged_setpoint_not_reasserted_while_idle(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """Pump already at 31: nothing is written, even past the 45-min re-assert."""
    coordinator, calls = await _setup(hass, entry_data, pump_c=31.0, outlet_c=29.0)
    # A write cache older than the re-assert period would make the ungated
    # path write again; the active gate must not.
    coordinator._writer._last_written_hp_setpoint[_HEAT] = (
        31.0,
        time.monotonic() - 3 * 3600.0,
    )
    for _ in range(3):
        heating = await _refresh(hass, coordinator)
        assert heating["gate"]["reason"] == "in_sync"
    assert _heat_writes(calls) == []


async def test_fallback_without_inputs_writes_and_reasserts(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """No compressor/outlet entity: written at once, as before #33."""
    coordinator, calls = await _setup(hass, entry_data, wire_inputs=False)
    heating = await _refresh(hass, coordinator)
    gate = heating["gate"]
    assert gate["reason"] == "ungated_no_input"
    assert gate["gate_active"] is False
    assert heating["held"] is False
    assert _heat_writes(calls) == [31.0]

    # The 45-min re-assert still runs in the fallback (unchanged behaviour).
    hass.states.async_set(_HEAT, "31.0", _NUMBER_ATTRS)
    coordinator._writer._last_written_hp_setpoint[_HEAT] = (
        31.0,
        time.monotonic() - 3 * 3600.0,
    )
    await _refresh(hass, coordinator)
    assert _heat_writes(calls) == [31.0, 31.0]


async def test_unavailable_input_falls_back_to_ungated(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """A wired but unavailable outlet sensor makes the gate inactive."""
    coordinator, calls = await _setup(hass, entry_data, outlet_c=None)
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "ungated_no_input"
    assert _heat_writes(calls) == [31.0]


async def test_gate_disabled_by_knob_writes_at_once(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """heating_gate_enabled off: the held shape is written (pre-#33)."""
    coordinator, calls = await _setup(
        hass, entry_data, controller={"heating_gate_enabled": False}
    )
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "ungated_disabled"
    assert heating["gate"]["enabled"] is False
    assert _heat_writes(calls) == [31.0]


async def test_restart_band_entity_overrides_the_knob(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """A 1.5 K pump band writes at outlet 29.4 (the 3 K knob would hold)."""
    coordinator, calls = await _setup(hass, entry_data, outlet_c=29.4, band_k=1.5)
    heating = await _refresh(hass, coordinator)
    gate = heating["gate"]
    assert gate["restart_band_source"] == "entity"
    assert gate["restart_band_k"] == pytest.approx(1.5)
    assert gate["restart_band_entity_id"] == _BAND
    assert gate["reason"] == "write_increase_idle"
    assert _heat_writes(calls) == [31.0]


async def test_implausible_restart_band_entity_uses_the_knob(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """A 0 K band reading is ignored: the knob applies and the step is held."""
    coordinator, calls = await _setup(hass, entry_data, outlet_c=29.4, band_k=0.0)
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["restart_band_source"] == "knob"
    assert heating["gate"]["restart_band_entity_k"] == pytest.approx(0.0)
    assert heating["gate"]["reason"] == "hold_increase_idle"
    assert _heat_writes(calls) == []


async def test_target_clamped_to_entity_max(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """The target never exceeds the entity's own max (no range rejection)."""
    coordinator, calls = await _setup(
        hass,
        entry_data,
        pump_c=28.0,
        wire_inputs=False,
        number_attrs={"min": 20.0, "max": 30.0, "step": 1.0},
    )
    heating = await _refresh(hass, coordinator)
    assert heating["target_c"] == pytest.approx(30.0)
    assert _heat_writes(calls) == [30.0]


async def test_unconfirmed_write_is_retried_after_settle(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """A write the entity state never shows is sent again (not after 45 min).

    The mocked service never updates the state, like a rejecting entity.
    Inside the settle window nothing is repeated; once the write is older
    than the window, the next cycle writes it again.
    """
    coordinator, calls = await _setup(hass, entry_data, wire_inputs=False)
    await _refresh(hass, coordinator)
    assert _heat_writes(calls) == [31.0]

    await _refresh(hass, coordinator)
    assert _heat_writes(calls) == [31.0]

    value, _stamp = coordinator._writer._last_written_hp_setpoint[_HEAT]
    coordinator._writer._last_written_hp_setpoint[_HEAT] = (
        value,
        time.monotonic() - 300.0,
    )
    await _refresh(hass, coordinator)
    assert _heat_writes(calls) == [31.0, 31.0]


async def test_gated_write_retried_while_pump_still_differs(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """Gated path: the reference is the pump's state, so a lost write retries."""
    coordinator, calls = await _setup(hass, entry_data, freq_hz=40.0, outlet_c=30.0)
    await _refresh(hass, coordinator)
    assert _heat_writes(calls) == [31.0]
    value, _stamp = coordinator._writer._last_written_hp_setpoint[_HEAT]
    coordinator._writer._last_written_hp_setpoint[_HEAT] = (
        value,
        time.monotonic() - 300.0,
    )
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "write_increase_running"
    assert _heat_writes(calls) == [31.0, 31.0]

    # Once the pump shows the value, the gate is in sync and quiet.
    hass.states.async_set(_HEAT, "31.0", _NUMBER_ATTRS)
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "in_sync"
    assert _heat_writes(calls) == [31.0, 31.0]


async def test_cooling_mode_never_writes_heating(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """Out of HEATING the gate has no target and nothing is written."""
    coordinator, calls = await _setup(hass, entry_data, outlet_c=20.0)
    coordinator._mode = Mode.COOLING
    heating = await _refresh(hass, coordinator)
    assert heating["target_c"] is None
    assert heating["gate"]["reason"] == "no_target"
    assert _heat_writes(calls) == []


async def test_no_write_while_every_room_is_off(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """The gate decides, but the write gate of the link (no LIVE room) wins."""
    coordinator, calls = await _setup(hass, entry_data, freq_hz=40.0, outlet_c=30.0)
    coordinator._room_states["Salon"] = "off"
    heating = await _refresh(hass, coordinator)
    assert heating["gate"]["reason"] == "write_increase_running"
    assert _heat_writes(calls) == []


async def test_diagnostics_dump_carries_the_gate(
    hass: HomeAssistant, register_sources: None, entry_data: dict[str, Any]
) -> None:
    """The diagnostics download includes the heat-pump view with the gate."""
    from custom_components.tortoise_ufh.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    coordinator, _calls = await _setup(hass, entry_data, outlet_c=29.0)
    await _refresh(hass, coordinator)
    dump = await async_get_config_entry_diagnostics(hass, coordinator.config_entry)
    heating = dump["coordinator"]["data"]["heat_pump"]["heating"]
    assert heating["gate"]["reason"] == "hold_increase_idle"
    assert heating["held"] is True
