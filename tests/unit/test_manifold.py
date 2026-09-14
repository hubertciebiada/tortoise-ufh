"""Unit tests for the manifold view model (``core/manifold.py``).

Covers the storage-form dataclasses (validation, ``to_dict`` / ``from_dict``
round trip, cross-manifold invariants) and the pure view builder: resolving a
circuit's valve to its room loop and probes, the feedback-first valve opening,
the delta-T, the derived labels, free / dangling positions and the loop flag
mapping. No Home Assistant import anywhere.
"""

from __future__ import annotations

import json

import pytest

from custom_components.tortoise_ufh.core.manifold import (
    LOOP_FLAG_NO_FLOW,
    LOOP_FLAG_TEST_FAILED,
    MANIFOLD_CIRCUITS_MAX,
    MANIFOLD_SIDE_LEFT,
    MANIFOLD_SIDE_RIGHT,
    ManifoldConfig,
    ManifoldLoopConfig,
    RoomLoopSources,
    build_manifold_views,
    validate_manifolds,
)

pytestmark = pytest.mark.unit


def _east() -> ManifoldConfig:
    """A 4-circuit manifold with three assigned circuits (position 3 free)."""
    return ManifoldConfig(
        manifold_id="east",
        name="East",
        circuits=4,
        entity_supply_main="sensor.east_z0",
        entity_return_main="sensor.east_p0",
        loops=(
            ManifoldLoopConfig(position=1, entity_valve="valve.corridor"),
            ManifoldLoopConfig(position=2, entity_valve="valve.bedroom_a"),
            ManifoldLoopConfig(
                position=4, entity_valve="valve.bedroom_b", label="Wardrobe"
            ),
        ),
    )


def _rooms() -> tuple[RoomLoopSources, ...]:
    """Two rooms: a single-loop corridor and a two-loop bedroom (live, 40 %)."""
    return (
        RoomLoopSources(
            name="Corridor",
            valves=("valve.corridor",),
            supplies=("sensor.corridor_z",),
            returns=("sensor.corridor_p",),
            valve_command_pct=100.0,
            flags=("valve_mismatch", "sensor_lost"),
        ),
        RoomLoopSources(
            name="Bedroom",
            valves=("valve.bedroom_a", "valve.bedroom_b"),
            supplies=("sensor.bedroom_z1", "sensor.bedroom_z2"),
            returns=("sensor.bedroom_p1", None),
            valve_command_pct=40.0,
            flags=("loop_no_flow",),
            loop_flow_status=("ok", "no_flow"),
            actuation_test_loops=("failed", "untested"),
        ),
    )


def _readings() -> dict[str, float | None]:
    """Readings for the fixture: the corridor valve has no feedback."""
    return {
        "sensor.east_z0": 35.2,
        "sensor.east_p0": 29.9,
        "valve.corridor": None,
        "sensor.corridor_z": 34.8,
        "sensor.corridor_p": 30.1,
        "valve.bedroom_a": 38.0,
        "sensor.bedroom_z1": 34.5,
        "sensor.bedroom_p1": 31.0,
        "valve.bedroom_b": 41.0,
        "sensor.bedroom_z2": 34.4,
    }


# -- Configuration dataclasses ----------------------------------------------


def test_config_round_trips_through_dict() -> None:
    """to_dict() is the storage form and from_dict() rebuilds it exactly."""
    manifold = _east()

    rebuilt = ManifoldConfig.from_dict(json.loads(json.dumps(manifold.to_dict())))

    assert rebuilt == manifold


def test_from_dict_tolerates_optional_keys() -> None:
    """Absent side / probes / loops and empty probe strings take defaults."""
    manifold = ManifoldConfig.from_dict(
        {"manifold_id": "w", "name": "West", "circuits": 6, "entity_supply_main": ""}
    )

    assert manifold.side == MANIFOLD_SIDE_LEFT
    assert manifold.entity_supply_main is None
    assert manifold.entity_return_main is None
    assert manifold.loops == ()


def test_from_dict_requires_identity_keys() -> None:
    """A stored dict without its required keys is rejected, not guessed."""
    with pytest.raises(KeyError):
        ManifoldConfig.from_dict({"name": "West", "circuits": 6})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"manifold_id": " ", "name": "East", "circuits": 4},
        {"manifold_id": "east", "name": "", "circuits": 4},
        {"manifold_id": "east", "name": "East", "circuits": 0},
        {"manifold_id": "east", "name": "East", "circuits": MANIFOLD_CIRCUITS_MAX + 1},
        {"manifold_id": "east", "name": "East", "circuits": 4, "side": "top"},
    ],
)
def test_config_rejects_bad_identity_geometry(kwargs: dict[str, object]) -> None:
    """Empty ids / names, an out-of-range circuit count and unknown sides fail."""
    with pytest.raises(ValueError):
        ManifoldConfig(**kwargs)  # type: ignore[arg-type]


