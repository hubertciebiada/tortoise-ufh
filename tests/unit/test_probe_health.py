"""Unit tests for the loop water-probe health monitor (issue #16).

Covers :mod:`tortoise_ufh.core.probe_health`: the input dataclasses (validation,
the still-water and circulation witnesses), the still-water window machine
(settle, window, median deviation, baseline admission and spacing, the
drift raise/clear streaks), the flowing supply-vs-main check (persistence,
HOLD when not judged, the main-probe-suspect guard) and the persisted form.

Units: temperatures degC, deviations K, valve percent 0..100, ``dt`` seconds.
This module never imports ``homeassistant``.
"""

from __future__ import annotations

import math

import pytest

from custom_components.tortoise_ufh.core import probe_health as ph
from custom_components.tortoise_ufh.core.probe_health import (
    LoopProbes,
    ManifoldProbes,
    ProbeHealthMonitor,
    ProbeHealthReport,
    ProbeStatus,
)

pytestmark = pytest.mark.unit

_DT_S = 300.0
"""Nominal control cycle [s]."""

_SETTLE_CYCLES = int(ph.STILL_SETTLE_S / _DT_S)
"""Cycles until the water counts as still (36 at 300 s)."""

_WINDOW_CYCLES = int(ph.STILL_WINDOW_S / _DT_S)
"""Cycles per still-water window (12 at 300 s)."""

_SPACING_CYCLES = int(ph.BASELINE_SPACING_S / _DT_S)
"""Cycles between two baseline admissions (144 at 300 s)."""

# Static per-probe offsets in still water (issue #16's measured range): loop
# supplies sit above the manifold median, returns below it.
_OFFSETS: dict[str, float] = {
    "s1": 0.3,
    "r1": -0.2,
    "s2": 0.7,
    "r2": -0.35,
    "s3": 0.1,
    "r3": -0.16,
    "s4": 0.5,
    "r4": 0.0,
}
_BASE_C = 24.0


def _manifold(
    offsets: dict[str, float] | None = None,
    *,
    valve_pct: float | None = 0.0,
    base_c: float = _BASE_C,
    main_supply_c: float | None = None,
    main_return_c: float | None = None,
    learn: bool = True,
    group_id: str = "east",
) -> ManifoldProbes:
    """Build a 4-loop manifold whose probes read ``base + offset``."""
    off = {**_OFFSETS, **(offsets or {})}
    loops = tuple(
        LoopProbes(
            supply_id=f"s{i}",
            return_id=f"r{i}",
            supply_c=base_c + off[f"s{i}"],
            return_c=base_c + off[f"r{i}"],
            valve_pct=valve_pct,
        )
        for i in range(1, 5)
    )
    return ManifoldProbes(
        group_id=group_id,
        loops=loops,
        main_supply_c=main_supply_c,
        main_return_c=main_return_c,
        learn=learn,
    )


def _run(
    monitor: ProbeHealthMonitor,
    group: ManifoldProbes,
    cycles: int,
    *,
    open_threshold_pct: float = 15.0,
) -> ProbeHealthReport:
    """Advance ``monitor`` by ``cycles`` identical cycles; return the last."""
    report = ProbeHealthReport()
    for _ in range(cycles):
        report = monitor.update(
            [group], dt_seconds=_DT_S, open_threshold_pct=open_threshold_pct
        )
    return report


def _learn(monitor: ProbeHealthMonitor) -> ProbeHealthReport:
    """Run still water until every probe holds a ready (3-window) baseline."""
    cycles = _SETTLE_CYCLES + 2 * _SPACING_CYCLES + _WINDOW_CYCLES
    report = _run(monitor, _manifold(), cycles)
    assert all(
        st.baseline_windows == ph.BASELINE_MIN_WINDOWS for st in report.probes.values()
    )
    return report


# ---------------------------------------------------------------------------
# Input dataclasses
# ---------------------------------------------------------------------------


