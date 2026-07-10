"""Actuator command writers for the Tortoise-UFH coordinator.

Extracted verbatim from ``coordinator.py`` (2026-07-10): everything that turns
a core command into a Home Assistant service call — the per-entity valve write
threshold, the domain dispatch (``valve.set_valve_position`` vs
``number.set_value``), the fast-source command cache with its periodic
re-assert (S3) and the farewell parking write (C5) — now lives in one
:class:`CommandWriter` with its own private caches, so the write path is
testable without the whole coordinator.

The coordinator owns exactly one :class:`CommandWriter` and delegates; the
LIVE/SHADOW gating (who may be written at all) stays in the coordinator.

Units: temperatures degrees Celsius, valve percent 0..100, cache ages in
seconds (monotonic).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from .core.models import FastSourceMode, Mode
from .readers import is_valve_domain

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .core.models import RoomOutputs

_LOGGER = logging.getLogger(__name__)

__all__ = ["CommandWriter"]

_FAST_REASSERT_SECONDS: float = 45.0 * 60.0
"""Age after which an unchanged fast-source command is re-written anyway [s].

The splits are local (ESPHome), so this is hygiene, not an API budget: an
unchanged (hvac_mode, target) pair is normally NOT re-sent every cycle (no IR
beeps / stomping on manual tweaks), but a periodic re-assert self-heals the
hardware after a missed write or a manual override — the machine stays the
owner (S3, 2026-07-09).
"""

# FastSourceMode -> Home Assistant climate HVAC mode string.
_HVAC_MODE_BY_FAST_SOURCE: dict[FastSourceMode, str] = {
    FastSourceMode.HEATING: "heat",
    FastSourceMode.COOLING: "cool",
    FastSourceMode.OFF: "off",
}


class CommandWriter:
    """Writes valve and fast-source commands to the Home Assistant actuators.

    Stateful: owns the last value written per valve entity (the write
    threshold's reference) and the last fast-source command written per climate
    entity (the S3 re-assert cache). One instance per coordinator; every method
    body is the verbatim translocation of the coordinator's former ``_write_*``
    / farewell helpers (2026-07-10).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise an empty writer.

        Args:
            hass: The Home Assistant instance whose services are called.
        """
        self._hass = hass
        # Last value written to each valve entity (for the write threshold).
        self._last_written_valve: dict[str, float] = {}
        # Last fast-source command written per climate entity (S3):
        # entity_id -> (hvac_mode, target_temp_c or None, monotonic timestamp).
        # An unchanged command younger than _FAST_REASSERT_SECONDS is skipped.
        self._last_written_fast: dict[str, tuple[str, float | None, float]] = {}

    def last_written_valve(self, entity_id: str) -> float | None:
        """Return the last position written to a valve entity, if any.

        Exposed for the coordinator's S8 command-vs-feedback mismatch tracker.

        Args:
            entity_id: The valve actuator entity id.

        Returns:
            The last written position [%], or ``None`` when never written.
        """
        return self._last_written_valve.get(entity_id)

    async def write_valves(
        self,
        valves: list[str],
        name: str,
        outputs: RoomOutputs,
        *,
        threshold_pct: float,
    ) -> None:
        """Write the recommended valve position to every room valve entity.

        Each actuator is driven per its domain: a ``valve``-domain entity via
        ``valve.set_valve_position`` (integer ``position`` 0..100) and any other
        (``number`` …) via ``number.set_value`` (float ``value``). A value is
        written to an entity only when it differs from the last value written to
        that entity by at least ``threshold_pct``. All calls are non-blocking.

        Args:
            valves: The room's valve actuator entity ids.
            name: The room name (log context).
            outputs: The room's controller outputs.
            threshold_pct: The room's ``valve_write_threshold_pct`` [%].
        """
        if not valves:
            return
        value = outputs.valve_position_pct
        for valve_entity in valves:
            last = self._last_written_valve.get(valve_entity)
            if last is not None and abs(value - last) < threshold_pct:
                continue
            try:
                if is_valve_domain(valve_entity):
                    # valve.set_valve_position expects an int 0..100 position.
                    await self._hass.services.async_call(
                        "valve",
                        "set_valve_position",
                        {"entity_id": valve_entity, "position": round(value)},
                        blocking=False,
                    )
                else:
                    await self._hass.services.async_call(
                        "number",
                        "set_value",
                        {"entity_id": valve_entity, "value": value},
                        blocking=False,
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Failed to set valve %s for room %s", valve_entity, name
                )
            else:
                self._last_written_valve[valve_entity] = value

    async def write_fast_source(
        self, entity_id: str | None, name: str, outputs: RoomOutputs
    ) -> None:
        """Write the fast-source command to the room's climate entity.

        Issues ``set_hvac_mode`` and, when the split is on, ``set_temperature``.
        All calls are non-blocking.

        Command cache (S3, 2026-07-09): an unchanged ``(hvac_mode, target)``
        pair is NOT re-sent every cycle — mirroring the valve write cache —
        so the split is not spammed with identical commands (IR beeps, stomping
        on manual louvre/fan tweaks). The command is still re-asserted after
        :data:`_FAST_REASSERT_SECONDS` so a missed write or a manual override
        self-heals: the machine stays the owner.

        Args:
            entity_id: The fast-source climate entity id, or ``None`` / empty.
            name: The room name (log context).
            outputs: The room's controller outputs.
        """
        if not entity_id:
            return
        command = outputs.fast_source
        hvac_mode = (
            _HVAC_MODE_BY_FAST_SOURCE.get(command.mode, "off") if command.on else "off"
        )
        target = command.target_temperature_c if command.on else None
        cached = self._last_written_fast.get(entity_id)
        now_monotonic = time.monotonic()
        if (
            cached is not None
            and cached[0] == hvac_mode
            and cached[1] == target
            and now_monotonic - cached[2] < _FAST_REASSERT_SECONDS
        ):
            return
        try:
            await self._hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": entity_id, "hvac_mode": hvac_mode},
                blocking=False,
            )
            if command.on and command.target_temperature_c is not None:
                await self._hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": entity_id,
                        "temperature": command.target_temperature_c,
                    },
                    blocking=False,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Failed to control fast source %s for room %s", entity_id, name
            )
        else:
            self._last_written_fast[entity_id] = (hvac_mode, target, now_monotonic)

    async def farewell_room(
        self,
        fast_source_entity: str | None,
        valves: list[str],
        name: str,
        *,
        mode: Mode,
    ) -> None:
        """Park a room's actuators safely when releasing live ownership (C5).

        Emitted exactly once on a live -> shadow/off transition and on entry
        unload. The split is always commanded OFF (nobody regulates it any
        more). The valve is mode-dependent: in COOLING it is driven to 0 —
        an orphaned open valve would keep passing chilled water while the room
        silently drops out of BOTH condensation defences (the global dew
        maximum and the local S2 throttle). In HEATING the position is left
        untouched: warm supply water is bounded by the heat pump's own curve,
        so holding the last position keeps the house warm and is strictly
        safer than cold-parking it in winter.

        Args:
            fast_source_entity: The fast-source climate entity id, if any.
            valves: The room's valve actuator entity ids.
            name: The room name (log context).
            mode: The current global operating mode (drives the valve rule).
        """
        if fast_source_entity:
            try:
                await self._hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": fast_source_entity, "hvac_mode": "off"},
                    blocking=False,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Farewell: failed to turn off fast source %s for room %s",
                    fast_source_entity,
                    name,
                )
            else:
                self._last_written_fast[fast_source_entity] = (
                    "off",
                    None,
                    time.monotonic(),
                )
        if mode is not Mode.COOLING:
            return
        for valve_entity in valves:
            try:
                if is_valve_domain(valve_entity):
                    await self._hass.services.async_call(
                        "valve",
                        "set_valve_position",
                        {"entity_id": valve_entity, "position": 0},
                        blocking=False,
                    )
                else:
                    await self._hass.services.async_call(
                        "number",
                        "set_value",
                        {"entity_id": valve_entity, "value": 0.0},
                        blocking=False,
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Farewell: failed to close valve %s for room %s",
                    valve_entity,
                    name,
                )
            else:
                self._last_written_valve[valve_entity] = 0.0
