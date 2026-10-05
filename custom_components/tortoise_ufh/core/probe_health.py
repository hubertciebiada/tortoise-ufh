"""Loop water-probe health: learned still-water baselines and drift detection.

Motivated by the 2026-09 production incident (issue #16): during a service
visit one loop SUPPLY probe slipped on its pipe and read +5.3 K too warm for 13
days. The loop probes feed the hard-safety layer directly (S1 overheat takes
the hottest supply in heating, S2 condensation the coldest in cooling, S6 and
the circulation gate read the supply/return pair), yet nothing noticed.

Every probe carries its own STATIC measurement error (placement, sensor
tolerance): with the water still, its deviation from the median of all loop
probes on the same manifold is stable to ~0.1 K from month to month. That
offset is harmless; a CHANGE of it is the fault signature. This module
therefore never calibrates a probe — it learns each probe's still-water
baseline and flags departures from it, plus one flowing-state physical check:

* **Still-water drift** (per manifold group). Once every loop valve on the
  manifold has been closed for :data:`STILL_SETTLE_S`, each probe's deviation
  ``value - median(group)`` is sampled over windows of
  :data:`STILL_WINDOW_S`. A window's deviation (the median of its samples) is
  compared with the probe's baseline (the median of up to
  :data:`BASELINE_MAX_WINDOWS` earlier windows, admitted at most once per
  :data:`BASELINE_SPACING_S`). A departure above :data:`DRIFT_K` in
  :data:`DRIFT_WINDOWS` consecutive windows raises the probe's drift flag; the
  same number of consecutive windows back within :data:`DRIFT_K` clears it.
  A drifting window is never admitted, so the baseline cannot learn the fault.
* **Supply vs. main supply** (flowing). While the group visibly circulates
  and a loop's valve is open, that loop's supply probe sits on the same water
  as the manifold main-supply probe: a gap above :data:`FLOW_SUPPLY_MAX_K`
  held for :data:`FLOW_PERSIST_S` flags the probe (the incident read 32.2 degC
  on a loop fed by a 28.2 degC bar). When most open loops disagree with the
  main probe at once, the MAIN probe is the suspect and nothing is judged.

What the adapter does with a flagged probe (issue #16, item 4): it is removed
from the core's ``LoopInput`` (S1/S2 governing supply, S6, circulation gate),
a flagged supply falling back to the manifold main supply when one is healthy
— or reading as missing, which S2 already handles by throttling. Nothing here
moves a valve or freezes an integrator.

Deliberately NOT a :class:`~tortoise_ufh.core.safety.SafetyRule` and not part
of the per-room controller: probes belong to manifolds, not rooms, and the
learned baselines outlive a restart (the adapter persists :meth:`to_dict`).
Pure Python (stdlib only); MUST NOT import ``homeassistant``.

Units:
    * Temperatures: degrees Celsius (``_c``); deviations/margins: kelvin
      (``_k``).
    * Valve positions: percent 0..100 (``_pct``).
    * Times: seconds (``_s``) of accumulated control-cycle ``dt`` (the core
      has no wall clock).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from .flow_watchdog import CIRCULATION_DELTA_K

__all__ = [
    "BASELINE_MAX_WINDOWS",
    "BASELINE_MIN_WINDOWS",
    "BASELINE_SPACING_S",
    "DRIFT_K",
    "DRIFT_WINDOWS",
    "FLOW_PERSIST_S",
    "FLOW_SUPPLY_MAX_K",
    "MIN_GROUP_PROBES",
    "STILL_CLOSED_MAX_PCT",
    "STILL_SETTLE_S",
    "STILL_WINDOW_S",
    "LoopProbes",
    "ManifoldProbes",
    "ProbeHealthMonitor",
    "ProbeHealthReport",
    "ProbeStatus",
]


# --- Module constants (deliberately NOT tuning knobs — fixed like S6's) -----

STILL_CLOSED_MAX_PCT: float = 2.0
"""Valve position at or below which a loop counts as closed for the
still-water witness [%]."""

STILL_SETTLE_S: float = 3.0 * 3600.0
"""How long every loop on a manifold must stay closed before its water counts
as still [s] (the issue's ">= 3 h of still water")."""

STILL_WINDOW_S: float = 3600.0
"""Length of one still-water sampling window [s]."""

MIN_WINDOW_SAMPLES: int = 3
"""Samples a probe needs inside one window for the window to count."""

MIN_GROUP_PROBES: int = 3
"""Valid loop probes a manifold needs for a meaningful median."""

BASELINE_MAX_WINDOWS: int = 14
"""Window deviations kept per probe for the rolling-median baseline."""

BASELINE_MIN_WINDOWS: int = 3
"""Window deviations a baseline needs before drift is judged."""

BASELINE_SPACING_S: float = 12.0 * 3600.0
"""Minimum monitor time between two baseline admissions of one probe [s]:
spreads the 14 kept windows over at least a week of still water rather than
one long standstill."""

DRIFT_K: float = 1.5
"""Departure of a window deviation from the baseline that counts as drift
[K]. Normal per-probe offsets (+-0.8 K) are stable, so they never get near
it; the incident moved by 5.0 K."""

DRIFT_WINDOWS: int = 2
"""Consecutive drifting (or healthy) windows that raise (or clear) the flag."""

FLOW_SUPPLY_MAX_K: float = 3.0
"""Largest plausible gap between an open, flowing loop's supply probe and the
manifold main-supply probe [K]."""

FLOW_PERSIST_S: float = 30.0 * 60.0
"""How long the supply-vs-main gap must hold (or stay healed) before the
flag is raised (or cleared) [s]."""

_REASON_DRIFT: str = "still_water_drift"
_REASON_SUPPLY: str = "supply_vs_main"


def _finite(value: float | None) -> float | None:
    """Return ``value`` when it is a finite number, else ``None``."""
    if value is None or not math.isfinite(value):
        return None
    return value


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopProbes:
    """One loop's probe readings for one control cycle.

    Attributes:
        supply_id: Stable id of the supply probe (the adapter uses the entity
            id), or ``None`` when the loop has none.
        return_id: Stable id of the return probe, or ``None``.
        supply_c: The supply reading [degC] after the adapter's sample gate,
            or ``None``.
        return_c: The return reading [degC], or ``None``.
        valve_pct: The loop's valve position [%] — the core's last command
            for a live room, the actuator feedback otherwise — or ``None``
            when unknown (an unknown loop never counts as closed).

    Raises:
        ValueError: If ``valve_pct`` is outside 0..100 or a probe id is
            empty.
    """

    supply_id: str | None = None
    return_id: str | None = None
    supply_c: float | None = None
    return_c: float | None = None
    valve_pct: float | None = None

    def __post_init__(self) -> None:
        """Validate the valve position and the probe ids."""
        if self.valve_pct is not None and not 0.0 <= self.valve_pct <= 100.0:
            msg = f"valve_pct must be in [0, 100] %, got {self.valve_pct}"
            raise ValueError(msg)
        for probe_id in (self.supply_id, self.return_id):
            if probe_id is not None and not probe_id.strip():
                msg = "probe ids must be non-empty strings or None"
                raise ValueError(msg)

    def readings(self) -> tuple[tuple[str, float], ...]:
        """Return the ``(probe_id, value)`` pairs that carry a reading."""
        pairs: list[tuple[str, float]] = []
        supply = _finite(self.supply_c)
        if self.supply_id is not None and supply is not None:
            pairs.append((self.supply_id, supply))
        ret = _finite(self.return_c)
        if self.return_id is not None and ret is not None:
            pairs.append((self.return_id, ret))
        return tuple(pairs)


@dataclass(frozen=True)
class ManifoldProbes:
    """One manifold's loops and main probes for one control cycle.

    Attributes:
        group_id: Stable id of the manifold (baselines are per manifold).
        loops: The loops sitting on the manifold.
        main_supply_c: The main-supply (Z0) probe [degC], or ``None``.
        main_return_c: The main-return (P0) probe [degC], or ``None``.
        learn: ``False`` for the catch-all group of loops on no configured
            manifold: probes across several physical manifolds have no common
            still-water median, so only the flowing check runs there.

    Raises:
        ValueError: If ``group_id`` is empty.
    """

    group_id: str
    loops: tuple[LoopProbes, ...] = ()
    main_supply_c: float | None = None
    main_return_c: float | None = None
    learn: bool = True

    def __post_init__(self) -> None:
        """Validate the group id."""
        if not self.group_id.strip():
            msg = "group_id must be a non-empty string"
            raise ValueError(msg)

    @property
    def still(self) -> bool:
        """Whether every loop's valve is known to be closed this cycle."""
        return bool(self.loops) and all(
            loop.valve_pct is not None and loop.valve_pct <= STILL_CLOSED_MAX_PCT
            for loop in self.loops
        )

    def circulating(self, excluded: frozenset[str]) -> bool:
        """Whether the manifold visibly circulates water this cycle.

        ``True`` when the main supply/return pair shows ``|delta-T| >=``
        :data:`~tortoise_ufh.core.flow_watchdog.CIRCULATION_DELTA_K`, or at
        least TWO loops do (one loop alone could be a drifted probe faking
        the signature). Probes in ``excluded`` (already flagged) never count.

        Args:
            excluded: Probe ids to ignore.

        Returns:
            Whether circulation is evident.
        """
        main_s = _finite(self.main_supply_c)
        main_r = _finite(self.main_return_c)
        if (
            main_s is not None
            and main_r is not None
            and abs(main_s - main_r) >= CIRCULATION_DELTA_K
        ):
            return True
        witnesses = 0
        for loop in self.loops:
            if loop.supply_id in excluded or loop.return_id in excluded:
                continue
            supply = _finite(loop.supply_c)
            ret = _finite(loop.return_c)
            if (
                supply is not None
                and ret is not None
                and abs(supply - ret) >= CIRCULATION_DELTA_K
            ):
                witnesses += 1
        return witnesses >= 2


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeStatus:
    """Diagnostics of one probe (JSON-serialisable via :meth:`to_dict`).

    Attributes:
        probe_id: The probe's id.
        baseline_k: Learned still-water deviation from the manifold median
            [K], or ``None`` while fewer than :data:`BASELINE_MIN_WINDOWS`
            windows are known.
        baseline_windows: Window deviations in the baseline.
        deviation_k: Deviation of the most recent still-water window [K], or
            ``None`` before the first one.
        flagged: Whether the probe is currently flagged.
        reasons: Why it is flagged (``"still_water_drift"`` and/or
            ``"supply_vs_main"``); empty when healthy.
    """

    probe_id: str
    baseline_k: float | None
    baseline_windows: int
    deviation_k: float | None
    flagged: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict (reasons as a list)."""
        return {
            "probe_id": self.probe_id,
            "baseline_k": self.baseline_k,
            "baseline_windows": self.baseline_windows,
            "deviation_k": self.deviation_k,
            "flagged": self.flagged,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ProbeHealthReport:
    """Result of one :meth:`ProbeHealthMonitor.update`.

    Attributes:
        probes: Diagnostics per probe id seen this cycle.
    """

    probes: Mapping[str, ProbeStatus] = field(default_factory=dict)

    @property
    def flagged(self) -> frozenset[str]:
        """Ids of the probes currently flagged."""
        return frozenset(pid for pid, st in self.probes.items() if st.flagged)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict keyed by probe id."""
        return {pid: st.to_dict() for pid, st in self.probes.items()}


# ---------------------------------------------------------------------------
# Per-probe and per-group state
# ---------------------------------------------------------------------------


@dataclass
class _ProbeState:
    """Mutable learning state of one probe (persisted)."""

    baseline: list[float] = field(default_factory=list)
    last_admit_s: float | None = None
    last_deviation_k: float | None = None
    drift_streak: int = 0
    ok_streak: int = 0
    drift: bool = False
    # Flowing check (not persisted: short timers, re-proven after a restart):
    # the current gap verdict and how long it has held.
    supply_gap: bool | None = None
    supply_held_s: float = 0.0
    supply_flag: bool = False

    def baseline_k(self) -> float | None:
        """Median of the admitted windows, or ``None`` while learning."""
        if len(self.baseline) < BASELINE_MIN_WINDOWS:
            return None
        return float(median(self.baseline))

    def to_dict(self) -> dict[str, Any]:
        """Return the persisted form."""
        return {
            "baseline": list(self.baseline),
            "last_admit_s": self.last_admit_s,
            "last_deviation_k": self.last_deviation_k,
            "drift_streak": self.drift_streak,
            "ok_streak": self.ok_streak,
            "drift": self.drift,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> _ProbeState:
        """Rebuild from the persisted form (raises on garbage)."""
        baseline = [float(v) for v in raw.get("baseline") or ()]
        if not all(math.isfinite(v) for v in baseline):
            msg = f"baseline values must be finite, got {baseline}"
            raise ValueError(msg)
        last_admit = raw.get("last_admit_s")
        last_dev = raw.get("last_deviation_k")
        return cls(
            baseline=baseline[-BASELINE_MAX_WINDOWS:],
            last_admit_s=None if last_admit is None else float(last_admit),
            last_deviation_k=None if last_dev is None else float(last_dev),
            drift_streak=int(raw.get("drift_streak") or 0),
            ok_streak=int(raw.get("ok_streak") or 0),
            drift=bool(raw.get("drift")),
        )


@dataclass
class _GroupState:
    """Mutable still-water window state of one manifold (not persisted).

    Dropped whenever the water stops being still, so a new still stretch
    always settles and samples from scratch.
    """

    still_s: float = 0.0
    window_s: float = 0.0
    samples: dict[str, list[float]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class ProbeHealthMonitor:
    """Stateful probe-health monitor for every manifold of one installation.

    One instance per adapter coordinator. :meth:`update` is called once per
    control cycle with every manifold's probes; :meth:`to_dict` /
    :meth:`from_dict` carry the learned baselines across restarts.
    """

    def __init__(self) -> None:
        """Initialise a monitor with nothing learned."""
        self._clock_s: float = 0.0
        self._probes: dict[str, _ProbeState] = {}
        self._groups: dict[str, _GroupState] = {}

    # -- public API -----------------------------------------------------------

    def update(
        self,
        groups: Sequence[ManifoldProbes],
        *,
        dt_seconds: float,
        open_threshold_pct: float,
    ) -> ProbeHealthReport:
        """Advance every manifold by one control cycle.

        Args:
            groups: Every manifold's probes this cycle. A probe id must
                appear in one group only.
            dt_seconds: Elapsed time this cycle [s] (> 0).
            open_threshold_pct: Valve position at or above which a loop
                counts as open for the flowing check [%] (the S6
                ``flow_open_threshold_pct``).

        Returns:
            The :class:`ProbeHealthReport` after this cycle.

        Raises:
            ValueError: If ``dt_seconds`` is not positive.
        """
        if dt_seconds <= 0:
            msg = f"dt_seconds must be > 0, got {dt_seconds}"
            raise ValueError(msg)
        self._clock_s += dt_seconds
        seen: dict[str, None] = {}
        for group in groups:
            for loop in group.loops:
                for probe_id in (loop.supply_id, loop.return_id):
                    if probe_id is not None:
                        seen[probe_id] = None
                        self._probes.setdefault(probe_id, _ProbeState())
            self._update_flowing(
                group, dt_seconds=dt_seconds, open_threshold_pct=open_threshold_pct
            )
            if group.learn:
                self._update_still(group, dt_seconds=dt_seconds)
        live_groups = {g.group_id for g in groups}
        for gid in [g for g in self._groups if g not in live_groups]:
            del self._groups[gid]
        return ProbeHealthReport(
            probes={pid: self._status(pid) for pid in seen},
        )

    def flagged(self) -> frozenset[str]:
        """Ids of the probes flagged by the latest update."""
        return frozenset(
            pid for pid, st in self._probes.items() if st.drift or st.supply_flag
        )

    def retain(self, probe_ids: frozenset[str]) -> None:
        """Forget every probe not in ``probe_ids`` (de-configured probes).

        Args:
            probe_ids: The probe ids still configured.
        """
        for pid in [p for p in self._probes if p not in probe_ids]:
            del self._probes[pid]

    def reset_baselines(self, probe_ids: frozenset[str] | None = None) -> None:
        """Forget the learned baseline and flags of some or all probes.

        For a probe that was deliberately moved or replaced: its new static
        offset is relearned from scratch instead of reading as drift.

        Args:
            probe_ids: The probes to reset, or ``None`` for all of them.
        """
        targets = list(self._probes) if probe_ids is None else list(probe_ids)
        for pid in targets:
            if pid in self._probes:
                self._probes[pid] = _ProbeState()
        self._groups.clear()

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable persisted form (baselines + flags)."""
        return {
            "clock_s": self._clock_s,
            "probes": {pid: st.to_dict() for pid, st in self._probes.items()},
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProbeHealthMonitor:
        """Rebuild a monitor from :meth:`to_dict` output.

        A probe entry that cannot be parsed is dropped (it relearns) rather
        than failing the whole restore.

        Args:
            raw: The persisted mapping.

        Returns:
            The restored monitor.
        """
        monitor = cls()
        try:
            clock_s = float(raw.get("clock_s") or 0.0)
        except (TypeError, ValueError):
            clock_s = 0.0
        if math.isfinite(clock_s):
            monitor._clock_s = clock_s
        probes = raw.get("probes")
        if isinstance(probes, Mapping):
            for pid, entry in probes.items():
                if not isinstance(entry, Mapping):
                    continue
                try:
                    monitor._probes[str(pid)] = _ProbeState.from_dict(entry)
                except (TypeError, ValueError):
                    continue
        return monitor

    # -- internal -------------------------------------------------------------

    def _status(self, probe_id: str) -> ProbeStatus:
        """Build one probe's diagnostics."""
        st = self._probes[probe_id]
        reasons: list[str] = []
        if st.drift:
            reasons.append(_REASON_DRIFT)
        if st.supply_flag:
            reasons.append(_REASON_SUPPLY)
        return ProbeStatus(
            probe_id=probe_id,
            baseline_k=st.baseline_k(),
            baseline_windows=len(st.baseline),
            deviation_k=st.last_deviation_k,
            flagged=bool(reasons),
            reasons=tuple(reasons),
        )

    def _update_flowing(
        self,
        group: ManifoldProbes,
        *,
        dt_seconds: float,
        open_threshold_pct: float,
    ) -> None:
        """Advance the supply-vs-main check of every loop on ``group``."""
        main = _finite(group.main_supply_c)
        if main is None or not group.circulating(self.flagged()):
            return  # not judged: HOLD every timer
        judged: list[tuple[str, bool]] = []
        for loop in group.loops:
            supply = _finite(loop.supply_c)
            if (
                loop.supply_id is None
                or supply is None
                or loop.valve_pct is None
                or loop.valve_pct < open_threshold_pct
            ):
                continue
            judged.append((loop.supply_id, abs(supply - main) > FLOW_SUPPLY_MAX_K))
        bad = sum(1 for _, gap in judged if gap)
        if len(judged) >= 2 and 2 * bad > len(judged):
            return  # most open loops disagree: the MAIN probe is the suspect
        for probe_id, gap in judged:
            st = self._probes[probe_id]
            if gap is st.supply_gap:
                st.supply_held_s += dt_seconds
            else:
                st.supply_gap = gap
                st.supply_held_s = dt_seconds
            if st.supply_held_s >= FLOW_PERSIST_S:
                st.supply_flag = gap

    def _update_still(self, group: ManifoldProbes, *, dt_seconds: float) -> None:
        """Advance the still-water window of ``group``."""
        if not group.still:
            self._groups.pop(group.group_id, None)
            return
        gs = self._groups.setdefault(group.group_id, _GroupState())
        gs.still_s += dt_seconds
        if gs.still_s < STILL_SETTLE_S:
            return
        readings = [pair for loop in group.loops for pair in loop.readings()]
        if len(readings) >= MIN_GROUP_PROBES:
            center = median(value for _, value in readings)
            for probe_id, value in readings:
                gs.samples.setdefault(probe_id, []).append(value - center)
        gs.window_s += dt_seconds
        if gs.window_s >= STILL_WINDOW_S:
            for probe_id, samples in gs.samples.items():
                if len(samples) >= MIN_WINDOW_SAMPLES:
                    self._close_window(probe_id, float(median(samples)))
            self._groups[group.group_id] = _GroupState(still_s=gs.still_s)

    def _close_window(self, probe_id: str, deviation_k: float) -> None:
        """Judge one probe's completed still-water window."""
        st = self._probes[probe_id]
        st.last_deviation_k = deviation_k
        baseline = st.baseline_k()
        drifting = baseline is not None and abs(deviation_k - baseline) > DRIFT_K
        if drifting:
            st.ok_streak = 0
            st.drift_streak += 1
            if st.drift_streak >= DRIFT_WINDOWS:
                st.drift = True
            return
        st.drift_streak = 0
        st.ok_streak += 1
        if st.ok_streak >= DRIFT_WINDOWS:
            st.drift = False
        if st.drift:
            return  # healing but not yet cleared: keep the fault out
        if (
            st.last_admit_s is None
            or self._clock_s - st.last_admit_s >= BASELINE_SPACING_S
        ):
            st.baseline.append(deviation_k)
            del st.baseline[:-BASELINE_MAX_WINDOWS]
            st.last_admit_s = self._clock_s
