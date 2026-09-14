"""Manifold (distributor) view model for the panel's Manifolds tab.

Pure core: builds a JSON-serialisable picture of every configured underfloor
manifold — which room loop sits on which circuit position, the valve opening,
the loop's supply / return water probes and the loop-related flags — from the
manifold definitions, the rooms' loop wiring and a plain mapping of current
entity readings. The manifold is presentation only: nothing here feeds the
controller, and this module MUST NOT import ``homeassistant``.

A loop is identified by its VALVE entity id. The adapter stores each room's
loops as parallel entity lists (valves / supply probes / return probes indexed
by loop), so a valve id resolves to ``(room, loop index)`` and from there to
that loop's probes — robust against rooms being edited or removed (a valve no
longer wired to any room simply leaves its circuit position free).

Units: temperatures in degrees Celsius (``_c``), valve opening in percent
(0..100, ``_pct``), temperature differences in kelvin (``_k``). Circuit
positions are 1-based, counted from the manifold's main-connection end.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

MANIFOLD_SIDE_LEFT: str = "left"
"""Main connection (supply inlet / return outlet, probes Z0 / P0) on the left."""

MANIFOLD_SIDE_RIGHT: str = "right"
"""Main connection on the right; circuit 1 is then the rightmost position."""

MANIFOLD_SIDES: tuple[str, ...] = (MANIFOLD_SIDE_LEFT, MANIFOLD_SIDE_RIGHT)
"""Accepted ``side`` values of a manifold."""

MANIFOLD_CIRCUITS_MIN: int = 1
MANIFOLD_CIRCUITS_MAX: int = 20
"""Inclusive circuit-count range of a manifold (the drawing is parametric;
catalogue units run 2..12, longer bars are an extrapolation)."""

OPEN_THRESHOLD_PCT: float = 5.0
"""Valve opening at or above which a circuit counts as open (panel statistic)."""

LOOP_ROOM_FLAGS: frozenset[str] = frozenset(
    {"s1_floor_overheat", "s2_condensation", "valve_mismatch"}
)
"""Room-level flags that describe the room's VALVE side and therefore belong on
every circuit the room owns (S1 closes the valve, the S2 hard backstop closes
it, S8 says the actuator does not follow the command)."""

LOOP_FLAG_NO_FLOW: str = "loop_no_flow"
"""Per-loop S6 verdict (``RoomReport.loop_flow_status[i] == "no_flow"``)."""

LOOP_FLAG_TEST_FAILED: str = "actuation_test_failed"
"""Per-loop self-test verdict (``RoomReport.actuation_test_loops[i] == "failed"``)."""


# ---------------------------------------------------------------------------
# Configuration (the storage form: ``to_dict()`` is what the adapter persists)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifoldLoopConfig:
    """One circuit assignment: a room loop (by its valve entity) on a position.

    Attributes:
        position: 1-based circuit position on the manifold (the upper bound is
            validated by the owning :class:`ManifoldConfig`).
        entity_valve: The valve entity id of the room loop sitting there. It
            is the loop's identity: the adapter resolves it to the room and
            the loop index, and from there to the supply / return probes.
        label: Optional short name shown on the drawing / in the table
            (e.g. the name printed on the manifold's circuit tag). Empty
            = derived from the room name.

    Raises:
        ValueError: If ``position`` is below 1 or ``entity_valve`` is empty.
    """

    position: int
    entity_valve: str
    label: str = ""

    def __post_init__(self) -> None:
        """Validate the position and the valve id."""
        if self.position < 1:
            msg = f"position must be >= 1, got {self.position}"
            raise ValueError(msg)
        if not self.entity_valve.strip():
            msg = "entity_valve must be a non-empty entity id"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable storage form of this assignment."""
        return {
            "position": self.position,
            "entity_valve": self.entity_valve,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ManifoldLoopConfig:
        """Build an assignment from its storage form.

        Args:
            raw: A mapping with ``position`` and ``entity_valve`` (``label``
                optional).

        Returns:
            The validated :class:`ManifoldLoopConfig`.

        Raises:
            KeyError: If a required key is missing.
            ValueError: If a value fails validation or coercion.
        """
        return cls(
            position=int(raw["position"]),
            entity_valve=str(raw["entity_valve"]),
            label=str(raw.get("label") or "").strip(),
        )


@dataclass(frozen=True)
class ManifoldConfig:
    """One underfloor manifold (distributor) and its circuit assignments.

    Attributes:
        manifold_id: Stable identifier (the slug of the name at creation).
        name: Human-readable name (e.g. ``"East"``).
        circuits: Number of circuit positions (see
            :data:`MANIFOLD_CIRCUITS_MIN` / :data:`MANIFOLD_CIRCUITS_MAX`).
        side: Which end carries the main connection — one of
            :data:`MANIFOLD_SIDES`. Circuit 1 is the position next to it.
        entity_supply_main: Optional temperature probe on the main supply
            inlet (Z0), or ``None``.
        entity_return_main: Optional temperature probe on the main return
            outlet (P0), or ``None``.
        loops: The circuit assignments; positions and valve ids unique.

    Raises:
        ValueError: If the id or name is empty, ``circuits`` is out of range,
            ``side`` is unknown, an assignment's position exceeds
            ``circuits``, or two assignments share a position or a valve.
    """

    manifold_id: str
    name: str
    circuits: int
    side: str = MANIFOLD_SIDE_LEFT
    entity_supply_main: str | None = None
    entity_return_main: str | None = None
    loops: tuple[ManifoldLoopConfig, ...] = ()

    def __post_init__(self) -> None:
        """Validate identity, geometry and assignment uniqueness."""
        if not self.manifold_id.strip():
            msg = "manifold_id must be a non-empty string"
            raise ValueError(msg)
        if not self.name.strip():
            msg = "name must be a non-empty string"
            raise ValueError(msg)
        if not MANIFOLD_CIRCUITS_MIN <= self.circuits <= MANIFOLD_CIRCUITS_MAX:
            msg = (
                f"circuits must be in [{MANIFOLD_CIRCUITS_MIN}, "
                f"{MANIFOLD_CIRCUITS_MAX}], got {self.circuits}"
            )
            raise ValueError(msg)
        if self.side not in MANIFOLD_SIDES:
            msg = f"side must be one of {MANIFOLD_SIDES}, got {self.side!r}"
            raise ValueError(msg)
        positions = [loop.position for loop in self.loops]
        if any(position > self.circuits for position in positions):
            msg = (
                f"loop position exceeds the {self.circuits} circuits of "
                f"manifold {self.name!r}: {sorted(positions)}"
            )
            raise ValueError(msg)
        if len(positions) != len(set(positions)):
            msg = f"manifold {self.name!r} assigns one position twice"
            raise ValueError(msg)
        valves = [loop.entity_valve for loop in self.loops]
        if len(valves) != len(set(valves)):
            msg = f"manifold {self.name!r} assigns one loop to two positions"
            raise ValueError(msg)

    def loop_at(self, position: int) -> ManifoldLoopConfig | None:
        """Return the assignment on ``position``, or ``None`` when free.

        Args:
            position: 1-based circuit position.

        Returns:
            The matching :class:`ManifoldLoopConfig`, or ``None``.
        """
        for loop in self.loops:
            if loop.position == position:
                return loop
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable storage form of this manifold."""
        return {
            "manifold_id": self.manifold_id,
            "name": self.name,
            "circuits": self.circuits,
            "side": self.side,
            "entity_supply_main": self.entity_supply_main,
            "entity_return_main": self.entity_return_main,
            "loops": [loop.to_dict() for loop in self.loops],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ManifoldConfig:
        """Build a manifold from its storage form.

        Tolerant of absent optional keys (``side`` defaults to left, probes
        to ``None``, ``loops`` to none); an empty probe string reads as
        ``None``.

        Args:
            raw: A mapping with ``manifold_id``, ``name`` and ``circuits``.

        Returns:
            The validated :class:`ManifoldConfig`.

        Raises:
            KeyError: If a required key is missing.
            TypeError: If ``loops`` is not a sequence of mappings.
            ValueError: If a value fails validation or coercion.
        """
        raw_loops = raw.get("loops") or ()
        return cls(
            manifold_id=str(raw["manifold_id"]),
            name=str(raw["name"]),
            circuits=int(raw["circuits"]),
            side=str(raw.get("side") or MANIFOLD_SIDE_LEFT),
            entity_supply_main=str(raw.get("entity_supply_main") or "") or None,
            entity_return_main=str(raw.get("entity_return_main") or "") or None,
            loops=tuple(ManifoldLoopConfig.from_dict(loop) for loop in raw_loops),
        )


def validate_manifolds(manifolds: Sequence[ManifoldConfig]) -> None:
    """Check the cross-manifold invariants of a whole configuration.

    Args:
        manifolds: Every configured manifold.

    Raises:
        ValueError: If two manifolds share an id, or one loop (valve entity)
            is assigned on more than one manifold.
    """
    ids = [manifold.manifold_id for manifold in manifolds]
    if len(ids) != len(set(ids)):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        msg = f"manifold ids must be unique, duplicates: {duplicates}"
        raise ValueError(msg)
    seen: dict[str, str] = {}
    for manifold in manifolds:
        for loop in manifold.loops:
            owner = seen.get(loop.entity_valve)
            if owner is not None:
                msg = (
                    f"loop {loop.entity_valve!r} is assigned on both "
                    f"{owner!r} and {manifold.name!r}"
                )
                raise ValueError(msg)
            seen[loop.entity_valve] = manifold.name


# ---------------------------------------------------------------------------
# Inputs: what the adapter knows about each room's loops this cycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoomLoopSources:
    """A room's loop wiring plus its current valve-related state.

    The three entity tuples are the room's parallel loop lists (index =
    loop); a missing probe reads as ``None``. The state fields are copied
    from the room's latest outputs / report (all optional so a room without
    a computed cycle still resolves its wiring).

    Attributes:
        name: The room name.
        valves: Valve entity id per loop (``None`` when the loop has none).
        supplies: Supply-water probe entity id per loop, or ``None``.
        returns: Return-water probe entity id per loop, or ``None``.
        valve_command_pct: The commanded valve position [0..100 %] common to
            all the room's loops, or ``None`` when unknown.
        flags: The room report's flags.
        loop_flow_status: Per-loop S6 status (``"ok"`` / ``"no_flow"`` /
            ``"inactive"``), aligned with the loops.
        actuation_test_loops: Per-loop self-test verdicts, aligned with the
            loops.

    Raises:
        ValueError: If ``name`` is empty or ``valve_command_pct`` is outside
            0..100.
    """

    name: str
    valves: tuple[str | None, ...] = ()
    supplies: tuple[str | None, ...] = ()
    returns: tuple[str | None, ...] = ()
    valve_command_pct: float | None = None
    flags: tuple[str, ...] = ()
    loop_flow_status: tuple[str, ...] = ()
    actuation_test_loops: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the name and the command range."""
        if not self.name.strip():
            msg = "name must be a non-empty string"
            raise ValueError(msg)
        if self.valve_command_pct is not None and not (
            0.0 <= self.valve_command_pct <= 100.0
        ):
            msg = (
                f"valve_command_pct must be in [0, 100] %, got {self.valve_command_pct}"
            )
            raise ValueError(msg)

    @property
    def n_loops(self) -> int:
        """Number of loops — the longest of the three entity lists."""
        return max(len(self.valves), len(self.supplies), len(self.returns))


