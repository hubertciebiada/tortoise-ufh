"""Controller-knob introspection shared by the tuning surfaces.

Extracted verbatim from ``websocket.py`` (2026-07-10): the helpers that expose
the :data:`~const.CONTROLLER_NUMBER_KNOBS` / :data:`~const.CONTROLLER_BOOL_KNOBS`
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
    CONF_ENTITY_RETURN,
    CONF_ENTITY_SUPPLY,
    CONF_ENTITY_VALVES,
    CONF_ROOM_TUNING,
    CONTROLLER_BOOL_KNOBS,
    CONTROLLER_KNOB_UNITS,
    CONTROLLER_NUMBER_KNOBS,
    HP_GLOBAL_ONLY_KNOBS,
)
from .core.config import ControllerConfig

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

__all__ = [
    "coerce_tuning_values",
    "flicker_open_max_pct",
    "global_controller",
    "global_controller_dict",
    "knob_names",
    "knob_range",
    "knob_values",
    "room_overrides",
    "tuning_fields",
]

_FLICKER_OPEN_KNOB = "hp_flicker_min_open_pct"
"""The one knob whose UI maximum is dynamic (total loops x 100)."""


def flicker_open_max_pct(
    rooms: list[dict[str, Any]], *, current_value: float | None = None
) -> float:
    """The effective ceiling of ``hp_flicker_min_open_pct``.

    The physical ceiling is ``total loop count x 100``; a room's loop count
    mirrors the coordinator's ``_build_loops``: ``max(len(valves),
    len(supplies), len(returns))``, at least 1 (the config flow enforces one
    valve per room). Takes the raw room-config dicts — not the entry — so the
    setup wizard can pass its in-flight room list before an entry exists.

    The ceiling never shrinks below ``current_value`` (the stored knob value):
    after removing rooms/loops the persisted value may exceed the new physical
    ceiling, and it must still round-trip — the panel's global save resends
    EVERY global knob, so a suddenly-too-low ceiling would reject the whole
    batch over a knob the user never touched, and the options form would
    silently pull the stored value down. Widening to the stored value keeps
    both paths lossless while still only allowing the user to KEEP or LOWER
    an over-ceiling value, never raise it further.

    Args:
        rooms: The per-room config dicts (``entry.data[CONF_ROOMS]`` shape).
        current_value: The currently persisted ``hp_flicker_min_open_pct``,
            or ``None`` when there is none to protect (setup wizard).

    Returns:
        ``max(total loop count, 1) x 100`` [%], floored by ``current_value``.
    """
    total_loops = 0
    for room_cfg in rooms:
        if not isinstance(room_cfg, dict):
            continue
        total_loops += max(
            1,
            len(room_cfg.get(CONF_ENTITY_VALVES) or []),
            len(room_cfg.get(CONF_ENTITY_SUPPLY) or []),
            len(room_cfg.get(CONF_ENTITY_RETURN) or []),
        )
    ceiling = float(max(1, total_loops)) * 100.0
    if current_value is not None:
        ceiling = max(ceiling, float(current_value))
    return ceiling


def knob_names() -> list[str]:
    """Return every exposed knob field name (numeric knobs + boolean knobs)."""
    return [name for name, _low, _high, _step in CONTROLLER_NUMBER_KNOBS] + list(
        CONTROLLER_BOOL_KNOBS
    )


def knob_range(field_name: str) -> tuple[float, float] | None:
    """Return a numeric knob's ``(min, max)``, or ``None`` for a boolean knob.

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


def tuning_fields(*, open_max_pct: float | None = None) -> tuple[dict[str, Any], ...]:
    """Build the ordered knob descriptors for the ``get_tuning`` payload.

    Args:
        open_max_pct: The dynamic ceiling for ``hp_flicker_min_open_pct``
            (total configured loops x 100; see :func:`flicker_open_max_pct`).
            ``None`` keeps the loose static spec maximum.

    Returns:
        One descriptor dict per exposed knob, numeric knobs first.
    """
    fields: list[dict[str, Any]] = []
    for name, low, high, step in CONTROLLER_NUMBER_KNOBS:
        if name == _FLICKER_OPEN_KNOB and open_max_pct is not None:
            high = max(low, open_max_pct)
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
    for name in CONTROLLER_BOOL_KNOBS:
        fields.append(
            {
                "name": name,
                "type": "bool",
                "unit": CONTROLLER_KNOB_UNITS.get(name, ""),
            }
        )
    return tuple(fields)


def knob_values(config: ControllerConfig) -> dict[str, Any]:
    """Extract the exposed-knob values (numeric + boolean) from a config."""
    values: dict[str, Any] = {
        name: float(getattr(config, name))
        for name, _low, _high, _step in CONTROLLER_NUMBER_KNOBS
    }
    for name in CONTROLLER_BOOL_KNOBS:
        values[name] = bool(getattr(config, name))
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
        or fields that are not valid knobs are dropped. The global-only
        heat-pump knobs (:data:`~const.HP_GLOBAL_ONLY_KNOBS`) are dropped too —
        a hand-edited per-room override of a building-level water setpoint
        would have zero effect and must never surface as "overridden".
    """
    raw: Any = entry.options.get(CONF_ROOM_TUNING, {})
    knobs = set(knob_names()) - HP_GLOBAL_ONLY_KNOBS
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
    raw_values: dict[str, Any],
    *,
    allow_delete: bool,
    open_max_pct: float | None = None,
) -> dict[str, Any]:
    """Coerce + range-validate submitted knob values.

    Args:
        raw_values: The ``{field: value}`` payload. A ``None`` value requests
            deletion of that field's override (room scope only).
        allow_delete: Whether ``None`` values are permitted. ``True`` is the
            ROOM scope (the only scope with deletable overrides), so it also
            gates the global-only heat-pump knobs below.
        open_max_pct: The dynamic ceiling for ``hp_flicker_min_open_pct``
            (see :func:`flicker_open_max_pct`); ``None`` falls back to the
            loose static spec maximum.

    Returns:
        A ``{field: value}`` dict where numeric knobs are floats, the boolean
        knob is a bool, and (when ``allow_delete``) a deleted field maps to
        ``None``.

    Raises:
        ValueError: If a field is not an exposed knob, a global-only heat-pump
            knob is submitted for a room scope (B2 — a per-room water setpoint
            has no physical meaning), a value has the wrong type, a numeric
            value is out of range, or a ``None`` is submitted when
            ``allow_delete`` is ``False``.
    """
    knobs = set(knob_names())
    coerced: dict[str, Any] = {}
    for field_name, value in raw_values.items():
        if field_name not in knobs:
            msg = f"unknown knob {field_name!r}"
            raise ValueError(msg)
        if allow_delete and field_name in HP_GLOBAL_ONLY_KNOBS:
            msg = f"{field_name!r} is a global-only knob (no per-room override)"
            raise ValueError(msg)
        if value is None:
            if not allow_delete:
                msg = f"cannot clear global knob {field_name!r}"
                raise ValueError(msg)
            coerced[field_name] = None
            continue
        if field_name in CONTROLLER_BOOL_KNOBS:
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
        if rng is not None:
            low, high = rng
            if field_name == _FLICKER_OPEN_KNOB and open_max_pct is not None:
                high = max(low, open_max_pct)
            if not low <= numeric <= high:
                msg = f"{field_name} must be in [{low}, {high}], got {numeric}"
                raise ValueError(msg)
        coerced[field_name] = numeric
    return coerced
