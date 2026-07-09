"""Hardware-agnostic entity validation for the Tortoise-UFH config flow.

Validates a Home Assistant entity's ``unit_of_measurement``, ``device_class``,
and availability at config time. This is a standalone utility class that
delegates to :meth:`hass.states.get`; it carries no HA base-class inheritance
and stores no mutable state.

Hardware-agnostic contract: validate *units only* (temperatures in degrees
Celsius ``degC``, relative humidity / valve position in percent ``%``, power in
watts ``W``) and never a brand or model. Missing ``unit_of_measurement`` or
``device_class`` attributes are tolerated and PASS -- some actuators and
sensors legitimately do not declare them. The one capability (not brand) gate is
for ``valve``-domain actuators: they must advertise
``ValveEntityFeature.SET_POSITION`` to be driven to a percentage.

Units:
    Temperature entities in degrees Celsius (degC), humidity and valve
    position in percent (%), power in watts (W).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.valve import ValveEntityFeature
from homeassistant.core import HomeAssistant, split_entity_id

_LOGGER = logging.getLogger(__name__)

_VALVE_DOMAIN: str = "valve"
"""Home Assistant domain of position-capable ``valve`` actuator entities."""


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a single entity validation check.

    Attributes:
        valid: ``True`` when the check passed, ``False`` on a hard failure.
        error_key: Machine-readable failure key (e.g. ``"invalid_unit"``),
            or ``None`` on success.
        error_details: Human-readable explanation of the failure, or ``None``
            on success.

    Raises:
        ValueError: If a failing result (``valid=False``) carries no
            ``error_key``, or a passing result carries one.
    """

    valid: bool
    error_key: str | None = None
    error_details: str | None = None

    def __post_init__(self) -> None:
        """Validate internal consistency of the result.

        Raises:
            ValueError: On an inconsistent (valid, error_key) combination.
        """
        if not self.valid and self.error_key is None:
            msg = "A failing ValidationResult must carry an error_key"
            raise ValueError(msg)
        if self.valid and self.error_key is not None:
            msg = "A passing ValidationResult must not carry an error_key"
            raise ValueError(msg)


