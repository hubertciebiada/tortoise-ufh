"""Property-based invariants of the pure core (Hypothesis).

Example-based suites pin the behaviours someone thought of; these pin the
rules that must hold for EVERY input the adapter can build. Each property
drives a real :class:`RoomController` / :class:`BuildingController` through
random, finite input sequences and checks the contract after every step:

* the valve is always a finite ``0..100 %`` and every output serialises to
  strict JSON (no NaN / inf reaches the websocket);
* ``Mode.OFF`` parks the valve at 0 and keeps the fast source off;
* a lost room sensor holds the last valve in HEATING, parks it at 0 in
  COOLING, and always stops the fast source;
* a cooled floor valve opens only with a known humidity, a known supply and
  that supply strictly above the room dew point (the local S2 layer);
* a fast source never lowers the floor valve (anti priority-inversion);
* the fast source's emitted ON/OFF edges respect min-ON / min-OFF, and a
  direction change always passes through a full min-OFF;
* the PID output stays inside its range; the dew-point helpers are bounded
  and monotonic.

The hard-safety layer (S1 overheat, S3/S4 emergency, S5 watchdog) is a
deliberate override of these rules, so the strategies keep the room air
between the S3 and S4 thresholds, the supply water below S1 and the data age
below S5; the safety rules have their own example-based tests.

Deterministic (``derandomize=True``) and database-free, like the rest of the
unit tier. ``TORTOISE_HYPOTHESIS_EXAMPLES`` (default 60) sets the examples per
property; ``scripts/mutation.py`` lowers it, since every mutant re-runs them.
Skipped when Hypothesis is not installed (the core tiers must run on
numpy/scipy/pytest alone); ``pip install -e ".[dev]"`` brings it in.

Units: temperatures in degC; valve / humidity in percent; ``dt`` in seconds;
dwells in minutes (config). This module never imports ``homeassistant``.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import pytest

pytest.importorskip("hypothesis")

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from custom_components.tortoise_ufh.core.config import ControllerConfig  # noqa: E402
from custom_components.tortoise_ufh.core.controller import (  # noqa: E402
    BuildingController,
    RoomController,
)
from custom_components.tortoise_ufh.core.dew_point import (  # noqa: E402
    cooling_throttle_factor,
    dew_point,
)
from custom_components.tortoise_ufh.core.models import (  # noqa: E402
    FastSourceKind,
    FastSourceMode,
    LoopInput,
    Mode,
    RoomInputs,
    RoomOutputs,
)
from custom_components.tortoise_ufh.core.pid import PIDController  # noqa: E402
from custom_components.tortoise_ufh.core.safety import (  # noqa: E402
    S1_SUPPLY_OFF_C,
    S3_ROOM_OFF_C,
    S4_ROOM_OFF_C,
    S5_WATCHDOG_OFF_MINUTES,
)

pytestmark = pytest.mark.unit

_SETTINGS = settings(
    derandomize=True,
    database=None,
    deadline=None,
    max_examples=int(os.environ.get("TORTOISE_HYPOTHESIS_EXAMPLES", "60")),
)

# -- strategies -------------------------------------------------------------

_ROOM_C = st.floats(S3_ROOM_OFF_C + 0.5, S4_ROOM_OFF_C - 0.5)
_SUPPLY_C = st.floats(8.0, S1_SUPPLY_OFF_C - 0.5)
_HUMIDITY = st.one_of(st.none(), st.floats(1.0, 100.0))
_DT_S = st.floats(2.0, 900.0)
_HVAC = st.sampled_from(["heat", "cool", "dry", "auto", "off", "fan_only"])


@st.composite
def _loops(draw: st.DrawFn) -> tuple[LoopInput, ...]:
    """Zero to three loops with optional feedback / supply / return probes."""
    return tuple(
        LoopInput(
            draw(st.one_of(st.none(), st.floats(0.0, 100.0))),
            draw(st.one_of(st.none(), _SUPPLY_C)),
            draw(st.one_of(st.none(), _SUPPLY_C)),
        )
        for _ in range(draw(st.integers(0, 3)))
    )


@dataclass(frozen=True)
class _Room:
    """The per-sequence constants of one simulated room."""

    mode: Mode
    kind: FastSourceKind
    feedback: bool


@st.composite
def _room(draw: st.DrawFn, *, mode: Mode | None = None) -> _Room:
    """A room: mode, fast-source kind, whether the unit reports feedback."""
    return _Room(
        mode=mode if mode is not None else draw(st.sampled_from(list(Mode))),
        kind=draw(st.sampled_from(list(FastSourceKind))),
        feedback=draw(st.booleans()),
    )


@st.composite
def _inputs(
    draw: st.DrawFn,
    room: _Room,
    *,
    room_c: st.SearchStrategy[float | None] = _ROOM_C,
) -> RoomInputs:
    """One cycle's inputs for ``room`` with everything else drawn freely."""
    return RoomInputs(
        mode=room.mode,
        setpoint_c=draw(st.floats(16.0, 28.0)),
        room_temperature_c=draw(room_c),
        humidity_pct=draw(_HUMIDITY),
        outdoor_temperature_c=draw(st.one_of(st.none(), st.floats(-25.0, 38.0))),
        loops=draw(_loops()),
        fast_source_kind=room.kind,
        fast_source_on=draw(st.booleans()) if room.feedback else None,
        hp_active_for_ufh=draw(st.one_of(st.none(), st.booleans())),
        cooling_enabled=draw(st.booleans()),
        last_update_age_minutes=draw(st.floats(0.0, S5_WATCHDOG_OFF_MINUTES - 0.5)),
        fast_source_hvac_mode=draw(st.one_of(st.none(), _HVAC)),
        humidity_stale_frac=draw(st.floats(0.0, 1.0)),
        fast_source_allowed=draw(st.booleans()),
        circulation_evident=draw(st.one_of(st.none(), st.booleans())),
    )


