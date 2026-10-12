"""Regression: false S6 ``loop_no_flow`` on a short, high-flow loop.

Replays the owner's Home Assistant history of 2026-10-11/12 (v0.22.1, room
"Toaleta", west manifold loop 1): 15-min samples of the commanded valve and
the loop's supply/return probes, interpolated onto the nominal 5-min control
cycle. The loop is short, so while the heat pump runs its supply-return
difference is only ~0.5 K (the probes also carry ~1 K of still-water offset),
and the room overrides ``flow_epsilon_k`` to 1.0 K — a leftover of the removed
stuck-open detector. v0.22.1 latched ``loop_no_flow`` on this healthy loop
during a steady compressor run and kept it latched all night and through the
next morning's re-heat (the references stayed at the pre-pause peak), which
froze the room's integrator while the room sat 1 K below setpoint.

The circulation gate is reconstructed the way ``BuildingController`` builds
it: ``True`` when any loop on the manifold (the bar pair, the other rooms'
loops or this loop) shows ``|supply - return| >= 1.0 K``.

Units: degC, %, seconds. This module never imports ``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.flow_watchdog import (
    CIRCULATION_DELTA_K,
    LoopFlowMonitor,
)
from custom_components.tortoise_ufh.core.models import Mode

_DT_S = 300.0
"""Nominal control cycle [s]."""

_WINDOW_S = 55.0 * 60.0
"""The owner's ``flow_response_window_min`` (55) [s]."""

# (valve %, Z1 supply, P1 return, bar Z0-P0, max other west-loop delta-T)
# every 15 min from 2026-10-11 21:00 to 2026-10-12 11:45 local time.
_HISTORY: tuple[tuple[float, float, float, float, float], ...] = (
    (0, 23.8, 22.8, 1.0, 0.6),
    (0, 23.8, 22.8, 1.0, 0.6),
    (0, 23.8, 22.8, 1.0, 0.6),
    (15, 26.0, 24.9, 2.1, 3.4),
    (23, 23.9, 23.5, 0.9, 1.3),
    (36, 22.7, 22.4, 0.4, 1.0),
    (49, 24.5, 23.9, 1.7, 2.5),
    (68, 25.3, 24.7, 1.8, 2.4),
    (68, 25.3, 24.8, 1.7, 2.3),
    (75, 25.5, 24.9, 1.7, 2.4),
    (97, 25.5, 25.0, 1.7, 2.2),
    (100, 25.6, 25.1, 1.6, 2.3),
    (100, 25.7, 25.1, 1.7, 2.3),
    (100, 25.8, 25.3, 1.7, 2.4),
    (72, 25.9, 25.4, 1.7, 2.3),
    (89, 26.0, 25.5, 1.7, 2.4),
    (100, 25.1, 24.8, 1.0, 1.4),
    (92, 24.1, 23.8, 0.5, 0.6),
    (95, 23.7, 23.4, 0.5, 0.5),
    (86, 23.5, 23.2, 0.5, 0.2),
    (91, 23.4, 23.1, 0.4, 0.2),
    (98, 23.3, 23.0, 0.4, 0.2),
    (98, 23.3, 22.9, 0.4, 0.1),
    (98, 23.2, 22.9, 0.3, 0.1),
    (100, 23.2, 22.8, 0.4, 0.1),
    (100, 23.1, 22.8, 0.3, 0.1),
    (100, 23.1, 22.7, 0.4, 0.1),
    (100, 23.0, 22.7, 0.4, 0.0),
    (100, 23.0, 22.6, 0.4, 0.1),
    (90, 22.9, 22.6, 0.4, 0.1),
    (100, 22.9, 22.6, 0.3, 0.0),
    (90, 22.9, 22.5, 0.3, 0.1),
    (100, 22.8, 22.5, 0.4, 0.1),
    (100, 22.8, 22.4, 0.3, 0.1),
    (100, 22.8, 22.4, 0.3, 0.1),
    (91, 22.8, 22.4, 0.4, 0.0),
    (85, 22.7, 22.4, 0.3, 0.0),
    (75, 22.7, 22.3, 0.3, 0.0),
    (80, 22.7, 22.3, 0.4, 0.1),
    (73, 22.6, 22.3, 0.4, 0.1),
    (88, 22.6, 22.3, 0.3, 0.0),
    (88, 22.6, 22.3, 0.3, 0.1),
    (88, 22.9, 22.4, 0.7, 0.4),
    (92, 24.9, 24.4, 1.7, 2.1),
    (92, 25.2, 24.7, 1.6, 2.1),
    (92, 25.4, 24.9, 1.6, 2.2),
    (92, 25.6, 25.1, 1.7, 2.3),
    (92, 25.8, 25.2, 1.7, 2.3),
    (92, 25.2, 24.8, 1.1, 1.7),
    (96, 23.9, 23.6, 0.6, 0.6),
    (89, 23.5, 23.3, 0.5, 0.5),
    (91, 23.3, 23.0, 0.4, 0.3),
    (91, 23.2, 22.9, 0.3, 0.3),
    (91, 23.1, 22.8, 0.4, 0.2),
    (92, 23.1, 22.8, 0.5, 0.1),
    (92, 23.2, 22.7, 0.4, 0.1),
    (89, 23.2, 22.7, 0.5, 0.1),
    (88, 23.3, 23.0, 0.5, 0.2),
    (96, 23.1, 22.8, 0.4, 0.2),
    (96, 23.0, 22.6, 0.4, 0.2),
)

