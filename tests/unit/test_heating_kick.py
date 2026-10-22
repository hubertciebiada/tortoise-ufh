"""Unit tests for the heating compressor kick (issue #34, 2026-10-22).

Exercises :class:`~tortoise_ufh.hp_link.HeatingCompressorKick` — the pure,
opt-in state machine that raises the heating water target by +1 K through
the issue #33 write gate to release a locked-out Panasonic restart — its
knobs on :class:`~tortoise_ufh.config.ControllerConfig`, the frozen
:class:`~tortoise_ufh.hp_link.HeatingKickDecision`, :func:`kick_step_k`,
:func:`resolve_restart_band` and the gate's :meth:`preview_target_c`.

Covered: trigger timing (exact idle-minute and deficit boundaries), one kick
per idle period, the start timeout (exact boundary), the restore only through
the gate (decrease while running; a failed kick's lower target held while
idle), DHW exclusion via the hot outlet, missing inputs / disabled kick /
disabled gate => no kick, the gate-increase, limit and restart-band
exclusions, the rolling 24 h counter, and the kick + gate pipeline end to end.

Never imports ``homeassistant``; time advances only through ``tick``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.hp_link import (
    HEATING_KICK_REASONS,
    HEATING_KICK_RESULTS,
    HEATING_KICK_STATES,
    HEATING_KICK_STEP_K,
    HeatingCompressorKick,
    HeatingKickDecision,
    HeatingSetpointGate,
    kick_step_k,
    resolve_restart_band,
)

pytestmark = pytest.mark.unit

_DT: float = 300.0
_IDLE_S: float = 15 * 60.0


def _kick(**knobs: object) -> HeatingCompressorKick:
    """A kick built from a config with the given knobs (enabled by default)."""
    knobs.setdefault("heat_kick_enabled", True)
    return HeatingCompressorKick(ControllerConfig(**knobs))  # type: ignore[arg-type]


def _step(
    kick: HeatingCompressorKick,
    *,
    active: bool = True,
    freq_hz: float | None = 0.0,
    outlet_c: float | None = 26.0,
    pump_c: float | None = 30.0,
    base_c: float | None = 30.0,
    step_c: float = 1.0,
    **extra: object,
) -> HeatingKickDecision:
    """One kick step with defaults: pump 30, normal target 30, outlet 26, idle."""
    return kick.step(
        heating_active=active,
        compressor_freq_hz=freq_hz,
        outlet_c=outlet_c,
        pump_setpoint_c=pump_c,
        base_target_c=base_c,
        step_c=step_c,
        **extra,  # type: ignore[arg-type]
    )


def _fire(kick: HeatingCompressorKick, **kw: object) -> HeatingKickDecision:
    """Open an idle period, wait the full idle time and step once more."""
    first = _step(kick, **kw)  # type: ignore[arg-type]
    assert first.reason == "idle_wait"
    kick.tick(_IDLE_S)
    return _step(kick, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------


class TestKickKnobs:
    """The four global-only kick knobs and their validation."""

    def test_defaults(self) -> None:
        """Off, 15 min idle, 3.5 K deficit, 5 min start timeout."""
        cfg = ControllerConfig()
        assert cfg.heat_kick_enabled is False
        assert cfg.kick_idle_minutes == 15.0
        assert cfg.kick_deficit_k == 3.5
        assert cfg.kick_start_timeout_minutes == 5.0

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("kick_idle_minutes", 5.0),
            ("kick_idle_minutes", 120.0),
            ("kick_deficit_k", 1.0),
            ("kick_deficit_k", 10.0),
            ("kick_start_timeout_minutes", 1.0),
            ("kick_start_timeout_minutes", 60.0),
        ],
    )
    def test_range_ends_accepted(self, field_name: str, value: float) -> None:
        """Both ends of every range are valid."""
        cfg = ControllerConfig(**{field_name: value})  # type: ignore[arg-type]
        assert getattr(cfg, field_name) == value

    @pytest.mark.parametrize(
        ("field_name", "value", "message"),
        [
            (
                "kick_idle_minutes",
                4.9,
                "kick_idle_minutes must be in [5, 120], got 4.9",
            ),
            (
                "kick_idle_minutes",
                120.1,
                "kick_idle_minutes must be in [5, 120], got 120.1",
            ),
            ("kick_deficit_k", 0.9, "kick_deficit_k must be in [1, 10], got 0.9"),
            ("kick_deficit_k", 10.1, "kick_deficit_k must be in [1, 10], got 10.1"),
            (
                "kick_start_timeout_minutes",
                0.9,
                "kick_start_timeout_minutes must be in [1, 60], got 0.9",
            ),
            (
                "kick_start_timeout_minutes",
                60.1,
                "kick_start_timeout_minutes must be in [1, 60], got 60.1",
            ),
        ],
    )
    def test_out_of_range_rejected(
        self, field_name: str, value: float, message: str
    ) -> None:
        """Just outside a range raises with the exact message."""
        with pytest.raises(ValueError) as excinfo:
            ControllerConfig(**{field_name: value})  # type: ignore[arg-type]
        assert str(excinfo.value) == message


# ---------------------------------------------------------------------------
# Helpers: kick_step_k, resolve_restart_band, preview_target_c
# ---------------------------------------------------------------------------


class TestHelpers:
    """The kick step on the grid, the band resolver and the gate preview."""

    def test_step_constant(self) -> None:
        """The kick raises the target by 1 K."""
        assert HEATING_KICK_STEP_K == 1.0

    @pytest.mark.parametrize(
        ("step_c", "expected"),
        [
            (0.0, 1.0),
            (-1.0, 1.0),
            (1.0, 1.0),
            (0.5, 1.0),
            (0.25, 1.0),
            (2.0, 2.0),
            (0.3, 1.2),
            (0.4, 1.2),
        ],
    )
    def test_kick_step_k(self, step_c: float, expected: float) -> None:
        """+1 K, or the smallest grid multiple of at least 1 K."""
        assert kick_step_k(step_c) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("entity_k", "expected"),
        [
            (None, (3.0, "knob")),
            (1.0, (1.0, "entity")),
            (10.0, (10.0, "entity")),
            (0.99, (3.0, "knob")),
            (10.01, (3.0, "knob")),
            (float("nan"), (3.0, "knob")),
            (4.5, (4.5, "entity")),
        ],
    )
    def test_resolve_restart_band(
        self, entity_k: float | None, expected: tuple[float, str]
    ) -> None:
        """A plausible entity value wins; otherwise the knob."""
        assert resolve_restart_band(entity_k, 3.0) == expected

    def test_preview_matches_step_and_is_pure(self) -> None:
        """preview_target_c equals the step's target and never moves memory."""
        gate = HeatingSetpointGate(ControllerConfig())
        assert gate.preview_target_c(curve_c=None, step_c=1.0) is None
        assert gate.preview_target_c(curve_c=30.4, step_c=1.0) == 30.0
        first = gate.step(
            curve_c=30.4,
            step_c=1.0,
            pump_setpoint_c=30.0,
            compressor_freq_hz=0.0,
            outlet_c=29.0,
        )
        assert first.target_c == 30.0
        # With memory 30.4 and 0.5 K play, a raw 30.8 stays at 30.4 -> 30.
        assert gate.preview_target_c(curve_c=30.8, step_c=1.0) == 30.0
        # A preview far away must not drag the play memory with it.
        assert gate.preview_target_c(curve_c=35.3, step_c=1.0) == 35.0
        second = gate.step(
            curve_c=30.8,
            step_c=1.0,
            pump_setpoint_c=30.0,
            compressor_freq_hz=0.0,
            outlet_c=29.0,
        )
        assert second.target_c == 30.0
        assert second.hysteresis_c == pytest.approx(30.4)

    @pytest.mark.parametrize(
        ("knobs", "freq_hz", "reason"),
        [
            ({"heating_gate_enabled": False}, 0.0, "ungated_disabled"),
            ({}, None, "ungated_no_input"),
        ],
    )
    def test_ungated_decision_reports_the_play_value(
        self, knobs: dict[str, object], freq_hz: float | None, reason: str
    ) -> None:
        """The ungated path carries the hysteresis value, like the gated one."""
        gate = HeatingSetpointGate(ControllerConfig(**knobs))  # type: ignore[arg-type]
        gate.step(
            curve_c=30.4,
            step_c=1.0,
            pump_setpoint_c=30.0,
            compressor_freq_hz=freq_hz,
            outlet_c=29.0,
        )
        decision = gate.step(
            curve_c=30.8,
            step_c=1.0,
            pump_setpoint_c=30.0,
            compressor_freq_hz=freq_hz,
            outlet_c=29.0,
        )
        assert decision.reason == reason
        assert decision.hysteresis_c == pytest.approx(30.4)
        assert decision.curve_c == pytest.approx(30.8)

    def test_preview_clamps_to_entity_limits(self) -> None:
        """The preview is clamped exactly like the step."""
        gate = HeatingSetpointGate(ControllerConfig())
        assert (
            gate.preview_target_c(curve_c=33.0, step_c=1.0, entity_max_c=31.0) == 31.0
        )
        assert (
            gate.preview_target_c(curve_c=20.0, step_c=1.0, entity_min_c=25.0) == 25.0
        )
        assert (
            gate.preview_target_c(
                curve_c=28.0, step_c=1.0, entity_min_c=25.0, entity_max_c=31.0
            )
            == 28.0
        )


