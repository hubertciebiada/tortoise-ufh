"""Unit tests for the heating-setpoint write gate (issue #33, 2026-10-22).

Exercises :class:`~tortoise_ufh.hp_link.HeatingSetpointGate` — the pure state
machine that holds a heating water target change while the compressor state
makes the step unsafe (Panasonic restart lockout) — and its knobs on
:class:`~tortoise_ufh.config.ControllerConfig`.

Covered: hysteresis (play operator) before quantization, quantization to the
entity step and clamping to the entity limits, the four running/idle x
increase/decrease rules incl. their exact boundaries, ``in_sync`` (no
re-assert), the unknown-current fallback, the ungated fallbacks (gate off /
missing input), the restart-band entity override and its plausibility
fallback, the hold timer, the #34 offset hook and the decision validation.

Never imports ``homeassistant``; time advances only through ``tick``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.hp_link import (
    HEATING_GATE_REASONS,
    HeatingGateDecision,
    HeatingSetpointGate,
)

pytestmark = pytest.mark.unit

_DT: float = 300.0


def _gate(**knobs: object) -> HeatingSetpointGate:
    """A gate built from a config with the given knob overrides."""
    return HeatingSetpointGate(ControllerConfig(**knobs))  # type: ignore[arg-type]


def _step(
    gate: HeatingSetpointGate,
    *,
    curve_c: float | None = 30.0,
    step_c: float = 1.0,
    pump_setpoint_c: float | None = 29.0,
    freq_hz: float | None = 0.0,
    outlet_c: float | None = 29.0,
    **extra: object,
) -> HeatingGateDecision:
    """One gate step with defaults: curve 30, pump at 29, compressor idle."""
    return gate.step(
        curve_c=curve_c,
        step_c=step_c,
        pump_setpoint_c=pump_setpoint_c,
        compressor_freq_hz=freq_hz,
        outlet_c=outlet_c,
        **extra,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------


class TestGateKnobs:
    """The four global-only gate knobs and their validation."""

    def test_defaults(self) -> None:
        """Gate on, 0.5 K hysteresis, 3 K restart band, 2 K stop offset."""
        cfg = ControllerConfig()
        assert cfg.heating_gate_enabled is True
        assert cfg.heating_curve_hysteresis_k == 0.5
        assert cfg.heating_restart_band_k == 3.0
        assert cfg.heating_stop_offset_k == 2.0

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("heating_curve_hysteresis_k", 0.0),
            ("heating_curve_hysteresis_k", 2.0),
            ("heating_restart_band_k", 1.0),
            ("heating_restart_band_k", 10.0),
            ("heating_stop_offset_k", 0.5),
            ("heating_stop_offset_k", 5.0),
        ],
    )
    def test_boundaries_allowed(self, field_name: str, value: float) -> None:
        """Inclusive range boundaries construct cleanly."""
        cfg = ControllerConfig(**{field_name: value})  # type: ignore[arg-type]
        assert getattr(cfg, field_name) == value

    @pytest.mark.parametrize(
        ("field_name", "value", "message"),
        [
            (
                "heating_curve_hysteresis_k",
                -0.1,
                "heating_curve_hysteresis_k must be in [0, 2], got -0.1",
            ),
            (
                "heating_curve_hysteresis_k",
                2.1,
                "heating_curve_hysteresis_k must be in [0, 2], got 2.1",
            ),
            (
                "heating_restart_band_k",
                0.5,
                "heating_restart_band_k must be in [1, 10], got 0.5",
            ),
            (
                "heating_restart_band_k",
                10.5,
                "heating_restart_band_k must be in [1, 10], got 10.5",
            ),
            (
                "heating_stop_offset_k",
                0.4,
                "heating_stop_offset_k must be in [0.5, 5], got 0.4",
            ),
            (
                "heating_stop_offset_k",
                5.5,
                "heating_stop_offset_k must be in [0.5, 5], got 5.5",
            ),
        ],
    )
    def test_out_of_range_exact_message(
        self, field_name: str, value: float, message: str
    ) -> None:
        """Each knob outside its range raises with the exact message."""
        with pytest.raises(ValueError) as exc_info:
            ControllerConfig(**{field_name: value})  # type: ignore[arg-type]
        assert str(exc_info.value) == message


# ---------------------------------------------------------------------------
# Hysteresis + quantization
# ---------------------------------------------------------------------------


class TestHysteresisAndQuantization:
    """Item 3: the target must not toggle at a rounding boundary."""

    def test_first_value_is_rounded_to_the_entity_step(self) -> None:
        """The first curve value seeds the play operator, then rounds."""
        d = _step(_gate(), curve_c=30.4, step_c=1.0, pump_setpoint_c=None)
        assert d.hysteresis_c == pytest.approx(30.4)
        assert d.target_c == pytest.approx(30.0)

    def test_half_step_grid(self) -> None:
        """A 0.5 K entity step quantizes onto the 0.5 grid."""
        d = _step(_gate(), curve_c=30.3, step_c=0.5, pump_setpoint_c=None)
        assert d.target_c == pytest.approx(30.5)

    def test_flicker_at_rounding_boundary_does_not_toggle(self) -> None:
        """A curve wobbling 30.45 <-> 30.55 keeps one target with 0.5 K play."""
        gate = _gate()
        targets = [
            _step(gate, curve_c=c, pump_setpoint_c=None).target_c
            for c in (30.45, 30.55, 30.45, 30.6, 30.4, 30.55)
        ]
        assert targets == [30.0] * 6

    def test_without_hysteresis_the_target_toggles(self) -> None:
        """With the knob at 0 the same wobble toggles (the pre-#33 problem)."""
        gate = _gate(heating_curve_hysteresis_k=0.0)
        targets = [
            _step(gate, curve_c=c, pump_setpoint_c=None).target_c
            for c in (30.45, 30.55, 30.45)
        ]
        assert targets == [30.0, 31.0, 30.0]

    def test_target_moves_once_the_curve_clears_the_play(self) -> None:
        """Up at curve >= 31.0 (boundary 30.5 + 0.5), down at <= 30.0."""
        gate = _gate()
        assert _step(gate, curve_c=30.2, pump_setpoint_c=None).target_c == 30.0
        assert _step(gate, curve_c=30.9, pump_setpoint_c=None).target_c == 30.0
        assert _step(gate, curve_c=31.0, pump_setpoint_c=None).target_c == 31.0
        assert _step(gate, curve_c=30.1, pump_setpoint_c=None).target_c == 31.0
        assert _step(gate, curve_c=29.9, pump_setpoint_c=None).target_c == 30.0

    def test_no_target_resets_the_play_memory(self) -> None:
        """Leaving HEATING (curve None) forgets the play position."""
        gate = _gate()
        _step(gate, curve_c=31.0, pump_setpoint_c=None)
        d = _step(gate, curve_c=None)
        assert d.reason == "no_target"
        assert d.target_c is None and d.write_c is None
        assert _step(gate, curve_c=30.4, pump_setpoint_c=None).hysteresis_c == 30.4

    def test_entity_limits_clamp_the_target(self) -> None:
        """The target never leaves the entity's own min/max (no rejection)."""
        d = _step(
            _gate(heating_gate_enabled=False),
            curve_c=40.0,
            entity_min_c=20.0,
            entity_max_c=35.0,
        )
        assert d.target_c == 35.0
        assert d.write_c == 35.0
        d = _step(_gate(heating_gate_enabled=False), curve_c=20.0, entity_min_c=25.0)
        assert d.target_c == 25.0

    def test_hysteresis_applies_while_ungated(self) -> None:
        """The play operator works with the gate disabled too."""
        gate = _gate(heating_gate_enabled=False)
        for curve in (30.45, 30.55, 30.45):
            d = _step(gate, curve_c=curve)
            assert d.reason == "ungated_disabled"
            assert d.write_c == 30.0


# ---------------------------------------------------------------------------
# Compressor-state rules
# ---------------------------------------------------------------------------


class TestRunningRules:
    """Compressor running (frequency > 0)."""

    def test_increase_is_written_at_once(self) -> None:
        """A running increase writes even with a hot outlet."""
        d = _step(
            _gate(), curve_c=31.0, pump_setpoint_c=30.0, freq_hz=40.0, outlet_c=35.0
        )
        assert d.reason == "write_increase_running"
        assert d.write_c == 31.0
        assert d.gate_active and d.compressor_running is True and not d.held

    def test_decrease_held_while_outlet_would_stop_the_run(self) -> None:
        """outlet >= new target + 2 K holds the decrease (boundary inclusive)."""
        d = _step(
            _gate(), curve_c=29.0, pump_setpoint_c=30.0, freq_hz=40.0, outlet_c=31.0
        )
        assert d.reason == "hold_decrease_running"
        assert d.write_c is None and d.held

    def test_decrease_written_once_outlet_below_target_plus_offset(self) -> None:
        """outlet < new target + 2 K writes the decrease."""
        d = _step(
            _gate(), curve_c=29.0, pump_setpoint_c=30.0, freq_hz=40.0, outlet_c=30.9
        )
        assert d.reason == "write_decrease_running"
        assert d.write_c == 29.0

    def test_stop_offset_knob_is_used(self) -> None:
        """A 1 K stop offset holds at outlet 30.0 for a 29 target."""
        d = _step(
            _gate(heating_stop_offset_k=1.0),
            curve_c=29.0,
            pump_setpoint_c=30.0,
            freq_hz=40.0,
            outlet_c=30.0,
        )
        assert d.reason == "hold_decrease_running"
        assert d.stop_offset_k == 1.0


class TestIdleRules:
    """Compressor idle (frequency 0)."""

    def test_increase_written_when_it_starts_the_compressor(self) -> None:
        """outlet < new target - 3 K writes the increase (it starts at once)."""
        d = _step(_gate(), curve_c=31.0, pump_setpoint_c=30.0, outlet_c=27.9)
        assert d.reason == "write_increase_idle"
        assert d.write_c == 31.0
        assert d.compressor_running is False

    @pytest.mark.parametrize("outlet", [28.0, 29.5, 30.5, 33.0])
    def test_increase_held_inside_or_above_the_band(self, outlet: float) -> None:
        """outlet >= new target - 3 K holds the increase (the lockout shape)."""
        d = _step(_gate(), curve_c=31.0, pump_setpoint_c=30.0, outlet_c=outlet)
        assert d.reason == "hold_increase_idle"
        assert d.write_c is None and d.held
        assert d.target_c == 31.0 and d.pump_setpoint_c == 30.0

    @pytest.mark.parametrize("outlet", [29.0, 31.0])
    def test_decrease_written_when_outlet_at_or_above(self, outlet: float) -> None:
        """outlet >= new target writes the decrease (boundary inclusive)."""
        d = _step(_gate(), curve_c=29.0, pump_setpoint_c=30.0, outlet_c=outlet)
        assert d.reason == "write_decrease_idle"
        assert d.write_c == 29.0

    def test_decrease_held_when_outlet_below(self) -> None:
        """outlet < new target holds the decrease."""
        d = _step(_gate(), curve_c=29.0, pump_setpoint_c=30.0, outlet_c=28.9)
        assert d.reason == "hold_decrease_idle"
        assert d.write_c is None

    def test_unchanged_setpoint_is_not_reasserted(self) -> None:
        """Target equal to the pump setpoint: nothing to write (item 4)."""
        d = _step(_gate(), curve_c=30.0, pump_setpoint_c=30.0, outlet_c=26.0)
        assert d.reason == "in_sync"
        assert d.write_c is None and not d.held

    def test_in_sync_tolerance_is_half_a_step(self) -> None:
        """A pump value off-grid by < half a step counts as in sync."""
        d = _step(_gate(), curve_c=30.0, pump_setpoint_c=30.4, outlet_c=26.0)
        assert d.reason == "in_sync"

    def test_held_increase_released_when_outlet_cools(self) -> None:
        """Held while 28 <= outlet, written the cycle outlet drops below 28."""
        gate = _gate()
        outlets = (29.5, 28.6, 28.0, 27.8)
        reasons = []
        for outlet in outlets:
            gate.tick(_DT)
            reasons.append(
                _step(gate, curve_c=31.0, pump_setpoint_c=30.0, outlet_c=outlet).reason
            )
        assert reasons == ["hold_increase_idle"] * 3 + ["write_increase_idle"]

    def test_held_increase_written_once_compressor_runs(self) -> None:
        """The pump restarting on its own releases a held increase."""
        gate = _gate()
        assert _step(gate, curve_c=31.0, pump_setpoint_c=30.0, outlet_c=29.0).held
        d = _step(gate, curve_c=31.0, pump_setpoint_c=30.0, freq_hz=25.0, outlet_c=27.5)
        assert d.reason == "write_increase_running"

    def test_restart_band_knob_is_used(self) -> None:
        """A 2 K band writes at outlet 28.9 for a 31 target."""
        d = _step(
            _gate(heating_restart_band_k=2.0),
            curve_c=31.0,
            pump_setpoint_c=30.0,
            outlet_c=28.9,
        )
        assert d.reason == "write_increase_idle"
        assert d.restart_band_k == 2.0 and d.restart_band_source == "knob"


class TestUnknownPumpSetpoint:
    """No readable pump setpoint: only a step safe either way is written."""

    def test_running_writes_below_stop_threshold(self) -> None:
        d = _step(_gate(), pump_setpoint_c=None, freq_hz=30.0, outlet_c=31.9)
        assert d.reason == "write_unknown_running"

    def test_running_holds_at_stop_threshold(self) -> None:
        d = _step(_gate(), pump_setpoint_c=None, freq_hz=30.0, outlet_c=32.0)
        assert d.reason == "hold_unknown_running"

    def test_idle_writes_only_when_it_starts(self) -> None:
        assert _step(_gate(), pump_setpoint_c=None, outlet_c=26.9).reason == (
            "write_unknown_idle"
        )
        assert _step(_gate(), pump_setpoint_c=None, outlet_c=27.0).reason == (
            "hold_unknown_idle"
        )


class TestUngatedFallback:
    """Gate off or an input missing: write as before #33."""

    @pytest.mark.parametrize(
        ("freq", "outlet"), [(None, 29.0), (0.0, None), (None, None)]
    )
    def test_missing_input_is_ungated(
        self, freq: float | None, outlet: float | None
    ) -> None:
        d = _step(
            _gate(), curve_c=31.0, pump_setpoint_c=30.0, freq_hz=freq, outlet_c=outlet
        )
        assert d.reason == "ungated_no_input"
        assert d.write_c == 31.0
        assert not d.gate_active and not d.held

    def test_disabled_gate_writes_the_held_shape(self) -> None:
        """An idle in-band increase is written when the gate is off."""
        d = _step(
            _gate(heating_gate_enabled=False),
            curve_c=31.0,
            pump_setpoint_c=30.0,
            outlet_c=29.0,
        )
        assert d.reason == "ungated_disabled"
        assert d.write_c == 31.0

    def test_compressor_reading_exposed_while_ungated(self) -> None:
        d = _step(_gate(), freq_hz=12.0, outlet_c=None)
        assert d.compressor_running is True


class TestRestartBandEntity:
    """The optional pump entity overrides the knob when plausible."""

    def test_entity_value_overrides_knob(self) -> None:
        d = _step(
            _gate(),
            curve_c=31.0,
            pump_setpoint_c=30.0,
            outlet_c=26.5,
            restart_band_k=5.0,
        )
        assert d.restart_band_source == "entity"
        assert d.restart_band_k == 5.0
        assert d.reason == "hold_increase_idle"  # 26.5 >= 31 - 5

    @pytest.mark.parametrize("value", [0.0, 0.5, 10.5, float("nan"), None])
    def test_implausible_entity_value_falls_back(self, value: float | None) -> None:
        d = _step(_gate(), restart_band_k=value)
        assert d.restart_band_source == "knob"
        assert d.restart_band_k == 3.0


class TestHoldTimerAndOffset:
    """The hold timer and the issue #34 offset hook."""

    def test_hold_timer_accumulates_and_resets(self) -> None:
        gate = _gate()
        held = []
        for outlet in (29.0, 29.0, 29.0, 27.0, 29.0):
            gate.tick(_DT)
            held.append(
                _step(
                    gate, curve_c=31.0, pump_setpoint_c=30.0, outlet_c=outlet
                ).held_for_s
            )
        assert held == [0.0, 300.0, 600.0, None, 0.0]

    def test_offset_obeys_the_same_rules(self) -> None:
        """A +1 K kick on an idle in-band pump is held; its restore is a
        decrease and obeys the decrease rule."""
        gate = _gate()
        kick = _step(
            gate, curve_c=30.0, pump_setpoint_c=30.0, outlet_c=26.0, offset_k=1.0
        )
        assert kick.target_c == 31.0
        assert kick.reason == "write_increase_idle"
        restore = _step(
            gate, curve_c=30.0, pump_setpoint_c=31.0, freq_hz=30.0, outlet_c=32.5
        )
        assert restore.reason == "hold_decrease_running"
        restore = _step(
            gate, curve_c=30.0, pump_setpoint_c=31.0, freq_hz=30.0, outlet_c=31.5
        )
        assert restore.reason == "write_decrease_running"


class TestDecisionValidation:
    """HeatingGateDecision vocabulary + JSON."""

    def _kwargs(self) -> dict[str, object]:
        return {
            "reason": "in_sync",
            "target_c": 30.0,
            "write_c": None,
            "pump_setpoint_c": 30.0,
            "curve_c": 30.0,
            "hysteresis_c": 30.0,
            "gate_active": True,
            "held": False,
            "compressor_running": False,
            "outlet_c": 28.0,
            "restart_band_k": 3.0,
            "restart_band_source": "knob",
            "stop_offset_k": 2.0,
        }

    def test_unknown_reason_rejected(self) -> None:
        kwargs = self._kwargs() | {"reason": "bogus"}
        with pytest.raises(ValueError, match="reason must be one of"):
            HeatingGateDecision(**kwargs)  # type: ignore[arg-type]

    def test_unknown_band_source_rejected(self) -> None:
        kwargs = self._kwargs() | {"restart_band_source": "pump"}
        with pytest.raises(ValueError, match="restart_band_source"):
            HeatingGateDecision(**kwargs)  # type: ignore[arg-type]

    def test_held_must_match_reason(self) -> None:
        kwargs = self._kwargs() | {"held": True}
        with pytest.raises(ValueError, match="disagrees"):
            HeatingGateDecision(**kwargs)  # type: ignore[arg-type]

    def test_to_dict_is_json_ready(self) -> None:
        import json

        d = HeatingGateDecision(**self._kwargs())  # type: ignore[arg-type]
        assert json.loads(json.dumps(d.to_dict()))["reason"] == "in_sync"

    def test_every_reason_is_reachable_vocabulary(self) -> None:
        """Every reason the gate can emit is in the closed vocabulary."""
        assert {
            "no_target",
            "in_sync",
            "ungated_disabled",
            "ungated_no_input",
        } <= HEATING_GATE_REASONS
        assert all(
            r.startswith(("write_", "hold_"))
            for r in HEATING_GATE_REASONS
            - {"no_target", "in_sync", "ungated_disabled", "ungated_no_input"}
        )


class TestExactEdges:
    """Pinned edges and exact payloads (mutation hardening)."""

    def test_to_dict_exact(self) -> None:
        d = _step(_gate(), curve_c=31.0, pump_setpoint_c=30.0, outlet_c=29.0)
        assert d.to_dict() == {
            "reason": "hold_increase_idle",
            "target_c": 31.0,
            "write_c": None,
            "pump_setpoint_c": 30.0,
            "curve_c": 31.0,
            "hysteresis_c": 31.0,
            "gate_active": True,
            "held": True,
            "compressor_running": False,
            "outlet_c": 29.0,
            "restart_band_k": 3.0,
            "restart_band_source": "knob",
            "stop_offset_k": 2.0,
            "held_for_s": 0.0,
        }

    def test_no_target_decision_exact(self) -> None:
        d = _step(_gate(), curve_c=None, freq_hz=None, outlet_c=None)
        assert d.to_dict() == {
            "reason": "no_target",
            "target_c": None,
            "write_c": None,
            "pump_setpoint_c": 29.0,
            "curve_c": None,
            "hysteresis_c": None,
            "gate_active": False,
            "held": False,
            "compressor_running": None,
            "outlet_c": None,
            "restart_band_k": 3.0,
            "restart_band_source": "knob",
            "stop_offset_k": 2.0,
            "held_for_s": None,
        }

    def test_exact_validation_messages(self) -> None:
        base = TestDecisionValidation()._kwargs()
        with pytest.raises(ValueError) as exc_info:
            HeatingGateDecision(**(base | {"reason": "bogus"}))  # type: ignore[arg-type]
        assert str(exc_info.value) == (
            f"reason must be one of {sorted(HEATING_GATE_REASONS)}, got 'bogus'"
        )
        with pytest.raises(ValueError) as exc_info:
            HeatingGateDecision(
                **(base | {"restart_band_source": "pump"})  # type: ignore[arg-type]
            )
        assert str(exc_info.value) == (
            "restart_band_source must be one of ['entity', 'knob'], got 'pump'"
        )
        with pytest.raises(ValueError) as exc_info:
            HeatingGateDecision(**(base | {"held": True}))  # type: ignore[arg-type]
        assert str(exc_info.value) == "held=True disagrees with reason 'in_sync'"
        assert HeatingGateDecision(**base).held_for_s is None  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [1.0, 10.0])
    def test_band_entity_boundaries_accepted(self, value: float) -> None:
        d = _step(_gate(), restart_band_k=value)
        assert d.restart_band_source == "entity"
        assert d.restart_band_k == value

    @pytest.mark.parametrize("value", [0.99, 10.01, float("inf"), float("-inf")])
    def test_band_entity_just_outside_rejected(self, value: float) -> None:
        assert _step(_gate(), restart_band_k=value).restart_band_source == "knob"

    def test_compressor_running_threshold(self) -> None:
        assert _step(_gate(), freq_hz=0.0).compressor_running is False
        assert _step(_gate(), freq_hz=0.1).compressor_running is True

    def test_no_grid_uses_minimum_sync_tolerance(self) -> None:
        """step <= 0: no quantization and a 0.05 K same-setpoint tolerance."""
        gate = _gate(heating_curve_hysteresis_k=0.0)
        d = _step(gate, curve_c=30.3, step_c=0.0, pump_setpoint_c=30.26)
        assert d.target_c == pytest.approx(30.3)
        assert d.reason == "in_sync"
        d = _step(gate, curve_c=30.3, step_c=0.0, pump_setpoint_c=30.25)
        assert d.reason == "hold_increase_idle"

    def test_half_step_sync_boundary(self) -> None:
        """Exactly half a step away is NOT in sync."""
        d = _step(_gate(), curve_c=30.0, pump_setpoint_c=30.5, outlet_c=31.0)
        assert d.reason == "write_decrease_idle"

    def test_restart_band_boundary_is_strict(self) -> None:
        """outlet == target - band holds; just below writes."""
        assert (
            _step(_gate(), curve_c=31.0, pump_setpoint_c=30.0, outlet_c=28.0).reason
            == "hold_increase_idle"
        )
        assert (
            _step(_gate(), curve_c=31.0, pump_setpoint_c=30.0, outlet_c=27.99).reason
            == "write_increase_idle"
        )

    def test_limits_inside_range_leave_target(self) -> None:
        d = _step(
            _gate(),
            curve_c=31.0,
            pump_setpoint_c=None,
            freq_hz=None,
            entity_min_c=31.0,
            entity_max_c=31.0,
        )
        assert d.target_c == 31.0

    def test_hold_timer_ignores_ticks_when_not_holding(self) -> None:
        gate = _gate()
        gate.tick(_DT)
        d = _step(gate, curve_c=31.0, pump_setpoint_c=30.0, outlet_c=29.0)
        assert d.held_for_s == 0.0

    def test_hysteresis_down_edge_exact(self) -> None:
        """Play follows the curve down with exactly the hysteresis gap."""
        gate = _gate(heating_curve_hysteresis_k=0.3)
        _step(gate, curve_c=31.0, pump_setpoint_c=None)
        d = _step(gate, curve_c=30.0, pump_setpoint_c=None)
        assert d.hysteresis_c == pytest.approx(30.3)
        d = _step(gate, curve_c=31.0, pump_setpoint_c=None)
        assert d.hysteresis_c == pytest.approx(30.7)