_SUB_STEPS = 3
"""5-min cycles per 15-min history sample."""


def _cycles(
    history: tuple[tuple[float, float, float, float, float], ...],
) -> list[tuple[float, float, float, bool]]:
    """Interpolate *history* onto 5-min cycles.

    Returns:
        ``(commanded %, supply, return, circulation_evident)`` per cycle.
    """
    out: list[tuple[float, float, float, bool]] = []
    for (v0, s0, r0, b0, o0), (_v1, s1, r1, b1, o1) in zip(
        history, history[1:], strict=False
    ):
        for k in range(_SUB_STEPS):
            f = k / _SUB_STEPS
            supply = s0 + f * (s1 - s0)
            ret = r0 + f * (r1 - r0)
            witness = max(b0 + f * (b1 - b0), o0 + f * (o1 - o0), abs(supply - ret))
            out.append((v0, supply, ret, witness >= CIRCULATION_DELTA_K - 1e-9))
    return out


def _replay(
    cycles: list[tuple[float, float, float, bool]], *, epsilon_k: float
) -> list[bool]:
    """Feed *cycles* to one monitor; return the latched flag per cycle."""
    monitor = LoopFlowMonitor()
    commanded: float | None = None
    latched: list[bool] = []
    for valve_pct, supply, ret, circulation in cycles:
        monitor.update(
            mode=Mode.HEATING,
            commanded_pct=commanded,
            supply_c=supply,
            return_c=ret,
            circulation_evident=circulation,
            dt_seconds=_DT_S,
            epsilon_k=epsilon_k,
            open_threshold_pct=15.0,
            window_s=_WINDOW_S,
        )
        latched.append(monitor.no_flow_active)
        commanded = valve_pct  # the core's emission feeds the NEXT cycle
    return latched


@pytest.mark.unit
@pytest.mark.parametrize("epsilon_k", [1.0, 0.4, 0.3])
def test_healthy_short_loop_never_flags(epsilon_k: float) -> None:
    """The owner's healthy short loop stays clear for the whole night,
    both with the leftover 1.0 K room override and the global 0.4 K."""
    latched = _replay(_cycles(_HISTORY), epsilon_k=epsilon_k)
    assert not any(latched), f"false loop_no_flow at cycle {latched.index(True)}"


@pytest.mark.unit
def test_same_night_with_a_dead_loop_still_flags() -> None:
    """Same valve commands and circulation, but the loop never receives
    water: its probes just cool from the opening state toward the screed.
    The watchdog must still latch within one window of circulation."""
    cycles = _cycles(_HISTORY)
    dead: list[tuple[float, float, float, bool]] = []
    supply, ret = 23.8, 22.8
    for valve_pct, _s, _r, circulation in cycles:
        supply += (22.5 - supply) * 0.05
        ret += (22.3 - ret) * 0.05
        dead.append((valve_pct, supply, ret, circulation))
    latched = _replay(dead, epsilon_k=1.0)
    assert any(latched)
    # Opened at 21:45 (cycle 9); circulation is proven from ~22:20 onwards
    # continuously until ~01:00, so the flag lands well before midnight.
    assert latched.index(True) <= 9 + 3 * 9