class TestInputs:
    @pytest.mark.parametrize("pct", [-0.1, 100.1])
    def test_valve_out_of_range_rejected(self, pct: float) -> None:
        with pytest.raises(ValueError, match="valve_pct"):
            LoopProbes(valve_pct=pct)

    @pytest.mark.parametrize("pct", [0.0, 100.0])
    def test_valve_bounds_accepted(self, pct: float) -> None:
        assert LoopProbes(valve_pct=pct).valve_pct == pct

    @pytest.mark.parametrize("field", ["supply_id", "return_id"])
    def test_blank_probe_id_rejected(self, field: str) -> None:
        with pytest.raises(
            ValueError, match=r"^probe ids must be non-empty strings or None$"
        ):
            LoopProbes(**{field: "  "})

    def test_blank_group_id_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"^group_id must be a non-empty string$"):
            ManifoldProbes(group_id=" ")

    def test_readings_skip_missing_and_non_finite(self) -> None:
        assert LoopProbes(supply_id="s", supply_c=20.0).readings() == (("s", 20.0),)
        assert LoopProbes(return_id="r", return_c=21.0).readings() == (("r", 21.0),)
        assert LoopProbes(supply_c=20.0, return_c=21.0).readings() == ()
        assert LoopProbes(supply_id="s", return_id="r").readings() == ()
        assert (
            LoopProbes(
                supply_id="s", return_id="r", supply_c=math.nan, return_c=math.inf
            ).readings()
            == ()
        )
        both = LoopProbes(supply_id="s", return_id="r", supply_c=1.0, return_c=2.0)
        assert both.readings() == (("s", 1.0), ("r", 2.0))

    def test_still_needs_every_loop_known_and_closed(self) -> None:
        assert not ManifoldProbes(group_id="g").still
        assert _manifold(valve_pct=ph.STILL_CLOSED_MAX_PCT).still
        assert not _manifold(valve_pct=ph.STILL_CLOSED_MAX_PCT + 0.1).still
        assert not _manifold(valve_pct=None).still
        mixed = ManifoldProbes(
            group_id="g",
            loops=(LoopProbes(valve_pct=0.0), LoopProbes(valve_pct=50.0)),
        )
        assert not mixed.still

    def test_circulating_main_pair(self) -> None:
        none: frozenset[str] = frozenset()
        assert _manifold(main_supply_c=30.0, main_return_c=29.0).circulating(none)
        assert _manifold(main_supply_c=29.0, main_return_c=30.0).circulating(none)
        assert not _manifold(main_supply_c=30.0, main_return_c=29.01).circulating(none)
        assert not _manifold(main_supply_c=math.nan, main_return_c=20.0).circulating(
            none
        )
        assert not _manifold(main_supply_c=30.0).circulating(none)

    def test_circulating_needs_two_loops(self) -> None:
        def loop(i: int, delta: float) -> LoopProbes:
            return LoopProbes(
                supply_id=f"s{i}",
                return_id=f"r{i}",
                supply_c=30.0,
                return_c=30.0 - delta,
            )

        none: frozenset[str] = frozenset()
        one = ManifoldProbes(group_id="g", loops=(loop(1, 1.0), loop(2, 0.5)))
        assert not one.circulating(none)
        two = ManifoldProbes(group_id="g", loops=(loop(1, 1.0), loop(2, -1.0)))
        assert two.circulating(none)
        assert not two.circulating(frozenset({"s1"}))
        assert not two.circulating(frozenset({"r2"}))
        half = ManifoldProbes(
            group_id="g",
            loops=(loop(1, 2.0), loop(2, 2.0), LoopProbes(supply_id="s3")),
        )
        assert half.circulating(none)
        missing = ManifoldProbes(
            group_id="g",
            loops=(
                LoopProbes(supply_id="a", supply_c=30.0, return_c=None),
                LoopProbes(supply_id="b", supply_c=None, return_c=20.0),
                loop(3, 3.0),
            ),
        )
        assert not missing.circulating(none)

    def test_dt_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="dt_seconds"):
            ProbeHealthMonitor().update([], dt_seconds=0.0, open_threshold_pct=15.0)
        # Any positive dt is accepted, including sub-second ones.
        report = ProbeHealthMonitor().update(
            [], dt_seconds=0.5, open_threshold_pct=15.0
        )
        assert report.probes == {}


