"""Controller-knob introspection shared by the tuning surfaces.

Extracted verbatim from ``websocket.py`` (2026-07-10): the helpers that expose
the :data:`~const.CONTROLLER_NUMBER_KNOBS` / :data:`~const.CONTROLLER_BOOL_KNOB`
vocabulary — names, ranges, descriptors, per-entry effective values, sparse
per-room overrides and payload coercion — now live in one adapter module, so
the websocket handlers (``get_tuning`` / ``set_tuning``) and the options flow
consume a single source of knob logic instead of re-deriving it.

This module holds no handlers and no physical control logic; it only maps
between the persisted ``entry.data`` / ``entry.options`` dicts and the frozen
core :class:`~tortoise_ufh.config.ControllerConfig`.

Units: knob values follow the core config contract (gains in %/K etc.; see
:data:`~const.CONTROLLER_KNOB_UNITS`).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from .const import (
    CONF_CONTROLLER,
    CONF_ROOM_TUNING,
    CONTROLLER_BOOL_KNOB,
    CONTROLLER_KNOB_UNITS,
    CONTROLLER_NUMBER_KNOBS,
)
from .core.config import ControllerConfig

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

__all__ = [
    "coerce_tuning_values",
    "global_controller",
    "global_controller_dict",
    "knob_names",
    "knob_range",
    "knob_values",
    "room_overrides",
    "tuning_fields",
]


def knob_names() -> list[str]:
    """Return every exposed knob field name (numeric knobs + the boolean knob)."""
    return [name for name, _low, _high, _step in CONTROLLER_NUMBER_KNOBS] + [
        CONTROLLER_BOOL_KNOB
    ]


def knob_range(field_name: str) -> tuple[float, float] | None:
    """Return a numeric knob's ``(min, max)``, or ``None`` for the boolean knob.

    Args:
        field_name: A candidate knob field name.

    Returns:
        The inclusive ``(min, max)`` for a numeric knob, or ``None`` when the
        field is the boolean knob or is not an exposed knob.
    """
    for name, low, high, _step in CONTROLLER_NUMBER_KNOBS:
        if name == field_name:
            return (low, high)
    return None


def tuning_fields() -> tuple[dict[str, Any], ...]:
    """Build the ordered knob descriptors for the ``get_tuning`` payload."""
    fields: list[dict[str, Any]] = []
    for name, low, high, step in CONTROLLER_NUMBER_KNOBS:
        fields.append(
            {
                "name": name,
                "type": "number",
                "min": low,
                "max": high,
                "step": step,
                "unit": CONTROLLER_KNOB_UNITS.get(name, ""),
            }
        )
    fields.append(
        {
            "name": CONTROLLER_BOOL_KNOB,
            "type": "bool",
            "unit": CONTROLLER_KNOB_UNITS.get(CONTROLLER_BOOL_KNOB, ""),
        }
    )
    return tuple(fields)


def knob_values(config: ControllerConfig) -> dict[str, Any]:
    """Extract the exposed-knob values (numeric + boolean) from a config."""
    values: dict[str, Any] = {
        name: float(getattr(config, name))
        for name, _low, _high, _step in CONTROLLER_NUMBER_KNOBS
    }
    values[CONTROLLER_BOOL_KNOB] = bool(getattr(config, CONTROLLER_BOOL_KNOB))
    return values


def global_controller_dict(entry: ConfigEntry) -> dict[str, Any]:
    """Return the merged global controller dict (``entry.data`` <- ``options``)."""
    return {
        **entry.data.get(CONF_CONTROLLER, {}),
        **entry.options.get(CONF_CONTROLLER, {}),
    }


def global_controller(entry: ConfigEntry) -> ControllerConfig:
    """Resolve the effective global :class:`ControllerConfig` for an entry.

    Args:
        entry: The config entry.

    Returns:
        The validated global controller config, or library defaults when the
        persisted values are absent or invalid.
    """
    try:
        return ControllerConfig(**global_controller_dict(entry))
    except (TypeError, ValueError):
        return ControllerConfig()


def room_overrides(entry: ConfigEntry) -> dict[str, dict[str, Any]]:
    """Return the sparse per-room override map, filtered to known knob fields.

    Args:
        entry: The config entry.

    Returns:
        ``{room: {field: value}}`` containing only recognised knob fields; rooms
        or fields that are not valid knobs are dropped.
    """
    raw: Any = entry.options.get(CONF_ROOM_TUNING, {})
    knobs = set(knob_names())
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for room, override in raw.items():
        if not isinstance(override, dict):
            continue
        clean = {
            field_name: value
            for field_name, value in override.items()
            if field_name in knobs
        }
        if clean:
            out[str(room)] = clean
    return out


def coerce_tuning_values(
    raw_values: dict[str, Any], *, allow_delete: bool
) -> dict[str, Any]:
    """Coerce + range-validate submitted knob values.

    Args:
        raw_values: The ``{field: value}`` payload. A ``None`` value requests
            deletion of that field's override (room scope only).
        allow_delete: Whether ``None`` values are permitted (room scope).

    Returns:
        A ``{field: value}`` dict where numeric knobs are floats, the boolean
        knob is a bool, and (when ``allow_delete``) a deleted field maps to
        ``None``.

    Raises:
        ValueError: If a field is not an exposed knob, a value has the wrong
            type, a numeric value is out of range, or a ``None`` is submitted
            when ``allow_delete`` is ``False``.
    """
    knobs = set(knob_names())
    coerced: dict[str, Any] = {}
    for field_name, value in raw_values.items():
        if field_name not in knobs:
            msg = f"unknown knob {field_name!r}"
            raise ValueError(msg)
        if value is None:
            if not allow_delete:
                msg = f"cannot clear global knob {field_name!r}"
                raise ValueError(msg)
            coerced[field_name] = None
            continue
        if field_name == CONTROLLER_BOOL_KNOB:
            if not isinstance(value, bool):
                msg = f"{field_name} must be a boolean, got {value!r}"
                raise ValueError(msg)
            coerced[field_name] = value
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError) as err:
            msg = f"{field_name} must be a number, got {value!r}"
            raise ValueError(msg) from err
        if not math.isfinite(numeric):
            msg = f"{field_name} must be finite, got {numeric}"
            raise ValueError(msg)
        rng = knob_range(field_name)
        if rng is not None and not rng[0] <= numeric <= rng[1]:
            msg = f"{field_name} must be in [{rng[0]}, {rng[1]}], got {numeric}"
            raise ValueError(msg)
        coerced[field_name] = numeric
    return coerced
