"""Sidebar panel registration for the Tortoise-UFH Home Assistant adapter.

This module registers (and unregisters) the custom sidebar panel and the
static HTTP path that serves the self-contained vanilla-JS panel module
``frontend/tortoise-ufh-panel.js``.

Registration is process-wide (not per config entry): the panel and its static
path are registered exactly once, guarded by a boolean flag stored in
``hass.data`` so that a second config entry does not attempt a duplicate
registration (which Home Assistant would reject with a ``ValueError``).

No units are involved in this module; it deals only with HTTP paths and the
frontend panel descriptor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Key under which the "panel already registered" flag lives in ``hass.data``.
PANEL_REGISTERED_KEY = f"{DOMAIN}_panel_registered"

# Key under which the "static path already registered" flag lives in
# ``hass.data``. The static HTTP path cannot be withdrawn at runtime, so this
# flag is set once for the process lifetime and never cleared on reload.
_STATIC_PATH_REGISTERED_KEY = f"{DOMAIN}_static_path_registered"

# Relative location (inside this package) of the served JS module.
_FRONTEND_DIR_NAME = "frontend"
_PANEL_JS_FILENAME = "tortoise-ufh-panel.js"


@dataclass(frozen=True)
class PanelRegistration:
    """Immutable descriptor of the Tortoise-UFH sidebar panel.

    Attributes:
        static_url_path: HTTP path at which the JS module is served
            (e.g. ``/tortoise_ufh_panel/panel.js``).
        custom_element_name: Name of the custom element defined by the module
            and referenced by ``_panel_custom.name``.
        frontend_url_path: URL slug of the panel in the HA sidebar.
        sidebar_title: Human-readable sidebar label.
        sidebar_icon: Material Design Icon identifier (``mdi:...``).
        require_admin: Whether the panel is restricted to admin users.
        embed_iframe: Whether HA should embed the module inside an iframe.

    Raises:
        ValueError: If any string field is empty or a path/icon is malformed.
    """

    static_url_path: str = "/tortoise_ufh_panel/panel.js"
    custom_element_name: str = "tortoise-ufh-panel"
    frontend_url_path: str = "tortoise-ufh"
    sidebar_title: str = "Tortoise-UFH"
    sidebar_icon: str = "mdi:tortoise"
    require_admin: bool = True
    embed_iframe: bool = False

    def __post_init__(self) -> None:
        """Validate the panel descriptor fields.

        Raises:
            ValueError: If a required string field is empty, the static URL
                path is not absolute, or the icon is not an ``mdi:`` identifier.
        """
        for field_name in (
            "static_url_path",
            "custom_element_name",
            "frontend_url_path",
            "sidebar_title",
            "sidebar_icon",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                msg = f"{field_name} must be a non-empty string"
                raise ValueError(msg)
        if not self.static_url_path.startswith("/"):
            msg = f"static_url_path must be absolute, got {self.static_url_path!r}"
            raise ValueError(msg)
        if not self.sidebar_icon.startswith("mdi:"):
            msg = (
                f"sidebar_icon must be an 'mdi:' identifier, got {self.sidebar_icon!r}"
            )
            raise ValueError(msg)

    def js_module_path(self) -> Path:
        """Return the absolute filesystem path to the served JS module.

        Returns:
            Absolute :class:`pathlib.Path` to
            ``frontend/tortoise-ufh-panel.js`` inside this package.
        """
        return (
            Path(__file__).parent / _FRONTEND_DIR_NAME / _PANEL_JS_FILENAME
        ).resolve()

    def panel_config(self) -> dict[str, object]:
        """Return the ``config`` dict for the custom frontend panel.

        Returns:
            The ``{"_panel_custom": {...}}`` descriptor consumed by
            :func:`frontend.async_register_built_in_panel`.
        """
        return {
            "_panel_custom": {
                "name": self.custom_element_name,
                "module_url": self.static_url_path,
                "embed_iframe": self.embed_iframe,
            }
        }


# Module-level singleton descriptor (validated at import time).
_PANEL = PanelRegistration()


async def async_register_panel(hass: HomeAssistant) -> None:
    """Register the Tortoise-UFH static path and sidebar panel.

    Idempotent across config entries: a boolean flag in ``hass.data`` guards
    against a second registration that Home Assistant would otherwise reject.

    Args:
        hass: The running Home Assistant instance.

    Returns:
        None.
    """
    if hass.data.get(PANEL_REGISTERED_KEY):
        return

    # The sidebar panel is an observability convenience: the controller,
    # entities, services and websocket API all work without it. A failure here
    # (e.g. the http/frontend component not yet available) must NOT abort setup
    # of the whole climate integration, so registration is best-effort.
    try:
        if not hass.data.get(_STATIC_PATH_REGISTERED_KEY):
            if hass.http is None:
                msg = "hass.http is unavailable; cannot serve the panel module"
                raise RuntimeError(msg)
            await hass.http.async_register_static_paths(
                [
                    StaticPathConfig(
                        _PANEL.static_url_path,
                        str(_PANEL.js_module_path()),
                        False,
                    )
                ]
            )
            hass.data[_STATIC_PATH_REGISTERED_KEY] = True

        frontend.async_register_built_in_panel(
            hass,
            "custom",
            sidebar_title=_PANEL.sidebar_title,
            sidebar_icon=_PANEL.sidebar_icon,
            frontend_url_path=_PANEL.frontend_url_path,
            require_admin=_PANEL.require_admin,
            config=_PANEL.panel_config(),
        )
        hass.data[PANEL_REGISTERED_KEY] = True
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "Tortoise-UFH sidebar panel could not be registered; the integration "
            "still works (entities, services, websocket). Use the fallback "
            "dashboard (docker/ or dashboard_tortoise_ufh.yaml) if needed.",
            exc_info=True,
        )


async def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the Tortoise-UFH sidebar panel and clear the guard flag.

    The static path registered by :func:`async_register_panel` cannot be
    withdrawn from the HA HTTP app at runtime; only the sidebar panel is
    removed. The guard flag is cleared so a subsequent setup re-registers the
    panel cleanly.

    Args:
        hass: The running Home Assistant instance.

    Returns:
        None.
    """
    if not hass.data.get(PANEL_REGISTERED_KEY):
        return

    try:
        frontend.async_remove_panel(hass, _PANEL.frontend_url_path)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Tortoise-UFH panel removal was a no-op", exc_info=True)
    hass.data[PANEL_REGISTERED_KEY] = False
