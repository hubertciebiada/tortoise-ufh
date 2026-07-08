"""Independent safety layer: rules S1-S5 as data + a stateful evaluator.

The safety layer is deliberately **algorithm-independent** (PRD Aneks Sec. 8.7):
it takes a plain :class:`SensorSnapshot` and decides which hard-safety conditions
are active, with hysteresis so a rule that trips at its ``threshold_on`` stays
active until the measured value crosses ``threshold_off`` in the recovery
direction. The controller feeds a snapshot each cycle and merges the resulting
flags into its :class:`~tortoise_ufh.models.RoomReport`.

Rules (each a frozen :class:`SafetyRule` constant):
    * **S1 floor overheat** -- close the valve when the (proxy) floor gets too
      hot. No floor sensor exists (PRD Sec. 6), so the *measured supply-water
      temperature* is used as a conservative floor-surface proxy.
    * **S2 condensation** -- close the cooling valve when the supply water drops
      to within ``DEW_MARGIN_DEFAULT_K`` of the room dew point (defense-in-depth
      local layer, PRD Sec. 8.4).
    * **S3 emergency heat** -- force heating when the room falls below a hard
      frost-protection floor.
    * **S4 emergency cool** -- force cooling when the room exceeds a hard ceiling.
    * **S5 watchdog** -- fall back to the heat-pump native curve when no fresh
      update has arrived for too long (PRD Sec. 8.7).

This module is pure Python and MUST NOT import ``homeassistant``.

Units:
    Temperatures: degrees Celsius (``_c``).
    Relative humidity: percent 0..100 (``_pct``).
    Watchdog age: minutes (``_minutes``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from tortoise_ufh.const import DEW_MARGIN_DEFAULT_K
from tortoise_ufh.dew_point import dew_point

# ---------------------------------------------------------------------------
# Default thresholds (degrees Celsius, or minutes for the watchdog)
# ---------------------------------------------------------------------------

S1_SUPPLY_ON_C: float = 40.0
"""S1 trips when supply-water (floor proxy) exceeds this, in degC."""

S1_SUPPLY_OFF_C: float = 38.0
"""S1 clears when supply-water falls below this, in degC (hysteresis)."""

S3_ROOM_ON_C: float = 5.0
"""S3 trips when room air falls below this, in degC (frost protection)."""

S3_ROOM_OFF_C: float = 6.0
"""S3 clears when room air rises above this, in degC (hysteresis)."""

S4_ROOM_ON_C: float = 35.0
"""S4 trips when room air exceeds this, in degC."""

S4_ROOM_OFF_C: float = 34.0
"""S4 clears when room air falls below this, in degC (hysteresis)."""

S5_WATCHDOG_ON_MINUTES: float = 15.0
"""S5 trips when the last update is older than this, in minutes."""

S5_WATCHDOG_OFF_MINUTES: float = 5.0
"""S5 clears once updates are younger than this, in minutes (hysteresis)."""


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class SafetyAction(Enum):
    """Action a triggered safety rule prescribes.

    Members:
        CLOSE_VALVE: Drive the UFH valve to 0 % (S1 overheat, S2 condensation).
        EMERGENCY_HEAT: Open the valve fully and force fast-source heating (S3).
        EMERGENCY_COOL: Force fast-source cooling (S4).
        FALLBACK_HP_CURVE: Defer to the heat pump's native curve (S5 watchdog).
    """

    CLOSE_VALVE = "close_valve"
    EMERGENCY_HEAT = "emergency_heat"
    EMERGENCY_COOL = "emergency_cool"
    FALLBACK_HP_CURVE = "fallback_hp_curve"


# ---------------------------------------------------------------------------
# Input snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SensorSnapshot:
    """Immutable per-room measurement snapshot fed to the safety evaluator.

    Any field may be ``None`` to represent a missing sensor; a rule whose
    condition cannot be computed holds its previous hysteresis state rather
    than tripping or clearing on incomplete data.

    Attributes:
        supply_temperature_c: Governing loop supply-water temperature in degC
            (hottest loop in heating, coldest in cooling), used as the
            floor-surface proxy for S1 and S2. ``None`` if unavailable.
        room_temperature_c: Room air temperature in degC, or ``None``.
        humidity_pct: Relative humidity in percent (0..100), required for S2.
            ``None`` if unavailable.
        last_update_age_minutes: Minutes since the last successful control
            update, used by the S5 watchdog. Must be >= 0.

    Raises:
        ValueError: If ``humidity_pct`` is outside [0, 100] or
            ``last_update_age_minutes`` is negative.
    """

    supply_temperature_c: float | None
    room_temperature_c: float | None
    humidity_pct: float | None
    last_update_age_minutes: float

    def __post_init__(self) -> None:
        """Validate humidity range and non-negative watchdog age."""
        if self.humidity_pct is not None and not (0.0 <= self.humidity_pct <= 100.0):
            msg = f"humidity_pct must be in [0, 100] %, got {self.humidity_pct}"
            raise ValueError(msg)
        if self.last_update_age_minutes < 0.0:
            msg = (
                "last_update_age_minutes must be >= 0, got "
                f"{self.last_update_age_minutes}"
            )
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Condition callables (pure; return None when inputs are missing)
# ---------------------------------------------------------------------------


def _supply_value(snapshot: SensorSnapshot) -> float | None:
    """Return the supply-water temperature (S1 floor-overheat proxy), degC."""
    return snapshot.supply_temperature_c


def _room_value(snapshot: SensorSnapshot) -> float | None:
    """Return the room air temperature (S3/S4), degC."""
    return snapshot.room_temperature_c


def _watchdog_value(snapshot: SensorSnapshot) -> float | None:
    """Return the last-update age (S5), minutes."""
    return snapshot.last_update_age_minutes


def _condensation_margin(snapshot: SensorSnapshot) -> float | None:
    """Return ``supply - (dew_point + margin)`` for S2, degC, or ``None``.

    ``None`` when any of supply water, room temperature, or a usable humidity
    (> 0 %) is missing, so an under-instrumented room never raises a false
    condensation alarm; the controller's own cooling path stays conservative.
    """
    if (
        snapshot.supply_temperature_c is None
        or snapshot.room_temperature_c is None
        or snapshot.humidity_pct is None
        or snapshot.humidity_pct <= 0.0
    ):
        return None
    t_dew = dew_point(snapshot.room_temperature_c, snapshot.humidity_pct)
    return snapshot.supply_temperature_c - (t_dew + DEW_MARGIN_DEFAULT_K)


# ---------------------------------------------------------------------------
# Safety rule dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafetyRule:
    """Frozen definition of one safety rule (data, not a class hierarchy).

    A rule pairs a ``condition`` that extracts a scalar from a snapshot with
    on/off thresholds forming a hysteresis band, a :class:`SafetyAction`, and a
    priority. When ``trigger_above`` is ``True`` the rule trips when
    ``condition(snapshot) > threshold_on`` and clears when
    ``condition(snapshot) < threshold_off``; when ``False`` both comparisons are
    reversed (a "too low" rule).

    Attributes:
        name: Stable flag identifier, e.g. ``"s1_floor_overheat"`` (also the
            string merged into the room report flags).
        description: Human-readable summary.
        priority: Lower is higher priority; must be >= 1.
        threshold_on: Value (degC or minutes) at which the rule trips.
        threshold_off: Recovery value at which the rule clears (hysteresis).
        action: :class:`SafetyAction` to apply while active.
        condition: Callable extracting the measured value, or ``None`` when the
            required inputs are missing.
        trigger_above: ``True`` for "too high" rules, ``False`` for "too low".

    Raises:
        ValueError: If ``name`` is empty, ``priority`` < 1, or the hysteresis
            band is ordered inconsistently with ``trigger_above``.
    """

    name: str
    description: str
    priority: int
    threshold_on: float
    threshold_off: float
    action: SafetyAction
    condition: Callable[[SensorSnapshot], float | None]
    trigger_above: bool

    def __post_init__(self) -> None:
        """Validate identity, priority, and hysteresis ordering."""
        if not self.name:
            msg = "SafetyRule name must be non-empty"
            raise ValueError(msg)
        if self.priority < 1:
            msg = f"priority must be >= 1, got {self.priority}"
            raise ValueError(msg)
        if self.trigger_above and self.threshold_off > self.threshold_on:
            msg = (
                f"trigger_above rule '{self.name}': threshold_off "
                f"({self.threshold_off}) must be <= threshold_on "
                f"({self.threshold_on})"
            )
            raise ValueError(msg)
        if not self.trigger_above and self.threshold_off < self.threshold_on:
            msg = (
                f"trigger_below rule '{self.name}': threshold_off "
                f"({self.threshold_off}) must be >= threshold_on "
                f"({self.threshold_on})"
            )
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Evaluation result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafetyRuleResult:
    """Immutable outcome of evaluating one rule against a snapshot.

    Attributes:
        rule: The rule that was evaluated.
        triggered: Whether the rule is currently active (after hysteresis).
        measured_value: Value returned by ``rule.condition(snapshot)``, or
            ``None`` if the inputs were missing this cycle.
        action: The action to apply, or ``None`` when not triggered.
    """

    rule: SafetyRule
    triggered: bool
    measured_value: float | None
    action: SafetyAction | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (enums -> ``.value``).

        Returns:
            Plain ``dict`` with the rule name, priority, trigger state, measured
            value (float or ``None``) and action (str or ``None``).
        """
        return {
            "name": self.rule.name,
            "priority": self.rule.priority,
            "triggered": self.triggered,
            "measured_value": self.measured_value,
            "action": self.action.value if self.action is not None else None,
        }