@pytest.mark.unit
def test_rewarm_after_a_compressor_pause_counts_from_the_trough() -> None:
    """Heating: after a pause the references ratchet down to the coldest
    point, so re-warming by >= 0.5 K is evidence even below the old peak."""
    monitor = LoopFlowMonitor()
    kwargs = {
        "mode": Mode.HEATING,
        "commanded_pct": 90.0,
        "dt_seconds": _DT_S,
        "epsilon_k": 1.0,
        "open_threshold_pct": 15.0,
        "window_s": 3 * _DT_S,
    }
    # Valve just opened: a through-flow sample (1.1 K) proves flow.
    monitor.update(supply_c=26.0, return_c=24.9, circulation_evident=True, **kwargs)
    # Pause: water cools (no circulation proof), the loop departs and arms.
    for temp in (25.0, 24.0, 23.0):
        monitor.update(
            supply_c=temp, return_c=temp - 0.3, circulation_evident=False, **kwargs
        )
    # Restart: two cycles of circulation without a rise yet...
    for _ in range(2):
        monitor.update(supply_c=23.0, return_c=22.7, circulation_evident=True, **kwargs)
    assert monitor.no_flow_active is False
    # ...then +0.6 K from the trough (still 2.4 K below the old peak).
    monitor.update(supply_c=23.6, return_c=23.3, circulation_evident=True, **kwargs)
    for _ in range(5):
        monitor.update(supply_c=23.6, return_c=23.3, circulation_evident=True, **kwargs)
    assert monitor.no_flow_active is False


def _step(
    monitor: LoopFlowMonitor,
    supply_c: float,
    return_c: float,
    *,
    mode: Mode = Mode.HEATING,
    epsilon_k: float = 0.3,
) -> None:
    """One circulating, commanded-open cycle with a 3-cycle window."""
    monitor.update(
        mode=mode,
        commanded_pct=80.0,
        supply_c=supply_c,
        return_c=return_c,
        circulation_evident=True,
        dt_seconds=_DT_S,
        epsilon_k=epsilon_k,
        open_threshold_pct=15.0,
        window_s=3 * _DT_S,
    )