# ---------------------------------------------------------------------------
# Still-water learning and drift
# ---------------------------------------------------------------------------


class TestStillWater:
    def test_first_window_timing_and_deviation(self) -> None:
        monitor = ProbeHealthMonitor()
        report = _run(monitor, _manifold(), _SETTLE_CYCLES + _WINDOW_CYCLES - 2)
        assert report.probes["s2"].deviation_k is None
        report = _run(monitor, _manifold(), 1)
        st = report.probes["s2"]
        # The median of the 8 offsets is 0.05: s2 = 0.70 - 0.05.
        assert st.deviation_k == pytest.approx(0.65)
        assert st.baseline_windows == 1
        assert st.baseline_k is None
        assert not st.flagged

    def test_baseline_spacing_and_readiness(self) -> None:
        monitor = ProbeHealthMonitor()
        _run(monitor, _manifold(), _SETTLE_CYCLES + _WINDOW_CYCLES - 1)
        # Further windows inside the 12-h spacing are not admitted.
        report = _run(monitor, _manifold(), _SPACING_CYCLES - _WINDOW_CYCLES)
        assert report.probes["s1"].baseline_windows == 1
        report = _run(monitor, _manifold(), _WINDOW_CYCLES)
        assert report.probes["s1"].baseline_windows == 2
        assert report.probes["s1"].baseline_k is None
        report = _run(monitor, _manifold(), _SPACING_CYCLES)
        assert report.probes["s1"].baseline_windows == 3
        assert report.probes["s1"].baseline_k == pytest.approx(0.25)

    def test_baseline_keeps_at_most_max_windows(self) -> None:
        monitor = ProbeHealthMonitor()
        n = ph.BASELINE_MAX_WINDOWS + 2
        report = _run(monitor, _manifold(), _SETTLE_CYCLES + n * _SPACING_CYCLES)
        assert report.probes["r1"].baseline_windows == ph.BASELINE_MAX_WINDOWS

    def test_normal_offsets_never_flag(self) -> None:
        monitor = ProbeHealthMonitor()
        _learn(monitor)
        flagged: set[str] = set()
        for k in range(20 * _WINDOW_CYCLES):
            wobble = 0.3 * math.sin(k / 7.0)
            offsets = {
                pid: off + (wobble if pid[0] == "s" else -wobble)
                for pid, off in _OFFSETS.items()
            }
            report = monitor.update(
                [_manifold(offsets, base_c=_BASE_C + k * 0.001)],
                dt_seconds=_DT_S,
                open_threshold_pct=15.0,
            )
            flagged |= report.flagged
        assert not flagged

    def test_step_raises_after_two_windows_and_clears_after_two(self) -> None:
        monitor = ProbeHealthMonitor()
        _learn(monitor)
        slipped = _manifold({"s2": 5.7})
        report = _run(monitor, slipped, _WINDOW_CYCLES)
        assert not report.probes["s2"].flagged
        report = _run(monitor, slipped, _WINDOW_CYCLES)
        st = report.probes["s2"]
        assert st.flagged
        assert st.reasons == ("still_water_drift",)
        assert report.flagged == frozenset({"s2"})
        assert monitor.flagged() == frozenset({"s2"})
        # The drifted level never enters the baseline.
        report = _run(monitor, slipped, 2 * _SPACING_CYCLES)
        assert report.probes["s2"].baseline_k == pytest.approx(0.65)
        assert report.probes["s2"].flagged
        # Re-seated: one healthy window is not enough, the second clears it.
        report = _run(monitor, _manifold(), _WINDOW_CYCLES)
        assert report.probes["s2"].flagged
        report = _run(monitor, _manifold(), _WINDOW_CYCLES)
        assert not report.probes["s2"].flagged
        assert report.probes["s2"].reasons == ()

    @pytest.mark.parametrize(("deviation", "drift"), [(1.5, False), (1.5001, True)])
    def test_drift_is_strictly_above_threshold(
        self, deviation: float, drift: bool
    ) -> None:
        monitor = ProbeHealthMonitor()
        monitor._probes["x"] = ph._ProbeState(baseline=[0.0, 0.0, 0.0])
        monitor._close_window("x", deviation)
        monitor._close_window("x", -deviation)
        assert (monitor.flagged() == frozenset({"x"})) is drift

    def test_settle_and_window_restart_exactly(self) -> None:
        # Sub-cycle dt lands exactly on the settle / window boundaries.
        monitor = ProbeHealthMonitor()
        group = _manifold()
        monitor.update(
            [group], dt_seconds=ph.STILL_SETTLE_S - 1.0, open_threshold_pct=15.0
        )
        monitor.update(
            [_manifold(valve_pct=50.0)], dt_seconds=1.0, open_threshold_pct=15.0
        )
        # A fresh still stretch must settle the full time again.
        monitor.update(
            [group], dt_seconds=ph.STILL_SETTLE_S - 0.5, open_threshold_pct=15.0
        )
        for _ in range(ph.MIN_WINDOW_SAMPLES - 1):
            report = monitor.update(
                [group], dt_seconds=ph.STILL_WINDOW_S / 3, open_threshold_pct=15.0
            )
        assert report.probes["s1"].deviation_k is None
        report = monitor.update(
            [group], dt_seconds=ph.STILL_WINDOW_S / 3, open_threshold_pct=15.0
        )
        assert report.probes["s1"].deviation_k is not None
        # The next window starts from zero: three more thirds close it, two
        # do not.
        monitor.update(
            [_manifold({"s1": 1.3})],
            dt_seconds=ph.STILL_WINDOW_S / 3,
            open_threshold_pct=15.0,
        )
        monitor.update(
            [_manifold({"s1": 1.3})],
            dt_seconds=ph.STILL_WINDOW_S / 3,
            open_threshold_pct=15.0,
        )
        report = monitor.update(
            [_manifold({"s1": 1.3})],
            dt_seconds=ph.STILL_WINDOW_S / 3 - 1.0,
            open_threshold_pct=15.0,
        )
        assert report.probes["s1"].deviation_k == pytest.approx(0.25)
        report = monitor.update(
            [_manifold({"s1": 1.3})], dt_seconds=1.0, open_threshold_pct=15.0
        )
        assert report.probes["s1"].deviation_k == pytest.approx(1.25)

    def test_single_drifting_window_resets(self) -> None:
        monitor = ProbeHealthMonitor()
        _learn(monitor)
        _run(monitor, _manifold({"s2": 5.7}), _WINDOW_CYCLES)
        _run(monitor, _manifold(), _WINDOW_CYCLES)
        report = _run(monitor, _manifold({"s2": 5.7}), _WINDOW_CYCLES)
        assert not report.probes["s2"].flagged

    @pytest.mark.parametrize(("shift", "flagged"), [(1.45, False), (1.6, True)])
    def test_drift_threshold(self, shift: float, flagged: bool) -> None:
        monitor = ProbeHealthMonitor()
        _learn(monitor)
        report = _run(monitor, _manifold({"r3": -0.16 - shift}), 2 * _WINDOW_CYCLES)
        assert report.probes["r3"].flagged is flagged

    def test_learning_needs_ready_baseline_before_flagging(self) -> None:
        monitor = ProbeHealthMonitor()
        report = _run(
            monitor, _manifold({"s2": 5.7}), _SETTLE_CYCLES + 6 * _WINDOW_CYCLES
        )
        assert not report.flagged

    def test_open_valve_resets_settle_and_window(self) -> None:
        monitor = ProbeHealthMonitor()
        _run(monitor, _manifold(), _SETTLE_CYCLES + _WINDOW_CYCLES - 2)
        _run(monitor, _manifold(valve_pct=50.0), 1)
        report = _run(monitor, _manifold(), _SETTLE_CYCLES + _WINDOW_CYCLES - 2)
        assert report.probes["s1"].deviation_k is None
        report = _run(monitor, _manifold(), 1)
        assert report.probes["s1"].deviation_k is not None

    def test_dropped_group_restarts_settle(self) -> None:
        monitor = ProbeHealthMonitor()
        _run(monitor, _manifold(), _SETTLE_CYCLES + _WINDOW_CYCLES - 2)
        monitor.update([], dt_seconds=_DT_S, open_threshold_pct=15.0)
        report = _run(monitor, _manifold(), _WINDOW_CYCLES)
        assert report.probes["s1"].deviation_k is None

    def test_too_few_probes_take_no_samples(self) -> None:
        group = ManifoldProbes(
            group_id="g",
            loops=(
                LoopProbes(
                    supply_id="a",
                    return_id="b",
                    supply_c=20.0,
                    return_c=21.0,
                    valve_pct=0.0,
                ),
                LoopProbes(supply_id="c", valve_pct=0.0),
            ),
        )
        report = _run(ProbeHealthMonitor(), group, _SETTLE_CYCLES + 3 * _WINDOW_CYCLES)
        assert report.probes["a"].deviation_k is None
        three = ManifoldProbes(
            group_id="g",
            loops=(
                *group.loops[:1],
                LoopProbes(supply_id="c", supply_c=22.0, valve_pct=0.0),
            ),
        )
        report = _run(ProbeHealthMonitor(), three, _SETTLE_CYCLES + _WINDOW_CYCLES)
        assert report.probes["a"].deviation_k == pytest.approx(-1.0)

    def test_probe_needs_enough_samples_in_window(self) -> None:
        monitor = ProbeHealthMonitor()
        _run(monitor, _manifold(), _SETTLE_CYCLES - 1)
        gone = _manifold()
        gone = ManifoldProbes(
            group_id="east",
            loops=(
                LoopProbes(supply_id="s1", return_id="r1", valve_pct=0.0),
                *gone.loops[1:],
            ),
        )
        n = _WINDOW_CYCLES - ph.MIN_WINDOW_SAMPLES + 1
        _run(monitor, gone, n)
        report = _run(monitor, _manifold(), ph.MIN_WINDOW_SAMPLES - 1)
        assert report.probes["s1"].deviation_k is None
        assert report.probes["s2"].deviation_k is not None
        monitor = ProbeHealthMonitor()
        _run(monitor, _manifold(), _SETTLE_CYCLES - 1)
        _run(monitor, gone, n - 1)
        report = _run(monitor, _manifold(), ph.MIN_WINDOW_SAMPLES)
        assert report.probes["s1"].deviation_k is not None

    def test_unassigned_group_never_learns(self) -> None:
        report = _run(
            ProbeHealthMonitor(),
            _manifold(learn=False),
            _SETTLE_CYCLES + 2 * _WINDOW_CYCLES,
        )
        assert all(st.deviation_k is None for st in report.probes.values())