# ---------------------------------------------------------------------------
# Default rule constants (S1..S5)
# ---------------------------------------------------------------------------

S1_FLOOR_OVERHEAT: SafetyRule = SafetyRule(
    name="s1_floor_overheat",
    description="Floor overheat protection via supply-water proxy (no floor sensor)",
    priority=1,
    threshold_on=S1_SUPPLY_ON_C,
    threshold_off=S1_SUPPLY_OFF_C,
    action=SafetyAction.CLOSE_VALVE,
    condition=_supply_value,
    trigger_above=True,
)
"""S1: close the valve when supply water > 40 degC, clear below 38 degC."""

S2_CONDENSATION: SafetyRule = SafetyRule(
    name="s2_condensation",
    description="Condensation protection: supply water within margin of dew point",
    priority=1,
    threshold_on=0.0,
    threshold_off=1.0,
    action=SafetyAction.CLOSE_VALVE,
    condition=_condensation_margin,
    trigger_above=False,
)
"""S2: close the cooling valve when the dew-point margin < 0, clear above 1 K."""

S3_EMERGENCY_HEAT: SafetyRule = SafetyRule(
    name="s3_emergency_heat",
    description="Emergency heating when the room falls below the frost floor",
    priority=2,
    threshold_on=S3_ROOM_ON_C,
    threshold_off=S3_ROOM_OFF_C,
    action=SafetyAction.EMERGENCY_HEAT,
    condition=_room_value,
    trigger_above=False,
)
"""S3: force heating when room < 5 degC, clear above 6 degC."""