@st.composite
def _sequence(
    draw: st.DrawFn,
    *,
    mode: Mode | None = None,
    room_c: st.SearchStrategy[float | None] = _ROOM_C,
    max_steps: int = 12,
) -> list[tuple[RoomInputs, float]]:
    """A run of ``(inputs, dt)`` cycles of one room with a fixed mode."""
    room = draw(_room(mode=mode))
    steps = draw(st.integers(1, max_steps))
    return [(draw(_inputs(room, room_c=room_c)), draw(_DT_S)) for _ in range(steps)]


def _run(
    sequence: list[tuple[RoomInputs, float]],
    check: Callable[[RoomInputs, RoomOutputs], None],
    config: ControllerConfig | None = None,
) -> list[RoomOutputs]:
    """Step a fresh controller through ``sequence``, checking every output."""
    controller = RoomController(config or ControllerConfig(), name="room")
    outputs = []
    for inputs, dt in sequence:
        out = controller.step(inputs, dt_seconds=dt)
        check(inputs, out)
        outputs.append(out)
    return outputs


def _valid_output(inputs: RoomInputs, out: RoomOutputs) -> None:
    """Finite 0..100 valve, a coherent command, strict-JSON serialisable."""
    assert math.isfinite(out.valve_position_pct)
    assert 0.0 <= out.valve_position_pct <= 100.0
    cmd = out.fast_source
    if inputs.fast_source_kind is FastSourceKind.NONE:
        assert cmd.on is False
    if cmd.on and inputs.fast_source_kind is FastSourceKind.HEATER:
        assert cmd.mode is FastSourceMode.HEATING
    assert 0.0 <= out.report.dew_throttle_factor <= 1.0
    json.dumps(out.to_dict(), allow_nan=False)


# -- every output honours the external contract -----------------------------


@_SETTINGS
@given(_sequence())
def test_any_sequence_yields_a_valid_output(
    sequence: list[tuple[RoomInputs, float]],
) -> None:
    """Valve, command and report stay inside the contract for any input."""
    _run(sequence, _valid_output)


@_SETTINGS
@given(_sequence(room_c=st.one_of(st.none(), _ROOM_C)))
def test_sensor_dropouts_keep_the_contract(
    sequence: list[tuple[RoomInputs, float]],
) -> None:
    """Intermittent sensor loss never breaks the output contract."""
    _run(sequence, _valid_output)


# -- OFF parks everything; a lost sensor degrades safely, per mode ----------


@_SETTINGS
@given(_sequence(mode=Mode.OFF, room_c=st.one_of(st.none(), _ROOM_C)))
def test_off_parks_the_valve_and_the_fast_source(
    sequence: list[tuple[RoomInputs, float]],
) -> None:
    """Mode.OFF: valve 0 %, fast source off, on every cycle."""

    def check(inputs: RoomInputs, out: RoomOutputs) -> None:
        assert out.valve_position_pct == 0.0
        assert out.fast_source.on is False

    _run(sequence, check)


