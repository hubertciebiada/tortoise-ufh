"""Diagnostic sensors for the Tortoise-UFH integration.

Publishes the controller's under-the-hood decision report as read-only Home
Assistant diagnostic sensors. Nothing here issues a command — the coordinator is
the single point of control output; these entities are a pure *presentation* of
:attr:`coordinator.data`.

Two families, both description-driven (a frozen ``kw_only`` entity description
carrying a ``value_fn`` that extracts the value from the typed coordinator
payload):

Per room (``EntityCategory.DIAGNOSTIC``):
    * ``recommended_valve`` — final valve position [percent, 0..100].
    * ``error_c`` — ``setpoint - room_temp`` [degrees Celsius].
    * ``trend_c_per_h`` — measured room-temperature trend [K/h].
    * ``i_term`` — integral contribution to the valve [percent].
    * ``trend_term`` — trend-damping contribution to the valve [percent].
    * ``room_dew_point`` — room dew-point temperature [degrees Celsius].
    * ``fast_source_mode`` — split direction text (``off`` / ``heating`` /
      ``cooling`` / ``dry``).
    * ``explanation`` — short "what & why" text.

Global (``EntityCategory.DIAGNOSTIC``):
    * ``global_safe_dew_point`` — ``max_over_cooled(T_dew) + 2 K`` [degrees
      Celsius]; the value the owner feeds to the heat pump as the cooling-supply
      lower limit.
    * ``algorithm_status`` — ``running`` / ``stale`` / ``error``.
    * ``last_update`` — timestamp of the last control cycle (device class
      TIMESTAMP).
    * ``watchdog_status`` — ``ok`` / ``stale``.
    * ``hp_flicker_state`` — force-cooling-start machine state text (``idle`` /
      ``pulse`` / ``cooldown``); its recorder history is the durable source the
      panel counts forced starts from (the in-memory hourly counter resets on
      every reload).

Units: temperatures in degrees Celsius, valve position in percent, trend in
kelvin per hour.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ROOM_NAME, CONF_ROOMS
from .coordinator import CoordinatorData, TortoiseUfhCoordinator
from .device import hub_device_info, room_device_info, room_slug

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

# ---------------------------------------------------------------------------
# Value extractors
# ---------------------------------------------------------------------------


def _room_recommended_valve(data: CoordinatorData, room: str | None) -> float | None:
    """Return a room's final valve position [percent], or ``None``."""
    if room is None or room not in data.rooms:
        return None
    return data.rooms[room].outputs.valve_position_pct


def _room_error_c(data: CoordinatorData, room: str | None) -> float | None:
    """Return a room's control error ``setpoint - room_temp`` [degC], or ``None``."""
    if room is None or room not in data.rooms:
        return None
    return data.rooms[room].report.error_c


def _room_trend_c_per_h(data: CoordinatorData, room: str | None) -> float | None:
    """Return a room's measured temperature trend [K/h], or ``None``."""
    if room is None or room not in data.rooms:
        return None
    return data.rooms[room].report.trend_c_per_h


def _room_dew_point_c(data: CoordinatorData, room: str | None) -> float | None:
    """Return a room's dew-point temperature [degC], or ``None``."""
    if room is None or room not in data.rooms:
        return None
    return data.rooms[room].report.room_dew_point_c


def _room_fast_source_mode(data: CoordinatorData, room: str | None) -> str | None:
    """Return a room's fast-source direction text, or ``None``."""
    if room is None or room not in data.rooms:
        return None
    return data.rooms[room].outputs.fast_source.mode.value


def _room_explanation(data: CoordinatorData, room: str | None) -> str | None:
    """Return a room's short "what & why" explanation text, or ``None``."""
    if room is None or room not in data.rooms:
        return None
    return data.rooms[room].report.explanation