# ---------------------------------------------------------------------------
# Decision validation
# ---------------------------------------------------------------------------


class TestDecision:
    """The frozen decision: vocabularies, offset rule, to_dict."""

    def test_vocabularies(self) -> None:
        """The closed state / reason / result sets."""
        assert frozenset({"off", "ready", "waiting", "kick", "done"}) == (
            HEATING_KICK_STATES
        )
        assert frozenset({"started", "timeout", "cancelled"}) == HEATING_KICK_RESULTS
        assert len(HEATING_KICK_REASONS) == 16
        assert {"fired", "kicking", "blocked_band", "gate_increase"} <= (
            HEATING_KICK_REASONS
        )

    def test_defaults_and_to_dict(self) -> None:
        """Defaults are neutral and to_dict carries every field."""
        decision = HeatingKickDecision(state="off", reason="disabled")
        assert decision.to_dict() == {
            "state": "off",
            "reason": "disabled",
            "offset_k": 0.0,
            "kick_target_c": None,
            "idle_s": None,
            "idle_remaining_s": None,
            "deficit_k": None,
            "timeout_remaining_s": None,
            "kicks_last_24h": 0,
            "last_result": None,
            "last_kick_target_c": None,
            "last_kick_deficit_k": None,
        }

    def test_to_dict_values(self) -> None:
        """Every field maps under its own key."""
        decision = HeatingKickDecision(
            state="kick",
            reason="kicking",
            offset_k=1.5,
            kick_target_c=31.0,
            idle_s=1200.0,
            idle_remaining_s=0.0,
            deficit_k=4.0,
            timeout_remaining_s=60.0,
            kicks_last_24h=2,
            last_result="timeout",
            last_kick_target_c=31.0,
            last_kick_deficit_k=4.0,
        )
        assert decision.to_dict() == {
            "state": "kick",
            "reason": "kicking",
            "offset_k": 1.5,
            "kick_target_c": 31.0,
            "idle_s": 1200.0,
            "idle_remaining_s": 0.0,
            "deficit_k": 4.0,
            "timeout_remaining_s": 60.0,
            "kicks_last_24h": 2,
            "last_result": "timeout",
            "last_kick_target_c": 31.0,
            "last_kick_deficit_k": 4.0,
        }

    def test_bad_state(self) -> None:
        """An unknown state is rejected."""
        with pytest.raises(ValueError, match=r"^state must be one of \[.*got 'x'$"):
            HeatingKickDecision(state="x", reason="disabled")

    def test_bad_reason(self) -> None:
        """An unknown reason is rejected."""
        with pytest.raises(ValueError, match=r"^reason must be one of \[.*got 'x'$"):
            HeatingKickDecision(state="off", reason="x")

    def test_bad_result(self) -> None:
        """An unknown last_result is rejected; None is fine."""
        with pytest.raises(
            ValueError, match=r"^last_result must be one of \[.* or None, got 'x'$"
        ):
            HeatingKickDecision(state="off", reason="disabled", last_result="x")
        for result in sorted(HEATING_KICK_RESULTS):
            ok = HeatingKickDecision(state="off", reason="done", last_result=result)
            assert ok.last_result == result

    def test_negative_offset(self) -> None:
        """A negative offset is rejected."""
        with pytest.raises(ValueError, match=r"^offset_k must be >= 0, got -0.1$"):
            HeatingKickDecision(state="kick", reason="kicking", offset_k=-0.1)

    def test_offset_only_while_kicking(self) -> None:
        """A positive offset outside state 'kick' is rejected; zero is fine."""
        with pytest.raises(
            ValueError, match=r"^offset_k=0.5 is only allowed in state 'kick'$"
        ):
            HeatingKickDecision(state="waiting", reason="idle_wait", offset_k=0.5)
        assert HeatingKickDecision(state="kick", reason="kicking").offset_k == 0.0
        assert (
            HeatingKickDecision(state="kick", reason="fired", offset_k=1.0).offset_k
            == 1.0
        )