class EntityValidator:
    """Validates HA entities for unit, device_class, and availability.

    Designed for use in the config flow. Instantiate with a live
    :class:`~homeassistant.core.HomeAssistant` instance and call
    :meth:`validate_entity` for a composite check, or the individual
    ``validate_*`` methods. The validator is hardware-agnostic: it only ever
    inspects declared units (degC / % / W) and device classes, never a brand.

    Typical usage::

        validator = EntityValidator(hass)
        result = validator.validate_entity(
            "sensor.living_room_temp",
            valid_units={"degC", "C"},
            expected_device_class="temperature",
        )
        if not result.valid:
            errors["base"] = result.error_key
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Store the Home Assistant instance used for state lookups.

        Args:
            hass: Live Home Assistant instance.
        """
        self._hass = hass

    # -- Individual checks --------------------------------------------------

    def validate_unit(self, entity_id: str, valid_units: set[str]) -> ValidationResult:
        """Check that the entity's ``unit_of_measurement`` is acceptable.

        Tolerant by design: an entity that declares no ``unit_of_measurement``
        PASSES, since some actuators do not report units.

        Args:
            entity_id: Entity id to inspect (empty string PASSES).
            valid_units: Accepted unit strings (e.g. ``{"degC", "C"}`` for
                temperature, ``{"%"}`` for percent, ``{"W"}`` for power).

        Returns:
            A passing result, or a failure with key ``entity_not_found`` or
            ``invalid_unit``.
        """
        if not entity_id:
            return ValidationResult(valid=True)

        state = self._hass.states.get(entity_id)
        if state is None:
            return ValidationResult(
                valid=False,
                error_key="entity_not_found",
                error_details=f"Entity {entity_id} not found in Home Assistant",
            )

        unit = getattr(state, "attributes", {}).get("unit_of_measurement")
        if unit is None:
            return ValidationResult(valid=True)

        if unit not in valid_units:
            return ValidationResult(
                valid=False,
                error_key="invalid_unit",
                error_details=(
                    f"Entity {entity_id} has unit '{unit}', "
                    f"expected one of: {', '.join(sorted(valid_units))}"
                ),
            )

        return ValidationResult(valid=True)

    def validate_device_class(
        self, entity_id: str, expected_device_class: str
    ) -> ValidationResult:
        """Check the entity's ``device_class`` attribute.

        Tolerant by design: an entity that declares no ``device_class``
        PASSES.

        Args:
            entity_id: Entity id to inspect (empty string PASSES).
            expected_device_class: Required device class (e.g.
                ``"temperature"`` or ``"humidity"``).

        Returns:
            A passing result, or a failure with key ``entity_not_found`` or
            ``invalid_device_class``.
        """
        if not entity_id:
            return ValidationResult(valid=True)

        state = self._hass.states.get(entity_id)
        if state is None:
            return ValidationResult(
                valid=False,
                error_key="entity_not_found",
                error_details=f"Entity {entity_id} not found in Home Assistant",
            )

        device_class = getattr(state, "attributes", {}).get("device_class")
        if device_class is None:
            return ValidationResult(valid=True)

        if device_class != expected_device_class:
            return ValidationResult(
                valid=False,
                error_key="invalid_device_class",
                error_details=(
                    f"Entity {entity_id} has device_class '{device_class}', "
                    f"expected '{expected_device_class}'"
                ),
            )

        return ValidationResult(valid=True)

    def validate_availability(self, entity_id: str) -> ValidationResult:
        """Check that the entity is not ``unavailable`` or ``unknown``.

        This is a config-time warning check; the caller decides whether to
        block or merely log, since an entity may come online later.

        Args:
            entity_id: Entity id to inspect (empty string PASSES).

        Returns:
            A passing result, or a failure with key ``entity_not_found`` or
            ``entity_unavailable``.
        """
        if not entity_id:
            return ValidationResult(valid=True)

        state = self._hass.states.get(entity_id)
        if state is None:
            return ValidationResult(
                valid=False,
                error_key="entity_not_found",
                error_details=f"Entity {entity_id} not found in Home Assistant",
            )

        if state.state in ("unavailable", "unknown"):
            return ValidationResult(
                valid=False,
                error_key="entity_unavailable",
                error_details=(
                    f"Entity {entity_id} is currently {state.state} "
                    "-- it may come online later"
                ),
            )

        return ValidationResult(valid=True)

    def validate_valve_set_position(self, entity_id: str) -> ValidationResult:
        """Assert a ``valve``-domain actuator can be driven to a position.

        Only ``valve``-domain entities are gated: their position is written via
        the ``valve.set_valve_position`` service, which requires the
        :attr:`~homeassistant.components.valve.ValveEntityFeature.SET_POSITION`
        capability bit (value ``4``). Any other domain (``number`` …), positioned
        via ``number.set_value``, PASSES unconditionally, as does an empty id.
        A ``valve`` whose ``supported_features`` omits ``SET_POSITION`` cannot be
        commanded to a percentage and is rejected here rather than failing
        silently at runtime.

        Args:
            entity_id: Entity id to inspect (empty string PASSES; a non-``valve``
                domain PASSES).

        Returns:
            A passing result, or a failure with key ``entity_not_found`` or
            ``valve_no_set_position``.
        """
        if not entity_id:
            return ValidationResult(valid=True)
        if split_entity_id(entity_id)[0] != _VALVE_DOMAIN:
            return ValidationResult(valid=True)

        state = self._hass.states.get(entity_id)
        if state is None:
            return ValidationResult(
                valid=False,
                error_key="entity_not_found",
                error_details=f"Entity {entity_id} not found in Home Assistant",
            )

        features = getattr(state, "attributes", {}).get("supported_features", 0) or 0
        if not int(features) & ValveEntityFeature.SET_POSITION:
            return ValidationResult(
                valid=False,
                error_key="valve_no_set_position",
                error_details=(
                    f"Valve {entity_id} does not support setting a position "
                    "(ValveEntityFeature.SET_POSITION); pick a position-capable "
                    "valve or a number entity"
                ),
            )

        return ValidationResult(valid=True)

    # -- Composite check ----------------------------------------------------

    def validate_entity(
        self,
        entity_id: str,
        valid_units: set[str] | None = None,
        expected_device_class: str | None = None,
    ) -> ValidationResult:
        """Run all applicable checks on a single entity.

        Order: existence -> availability (warning only) -> device_class ->
        unit. Returns the first hard failure, or a passing result. Availability
        is non-blocking: it is logged as a warning but never fails the check,
        because the entity may come online after setup.

        Args:
            entity_id: Entity id to inspect (empty string PASSES).
            valid_units: Accepted unit strings, or ``None`` to skip the unit
                check.
            expected_device_class: Required device class, or ``None`` to skip
                the device-class check.

        Returns:
            The first hard failure, or a passing result.
        """
        if not entity_id:
            return ValidationResult(valid=True)

        # 1. Existence check.
        state = self._hass.states.get(entity_id)
        if state is None:
            return ValidationResult(
                valid=False,
                error_key="entity_not_found",
                error_details=f"Entity {entity_id} not found in Home Assistant",
            )

        # 2. Availability (warning only -- logged, not blocking).
        avail = self.validate_availability(entity_id)
        if not avail.valid and avail.error_key == "entity_unavailable":
            _LOGGER.warning("%s", avail.error_details)

        # 3. Device class (blocking if requested).
        if expected_device_class is not None:
            dc_result = self.validate_device_class(entity_id, expected_device_class)
            if not dc_result.valid and dc_result.error_key != "entity_unavailable":
                return dc_result

        # 4. Unit (blocking if requested).
        if valid_units is not None:
            unit_result = self.validate_unit(entity_id, valid_units)
            if not unit_result.valid:
                return unit_result

        return ValidationResult(valid=True)
