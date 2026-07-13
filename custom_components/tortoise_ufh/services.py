"""Home Assistant services for the Tortoise-UFH adapter.

Registers the three whole-home services advertised in ``services.yaml`` and the
README, delegating to the coordinator's authoritative setters (the same ones the
panel websocket API uses):

* ``tortoise_ufh.set_home_temperature`` -> ``coordinator.set_home_temperature``
* ``tortoise_ufh.set_room_offset``      -> ``coordinator.set_room_offset``
* ``tortoise_ufh.set_mode``             -> ``coordinator.set_mode``
* ``tortoise_ufh.test_actuation``       -> ``coordinator.async_test_actuation``
  (S6/C, 2026-07-13 — the manual per-room actuation self-test)

Registration is process-wide: :func:`async_register_services` is called exactly
once per Home Assistant instance (guarded by the caller in ``__init__.py``),
mirroring the panel/websocket registration. This module holds no physical
control logic; temperatures are in degrees Celsius, offsets in kelvin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    HOME_SETPOINT_MAX_C,
    HOME_SETPOINT_MIN_C,
    MODE_OPTIONS,
    ROOM_OFFSET_MAX_C,
    ROOM_OFFSET_MIN_C,
)
from .core.models import Mode

if TYPE_CHECKING:
    from .coordinator import TortoiseUfhCoordinator

SERVICE_SET_HOME_TEMPERATURE = "set_home_temperature"
SERVICE_SET_ROOM_OFFSET = "set_room_offset"
SERVICE_SET_MODE = "set_mode"
SERVICE_TEST_ACTUATION = "test_actuation"

_ATTR_TEMPERATURE = "temperature"
_ATTR_ROOM = "room"
_ATTR_OFFSET = "offset"
_ATTR_MODE = "mode"
_ATTR_DURATION_MINUTES = "duration_minutes"
_ATTR_CANCEL = "cancel"

_TEST_DURATION_MIN_MINUTES = 20.0
"""Lower bound of the self-test duration [min] — shorter excursions leave too
faint a hydraulic signature to grade reliably."""

_TEST_DURATION_MAX_MINUTES = 30.0
"""Upper bound of the self-test duration [min] — a longer 100 % excursion of
chilled/hot water serves no diagnostic purpose."""

_TEST_DURATION_DEFAULT_MINUTES = 25.0
"""Default self-test duration [min]."""

# Human-readable elaborations of the refusal reasons returned by
# ``coordinator.async_test_actuation`` (kept terse and English-only, matching
# HomeAssistantError conventions; the panel renders its own localised copy).
_TEST_REFUSAL_DETAILS: dict[str, str] = {
    "unknown_room": "the room is not configured",
    "not_ready": "the controller is not ready yet",
    "room_not_live": "the room is not LIVE (only a live room may move a valve)",
    "no_probes": "the room has no loop with both supply and return probes",
    "already_running": "a self-test is already running for this room",
    "mode_inactive": "the system mode is neither heating nor cooling",
    "dew_unsafe": "cooling dew-point headroom is insufficient for an excursion",
}

_SET_HOME_TEMPERATURE_SCHEMA = vol.Schema(
    {
        vol.Required(_ATTR_TEMPERATURE): vol.All(
            vol.Coerce(float),
            vol.Range(min=HOME_SETPOINT_MIN_C, max=HOME_SETPOINT_MAX_C),
        ),
    }
)

_SET_ROOM_OFFSET_SCHEMA = vol.Schema(
    {
        vol.Required(_ATTR_ROOM): cv.string,
        vol.Required(_ATTR_OFFSET): vol.All(
            vol.Coerce(float), vol.Range(min=ROOM_OFFSET_MIN_C, max=ROOM_OFFSET_MAX_C)
        ),
    }
)

_SET_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(_ATTR_MODE): vol.In(list(MODE_OPTIONS)),
    }
)

_TEST_ACTUATION_SCHEMA = vol.Schema(
    {
        vol.Required(_ATTR_ROOM): cv.string,
        vol.Optional(
            _ATTR_DURATION_MINUTES, default=_TEST_DURATION_DEFAULT_MINUTES
        ): vol.All(
            vol.Coerce(float),
            vol.Range(min=_TEST_DURATION_MIN_MINUTES, max=_TEST_DURATION_MAX_MINUTES),
        ),
        vol.Optional(_ATTR_CANCEL, default=False): cv.boolean,
    }
)


def _resolve_coordinator(hass: HomeAssistant) -> TortoiseUfhCoordinator:
    """Return the single loaded coordinator, or raise if none is ready.

    Tortoise-UFH is a ``hub`` integration: one config entry manages every
    room. Only a fully LOADED entry qualifies (D1, 2026-07-12): an entry
    mid-unload or mid-setup still carries ``runtime_data``, and a service
    call used to mutate its dying coordinator inside the unload window.

    Args:
        hass: The Home Assistant instance.

    Returns:
        The live coordinator of the first loaded config entry.

    Raises:
        HomeAssistantError: When no config entry has finished loading.
    """
    from homeassistant.config_entries import ConfigEntryState

    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        runtime = getattr(entry, "runtime_data", None)
        coordinator = getattr(runtime, "coordinator", None)
        if coordinator is not None:
            return coordinator  # type: ignore[no-any-return]
    msg = "Tortoise-UFH is not loaded yet; no coordinator is available."
    raise HomeAssistantError(msg)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the Tortoise-UFH services (idempotent, process-wide).

    Args:
        hass: The Home Assistant instance.
    """

    async def _handle_set_home_temperature(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass)
        coordinator.set_home_temperature(float(call.data[_ATTR_TEMPERATURE]))

    async def _handle_set_room_offset(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass)
        room = str(call.data[_ATTR_ROOM])
        # The coordinator setter silently ignores unknown rooms, so validate
        # up front (mirroring the websocket path) to give callers feedback.
        if room not in coordinator._room_offsets:
            raise ServiceValidationError(
                f"Unknown room {room!r} for tortoise_ufh.set_room_offset."
            )
        coordinator.set_room_offset(room, float(call.data[_ATTR_OFFSET]))

    async def _handle_set_mode(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass)
        coordinator.set_mode(Mode(str(call.data[_ATTR_MODE])))

    async def _handle_test_actuation(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass)
        room = str(call.data[_ATTR_ROOM])
        cancel = bool(call.data[_ATTR_CANCEL])
        duration_s = float(call.data[_ATTR_DURATION_MINUTES]) * 60.0
        reason = await coordinator.async_test_actuation(
            room, duration_s=duration_s, cancel=cancel
        )
        if reason is None:
            return
        detail = _TEST_REFUSAL_DETAILS.get(reason, reason)
        message = (
            f"Cannot start actuation self-test for room {room!r}: {detail} ({reason})."
        )
        # Caller-fixable preconditions are validation errors; anything else
        # (unexpected core refusal) is a generic HomeAssistantError.
        if reason in _TEST_REFUSAL_DETAILS:
            raise ServiceValidationError(message)
        raise HomeAssistantError(message)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_HOME_TEMPERATURE,
        _handle_set_home_temperature,
        schema=_SET_HOME_TEMPERATURE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ROOM_OFFSET,
        _handle_set_room_offset,
        schema=_SET_ROOM_OFFSET_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_MODE,
        _handle_set_mode,
        schema=_SET_MODE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TEST_ACTUATION,
        _handle_test_actuation,
        schema=_TEST_ACTUATION_SCHEMA,
    )


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the Tortoise-UFH services (process-wide teardown).

    Args:
        hass: The Home Assistant instance.
    """
    for service in (
        SERVICE_SET_HOME_TEMPERATURE,
        SERVICE_SET_ROOM_OFFSET,
        SERVICE_SET_MODE,
        SERVICE_TEST_ACTUATION,
    ):
        hass.services.async_remove(DOMAIN, service)