def _room_i_term(data: CoordinatorData, room: str | None) -> float | None:
    """Return a room's integral valve contribution [percent], or ``None``."""
    if room is None or room not in data.rooms:
        return None
    return data.rooms[room].report.i_term


def _room_trend_term(data: CoordinatorData, room: str | None) -> float | None:
    """Return a room's trend-damping valve contribution [percent], or ``None``."""
    if room is None or room not in data.rooms:
        return None
    return data.rooms[room].report.trend_term


def _global_safe_dew_point(data: CoordinatorData, _room: str | None) -> float | None:
    """Return the global safe dew point [degC] fed to the heat pump, or ``None``."""
    return data.global_safe_dew_point_c


def _global_algorithm_status(data: CoordinatorData, _room: str | None) -> str:
    """Return the algorithm status (``running`` / ``stale`` / ``error``)."""
    return data.algorithm_status


def _global_last_update(data: CoordinatorData, _room: str | None) -> datetime | None:
    """Return the last control-cycle timestamp as a tz-aware datetime, or ``None``.

    The coordinator stores an ISO-8601 UTC string; a TIMESTAMP sensor requires a
    :class:`~datetime.datetime`, so the string is parsed back here.
    """
    raw = data.last_update_timestamp
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _global_watchdog_status(data: CoordinatorData, _room: str | None) -> str:
    """Return the watchdog status (``ok`` / ``stale``)."""
    return data.watchdog_state


def _global_hp_flicker_state(data: CoordinatorData, _room: str | None) -> str:
    """Return the force-cooling-start state (``idle`` / ``pulse`` / ``cooldown``).

    The flicker payload lives on the heat-pump runtime
    (``data.heat_pump.flicker``, issue #8 — reading it off ``data`` directly
    raised and rendered the sensor permanently unavailable). ``idle`` when the
    payload is absent (link not configured, or feature disabled with no
    diagnostic entities) — the machine cannot pulse then, so the recorded
    history stays truthful.
    """
    hp = data.heat_pump
    if hp is None or hp.flicker is None:
        return "idle"
    return str(hp.flicker.get("state", "idle"))


# ---------------------------------------------------------------------------
# Sensor descriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TortoiseUfhSensorEntityDescription(SensorEntityDescription):
    """Sensor description extended with a coordinator-data value extractor.

    Attributes:
        value_fn: Extractor mapping ``(coordinator_data, room_name)`` to the
            native value. ``room_name`` is the room key for per-room sensors and
            ``None`` for global sensors.

    Raises:
        ValueError: If ``value_fn`` is not callable.
    """

    value_fn: Callable[[CoordinatorData, str | None], Any]

    def __post_init__(self) -> None:
        """Validate that ``value_fn`` is callable."""
        if not callable(self.value_fn):
            msg = f"value_fn must be callable, got {self.value_fn!r}"
            raise ValueError(msg)


# Per-room diagnostic sensors.
ROOM_SENSORS: tuple[TortoiseUfhSensorEntityDescription, ...] = (
    TortoiseUfhSensorEntityDescription(
        key="recommended_valve",
        translation_key="recommended_valve",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=_room_recommended_valve,
    ),
    TortoiseUfhSensorEntityDescription(
        key="error_c",
        translation_key="error_c",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        value_fn=_room_error_c,
    ),
    TortoiseUfhSensorEntityDescription(
        key="trend_c_per_h",
        translation_key="trend_c_per_h",
        native_unit_of_measurement="°C/h",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        value_fn=_room_trend_c_per_h,
    ),
    TortoiseUfhSensorEntityDescription(
        key="i_term",
        translation_key="i_term",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=_room_i_term,
    ),
    TortoiseUfhSensorEntityDescription(
        key="trend_term",
        translation_key="trend_term",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=_room_trend_term,
    ),
    TortoiseUfhSensorEntityDescription(
        key="room_dew_point",
        translation_key="room_dew_point",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=_room_dew_point_c,
    ),
    TortoiseUfhSensorEntityDescription(
        key="fast_source_mode",
        translation_key="fast_source_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_room_fast_source_mode,
    ),
    TortoiseUfhSensorEntityDescription(
        key="explanation",
        translation_key="explanation",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_room_explanation,
    ),
)