# ---------------------------------------------------------------------------
# Output: the view the panel draws
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityReading:
    """A value read from one entity (both may be absent).

    Attributes:
        value: The current numeric reading, or ``None`` when unavailable.
        entity_id: The source entity id (the panel's link to its history),
            or ``None`` when nothing is configured.
    """

    value: float | None = None
    entity_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return ``{"value", "entity_id"}``."""
        return {"value": self.value, "entity_id": self.entity_id}


@dataclass(frozen=True)
class ValveReading:
    """A circuit's valve opening.

    Attributes:
        pct: The opening shown [0..100 %]: the actuator's reported position
            when it is readable, else the room's commanded position, else
            ``None``.
        command_pct: The commanded position [0..100 %], or ``None``.
        entity_id: The valve entity id, or ``None`` on a free position.
    """

    pct: float | None = None
    command_pct: float | None = None
    entity_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return ``{"pct", "command_pct", "entity_id"}``."""
        return {
            "pct": self.pct,
            "command_pct": self.command_pct,
            "entity_id": self.entity_id,
        }


@dataclass(frozen=True)
class ManifoldPositionView:
    """One circuit position; every field but ``position`` is ``None`` when free.

    Attributes:
        position: 1-based circuit position.
        room_name: The room owning the loop, or ``None``.
        loop_index: The loop's index inside the room, or ``None``.
        loop_label: The label drawn on the circuit, or ``None``.
        valve: The valve opening.
        supply: The loop's supply-water probe reading.
        return_: The loop's return-water probe reading (``"return"`` in the
            dict form; the trailing underscore only dodges the keyword).
        delta_k: ``supply - return`` [K] when both probes read, else ``None``.
        flags: Loop-related flags (see :data:`LOOP_ROOM_FLAGS`,
            :data:`LOOP_FLAG_NO_FLOW`, :data:`LOOP_FLAG_TEST_FAILED`).
    """

    position: int
    room_name: str | None = None
    loop_index: int | None = None
    loop_label: str | None = None
    valve: ValveReading = field(default_factory=ValveReading)
    supply: EntityReading = field(default_factory=EntityReading)
    return_: EntityReading = field(default_factory=EntityReading)
    delta_k: float | None = None
    flags: tuple[str, ...] = ()

    @property
    def assigned(self) -> bool:
        """Whether a room loop sits on this position."""
        return self.room_name is not None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable dict (``flags`` -> list)."""
        return {
            "position": self.position,
            "room_name": self.room_name,
            "loop_index": self.loop_index,
            "loop_label": self.loop_label,
            "valve": self.valve.to_dict(),
            "supply": self.supply.to_dict(),
            "return": self.return_.to_dict(),
            "delta_k": self.delta_k,
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class ManifoldView:
    """One manifold as drawn by the panel.

    Attributes:
        manifold_id: The manifold's stable id.
        name: Its name.
        circuits: Number of circuit positions.
        side: Main-connection side (:data:`MANIFOLD_SIDES`).
        main_supply: The Z0 probe reading.
        main_return: The P0 probe reading.
        positions: One entry per circuit position, ascending.
    """

    manifold_id: str
    name: str
    circuits: int
    side: str
    main_supply: EntityReading = field(default_factory=EntityReading)
    main_return: EntityReading = field(default_factory=EntityReading)
    positions: tuple[ManifoldPositionView, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable dict the websocket ships."""
        return {
            "id": self.manifold_id,
            "name": self.name,
            "circuits": self.circuits,
            "side": self.side,
            "main": {
                "supply": self.main_supply.to_dict(),
                "return": self.main_return.to_dict(),
            },
            "positions": [position.to_dict() for position in self.positions],
        }


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def _reading(
    readings: Mapping[str, float | None], entity_id: str | None
) -> float | None:
    """Look up an entity's reading, treating non-finite values as absent."""
    if not entity_id:
        return None
    value = readings.get(entity_id)
    if value is None or not math.isfinite(value):
        return None
    return value


