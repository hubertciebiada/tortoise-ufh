"""Tests for the Tortoise-UFH sidebar panel registration and teardown.

These assert the locked panel contract (BUILD_SPEC §9/§10, PRD §8): a
``custom`` sidebar panel titled *Tortoise-UFH* with the ``mdi:tortoise``
icon at ``frontend_url_path`` ``tortoise-ufh``, whose module is served from
the static path ``/tortoise_ufh_panel/panel.js`` — and that the panel is
removed once the last config entry unloads. Registration *state* is
inspected (``hass.data``, a spy on the static-path registrar); no HTTP
fetch of the JS body is performed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import frontend

pytestmark = pytest.mark.ha

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

_PANEL_URL = "tortoise-ufh"
_STATIC_URL = "/tortoise_ufh_panel/panel.js"
_STATIC_REGISTRAR = (
    "homeassistant.components.http.HomeAssistantHTTP.async_register_static_paths"
)


async def test_panel_registered(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Setup registers the custom sidebar panel with the locked descriptor."""
    panels = hass.data[frontend.DATA_PANELS]
    assert _PANEL_URL in panels

    panel = panels[_PANEL_URL]
    assert panel.component_name == "custom"
    assert panel.sidebar_title == "Tortoise-UFH"
    assert panel.sidebar_icon == "mdi:tortoise"
    assert panel.frontend_url_path == _PANEL_URL
    assert panel.require_admin is True
    assert panel.config["_panel_custom"]["module_url"] == _STATIC_URL


async def test_static_path_registered(
    hass: HomeAssistant,
    register_sources: None,
    mock_entry: MockConfigEntry,
) -> None:
    """The JS module is served from the locked static path on setup."""
    with patch(_STATIC_REGISTRAR, new_callable=AsyncMock) as register:
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    served = {
        config.url_path for call in register.call_args_list for config in call.args[0]
    }
    assert _STATIC_URL in served


async def test_panel_removed_on_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Unloading the last entry withdraws the sidebar panel."""
    entry = setup_integration
    assert _PANEL_URL in hass.data[frontend.DATA_PANELS]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert _PANEL_URL not in hass.data[frontend.DATA_PANELS]