# ---------------------------------------------------------------------------
# Flowing supply-vs-main check
# ---------------------------------------------------------------------------


def _flowing(
    s1_c: float = 30.0,
    *,
    main_c: float | None = 30.0,
    circulating: bool = True,
    valve_pct: float = 50.0,
    others_c: float = 30.0,
) -> ManifoldProbes:
    """Three open loops; ``s1`` is the probe under test."""
    ret = 26.0 if circulating else others_c
    supplies = (s1_c, others_c, others_c)
    return ManifoldProbes(
        group_id="g",
        loops=tuple(
            LoopProbes(
                supply_id=f"s{i + 1}",
                return_id=f"r{i + 1}",
                supply_c=supplies[i],
                return_c=ret,
                valve_pct=valve_pct,
            )
            for i in range(3)
        ),
        main_supply_c=main_c,
        learn=False,
    )


_PERSIST_CYCLES = int(ph.FLOW_PERSIST_S / _DT_S)


class TestFlowing:
    def test_gap_flags_after_persistence_and_clears(self) -> None:
        monitor = ProbeHealthMonitor()
        report = _run(monitor, _flowing(34.0), _PERSIST_CYCLES - 1)
        assert not report.flagged
        report = _run(monitor, _flowing(34.0), 1)
        assert report.probes["s1"].reasons == ("supply_vs_main",)
        assert report.flagged == frozenset({"s1"})
        report = _run(monitor, _flowing(30.5), _PERSIST_CYCLES - 1)
        assert report.probes["s1"].flagged
        report = _run(monitor, _flowing(30.5), 1)
        assert not report.flagged

    def test_colder_gap_flags_too(self) -> None:
        report = _run(ProbeHealthMonitor(), _flowing(26.5), _PERSIST_CYCLES)
        assert report.probes["s1"].flagged

    def test_gap_at_limit_is_healthy(self) -> None:
        report = _run(
            ProbeHealthMonitor(),
            _flowing(30.0 + ph.FLOW_SUPPLY_MAX_K),
            3 * _PERSIST_CYCLES,
        )
        assert not report.flagged

    def test_one_healthy_sample_restarts_the_window(self) -> None:
        monitor = ProbeHealthMonitor()
        _run(monitor, _flowing(34.0), _PERSIST_CYCLES - 1)
        _run(monitor, _flowing(30.0), 1)
        report = _run(monitor, _flowing(34.0), _PERSIST_CYCLES - 1)
        assert not report.flagged

    @pytest.mark.parametrize(
        "group",
        [
            _flowing(34.0, circulating=False),
            _flowing(34.0, main_c=None),
            _flowing(34.0, valve_pct=14.9),
        ],
    )
    def test_not_judged_holds(self, group: ManifoldProbes) -> None:
        monitor = ProbeHealthMonitor()
        _run(monitor, _flowing(34.0), _PERSIST_CYCLES - 1)
        report = _run(monitor, group, 3 * _PERSIST_CYCLES)
        assert not report.flagged
        report = _run(monitor, _flowing(34.0), 1)
        assert report.probes["s1"].flagged

    def test_open_threshold_inclusive(self) -> None:
        report = _run(
            ProbeHealthMonitor(), _flowing(34.0, valve_pct=15.0), _PERSIST_CYCLES
        )
        assert report.probes["s1"].flagged

    def test_main_probe_suspect_holds(self) -> None:
        # Two of three open loops disagree with the main probe: the main
        # probe is the suspect, nobody is flagged.
        group = _flowing(34.0, others_c=34.0)
        assert not _run(ProbeHealthMonitor(), group, 3 * _PERSIST_CYCLES).flagged

    def test_two_of_two_disagreeing_is_main_suspect(self) -> None:
        base = _flowing(34.0, others_c=34.0)
        group = ManifoldProbes(
            group_id="g",
            loops=base.loops[:2],
            main_supply_c=30.0,
            main_return_c=26.0,
            learn=False,
        )
        assert not _run(ProbeHealthMonitor(), group, 3 * _PERSIST_CYCLES).flagged

    def test_unjudged_loop_does_not_stop_the_scan(self) -> None:
        base = _flowing(34.0)
        group = ManifoldProbes(
            group_id="g",
            loops=(
                LoopProbes(supply_id="closed", supply_c=30.0, valve_pct=0.0),
                *base.loops,
            ),
            main_supply_c=30.0,
            learn=False,
        )
        report = _run(ProbeHealthMonitor(), group, _PERSIST_CYCLES)
        assert report.flagged == frozenset({"s1"})

    def test_one_of_two_still_judged(self) -> None:
        base = _flowing(34.0)
        group = ManifoldProbes(
            group_id="g",
            loops=base.loops[:2],
            main_supply_c=30.0,
            main_return_c=26.0,
            learn=False,
        )
        report = _run(ProbeHealthMonitor(), group, _PERSIST_CYCLES)
        assert report.flagged == frozenset({"s1"})

    def test_flagged_probe_no_longer_proves_circulation(self) -> None:
        # Only s1/r1 and s2/r2 carry a delta-T; once s1 is flagged the group
        # no longer circulates, so the window for s1 holds (stays flagged).
        loops = (
            LoopProbes(
                supply_id="s1",
                return_id="r1",
                supply_c=34.0,
                return_c=28.0,
                valve_pct=50.0,
            ),
            LoopProbes(
                supply_id="s2",
                return_id="r2",
                supply_c=29.0,
                return_c=28.0,
                valve_pct=50.0,
            ),
            LoopProbes(
                supply_id="s3",
                return_id="r3",
                supply_c=28.5,
                return_c=28.4,
                valve_pct=50.0,
            ),
        )
        group = ManifoldProbes(
            group_id="g", loops=loops, main_supply_c=29.0, learn=False
        )
        monitor = ProbeHealthMonitor()
        assert _run(monitor, group, _PERSIST_CYCLES).flagged == frozenset({"s1"})
        healed = ManifoldProbes(
            group_id="g",
            loops=(
                LoopProbes(
                    supply_id="s1",
                    return_id="r1",
                    supply_c=29.0,
                    return_c=28.0,
                    valve_pct=50.0,
                ),
                *loops[1:],
            ),
            main_supply_c=29.0,
            learn=False,
        )
        assert _run(monitor, healed, 3 * _PERSIST_CYCLES).flagged == frozenset({"s1"})