class TestArming:
    """After evidence a steady loop does not accumulate; departing re-arms."""

    @pytest.mark.unit
    @pytest.mark.parametrize("mode", [Mode.HEATING, Mode.COOLING])
    def test_steady_loop_after_evidence_never_latches(self, mode: Mode) -> None:
        """A loop that proved flow and then sits still stays clear."""
        monitor = LoopFlowMonitor()
        _step(monitor, 25.0, 25.0, mode=mode)
        sign = 1.0 if mode is Mode.HEATING else -1.0
        _step(monitor, 25.0 + sign * 0.6, 25.0 + sign * 0.6, mode=mode)  # evidence
        for _ in range(20):
            _step(monitor, 25.0 + sign * 0.6, 25.0 + sign * 0.6, mode=mode)
        assert monitor.no_flow_active is False
        assert monitor.status == "ok"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("mode", "supply_c", "return_c", "armed"),
        [
            # Anchor 26.0 / 26.0 (heating: away from the source = colder).
            (Mode.HEATING, 25.5, 26.0, True),  # supply exactly -0.5 K
            (Mode.HEATING, 26.0, 25.5, True),  # return exactly -0.5 K
            (Mode.HEATING, 25.6, 25.6, False),  # -0.4 K on both
            (Mode.HEATING, 26.2, 26.2, False),  # toward the source
            # Anchor 24.0 / 24.0 (cooling: away from the source = warmer).
            (Mode.COOLING, 24.5, 24.0, True),
            (Mode.COOLING, 24.0, 24.5, True),
            (Mode.COOLING, 24.4, 24.4, False),
            (Mode.COOLING, 23.8, 23.8, False),
        ],
    )
    def test_departure_away_from_the_source_rearms(
        self, mode: Mode, supply_c: float, return_c: float, armed: bool
    ) -> None:
        """Departure >= 0.5 K away from the evidence sample re-arms."""
        monitor = LoopFlowMonitor()
        base = 25.5 if mode is Mode.HEATING else 24.5
        anchor = 26.0 if mode is Mode.HEATING else 24.0
        _step(monitor, base, base, mode=mode)
        _step(monitor, anchor, anchor, mode=mode)  # displacement evidence
        for _ in range(3):
            _step(monitor, supply_c, return_c, mode=mode, epsilon_k=0.6)
        assert monitor.no_flow_active is armed

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("anchor_delta_k", "now_delta_k", "armed"),
        [
            (1.0, 0.9, True),  # through-flow witness lost (< epsilon)
            (1.0, 1.0, False),  # still at epsilon: evidence, not lost
            (0.9, 0.0, False),  # anchor was displacement, not through-flow
        ],
    )
    def test_lost_through_flow_rearms(
        self, anchor_delta_k: float, now_delta_k: float, armed: bool
    ) -> None:
        """Only an anchor that WAS a through-flow witness re-arms on loss."""
        monitor = LoopFlowMonitor()
        _step(monitor, 24.0, 24.0, epsilon_k=1.0)
        # Evidence: either delta-T >= 1.0 or +1 K supply displacement.
        _step(monitor, 25.0, 25.0 - anchor_delta_k, epsilon_k=1.0)
        for _ in range(3):
            # Supply holds at 25.0 and the return never drops 0.5 K below
            # the anchor: only the delta-T rule can arm.
            _step(monitor, 25.0, 25.0 - now_delta_k, epsilon_k=1.0)
        assert monitor.no_flow_active is armed

    @pytest.mark.unit
    def test_restart_clears_the_anchor(self) -> None:
        """After ``restart()`` the window opens armed again."""
        monitor = LoopFlowMonitor()
        _step(monitor, 25.0, 25.0)
        _step(monitor, 25.6, 25.6)  # evidence -> disarmed
        monitor.restart()
        for _ in range(3):
            _step(monitor, 25.6, 25.6)
        assert monitor.no_flow_active is True

    @pytest.mark.unit
    @pytest.mark.parametrize("mode", [Mode.HEATING, Mode.COOLING])
    def test_references_ratchet_per_probe(self, mode: Mode) -> None:
        """Each probe's reference follows its own extreme away from the
        source; a rise from that extreme on EITHER probe is evidence."""
        sign = 1.0 if mode is Mode.HEATING else -1.0
        for probe in ("supply", "return"):
            monitor = LoopFlowMonitor()
            _step(monitor, 25.0, 25.0, mode=mode, epsilon_k=3.0)
            # Supply dips 1 K, return 2 K away from the source (armed).
            _step(monitor, 25.0 - sign, 25.0 - 2 * sign, mode=mode, epsilon_k=3.0)
            # Back up: the moved probe is +0.5 K from ITS own extreme,
            # the other stays put.
            if probe == "supply":
                s, r = 25.0 - sign * 0.5, 25.0 - 2 * sign
            else:
                s, r = 25.0 - sign, 25.0 - sign * 1.5
            _step(monitor, s, r, mode=mode, epsilon_k=3.0)
            # The evidence reset the window: two more cycles cannot latch.
            _step(monitor, s - sign * 0.5, r - sign * 0.5, mode=mode, epsilon_k=3.0)
            _step(monitor, s - sign * 0.5, r - sign * 0.5, mode=mode, epsilon_k=3.0)
            assert monitor.no_flow_active is False, probe


@pytest.mark.unit
def test_partial_recovery_does_not_disarm() -> None:
    """Once armed, creeping back toward the anchor WITHOUT a 0.5 K rise
    from the trough keeps the window armed (only evidence disarms)."""
    monitor = LoopFlowMonitor()
    _step(monitor, 25.5, 25.5)
    _step(monitor, 26.0, 26.0)  # evidence, anchor 26.0 / 26.0
    _step(monitor, 25.4, 25.4)  # departed 0.6 K: armed (300 s)
    _step(monitor, 25.8, 25.8)  # +0.4 K from the trough: no evidence
    _step(monitor, 25.8, 25.8)
    assert monitor.no_flow_active is True


@pytest.mark.unit
def test_references_start_at_the_evidence_sample() -> None:
    """Displacement after evidence is measured from the EVIDENCE sample,
    not re-captured one cycle later."""
    monitor = LoopFlowMonitor()
    _step(monitor, 24.0, 24.0, epsilon_k=1.0)
    _step(monitor, 25.0, 24.0, epsilon_k=1.0)  # through-flow evidence
    _step(monitor, 25.3, 24.4, epsilon_k=1.0)  # dT lost: armed, +0.3 K
    _step(monitor, 25.5, 24.6, epsilon_k=1.0)  # +0.5 K from 25.0: evidence
    _step(monitor, 25.5, 24.6, epsilon_k=1.0)
    assert monitor.no_flow_active is False
