"""Shared fixtures for the Tortoise-UFH Home Assistant integration tests.

This tier needs ``pytest-homeassistant-custom-component`` (which pins a compatible
``homeassistant``); those are absent from the pure-core dev environment, so the
module-level :func:`importorskip` makes the default ``python -m pytest`` skip this
whole directory. The tests run inside the Docker image (``docker/Dockerfile.test``),
which installs the harness — see ``docker/README.md``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")

from typing import Any  # noqa: E402

import pytest_asyncio  # noqa: E402
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.tortoise_ufh.const import (  # noqa: E402
    CONF_COOLING_ENABLED,
    CONF_ENTITY_FAST_SOURCE,
    CONF_ENTITY_HUMIDITY,
    CONF_ENTITY_MODE,
    CONF_ENTITY_RETURN,
    CONF_ENTITY_SUPPLY,
    CONF_ENTITY_TEMP_OUTDOOR,
    CONF_ENTITY_TEMP_ROOM,
    CONF_ENTITY_VALVES,
    CONF_FAST_SOURCE_KIND,
    CONF_HOME_SETPOINT,
    CONF_PARTICIPATES,
    CONF_ROOM_AREA,
    CONF_ROOM_NAME,
    CONF_ROOM_OFFSET,
    CONF_ROOMS,
    DOMAIN,
    FAST_SOURCE_KIND_NONE,
    FAST_SOURCE_KIND_SPLIT,
)

_TEMP_ATTRS = {"unit_of_measurement": "°C", "device_class": "temperature"}
_HUM_ATTRS = {"unit_of_measurement": "%", "device_class": "humidity"}
_PCT_ATTRS = {"unit_of_measurement": "%"}


@pytest.fixture(autouse=True)
def _enable_custom(enable_custom_integrations: Any) -> None:
    """Auto-load ``custom_components/tortoise_ufh`` into the test HA instance."""


@pytest.fixture(autouse=True)
def _clear_farewell_registry() -> None:
    """Isolate the module-level farewell registry between tests (K10/R5).

    The registry deliberately survives config-entry reloads in production
    (that is its whole point), but across TESTS it would leak: a farewell
    written in one test would make the next test's first cycle distrust an ON
    feedback for the same mock entity id.
    """
    from custom_components.tortoise_ufh import writers

    writers._RECENT_FAREWELL_MONOTONIC.clear()


@pytest.fixture
def register_sources(hass: HomeAssistant) -> None:
    """Register the source entities referenced by :func:`entry_data`."""
    hass.states.async_set("sensor.salon_temp", "21.5", _TEMP_ATTRS)
    hass.states.async_set("sensor.salon_humidity", "45", _HUM_ATTRS)
    hass.states.async_set("sensor.outdoor_temp", "5.0", _TEMP_ATTRS)
    hass.states.async_set("number.salon_valve", "0", _PCT_ATTRS)
    hass.states.async_set("sensor.salon_supply", "30.0", _TEMP_ATTRS)
    hass.states.async_set("sensor.salon_return", "26.0", _TEMP_ATTRS)
    hass.states.async_set("climate.salon_split", "off", {})
    hass.states.async_set("sensor.lazienka_temp", "22.0", _TEMP_ATTRS)
    hass.states.async_set("sensor.lazienka_humidity", "55", _HUM_ATTRS)
    hass.states.async_set("number.lazienka_valve", "0", _PCT_ATTRS)
    hass.states.async_set("sensor.lazienka_supply", "30.0", _TEMP_ATTRS)
    hass.states.async_set("sensor.lazienka_return", "26.0", _TEMP_ATTRS)
    hass.states.async_set(
        "input_select.home_mode",
        "heating",
        {"options": ["heating", "transitional", "cooling", "off"]},
    )


@pytest.fixture
def entry_data() -> dict[str, Any]:
    """A valid two-room config-entry payload.

    Salon: has a split, participates in cooling. Lazienka: floor-only, excluded
    from cooling (mirrors the bathroom opt-out from the PRD).
    """
    return {
        CONF_LATITUDE: 50.5,
        CONF_LONGITUDE: 19.5,
        CONF_HOME_SETPOINT: 21.0,
        CONF_ENTITY_MODE: "input_select.home_mode",
        CONF_ROOMS: [
            {
                CONF_ROOM_NAME: "Salon",
                CONF_ROOM_AREA: 30.0,
                CONF_ENTITY_TEMP_ROOM: "sensor.salon_temp",
                CONF_ENTITY_HUMIDITY: "sensor.salon_humidity",
                CONF_ENTITY_TEMP_OUTDOOR: "sensor.outdoor_temp",
                CONF_ENTITY_VALVES: ["number.salon_valve"],
                CONF_ENTITY_SUPPLY: ["sensor.salon_supply"],
                CONF_ENTITY_RETURN: ["sensor.salon_return"],
                CONF_ENTITY_FAST_SOURCE: "climate.salon_split",
                CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_SPLIT,
                CONF_ROOM_OFFSET: 0.0,
                CONF_PARTICIPATES: True,
                CONF_COOLING_ENABLED: True,
            },
            {
                CONF_ROOM_NAME: "Lazienka",
                CONF_ROOM_AREA: 6.0,
                CONF_ENTITY_TEMP_ROOM: "sensor.lazienka_temp",
                CONF_ENTITY_HUMIDITY: "sensor.lazienka_humidity",
                CONF_ENTITY_VALVES: ["number.lazienka_valve"],
                CONF_ENTITY_SUPPLY: ["sensor.lazienka_supply"],
                CONF_ENTITY_RETURN: ["sensor.lazienka_return"],
                CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
                CONF_ROOM_OFFSET: 1.0,
                CONF_PARTICIPATES: True,
                CONF_COOLING_ENABLED: False,
            },
        ],
    }


@pytest.fixture
def mock_entry(hass: HomeAssistant, entry_data: dict[str, Any]) -> MockConfigEntry:
    """A :class:`MockConfigEntry` added to hass (not yet set up)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data,
        options={},
        title="Tortoise-UFH",
        unique_id="50.5_19.5",
    )
    entry.add_to_hass(hass)
    return entry


@pytest_asyncio.fixture
async def setup_integration(
    hass: HomeAssistant, register_sources: None, mock_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration from the mock entry and let it settle."""
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    return mock_entry