@_SETTINGS
@given(
    st.sampled_from([Mode.HEATING, Mode.COOLING, Mode.TRANSITIONAL]),
    st.data(),
)
def test_lost_sensor_holds_heating_parks_cooling_stops_fast(
    mode: Mode, data: st.DataObject
) -> None:
    """Sensor lost: HEATING holds the last valve, others park at 0; fast off."""
    healthy = data.draw(_sequence(mode=mode, max_steps=6))
    lost = data.draw(_sequence(mode=mode, room_c=st.none(), max_steps=4))
    controller = RoomController(ControllerConfig(), name="room")
    last = None
    for inputs, dt in healthy:
        last = controller.step(inputs, dt_seconds=dt)
    assert last is not None
    for inputs, dt in lost:
        # The lost-sensor cycles keep the room's kind: rebuild them on it.
        out = controller.step(
            _with(inputs, fast_source_kind=healthy[0][0].fast_source_kind),
            dt_seconds=dt,
        )
        assert out.fast_source.on is False
        assert "sensor_lost" in out.report.flags
        if mode is Mode.HEATING:
            assert out.valve_position_pct == pytest.approx(last.valve_position_pct)
        else:
            assert out.valve_position_pct == 0.0


def _with(inputs: RoomInputs, **changes: Any) -> RoomInputs:
    """``dataclasses.replace`` for a frozen :class:`RoomInputs`."""
    return replace(inputs, **changes)


# -- a cooled floor valve opens only above a KNOWN dew point ----------------


@_SETTINGS
@given(_sequence(mode=Mode.COOLING))
def test_cooling_valve_opens_only_with_supply_above_dew(
    sequence: list[tuple[RoomInputs, float]],
) -> None:
    """Valve > 0 in COOLING needs humidity, a supply probe, supply > dew."""

    def check(inputs: RoomInputs, out: RoomOutputs) -> None:
        if out.valve_position_pct <= 0.0:
            return
        assert inputs.cooling_enabled
        assert inputs.room_temperature_c is not None
        assert inputs.humidity_pct is not None
        assert inputs.humidity_pct > 0.0
        supplies = [
            loop.supply_temperature_c
            for loop in inputs.loops
            if loop.supply_temperature_c is not None
        ]
        assert supplies
        room_dew = dew_point(inputs.room_temperature_c, inputs.humidity_pct)
        assert min(supplies) > room_dew

    _run(sequence, check)


# -- the fast source only ever ADDS to the floor ----------------------------


@_SETTINGS
@given(st.sampled_from([Mode.HEATING, Mode.COOLING, Mode.TRANSITIONAL]), st.data())
def test_a_split_never_lowers_the_valve(mode: Mode, data: st.DataObject) -> None:
    """Same inputs with and without a split: the split's valve is >= ."""
    sequence = data.draw(_sequence(mode=mode))
    plain = RoomController(ControllerConfig(), name="plain")
    split = RoomController(ControllerConfig(), name="split")
    for inputs, dt in sequence:
        base = _with(inputs, fast_source_on=None, fast_source_hvac_mode=None)
        without = plain.step(
            _with(base, fast_source_kind=FastSourceKind.NONE), dt_seconds=dt
        )
        with_split = split.step(
            _with(base, fast_source_kind=FastSourceKind.SPLIT), dt_seconds=dt
        )
        assert with_split.valve_position_pct >= without.valve_position_pct - 1e-9


# -- emitted ON/OFF edges respect min-ON / min-OFF --------------------------


@_SETTINGS
@given(
    st.sampled_from([Mode.HEATING, Mode.COOLING, Mode.TRANSITIONAL]),
    st.sampled_from([FastSourceKind.SPLIT, FastSourceKind.HEATER]),
    st.data(),
)
def test_edges_respect_min_on_and_min_off(
    mode: Mode, kind: FastSourceKind, data: st.DataObject
) -> None:
    """No OFF before min-ON, no ON before min-OFF, direction via full OFF.

    The per-room cooling opt-out is configuration, constant here: flipping
    it (like Mode.OFF or a lost sensor) force-stops the unit on purpose.
    """
    cfg = ControllerConfig(fast_min_on_minutes=20.0, fast_min_off_minutes=15.0)
    controller = RoomController(cfg, name="room")
    steps = data.draw(st.integers(2, 30))
    prev: tuple[bool, FastSourceMode] | None = None
    since_edge_s: float | None = None  # None until the first real edge
    for _ in range(steps):
        room = _Room(mode=mode, kind=kind, feedback=False)
        inputs = _with(
            data.draw(_inputs(room)),
            fast_source_allowed=True,
            fast_source_hvac_mode=None,
            cooling_enabled=True,
        )
        dt = data.draw(_DT_S)
        out = controller.step(inputs, dt_seconds=dt)
        now = (out.fast_source.on, out.fast_source.mode)
        if since_edge_s is not None:
            since_edge_s += dt
        if prev is not None and now[0] != prev[0]:
            if since_edge_s is not None:
                dwell_min = (
                    cfg.fast_min_on_minutes if prev[0] else cfg.fast_min_off_minutes
                )
                assert since_edge_s >= dwell_min * 60.0 - 1e-6
            since_edge_s = 0.0
        if prev is not None and now[0] and prev[0]:
            # Running in both cycles: no direct HEATING <-> COOLING swap.
            heat = {FastSourceMode.HEATING}
            assert (now[1] in heat) == (prev[1] in heat)
        prev = now