# ---------------------------------------------------------------------------
# Not acting: disabled, gate off, inactive, missing input, running
# ---------------------------------------------------------------------------


class TestNotActing:
    """Every condition under which no kick can happen."""

    def test_initial_state(self) -> None:
        """A new machine is off."""
        assert _kick().state == "off"

    def test_disabled_by_default(self) -> None:
        """The default config never kicks."""
        kick = HeatingCompressorKick(ControllerConfig())
        for _ in range(10):
            kick.tick(_IDLE_S)
            decision = _step(kick, outlet_c=20.0)
            assert decision.reason == "disabled"
            assert decision.state == "off"
            assert decision.offset_k == 0.0
            assert decision.idle_s is None

    def test_gate_disabled_never_kicks(self) -> None:
        """The kick needs the #33 write gate."""
        kick = _kick(heating_gate_enabled=False)
        for _ in range(10):
            kick.tick(_IDLE_S)
            decision = _step(kick, outlet_c=20.0)
            assert decision.reason == "gate_disabled"
            assert decision.state == "off"
            assert decision.offset_k == 0.0

    @pytest.mark.parametrize(
        "kwargs",
        [{"active": False}, {"base_c": None}],
    )
    def test_inactive(self, kwargs: dict[str, object]) -> None:
        """Not heating / writes off / no target: inactive."""
        kick = _kick()
        for _ in range(6):
            kick.tick(_IDLE_S)
            decision = _step(kick, outlet_c=20.0, **kwargs)  # type: ignore[arg-type]
            assert decision.reason == "inactive"
            assert decision.state == "off"
            assert decision.idle_s is None

    @pytest.mark.parametrize(
        "kwargs",
        [{"freq_hz": None}, {"outlet_c": None}, {"pump_c": None}],
    )
    def test_missing_input(self, kwargs: dict[str, object]) -> None:
        """Any of frequency / outlet / pump setpoint missing: no kick."""
        kick = _kick()
        for _ in range(6):
            kick.tick(_IDLE_S)
            decision = _step(kick, **kwargs)  # type: ignore[arg-type]
            assert decision.reason == "no_input"
            assert decision.state == "off"
            assert decision.offset_k == 0.0

    def test_deficit_none_without_outlet_or_pump(self) -> None:
        """The reported deficit needs both readings."""
        kick = _kick()
        assert _step(kick, outlet_c=None).deficit_k is None
        assert _step(kick, pump_c=None).deficit_k is None
        assert _step(kick, outlet_c=26.5, pump_c=30.0).deficit_k == pytest.approx(3.5)

    def test_running(self) -> None:
        """A running compressor: ready, nothing counted."""
        kick = _kick()
        decision = _step(kick, freq_hz=0.1)
        assert decision.reason == "running"
        assert decision.state == "ready"
        assert decision.idle_s is None
        assert kick.state == "ready"

    def test_missing_input_restarts_the_idle_count(self) -> None:
        """An idle period cannot be verified across a missing reading."""
        kick = _kick()
        _step(kick)
        kick.tick(600.0)
        assert _step(kick).idle_s == 600.0
        kick.tick(_DT)
        assert _step(kick, freq_hz=None).reason == "no_input"
        kick.tick(_DT)
        again = _step(kick)
        assert again.idle_s == 0.0
        assert again.idle_remaining_s == _IDLE_S

    def test_inactive_restarts_the_idle_count(self) -> None:
        """Leaving HEATING ends the tracked idle period."""
        kick = _kick()
        _step(kick)
        kick.tick(600.0)
        _step(kick, active=False)
        kick.tick(_DT)
        assert _step(kick).idle_s == 0.0


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