def test_config_rejects_position_beyond_circuits() -> None:
    """An assignment past the last circuit is a configuration error."""
    with pytest.raises(ValueError, match="exceeds"):
        ManifoldConfig(
            manifold_id="east",
            name="East",
            circuits=2,
            loops=(ManifoldLoopConfig(position=3, entity_valve="valve.x"),),
        )


def test_config_rejects_duplicate_position_and_duplicate_valve() -> None:
    """One position holds one loop, and one loop sits on one position."""
    with pytest.raises(ValueError, match="position twice"):
        ManifoldConfig(
            manifold_id="east",
            name="East",
            circuits=2,
            loops=(
                ManifoldLoopConfig(position=1, entity_valve="valve.a"),
                ManifoldLoopConfig(position=1, entity_valve="valve.b"),
            ),
        )
    with pytest.raises(ValueError, match="two positions"):
        ManifoldConfig(
            manifold_id="east",
            name="East",
            circuits=2,
            loops=(
                ManifoldLoopConfig(position=1, entity_valve="valve.a"),
                ManifoldLoopConfig(position=2, entity_valve="valve.a"),
            ),
        )


def test_loop_config_validation() -> None:
    """A position below 1 or an empty valve id is rejected."""
    with pytest.raises(ValueError):
        ManifoldLoopConfig(position=0, entity_valve="valve.a")
    with pytest.raises(ValueError):
        ManifoldLoopConfig(position=1, entity_valve="  ")


def test_validate_manifolds_rejects_shared_ids_and_loops() -> None:
    """Ids are unique across manifolds and a loop lives on one manifold only."""
    east = _east()
    with pytest.raises(ValueError, match="ids must be unique"):
        validate_manifolds([east, east])
    west = ManifoldConfig(
        manifold_id="west",
        name="West",
        circuits=3,
        loops=(ManifoldLoopConfig(position=1, entity_valve="valve.corridor"),),
    )
    with pytest.raises(ValueError, match="assigned on both"):
        validate_manifolds([east, west])
    # Disjoint assignments pass.
    validate_manifolds(
        [
            east,
            ManifoldConfig(
                manifold_id="west",
                name="West",
                circuits=3,
                loops=(ManifoldLoopConfig(position=1, entity_valve="valve.z"),),
            ),
        ]
    )


def test_room_sources_validation() -> None:
    """A room needs a name and a plausible command; n_loops is the longest list."""
    with pytest.raises(ValueError):
        RoomLoopSources(name="")
    with pytest.raises(ValueError):
        RoomLoopSources(name="Salon", valve_command_pct=101.0)
    room = RoomLoopSources(name="Salon", valves=("valve.a",), supplies=("s1", "s2"))
    assert room.n_loops == 2


# -- The view builder ---------------------------------------------------------


def test_build_resolves_valves_to_room_loops_and_probes() -> None:
    """Each circuit finds its room, loop index and that loop's probes."""
    (view,) = build_manifold_views([_east()], _rooms(), _readings())

    assert view.manifold_id == "east"
    assert view.circuits == 4
    assert [p.position for p in view.positions] == [1, 2, 3, 4]

    corridor, bedroom_a, free, bedroom_b = view.positions
    assert corridor.room_name == "Corridor"
    assert corridor.loop_index == 0
    assert corridor.supply.entity_id == "sensor.corridor_z"
    assert corridor.return_.entity_id == "sensor.corridor_p"
    assert bedroom_a.room_name == "Bedroom"
    assert bedroom_a.loop_index == 0
    assert bedroom_b.loop_index == 1
    assert bedroom_b.supply.entity_id == "sensor.bedroom_z2"
    # The bedroom's second loop has no return probe wired.
    assert bedroom_b.return_.entity_id is None
    assert bedroom_b.return_.value is None
    assert not free.assigned


def test_build_valve_opening_prefers_feedback_then_command() -> None:
    """Feedback wins; without feedback the commanded position is shown."""
    (view,) = build_manifold_views([_east()], _rooms(), _readings())
    corridor, bedroom_a, _free, _bedroom_b = view.positions

    # No feedback reading -> the room's command (100 %).
    assert corridor.valve.pct == 100.0
    assert corridor.valve.command_pct == 100.0
    assert corridor.valve.entity_id == "valve.corridor"
    # Feedback available -> the reported 38 %, command kept alongside.
    assert bedroom_a.valve.pct == 38.0
    assert bedroom_a.valve.command_pct == 40.0


def test_build_ignores_implausible_feedback() -> None:
    """A garbage feedback (255 %) falls back to the command instead of showing."""
    readings = {**_readings(), "valve.bedroom_a": 255.0}

    (view,) = build_manifold_views([_east()], _rooms(), readings)

    assert view.positions[1].valve.pct == 40.0