# -- the building step: rooms valid, dew floor covers cooled rooms ----------


@_SETTINGS
@given(st.data())
def test_building_step_is_valid_and_dew_floor_covers_cooled_rooms(
    data: st.DataObject,
) -> None:
    """Rooms valid; the safe dew point is >= every fresh cooled room + 2 K."""
    names = ["a", "b", "c"]
    building = BuildingController({n: ControllerConfig() for n in names})
    rooms = {n: data.draw(_room()) for n in names}
    for _ in range(data.draw(st.integers(1, 6))):
        inputs = {n: data.draw(_inputs(rooms[n])) for n in names}
        out = building.step(inputs, dt_seconds=data.draw(_DT_S))
        json.dumps(out.to_dict(), allow_nan=False)
        for n in names:
            _valid_output(inputs[n], out.rooms[n])
        for n in names:
            room_in = inputs[n]
            if (
                room_in.mode is Mode.COOLING
                and room_in.cooling_enabled
                and room_in.room_temperature_c is not None
                and room_in.humidity_pct is not None
                and room_in.humidity_pct > 0.0
                and room_in.humidity_stale_frac == 0.0
            ):
                assert out.global_safe_dew_point_c is not None
                fresh = dew_point(room_in.room_temperature_c, room_in.humidity_pct)
                assert out.global_safe_dew_point_c >= fresh + 2.0 - 1e-9


# -- pure helpers -----------------------------------------------------------


# -- the PID output never leaves [output_min, output_max] -------------------


@_SETTINGS
@given(
    st.floats(0.0, 50.0),
    st.floats(0.0, 0.05),
    st.floats(0.0, 500.0),
    st.floats(1.0, 16.0),
    st.lists(
        st.tuples(st.floats(-20.0, 20.0), _DT_S, st.booleans()),
        min_size=1,
        max_size=40,
    ),
    st.lists(st.floats(-60.0, 60.0), max_size=5),
)
def test_output_is_clamped(
    kp: float,
    ki: float,
    kd: float,
    unwind: float,
    steps: list[tuple[float, float, bool]],
    shifts: list[float],
) -> None:
    """Any gains, errors, dts, freezes and shifts: output in range."""
    pid = PIDController(kp, ki, kd, unwind_factor=unwind)
    for shift in shifts:
        pid.shift_integral(shift)
    for error, dt, freeze in steps:
        out = pid.compute(error, dt_seconds=dt, freeze_integrator=freeze)
        assert 0.0 <= out <= 100.0
        assert math.isfinite(pid.integral)


# -- dew point and throttle factor: bounded and monotonic -------------------


@_SETTINGS
@given(st.floats(-30.0, 45.0), st.floats(0.5, 100.0), st.floats(0.5, 100.0))
def test_dew_point_below_air_and_rising_with_humidity(
    t_air: float, rh_a: float, rh_b: float
) -> None:
    """T_dew <= T_air, and more humidity never lowers the dew point."""
    low, high = sorted((rh_a, rh_b))
    assert dew_point(t_air, high) <= t_air + 1e-9
    assert dew_point(t_air, low) <= dew_point(t_air, high) + 1e-9


@_SETTINGS
@given(
    st.floats(-10.0, 10.0),
    st.floats(-10.0, 10.0),
    st.floats(0.0, 5.0),
    st.floats(0.05, 5.0),
)
def test_throttle_is_bounded_and_monotonic_in_supply(
    gap_a: float, gap_b: float, margin: float, ramp: float
) -> None:
    """0 <= factor <= 1; a warmer supply never throttles harder."""
    low, high = sorted((gap_a, gap_b))
    # A 0 degC dew point keeps the gap exact (no float cancellation).
    f_low = cooling_throttle_factor(low, 0.0, margin=margin, ramp=ramp)
    f_high = cooling_throttle_factor(high, 0.0, margin=margin, ramp=ramp)
    assert 0.0 <= f_low <= f_high <= 1.0
    if high <= 0.0:
        assert f_high == 0.0
    if low >= margin and margin > 0.0:
        assert f_low == 1.0