S4_EMERGENCY_COOL: SafetyRule = SafetyRule(
    name="s4_emergency_cool",
    description="Emergency cooling when the room exceeds the hard ceiling",
    priority=2,
    threshold_on=S4_ROOM_ON_C,
    threshold_off=S4_ROOM_OFF_C,
    action=SafetyAction.EMERGENCY_COOL,
    condition=_room_value,
    trigger_above=True,
)
"""S4: force cooling when room > 35 degC, clear below 34 degC."""

S5_WATCHDOG: SafetyRule = SafetyRule(
    name="s5_watchdog",
    description="Watchdog: fall back to the HP curve when updates go stale",
    priority=3,
    threshold_on=S5_WATCHDOG_ON_MINUTES,
    threshold_off=S5_WATCHDOG_OFF_MINUTES,
    action=SafetyAction.FALLBACK_HP_CURVE,
    condition=_watchdog_value,
    trigger_above=True,
)
"""S5: fall back to the HP curve when no update for > 15 min, clear below 5 min."""

DEFAULT_SAFETY_RULES: tuple[SafetyRule, ...] = (
    S1_FLOOR_OVERHEAT,
    S2_CONDENSATION,
    S3_EMERGENCY_HEAT,
    S4_EMERGENCY_COOL,
    S5_WATCHDOG,
)
"""The five default safety rules S1..S5."""