class TestTrigger:
    """Idle time, deficit and the gate-shape exclusions."""

    def test_idle_period_starts_at_first_idle_sample(self) -> None:
        """The first idle sample opens the period at 0 s."""
        kick = _kick()
        kick.tick(_DT)  # time before the first sample is not counted
        decision = _step(kick)
        assert decision.reason == "idle_wait"
        assert decision.state == "waiting"
        assert decision.idle_s == 0.0
        assert decision.idle_remaining_s == _IDLE_S
        assert decision.deficit_k == pytest.approx(4.0)

    def test_exact_idle_boundary(self) -> None:
        """899 s idle waits; 900 s fires."""
        kick = _kick()
        _step(kick)
        kick.tick(_IDLE_S - 1.0)
        waiting = _step(kick)
        assert waiting.reason == "idle_wait"
        assert waiting.idle_remaining_s == 1.0
        assert waiting.idle_s == _IDLE_S - 1.0
        kick.tick(1.0)
        assert _step(kick).reason == "fired"

    def test_idle_minutes_knob(self) -> None:
        """The idle threshold follows kick_idle_minutes."""
        kick = _kick(kick_idle_minutes=30.0)
        _step(kick)
        kick.tick(_IDLE_S)
        assert _step(kick).reason == "idle_wait"
        kick.tick(_IDLE_S)
        assert _step(kick).reason == "fired"

    def test_five_minute_cycles(self) -> None:
        """At 5-min cycles the 4th idle sample (15 min) fires."""
        kick = _kick()
        reasons = []
        for _ in range(4):
            reasons.append(_step(kick).reason)
            kick.tick(_DT)
        assert reasons == ["idle_wait", "idle_wait", "idle_wait", "fired"]

    def test_exact_deficit_boundary(self) -> None:
        """3.5 K below fires; 3.4 K waits."""
        kick = _kick()
        short = _fire(kick, outlet_c=26.6)
        assert short.reason == "no_deficit"
        assert short.state == "waiting"
        assert short.idle_remaining_s == 0.0
        assert short.offset_k == 0.0
        assert _step(kick, outlet_c=26.5).reason == "fired"

    def test_deficit_knob(self) -> None:
        """The threshold follows kick_deficit_k."""
        kick = _kick(kick_deficit_k=5.0)
        assert _fire(kick, outlet_c=25.1).reason == "no_deficit"
        assert _step(kick, outlet_c=25.0).reason == "fired"

    def test_hot_outlet_in_dhw_never_kicks(self) -> None:
        """During DHW the outlet is hot: far above the heating target."""
        kick = _kick()
        decision = _fire(kick, outlet_c=48.0)
        assert decision.reason == "no_deficit"
        assert decision.deficit_k == pytest.approx(-18.0)
        for _ in range(6):
            kick.tick(_DT)
            assert _step(kick, outlet_c=48.0).reason == "no_deficit"

    def test_gate_increase_needs_no_kick(self) -> None:
        """A pending increase the gate writes by itself is the release."""
        kick = _kick()
        decision = _fire(kick, base_c=31.0, outlet_c=26.0)
        assert decision.reason == "gate_increase"
        assert decision.state == "waiting"
        assert decision.offset_k == 0.0
        assert decision.idle_remaining_s == 0.0
        assert decision.kick_target_c is None

    def test_gate_increase_band_boundary(self) -> None:
        """outlet == q - band is held by the gate: the kick acts instead."""
        kick = _kick()
        # q 31, band 3 -> the gate writes iff outlet < 28; outlet 26.5 does.
        assert _fire(kick, base_c=31.0, outlet_c=26.5).reason == "gate_increase"
        # A 4.5 K pump band: 31 - 4.5 = 26.5 is not below -> gate holds; the
        # kick target 32 leaves 26.5 < 27.5 -> fires.
        kick2 = _kick()
        fired = _fire(kick2, base_c=31.0, outlet_c=26.5, restart_band_k=4.5)
        assert fired.reason == "fired"
        assert fired.kick_target_c == pytest.approx(32.0)
        assert fired.offset_k == pytest.approx(1.0)

    def test_gate_increase_tolerance_boundary(self) -> None:
        """q - c >= half a step counts as a pending increase."""
        kick = _kick()
        assert _fire(kick, base_c=30.5).reason == "gate_increase"
        kick2 = _kick()
        fired = _fire(kick2, base_c=30.49)
        assert fired.reason == "fired"
        assert fired.kick_target_c == pytest.approx(31.49)

    def test_gate_increase_minimum_tolerance(self) -> None:
        """Without a grid the tolerance is 0.05 K, not 0."""
        kick = _kick()
        assert _fire(kick, base_c=30.05, step_c=0.0).reason == "gate_increase"
        kick2 = _kick()
        assert _fire(kick2, base_c=30.04, step_c=0.0).reason == "fired"

    def test_blocked_by_heating_supply_max(self) -> None:
        """K above heating_supply_max_c is not kicked; exactly at it is."""
        kick = _kick()
        at_max = _fire(kick, pump_c=39.0, base_c=39.0, outlet_c=35.0)
        assert at_max.reason == "fired"
        assert at_max.kick_target_c == 40.0
        kick2 = _kick()
        over = _fire(kick2, pump_c=39.5, base_c=39.5, outlet_c=35.0, step_c=0.5)
        assert over.reason == "blocked_limit"
        assert over.state == "waiting"
        assert over.idle_remaining_s == 0.0

    def test_blocked_by_lower_supply_max_knob(self) -> None:
        """A lower heating_supply_max_c caps the kick."""
        kick = _kick(heating_supply_max_c=30.0, heating_supply_base_c=26.0)
        assert _fire(kick).reason == "blocked_limit"

    def test_blocked_by_entity_max(self) -> None:
        """The entity max caps the kick; exactly at it fires."""
        kick = _kick()
        assert _fire(kick, entity_max_c=31.0).reason == "fired"
        kick2 = _kick()
        assert _fire(kick2, entity_max_c=30.9).reason == "blocked_limit"
        # An entity max above heating_supply_max_c does not lift that cap.
        kick3 = _kick()
        assert (
            _fire(
                kick3,
                pump_c=39.5,
                base_c=39.5,
                outlet_c=35.0,
                step_c=0.5,
                entity_max_c=55.0,
            ).reason
            == "blocked_limit"
        )

    def test_blocked_by_restart_band_entity(self) -> None:
        """outlet == K - band would be held by the gate: no kick."""
        kick = _kick()
        # K = 31; band 4.5 -> 26.5 is not < 26.5.
        blocked = _fire(kick, outlet_c=26.5, restart_band_k=4.5)
        assert blocked.reason == "blocked_band"
        assert blocked.state == "waiting"
        assert blocked.offset_k == 0.0
        assert blocked.idle_remaining_s == 0.0
        assert _step(kick, outlet_c=26.5, restart_band_k=4.4).reason == "fired"

    def test_blocked_by_restart_band_knob(self) -> None:
        """The knob band applies without (or with an implausible) entity."""
        kick = _kick(heating_restart_band_k=4.5)
        assert _fire(kick, outlet_c=26.5).reason == "blocked_band"
        assert _step(kick, outlet_c=26.5, restart_band_k=0.5).reason == ("blocked_band")
        assert _step(kick, outlet_c=26.4).reason == "fired"

    def test_deficit_larger_than_band_band_check_still_applies(self) -> None:
        """A band above deficit + step blocks until the outlet falls further."""
        kick = _kick(heating_restart_band_k=6.0)
        assert _fire(kick, outlet_c=25.0).reason == "blocked_band"
        kick.tick(_DT)
        assert _step(kick, outlet_c=24.9).reason == "fired"