# ---------------------------------------------------------------------------
# Persistence, retain, reset, report
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_round_trip_keeps_baseline_and_flag(self) -> None:
        monitor = ProbeHealthMonitor()
        _learn(monitor)
        _run(monitor, _manifold({"s2": 5.7}), 2 * _WINDOW_CYCLES)
        raw = monitor.to_dict()
        restored = ProbeHealthMonitor.from_dict(raw)
        assert restored.to_dict() == raw
        assert restored.flagged() == frozenset({"s2"})
        report = restored.update(
            [_manifold({"s2": 5.7})], dt_seconds=_DT_S, open_threshold_pct=15.0
        )
        assert report.probes["s2"].flagged
        assert report.probes["s2"].baseline_k == pytest.approx(0.65)
        assert raw["clock_s"] > 0
        # The spacing clock survives too: no admission right after restore.
        assert restored.to_dict()["clock_s"] == pytest.approx(raw["clock_s"] + _DT_S)

    def test_from_dict_tolerates_garbage(self) -> None:
        restored = ProbeHealthMonitor.from_dict(
            {
                "clock_s": "x",
                "probes": {
                    "nan": {"baseline": [math.nan]},
                    "bad": {"baseline": ["z"]},
                    "notmap": 5,
                    "empty": {"baseline": None},
                    "ok": {"baseline": [0.1, 0.2, 0.3], "drift": True},
                },
            }
        )
        data = restored.to_dict()
        assert data["clock_s"] == 0.0
        assert set(data["probes"]) == {"ok", "empty"}
        assert data["probes"]["empty"]["baseline"] == []
        assert data["probes"]["empty"]["drift"] is False
        ok = data["probes"]["ok"]
        assert ok["drift"] is True
        assert ok["drift_streak"] == 0
        assert ok["ok_streak"] == 0
        assert ok["last_admit_s"] is None
        assert ok["last_deviation_k"] is None
        assert ProbeHealthMonitor.from_dict({"clock_s": math.inf}).to_dict() == {
            "clock_s": 0.0,
            "probes": {},
        }
        assert ProbeHealthMonitor.from_dict({"probes": []}).to_dict()["probes"] == {}
        assert ProbeHealthMonitor.from_dict({}).to_dict() == {
            "clock_s": 0.0,
            "probes": {},
        }

    def test_from_dict_truncates_and_parses(self) -> None:
        restored = ProbeHealthMonitor.from_dict(
            {
                "clock_s": "12.5",
                "probes": {
                    "p": {
                        "baseline": list(range(20)),
                        "last_admit_s": "3",
                        "last_deviation_k": "0.4",
                        "drift_streak": "1",
                        "ok_streak": 2,
                    }
                },
            }
        )
        data = restored.to_dict()
        assert data["clock_s"] == 12.5
        p = data["probes"]["p"]
        assert p["baseline"] == [float(v) for v in range(6, 20)]
        assert p["last_admit_s"] == 3.0
        assert p["last_deviation_k"] == 0.4
        assert p["drift_streak"] == 1
        assert p["ok_streak"] == 2
        assert p["drift"] is False

    def test_state_rejects_non_finite_baseline(self) -> None:
        with pytest.raises(ValueError, match=r"^baseline values must be finite"):
            ph._ProbeState.from_dict({"baseline": [0.1, math.inf]})
        assert ph._ProbeState.from_dict({}).baseline == []

    def test_retain_and_reset(self) -> None:
        monitor = ProbeHealthMonitor()
        _learn(monitor)
        _run(monitor, _manifold({"s2": 5.7}), 2 * _WINDOW_CYCLES)
        monitor.retain(frozenset(_OFFSETS) - {"r4"})
        assert "r4" not in monitor.to_dict()["probes"]
        assert "s1" in monitor.to_dict()["probes"]
        monitor.reset_baselines(frozenset({"s2", "unknown"}))
        assert monitor.flagged() == frozenset()
        assert monitor.to_dict()["probes"]["s2"]["baseline"] == []
        assert monitor.to_dict()["probes"]["s1"]["baseline"] != []
        monitor.reset_baselines()
        assert all(p["baseline"] == [] for p in monitor.to_dict()["probes"].values())
        # The reset also restarts the still-water settle.
        report = _run(monitor, _manifold(), _WINDOW_CYCLES)
        assert report.probes["s1"].deviation_k is None

    def test_report_serialisation(self) -> None:
        status = ProbeStatus(
            probe_id="s1",
            baseline_k=0.2,
            baseline_windows=3,
            deviation_k=0.3,
            flagged=True,
            reasons=("still_water_drift",),
        )
        assert status.to_dict() == {
            "probe_id": "s1",
            "baseline_k": 0.2,
            "baseline_windows": 3,
            "deviation_k": 0.3,
            "flagged": True,
            "reasons": ["still_water_drift"],
        }
        report = ProbeHealthReport(probes={"s1": status})
        assert report.to_dict() == {"s1": status.to_dict()}
        assert report.flagged == frozenset({"s1"})
        assert ProbeHealthReport().flagged == frozenset()

    def test_report_lists_only_probes_seen_this_cycle(self) -> None:
        monitor = ProbeHealthMonitor()
        monitor.update([_manifold()], dt_seconds=_DT_S, open_threshold_pct=15.0)
        report = monitor.update(
            [ManifoldProbes(group_id="x", loops=(LoopProbes(supply_id="only"),))],
            dt_seconds=_DT_S,
            open_threshold_pct=15.0,
        )
        assert set(report.probes) == {"only"}
        assert report.probes["only"] == ProbeStatus(
            probe_id="only",
            baseline_k=None,
            baseline_windows=0,
            deviation_k=None,
            flagged=False,
        )