# Global diagnostic sensors.
GLOBAL_SENSORS: tuple[TortoiseUfhSensorEntityDescription, ...] = (
    TortoiseUfhSensorEntityDescription(
        key="global_safe_dew_point",
        translation_key="global_safe_dew_point",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=_global_safe_dew_point,
    ),
    TortoiseUfhSensorEntityDescription(
        key="algorithm_status",
        translation_key="algorithm_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_global_algorithm_status,
    ),
    TortoiseUfhSensorEntityDescription(
        key="last_update",
        translation_key="last_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_global_last_update,
    ),
    TortoiseUfhSensorEntityDescription(
        key="watchdog_status",
        translation_key="watchdog_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_global_watchdog_status,
    ),
    TortoiseUfhSensorEntityDescription(
        key="hp_flicker_state",
        translation_key="hp_flicker_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_global_hp_flicker_state,
    ),
)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tortoise-UFH sensor entities from a config entry.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry (its ``runtime_data`` holds the coordinator).
        async_add_entities: Callback to register the created entities.
    """
    coordinator: TortoiseUfhCoordinator = entry.runtime_data.coordinator  # type: ignore[attr-defined]

    entities: list[TortoiseUfhSensorEntity] = []

    # Per-room sensors, one set per configured room. Built from the configured
    # room list (not coordinator.data.rooms) so rooms missing from the first
    # payload — e.g. after an error cycle — still get diagnostic entities; they
    # render as unavailable until the coordinator recovers.
    rooms: list[dict[str, Any]] = list(entry.data.get(CONF_ROOMS, []) or [])
    for room_cfg in rooms:
        room_name = str(room_cfg[CONF_ROOM_NAME])
        entities.extend(
            TortoiseUfhSensorEntity(
                coordinator=coordinator,
                description=description,
                entry_id=entry.entry_id,
                room_name=room_name,
            )
            for description in ROOM_SENSORS
        )

    # Global sensors (room_name=None).
    entities.extend(
        TortoiseUfhSensorEntity(
            coordinator=coordinator,
            description=description,
            entry_id=entry.entry_id,
            room_name=None,
        )
        for description in GLOBAL_SENSORS
    )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Sensor entity
# ---------------------------------------------------------------------------


class TortoiseUfhSensorEntity(CoordinatorEntity[TortoiseUfhCoordinator], SensorEntity):
    """Read-only diagnostic sensor backed by the coordinator's typed payload."""

    entity_description: TortoiseUfhSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TortoiseUfhCoordinator,
        description: TortoiseUfhSensorEntityDescription,
        entry_id: str,
        room_name: str | None,
    ) -> None:
        """Initialise a Tortoise-UFH diagnostic sensor.

        Args:
            coordinator: The Tortoise-UFH data coordinator.
            description: The sensor description carrying the ``value_fn``.
            entry_id: The config entry id (used for the unique id).
            room_name: The room name for a per-room sensor, or ``None`` for a
                global sensor.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._room_name = room_name

        if room_name is not None:
            slug = room_slug(room_name)
            self._attr_unique_id = f"{entry_id}_{slug}_{description.key}"
            self._attr_device_info = room_device_info(entry_id, room_name)
        else:
            self._attr_unique_id = f"{entry_id}_{description.key}"
            self._attr_device_info = hub_device_info(entry_id)

    @property
    def native_value(self) -> Any:
        """Return the sensor's value extracted from the coordinator data.

        Returns:
            The native value from the description's ``value_fn``, or ``None``
            when the coordinator has produced no data yet.
        """
        data = self.coordinator.data
        if data is None:
            return None
        return self.entity_description.value_fn(data, self._room_name)