# ---------------------------------------------------------------------------
# Fire, kicking, start, timeout, cancel
# ---------------------------------------------------------------------------


class TestKickLifecycle:
    """From the fired cycle to its end."""

    def test_fired_decision(self) -> None:
        """The fired cycle carries the raised target and the full timeout."""
        kick = _kick()
        fired = _fire(kick)
        assert fired.state == "kick"
        assert fired.reason == "fired"
        assert fired.kick_target_c == 31.0
        assert fired.offset_k == 1.0
        assert fired.timeout_remaining_s == 300.0
        assert fired.kicks_last_24h == 1
        assert fired.last_kick_target_c == 31.0
        assert fired.last_kick_deficit_k == pytest.approx(4.0)
        assert fired.last_result is None
        assert fired.idle_s == _IDLE_S
        assert kick.state == "kick"

    def test_fired_above_a_held_decrease(self) -> None:
        """With q below the pump's setpoint, K = c + 1 and offset = K - q."""
        kick = _kick()
        fired = _fire(kick, pump_c=30.0, base_c=28.0)
        assert fired.kick_target_c == 31.0
        assert fired.offset_k == 3.0

    def test_fired_uses_grid_step(self) -> None:
        """A 2 K grid raises by one step."""
        kick = _kick()
        fired = _fire(kick, outlet_c=25.0, step_c=2.0)
        assert fired.kick_target_c == 32.0
        assert fired.offset_k == 2.0

    def test_kicking_until_timeout(self) -> None:
        """299 s later still kicking; 300 s -> timeout, offset removed."""
        kick = _kick()
        _fire(kick)
        kick.tick(299.0)
        kicking = _step(kick)
        assert kicking.reason == "kicking"
        assert kicking.state == "kick"
        assert kicking.offset_k == 1.0
        assert kicking.kick_target_c == 31.0
        assert kicking.timeout_remaining_s == 1.0
        kick.tick(1.0)
        timeout = _step(kick)
        assert timeout.reason == "timeout"
        assert timeout.state == "done"
        assert timeout.offset_k == 0.0
        assert timeout.kick_target_c is None
        assert timeout.last_result == "timeout"
        assert timeout.timeout_remaining_s is None

    def test_timeout_knob(self) -> None:
        """The timeout follows kick_start_timeout_minutes."""
        kick = _kick(kick_start_timeout_minutes=10.0)
        fired = _fire(kick)
        assert fired.timeout_remaining_s == 600.0
        kick.tick(_DT)
        assert _step(kick).reason == "kicking"
        kick.tick(_DT)
        assert _step(kick).reason == "timeout"

    def test_kicking_offset_tracks_the_normal_target(self) -> None:
        """K stays fixed while q moves; the offset never goes negative."""
        kick = _kick()
        _fire(kick)
        kick.tick(60.0)
        assert _step(kick, base_c=29.0).offset_k == 2.0
        kick.tick(60.0)
        above = _step(kick, base_c=32.0)
        assert above.reason == "kicking"
        assert above.offset_k == 0.0
        assert above.kick_target_c == 31.0

    def test_started(self) -> None:
        """A start ends the kick: offset removed, result 'started'."""
        kick = _kick()
        _fire(kick)
        kick.tick(_DT)
        started = _step(kick, freq_hz=0.1, pump_c=31.0)
        assert started.reason == "started"
        assert started.state == "ready"
        assert started.offset_k == 0.0
        assert started.kick_target_c is None
        assert started.last_result == "started"
        assert started.idle_s is None
        assert started.kicks_last_24h == 1

    def test_start_beats_timeout(self) -> None:
        """A start seen at the timeout cycle counts as started."""
        kick = _kick()
        _fire(kick)
        kick.tick(10 * _DT)
        assert _step(kick, freq_hz=40.0).reason == "started"

    def test_one_kick_per_idle_period(self) -> None:
        """After a timeout the same idle period never kicks again."""
        kick = _kick()
        _fire(kick)
        kick.tick(_DT)
        assert _step(kick).reason == "timeout"
        for _ in range(24):
            kick.tick(_DT)
            done = _step(kick, outlet_c=20.0, pump_c=31.0)
            assert done.reason == "done"
            assert done.state == "done"
            assert done.offset_k == 0.0
        assert done.kicks_last_24h == 1

    def test_a_run_rearms_the_kick(self) -> None:
        """Once the compressor has run, the next idle period may kick."""
        kick = _kick()
        _fire(kick)
        kick.tick(_DT)
        _step(kick)  # timeout
        kick.tick(_DT)
        assert _step(kick, freq_hz=30.0).reason == "running"
        kick.tick(_DT)
        second = _fire(kick)
        assert second.reason == "fired"
        assert second.kicks_last_24h == 2
        assert second.last_result == "timeout"

    def test_started_rearms_the_kick(self) -> None:
        """After a successful kick the next idle period may kick again."""
        kick = _kick()
        _fire(kick)
        kick.tick(_DT)
        _step(kick, freq_hz=30.0)
        kick.tick(_DT)
        assert _fire(kick).reason == "fired"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"active": False},
            {"base_c": None},
            {"freq_hz": None},
            {"outlet_c": None},
            {"pump_c": None},
        ],
    )
    def test_cancelled(self, kwargs: dict[str, object]) -> None:
        """Lost conditions cancel the kick; no second kick in the period."""
        kick = _kick()
        _fire(kick)
        kick.tick(60.0)
        cancelled = _step(kick, **kwargs)  # type: ignore[arg-type]
        assert cancelled.reason == "cancelled"
        assert cancelled.state == "off"
        assert cancelled.offset_k == 0.0
        assert cancelled.kick_target_c is None
        assert cancelled.last_result == "cancelled"
        assert cancelled.idle_s is None
        kick.tick(_IDLE_S)
        assert _step(kick).reason == "done"
        kick.tick(_IDLE_S)
        assert _step(kick).reason == "done"

    def test_timer_only_runs_while_kicking(self) -> None:
        """Time before the kick does not shorten its timeout."""
        kick = _kick()
        _step(kick)
        kick.tick(10 * _IDLE_S)
        fired = _step(kick)
        assert fired.reason == "fired"
        kick.tick(_DT - 1.0)
        assert _step(kick).reason == "kicking"

    def test_second_kick_gets_a_full_timeout(self) -> None:
        """The kick timer restarts with every kick."""
        kick = _kick()
        _fire(kick)
        kick.tick(_DT - 1.0)
        _step(kick, freq_hz=30.0)  # started after 299 s
        kick.tick(_DT)
        _fire(kick)
        kick.tick(_DT - 1.0)
        assert _step(kick).reason == "kicking"


