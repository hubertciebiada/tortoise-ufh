"""Integration tests for the Tortoise-UFH config-entry diagnostics.

Exercises
:func:`custom_components.tortoise_ufh.diagnostics.async_get_config_entry_diagnostics`
directly against the entry set up by the shared ``setup_integration`` fixture.
The load-bearing assertion is the CRITICAL privacy rule: the home
latitude/longitude must be redacted and must never appear anywhere in the dump.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant

from custom_components.tortoise_ufh.const import CONF_HOME_SETPOINT
from custom_components.tortoise_ufh.diagnostics import (
    async_get_config_entry_diagnostics,
)

if TYPE_CHECKING:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = pytest.mark.ha


async def test_diagnostics_redacts_home_location(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """Latitude/longitude are replaced with the redaction sentinel."""
    result = await async_get_config_entry_diagnostics(hass, setup_integration)

    entry_data = result["entry"]["data"]
    assert entry_data[CONF_LATITUDE] == REDACTED
    assert entry_data[CONF_LONGITUDE] == REDACTED
    # Redaction is surgical: non-secret config survives untouched.
    assert entry_data[CONF_HOME_SETPOINT] == 21.0


async def test_diagnostics_is_json_safe_without_location_leak(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """The dump is JSON-safe and its location section leaks no coordinates.

    The leak check is scoped to the ``entry`` section, the only part that can
    carry the home location. Searching the whole serialized dump would
    false-positive on the coordinator snapshot's volatile ISO timestamp, whose
    ``SS.ffffff`` fraction can spell ``19.5`` / ``50.5`` by coincidence.
    """
    result = await async_get_config_entry_diagnostics(hass, setup_integration)

    json.dumps(result)  # must not raise: every value is JSON-safe

    entry = result["entry"]
    entry_json = json.dumps(entry)
    assert "50.5" not in entry_json
    assert "19.5" not in entry_json
    # The raw coordinate floats never survive as config values ...
    config_values = [*entry["data"].values(), *entry["options"].values()]
    assert 50.5 not in config_values
    assert 19.5 not in config_values
    # ... and the location-derived unique_id / entry_id are never included.
    assert "unique_id" not in entry
    assert "entry_id" not in entry


async def test_diagnostics_includes_coordinator_snapshot(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """The coordinator snapshot carries both rooms and their report terms."""
    result = await async_get_config_entry_diagnostics(hass, setup_integration)

    coordinator = result["coordinator"]
    assert coordinator["last_update_success"] is True
    assert coordinator["update_interval_seconds"] == 300.0

    snapshot = coordinator["data"]
    assert snapshot is not None
    assert set(snapshot["rooms"]) == {"Salon", "Lazienka"}
    for room in snapshot["rooms"].values():
        report = room["report"]
        # The new tuning-term fields the sensors surface ride along in the dump.
        assert "i_term" in report
        assert "trend_term" in report
        assert "integrator_frozen" in report