def _pct(value: float | None) -> float | None:
    """Return ``value`` when it is a plausible percentage, else ``None``."""
    if value is None or not 0.0 <= value <= 100.0:
        return None
    return value


def _at(items: Sequence[str | None], index: int) -> str | None:
    """Return ``items[index]`` (``None`` when out of range or empty)."""
    if 0 <= index < len(items):
        return items[index] or None
    return None


def _default_label(room: RoomLoopSources, loop_index: int) -> str:
    """Derive a circuit label: the room name, numbered for a multi-loop room."""
    if room.n_loops <= 1:
        return room.name
    return f"{room.name} {loop_index + 1}"


def _loop_flags(room: RoomLoopSources, loop_index: int) -> tuple[str, ...]:
    """Collect the flags relevant to one loop of a room (report order kept)."""
    flags: list[str] = [flag for flag in room.flags if flag in LOOP_ROOM_FLAGS]
    if (
        loop_index < len(room.loop_flow_status)
        and room.loop_flow_status[loop_index] == "no_flow"
    ):
        flags.append(LOOP_FLAG_NO_FLOW)
    if (
        loop_index < len(room.actuation_test_loops)
        and room.actuation_test_loops[loop_index] == "failed"
    ):
        flags.append(LOOP_FLAG_TEST_FAILED)
    return tuple(dict.fromkeys(flags))