class TestRollingCounter:
    """kicks_last_24h is a rolling 24 h window."""

    def test_window_boundary(self) -> None:
        """A kick drops out exactly 24 h later."""
        kick = _kick()
        _fire(kick)
        kick.tick(_DT)
        _step(kick, freq_hz=30.0)
        kick.tick(86400.0 - _DT - 1.0)
        assert _step(kick, freq_hz=30.0).kicks_last_24h == 1
        kick.tick(1.0)
        assert _step(kick, freq_hz=30.0).kicks_last_24h == 0


# ---------------------------------------------------------------------------
# Kick + gate end to end
# ---------------------------------------------------------------------------


class _Pipeline:
    """The coordinator order: kick tick/step, then gate tick/step."""

    def __init__(self, **knobs: object) -> None:
        knobs.setdefault("heat_kick_enabled", True)
        cfg = ControllerConfig(**knobs)  # type: ignore[arg-type]
        self.kick = HeatingCompressorKick(cfg)
        self.gate = HeatingSetpointGate(cfg)
        self.pump_c = 30.0

    def cycle(
        self,
        *,
        curve_c: float = 30.0,
        freq_hz: float = 0.0,
        outlet_c: float = 26.0,
        dt: float = _DT,
    ) -> tuple[HeatingKickDecision, float | None, str]:
        """One cycle; a written value is applied by the pump at once."""
        self.kick.tick(dt)
        decision = self.kick.step(
            heating_active=True,
            compressor_freq_hz=freq_hz,
            outlet_c=outlet_c,
            pump_setpoint_c=self.pump_c,
            base_target_c=self.gate.preview_target_c(curve_c=curve_c, step_c=1.0),
            step_c=1.0,
        )
        self.gate.tick(dt)
        gate = self.gate.step(
            curve_c=curve_c,
            step_c=1.0,
            pump_setpoint_c=self.pump_c,
            compressor_freq_hz=freq_hz,
            outlet_c=outlet_c,
            offset_k=decision.offset_k,
        )
        if gate.write_c is not None:
            self.pump_c = gate.write_c
        return decision, gate.write_c, gate.reason


