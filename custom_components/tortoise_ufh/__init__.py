"""Tortoise-UFH Home Assistant custom integration.

Home Assistant adapter entry point. Wires a :class:`TortoiseUfhCoordinator`
(one per config entry) into HA's config-entry lifecycle and, once per HA
instance, registers the sidebar panel (with its static JS path) and the panel
websocket API. Temperatures are in degrees Celsius; this module holds no
physical values of its own.

Nothing that depends on Home Assistant is imported at module top level. The pure
control core is vendored under this package at
``custom_components.tortoise_ufh.core``; importing any of its submodules runs
*this* package ``__init__`` first, so the top level must stay importable without
``homeassistant`` (the core is unit- and simulation-tested in an HA-free
environment). Every Home Assistant symbol, every HA-importing submodule, and even
:mod:`.const` (which imports the HA ``Platform`` enum) is therefore deferred to
:data:`typing.TYPE_CHECKING` or to a local import inside the function that needs
it — the module top level pulls in stdlib only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import TortoiseUfhCoordinator

_LOGGER = logging.getLogger(__name__)

# Key under ``hass.data[DOMAIN]`` guarding one-time global frontend setup
# (the sidebar panel and websocket commands are process-wide, not per-entry).
_FRONTEND_REGISTERED: str = "_frontend_registered"


@dataclass(frozen=True)
class RuntimeData:
    """Per-entry runtime state stored on ``entry.runtime_data``.

    Attributes:
        coordinator: The live data-update coordinator driving this entry.
    """

    coordinator: TortoiseUfhCoordinator


type TortoiseUfhConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: TortoiseUfhConfigEntry) -> bool:
    """Set up Tortoise-UFH from a config entry.

    Builds the coordinator, performs the first refresh, forwards the entry to
    all platforms, wires the options-reload listener, and — on the first entry
    for this HA instance — registers the sidebar panel and websocket commands.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being set up.

    Returns:
        ``True`` when setup succeeded.
    """
    from .const import DOMAIN, PLATFORMS
    from .coordinator import TortoiseUfhCoordinator
    from .panel import async_register_panel
    from .services import async_register_services
    from .websocket import async_register_ws

    _LOGGER.debug("Setting up Tortoise-UFH entry: %s", entry.entry_id)

    coordinator = TortoiseUfhCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = RuntimeData(coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the whole entry when options change (e.g. shadow -> live toggle,
    # kill switch, advanced knobs). No bespoke plumbing required.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Register process-wide frontend once, guarded against double registration
    # across multiple config entries.
    domain_data: dict[str, object] = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(_FRONTEND_REGISTERED, False):
        async_register_ws(hass)
        await async_register_panel(hass)
        async_register_services(hass)
        domain_data[_FRONTEND_REGISTERED] = True
        _LOGGER.debug("Registered Tortoise-UFH panel, websocket commands, and services")

    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: TortoiseUfhConfigEntry
) -> None:
    """Reload the integration when its options are updated.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry whose options changed.
    """
    _LOGGER.debug("Options updated for Tortoise-UFH entry: %s", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: TortoiseUfhConfigEntry
) -> bool:
    """Unload a Tortoise-UFH config entry.

    Unloads all platforms; when this was the last loaded entry for the HA
    instance, tears down the process-wide sidebar panel.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being unloaded.

    Returns:
        ``True`` when every platform unloaded cleanly.
    """
    from homeassistant.config_entries import ConfigEntryState

    from .const import DOMAIN, PLATFORMS
    from .panel import async_unregister_panel
    from .services import async_unregister_services

    unload_ok: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        remaining = [
            other
            for other in hass.config_entries.async_entries(DOMAIN)
            if other.entry_id != entry.entry_id
            and other.state is ConfigEntryState.LOADED
        ]
        if not remaining:
            await async_unregister_panel(hass)
            async_unregister_services(hass)
            hass.data.get(DOMAIN, {}).pop(_FRONTEND_REGISTERED, None)
            _LOGGER.debug(
                "Unregistered Tortoise-UFH panel and services (last entry unloaded)"
            )

    _LOGGER.debug("Unloaded Tortoise-UFH entry: %s (ok=%s)", entry.entry_id, unload_ok)

    return unload_ok