def test_build_delta_t_needs_both_probes() -> None:
    """delta_k = supply - return, None when either probe is unavailable."""
    (view,) = build_manifold_views([_east()], _rooms(), _readings())
    corridor, bedroom_a, _free, bedroom_b = view.positions

    assert corridor.delta_k == pytest.approx(34.8 - 30.1)
    assert bedroom_a.delta_k == pytest.approx(34.5 - 31.0)
    assert bedroom_b.delta_k is None


def test_build_main_probes_and_missing_readings() -> None:
    """Z0 / P0 read from the readings; a missing key reads as unavailable."""
    readings = _readings()
    del readings["sensor.east_p0"]

    (view,) = build_manifold_views([_east()], _rooms(), readings)

    assert view.main_supply.value == 35.2
    assert view.main_supply.entity_id == "sensor.east_z0"
    assert view.main_return.value is None
    assert view.main_return.entity_id == "sensor.east_p0"


def test_build_labels_default_to_room_name_numbered_for_multi_loop() -> None:
    """An explicit label wins; otherwise the room name (numbered when several)."""
    (view,) = build_manifold_views([_east()], _rooms(), _readings())
    corridor, bedroom_a, _free, bedroom_b = view.positions

    assert corridor.loop_label == "Corridor"
    assert bedroom_a.loop_label == "Bedroom 1"
    assert bedroom_b.loop_label == "Wardrobe"


def test_build_free_and_dangling_positions_are_empty() -> None:
    """A free position and a valve no room owns both render as empty."""
    dangling = ManifoldConfig(
        manifold_id="west",
        name="West",
        circuits=2,
        loops=(ManifoldLoopConfig(position=2, entity_valve="valve.gone"),),
    )

    (view,) = build_manifold_views([dangling], _rooms(), _readings())

    for position in view.positions:
        assert position.room_name is None
        assert position.loop_label is None
        assert position.valve.pct is None
        assert position.valve.entity_id is None
        assert position.supply.value is None
        assert position.delta_k is None
        assert position.flags == ()


def test_build_maps_room_and_loop_flags_to_the_circuit() -> None:
    """Valve-side room flags fan out; S6 / self-test verdicts land per loop."""
    (view,) = build_manifold_views([_east()], _rooms(), _readings())
    corridor, bedroom_a, _free, bedroom_b = view.positions

    # valve_mismatch is valve-side; sensor_lost is not a loop flag.
    assert corridor.flags == ("valve_mismatch",)
    # loop_no_flow only on the loop whose status says no_flow (index 1).
    assert LOOP_FLAG_NO_FLOW not in bedroom_a.flags
    assert LOOP_FLAG_NO_FLOW in bedroom_b.flags
    # The failed self-test verdict is on loop index 0 only.
    assert bedroom_a.flags == (LOOP_FLAG_TEST_FAILED,)
    assert LOOP_FLAG_TEST_FAILED not in bedroom_b.flags


def test_build_keeps_manifold_order_and_side() -> None:
    """Manifolds come back in configuration order with their side verbatim."""
    west = ManifoldConfig(
        manifold_id="west", name="West", circuits=1, side=MANIFOLD_SIDE_RIGHT
    )

    views = build_manifold_views([west, _east()], _rooms(), _readings())

    assert [v.name for v in views] == ["West", "East"]
    assert views[0].side == MANIFOLD_SIDE_RIGHT
    assert views[1].side == MANIFOLD_SIDE_LEFT


def test_build_with_no_rooms_or_readings_is_all_free() -> None:
    """No rooms wired yet: every position is free, nothing raises."""
    (view,) = build_manifold_views([_east()], (), {})

    assert all(not p.assigned for p in view.positions)
    assert view.main_supply.value is None


def test_view_to_dict_is_json_serialisable_with_the_contract_keys() -> None:
    """The dict form carries exactly the keys the panel reads."""
    (view,) = build_manifold_views([_east()], _rooms(), _readings())

    payload = json.loads(json.dumps(view.to_dict()))

    assert set(payload) == {"id", "name", "circuits", "side", "main", "positions"}
    assert set(payload["main"]) == {"supply", "return"}
    assert set(payload["main"]["supply"]) == {"value", "entity_id"}
    position = payload["positions"][1]
    assert set(position) == {
        "position",
        "room_name",
        "loop_index",
        "loop_label",
        "valve",
        "supply",
        "return",
        "delta_k",
        "flags",
    }
    assert set(position["valve"]) == {"pct", "command_pct", "entity_id"}
    assert position["flags"] == [LOOP_FLAG_TEST_FAILED]
    assert payload["positions"][2]["room_name"] is None
