"""Diagnostics support for the Tortoise-UFH integration.

Home Assistant auto-discovers this platform from the module name
``diagnostics.py`` and offers a "Download diagnostics" action on the config
entry, so no ``manifest.json`` change is required. The dump is serialized to
JSON, so every value produced here is a primitive, list or plain ``dict``.

PRIVACY: the home's latitude/longitude are the one secret this otherwise
shareable dump must never leak. They live at the top level of ``entry.data``;
:data:`TO_REDACT` replaces them with ``"**REDACTED**"`` via
:func:`homeassistant.components.diagnostics.async_redact_data`. The config
entry's ``unique_id`` is derived from the coordinates (``f"{lat}_{lon}"``), so
it is deliberately never included in the dump.

Units: temperatures degrees Celsius, valve percent 0..100, dew point degrees
Celsius, update interval seconds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import TortoiseUfhConfigEntry
    from .coordinator import CoordinatorData

# The home coordinates are the only secret in an otherwise shareable dump.
# ``async_redact_data`` recurses into nested dicts/lists, so listing the two
# top-level keys also scrubs any future nested copy of them.
TO_REDACT = {CONF_LATITUDE, CONF_LONGITUDE}


def _serialize_coordinator_data(
    data: CoordinatorData | None,
) -> dict[str, Any] | None:
    """Return a JSON-safe snapshot of one coordinator update cycle.

    Mirrors the websocket ``get_live`` serialization (``LiveResult`` /
    ``LiveRoomView``): the core :meth:`~tortoise_ufh.models.RoomOutputs.to_dict`
    already maps every enum to its value and preserves ``None``, and the
    HA-layer ``setpoint_c`` / ``live_control_enabled`` are merged onto each
    room. The per-room ``report`` therefore carries ``i_term``, ``trend_term``
    and ``integrator_frozen`` as plain primitives. Every value is JSON-safe.

    Args:
        data: The coordinator's current :class:`CoordinatorData`, or ``None``
            when no update cycle has completed yet.

    Returns:
        A plain ``dict`` snapshot, or ``None`` when ``data`` is ``None``.
    """
    if data is None:
        return None
    return {
        "algorithm_status": data.algorithm_status,
        "watchdog_state": data.watchdog_state,
        "last_update_timestamp": data.last_update_timestamp,
        "mode": data.mode,
        "global_safe_dew_point_c": data.global_safe_dew_point_c,
        "rooms": {
            name: {
                **runtime.outputs.to_dict(),
                "setpoint_c": runtime.setpoint_c,
                "live_control_enabled": runtime.live_control_enabled,
            }
            for name, runtime in data.rooms.items()
        },
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TortoiseUfhConfigEntry
) -> dict[str, Any]:
    """Return redacted, JSON-safe diagnostics for a Tortoise-UFH config entry.

    The home coordinates in ``entry.data`` and ``entry.options`` are redacted
    (see :data:`TO_REDACT`); the location-derived ``unique_id`` is never
    included. The coordinator snapshot reuses the same serialization the panel
    websocket uses, so it faithfully reproduces the per-room outputs and report
    (including the ``i_term`` / ``trend_term`` / ``integrator_frozen`` tuning
    terms) as primitives.

    Args:
        hass: The running Home Assistant instance. Unused, but part of the
            platform signature Home Assistant invokes this with.
        entry: The config entry to describe.

    Returns:
        A JSON-serializable diagnostics ``dict`` with a redacted ``"entry"``
        section and a ``"coordinator"`` snapshot.
    """
    coordinator = entry.runtime_data.coordinator
    update_interval = coordinator.update_interval
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": async_redact_data(entry.options, TO_REDACT),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                update_interval.total_seconds() if update_interval is not None else None
            ),
            "data": _serialize_coordinator_data(coordinator.data),
        },
    }