# ---------------------------------------------------------------------------
# Stateful evaluator with hysteresis
# ---------------------------------------------------------------------------


class SafetyEvaluator:
    """Stateful safety evaluator with per-rule hysteresis.

    Holds one boolean active-state per rule between calls so a tripped rule
    stays active until its ``threshold_off`` is crossed. When a rule's condition
    returns ``None`` (missing inputs) the prior active state is preserved.

    Typical usage::

        evaluator = SafetyEvaluator()
        results = evaluator.evaluate(snapshot)
        flags = evaluator.active_flags(snapshot)  # merge into RoomReport.flags

    Args:
        rules: Rules to evaluate, evaluated in ascending priority order.
            Defaults to :data:`DEFAULT_SAFETY_RULES`.

    Raises:
        ValueError: If two rules share the same ``name``.
    """

    def __init__(self, rules: tuple[SafetyRule, ...] = DEFAULT_SAFETY_RULES) -> None:
        names = [rule.name for rule in rules]
        if len(names) != len(set(names)):
            msg = f"safety rule names must be unique, got {names}"
            raise ValueError(msg)
        self._rules: tuple[SafetyRule, ...] = tuple(
            sorted(rules, key=lambda r: r.priority)
        )
        self._active: dict[str, bool] = {rule.name: False for rule in self._rules}

    @property
    def rules(self) -> tuple[SafetyRule, ...]:
        """The configured rules, ordered by ascending priority."""
        return self._rules

    @property
    def active_rule_names(self) -> tuple[str, ...]:
        """Names of the currently active rules, in priority order."""
        return tuple(rule.name for rule in self._rules if self._active[rule.name])

    def evaluate(self, snapshot: SensorSnapshot) -> list[SafetyRuleResult]:
        """Evaluate every rule against *snapshot*, updating hysteresis state.

        Args:
            snapshot: The current per-room measurement snapshot.

        Returns:
            One :class:`SafetyRuleResult` per rule, in ascending priority order.
        """
        results: list[SafetyRuleResult] = []
        for rule in self._rules:
            measured = rule.condition(snapshot)
            active = self._active[rule.name]
            if measured is not None:
                if rule.trigger_above:
                    if not active and measured > rule.threshold_on:
                        active = True
                    elif active and measured < rule.threshold_off:
                        active = False
                elif not active and measured < rule.threshold_on:
                    active = True
                elif active and measured > rule.threshold_off:
                    active = False
            self._active[rule.name] = active
            results.append(
                SafetyRuleResult(
                    rule=rule,
                    triggered=active,
                    measured_value=measured,
                    action=rule.action if active else None,
                )
            )
        return results

    def active_flags(self, snapshot: SensorSnapshot) -> tuple[str, ...]:
        """Evaluate *snapshot* and return the names of the tripped rules.

        Convenience wrapper around :meth:`evaluate` yielding exactly the flag
        strings to merge into a :class:`~tortoise_ufh.models.RoomReport`.

        Args:
            snapshot: The current per-room measurement snapshot.

        Returns:
            Names of the active rules, in ascending priority order.
        """
        return tuple(
            result.rule.name for result in self.evaluate(snapshot) if result.triggered
        )

    def reset(self) -> None:
        """Clear all hysteresis state back to inactive."""
        for name in self._active:
            self._active[name] = False