class TestKickThroughGate:
    """The kick write and its restore are ordinary gate decisions."""

    def test_kick_written_as_idle_increase_then_restored_while_running(
        self,
    ) -> None:
        """Fire -> write 31 (increase idle); start -> write 30 (decrease running)."""
        p = _Pipeline()
        writes = []
        for _ in range(3):
            kick, write, reason = p.cycle()
            assert kick.reason == "idle_wait"
            assert reason == "in_sync"
            writes.append(write)
        kick, write, reason = p.cycle()
        assert kick.reason == "fired"
        assert reason == "write_increase_idle"
        assert write == 31.0
        kick, write, reason = p.cycle(freq_hz=35.0, outlet_c=27.0)
        assert kick.reason == "started"
        assert reason == "write_decrease_running"
        assert write == 30.0
        assert writes == [None, None, None]

    def test_restore_waits_for_the_decrease_rule(self) -> None:
        """A start with a hot outlet: the restore is held until it cools."""
        p = _Pipeline()
        for _ in range(4):
            kick, _w, _r = p.cycle()
        assert kick.reason == "fired"
        # Outlet 32 >= 30 + 2: writing 30 now would stop the run.
        kick, write, reason = p.cycle(freq_hz=35.0, outlet_c=32.0)
        assert kick.reason == "started"
        assert reason == "hold_decrease_running"
        assert write is None
        assert p.pump_c == 31.0
        _kick_d, write, reason = p.cycle(freq_hz=35.0, outlet_c=31.9)
        assert reason == "write_decrease_running"
        assert write == 30.0

    def test_failed_kick_restore_is_held_while_idle(self) -> None:
        """Timeout: the lower target is never written as an idle step below o."""
        p = _Pipeline()
        for _ in range(4):
            p.cycle()
        kick, write, reason = p.cycle()
        assert kick.reason == "timeout"
        assert reason == "hold_decrease_idle"
        assert write is None
        assert p.pump_c == 31.0
        for _ in range(12):
            kick, write, reason = p.cycle(outlet_c=25.0)
            assert kick.reason == "done"
            assert reason == "hold_decrease_idle"
            assert write is None
        # Once the compressor runs, the restore is an ordinary decrease.
        kick, write, reason = p.cycle(freq_hz=30.0, outlet_c=25.5)
        assert kick.reason == "running"
        assert reason == "write_decrease_running"
        assert write == 30.0

    def test_failed_kick_restore_written_when_outlet_reaches_target(self) -> None:
        """An idle decrease with the outlet at the target is allowed (#33)."""
        p = _Pipeline()
        for _ in range(5):
            p.cycle()
        _kick_d, write, reason = p.cycle(outlet_c=30.0)
        assert reason == "write_decrease_idle"
        assert write == 30.0

    def test_disabled_kick_leaves_the_gate_alone(self) -> None:
        """heat_kick_enabled off: the lockout shape is simply held as before."""
        p = _Pipeline(heat_kick_enabled=False)
        for _ in range(12):
            kick, write, reason = p.cycle(outlet_c=24.0)
            assert kick.reason == "disabled"
            assert reason == "in_sync"
            assert write is None
        assert p.pump_c == 30.0