def build_manifold_views(
    manifolds: Sequence[ManifoldConfig],
    rooms: Sequence[RoomLoopSources],
    readings: Mapping[str, float | None],
) -> tuple[ManifoldView, ...]:
    """Resolve every manifold's circuits against the rooms and the readings.

    A circuit whose assigned valve is not wired to any room (the room was
    removed or rewired) renders as a free position. The valve opening shown
    is the actuator's reported position when readable, else the room's
    commanded position — a room that is switched off still shows where its
    valves physically stand.

    Args:
        manifolds: The configured manifolds, in display order.
        rooms: Every room's loop wiring and current valve-related state.
        readings: Current numeric readings keyed by entity id (``None`` =
            unavailable); missing keys read as unavailable too.

    Returns:
        One :class:`ManifoldView` per manifold, positions ascending.
    """
    by_valve: dict[str, tuple[RoomLoopSources, int]] = {}
    for room in rooms:
        for index, valve in enumerate(room.valves):
            if valve and valve not in by_valve:
                by_valve[valve] = (room, index)

    views: list[ManifoldView] = []
    for manifold in manifolds:
        positions: list[ManifoldPositionView] = []
        for position in range(1, manifold.circuits + 1):
            loop = manifold.loop_at(position)
            hit = by_valve.get(loop.entity_valve) if loop is not None else None
            if loop is None or hit is None:
                positions.append(ManifoldPositionView(position=position))
                continue
            room, index = hit
            supply_id = _at(room.supplies, index)
            return_id = _at(room.returns, index)
            feedback = _pct(_reading(readings, loop.entity_valve))
            shown_pct = feedback if feedback is not None else room.valve_command_pct
            supply = _reading(readings, supply_id)
            return_ = _reading(readings, return_id)
            positions.append(
                ManifoldPositionView(
                    position=position,
                    room_name=room.name,
                    loop_index=index,
                    loop_label=loop.label or _default_label(room, index),
                    valve=ValveReading(
                        pct=shown_pct,
                        command_pct=room.valve_command_pct,
                        entity_id=loop.entity_valve,
                    ),
                    supply=EntityReading(value=supply, entity_id=supply_id),
                    return_=EntityReading(value=return_, entity_id=return_id),
                    delta_k=(
                        supply - return_
                        if supply is not None and return_ is not None
                        else None
                    ),
                    flags=_loop_flags(room, index),
                )
            )
        views.append(
            ManifoldView(
                manifold_id=manifold.manifold_id,
                name=manifold.name,
                circuits=manifold.circuits,
                side=manifold.side,
                main_supply=EntityReading(
                    value=_reading(readings, manifold.entity_supply_main),
                    entity_id=manifold.entity_supply_main,
                ),
                main_return=EntityReading(
                    value=_reading(readings, manifold.entity_return_main),
                    entity_id=manifold.entity_return_main,
                ),
                positions=tuple(positions),
            )
        )
    return tuple(views)
