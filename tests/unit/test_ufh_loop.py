"""Unit tests for ``tortoise_ufh.ufh_loop.loop_power`` (EN 1264 reduced model).

These tests pin the sign contract and magnitude sanity of the UFH loop power
calculation:

    * Heating with a favourable gradient yields ``Q > 0`` [W].
    * Cooling with a favourable gradient yields ``Q < 0`` [W].
    * A wrong-direction gradient for the requested mode returns exactly
      ``0.0`` [W] ("never oppose the mode" baked into the physics).
    * The per-area power [W/m^2] is physically plausible, and the
      heating/cooling magnitudes are *asymmetric* because the default
      supply/return spread differs between modes (5 K heating vs 3 K cooling).

Units: temperatures in degC, power in W, area in m^2, per-area power in
W/m^2. This module never imports ``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import RoomConfig
from custom_components.tortoise_ufh.core.const import (
    DEFAULT_DT_COOLING,
    DEFAULT_DT_HEATING,
)
from custom_components.tortoise_ufh.core.rc_model import RCParams
from custom_components.tortoise_ufh.core.ufh_loop import (
    LoopGeometry,
    _compute_u_effective,
    _delta_t_log,
    loop_power,
    loop_power_with_valve,
)

# ---------------------------------------------------------------------------
# Fixtures — one realistic ~20 m^2 UFH loop group
# ---------------------------------------------------------------------------


@pytest.fixture
def geometry() -> LoopGeometry:
    """Return a realistic ~20 m^2 UFH loop geometry.

    Length approximates ``area / spacing * bend_factor`` at 150 mm spacing
    with a standard 16 x 2 mm PE-X pipe. Units: metres, millimetres, m^2.

    Returns:
        A validated :class:`~tortoise_ufh.ufh_loop.LoopGeometry`.
    """
    return LoopGeometry(
        effective_pipe_length_m=146.67,
        pipe_spacing_m=0.15,
        pipe_diameter_outer_mm=16.0,
        pipe_wall_thickness_mm=2.0,
        area_m2=20.0,
    )


# ---------------------------------------------------------------------------
# Sign contract
# ---------------------------------------------------------------------------


class TestLoopPowerSign:
    """Sign of the returned thermal power [W] per mode and gradient."""

    @pytest.mark.unit
    def test_heating_favourable_gradient_is_positive(
        self, geometry: LoopGeometry
    ) -> None:
        """Heating with supply warmer than the slab returns Q > 0 [W]."""
        q_w = loop_power(35.0, 24.0, geometry, "heating")
        assert q_w > 0.0

    @pytest.mark.unit
    def test_cooling_favourable_gradient_is_negative(
        self, geometry: LoopGeometry
    ) -> None:
        """Cooling with supply colder than the slab returns Q < 0 [W]."""
        q_w = loop_power(16.0, 24.0, geometry, "cooling")
        assert q_w < 0.0


# ---------------------------------------------------------------------------
# Wrong-gradient -> exactly 0.0
# ---------------------------------------------------------------------------


class TestLoopPowerWrongGradient:
    """A gradient opposing the mode returns exactly ``0.0`` [W]."""

    @pytest.mark.unit
    def test_heating_supply_below_slab_is_zero(self, geometry: LoopGeometry) -> None:
        """Heating with supply colder than the slab returns exactly 0.0 W."""
        q_w = loop_power(20.0, 24.0, geometry, "heating")
        assert q_w == 0.0

    @pytest.mark.unit
    def test_heating_supply_equal_slab_is_zero(self, geometry: LoopGeometry) -> None:
        """Heating with supply equal to the slab returns exactly 0.0 W."""
        q_w = loop_power(24.0, 24.0, geometry, "heating")
        assert q_w == 0.0

    @pytest.mark.unit
    def test_cooling_supply_above_slab_is_zero(self, geometry: LoopGeometry) -> None:
        """Cooling with supply warmer than the slab returns exactly 0.0 W."""
        q_w = loop_power(28.0, 24.0, geometry, "cooling")
        assert q_w == 0.0

    @pytest.mark.unit
    def test_cooling_supply_equal_slab_is_zero(self, geometry: LoopGeometry) -> None:
        """Cooling with supply equal to the slab returns exactly 0.0 W."""
        q_w = loop_power(24.0, 24.0, geometry, "cooling")
        assert q_w == 0.0


# ---------------------------------------------------------------------------
# Small favourable gradient -> nonzero (default return estimate clamp)
# ---------------------------------------------------------------------------


class TestLoopPowerSmallGradientDefaultReturn:
    """A favourable gradient smaller than the default spread stays nonzero.

    With ``t_return_estimate=None`` the default drop (5 K heating / 3 K
    cooling) must be clamped so the estimated return never reaches the slab,
    otherwise ``delta_t_out <= 0`` collapses Q to a false 0.0 W.
    """

    @pytest.mark.unit
    @pytest.mark.parametrize("gap_k", [2.0, 3.0, 4.0, 5.0])
    def test_heating_small_gap_is_nonzero(
        self, geometry: LoopGeometry, gap_k: float
    ) -> None:
        """Heating gaps <= DEFAULT_DT_HEATING still transfer heat (Q > 0)."""
        t_slab = 24.0
        q_w = loop_power(t_slab + gap_k, t_slab, geometry, "heating")
        assert q_w > 0.0

    @pytest.mark.unit
    @pytest.mark.parametrize("gap_k", [1.0, 2.0, 3.0])
    def test_cooling_small_gap_is_nonzero(
        self, geometry: LoopGeometry, gap_k: float
    ) -> None:
        """Cooling gaps <= DEFAULT_DT_COOLING still extract heat (Q < 0)."""
        t_slab = 24.0
        q_w = loop_power(t_slab - gap_k, t_slab, geometry, "cooling")
        assert q_w < 0.0


# ---------------------------------------------------------------------------
# Magnitude sanity + heating/cooling asymmetry
# ---------------------------------------------------------------------------


class TestLoopPowerMagnitude:
    """Per-area power [W/m^2] plausibility and mode asymmetry."""

    @pytest.mark.unit
    def test_heating_per_area_power_is_plausible(self, geometry: LoopGeometry) -> None:
        """Heating at 35/24 degC gives a sane per-area power [W/m^2]."""
        q_w = loop_power(35.0, 24.0, geometry, "heating")
        per_area_w_m2 = q_w / geometry.area_m2
        # Non-trivial and within an order-of-magnitude of practical UFH
        # output; the reduced EN 1264 model (pipe wall + screed in series)
        # is an approximation, so the ceiling is generous.
        assert 5.0 < per_area_w_m2 < 150.0

    @pytest.mark.unit
    def test_cooling_per_area_power_is_plausible(self, geometry: LoopGeometry) -> None:
        """Cooling at 16/24 degC gives a sane per-area magnitude [W/m^2]."""
        q_w = loop_power(16.0, 24.0, geometry, "cooling")
        per_area_w_m2 = abs(q_w) / geometry.area_m2
        # Floor cooling is non-trivial; ceiling generous for the reduced model.
        assert 2.0 < per_area_w_m2 < 100.0

    @pytest.mark.unit
    def test_heating_cooling_asymmetry_for_equal_gradient(
        self, geometry: LoopGeometry
    ) -> None:
        """Equal |supply - slab| gradient yields asymmetric magnitudes.

        The default supply/return spread differs by mode
        (``DEFAULT_DT_HEATING`` = 5 K vs ``DEFAULT_DT_COOLING`` = 3 K), so
        the LMTD and thus |Q| differ even for an identical supply-slab
        gradient.
        """
        assert DEFAULT_DT_HEATING != DEFAULT_DT_COOLING
        t_slab = 24.0
        gradient_k = 6.0
        q_heat_w = loop_power(t_slab + gradient_k, t_slab, geometry, "heating")
        q_cool_w = loop_power(t_slab - gradient_k, t_slab, geometry, "cooling")
        assert q_heat_w > 0.0
        assert q_cool_w < 0.0
        assert abs(q_heat_w) != pytest.approx(abs(q_cool_w))

    @pytest.mark.unit
    def test_explicit_return_estimate_scales_magnitude(
        self, geometry: LoopGeometry
    ) -> None:
        """A wider supply/return spread lowers the LMTD and thus |Q| [W]."""
        q_narrow_w = loop_power(35.0, 24.0, geometry, "heating", t_return_estimate=34.0)
        q_wide_w = loop_power(35.0, 24.0, geometry, "heating", t_return_estimate=28.0)
        assert q_narrow_w > q_wide_w > 0.0


# ---------------------------------------------------------------------------
# Mode validation
# ---------------------------------------------------------------------------


class TestLoopPowerValidation:
    """Invalid mode strings are rejected with ``ValueError``."""

    @pytest.mark.unit
    def test_invalid_mode_raises(self, geometry: LoopGeometry) -> None:
        """An unknown mode raises ``ValueError``."""
        with pytest.raises(ValueError, match="mode must be"):
            loop_power(35.0, 24.0, geometry, "auto")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LMTD helper — documented edge cases
# ---------------------------------------------------------------------------


class TestDeltaTLog:
    """Log-mean temperature difference (LMTD) edge cases and formula [K]."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("delta_t_in", "delta_t_out"),
        [(0.0, 5.0), (5.0, 0.0), (-1.0, 5.0), (5.0, -1.0)],
    )
    def test_non_positive_delta_returns_zero(
        self, delta_t_in: float, delta_t_out: float
    ) -> None:
        """Any delta <= 0 (either side) means no valid heat transfer: 0.0 K."""
        assert _delta_t_log(delta_t_in, delta_t_out) == 0.0

    @pytest.mark.unit
    def test_equal_deltas_return_arithmetic_mean(self) -> None:
        """Nearly equal deltas return their arithmetic mean (0/0 guard)."""
        assert _delta_t_log(5.0, 5.0) == pytest.approx(5.0)

    @pytest.mark.unit
    def test_standard_lmtd_formula(self) -> None:
        """LMTD = (dT_in - dT_out) / ln(dT_in / dT_out) for distinct deltas."""
        assert _delta_t_log(11.0, 6.0) == pytest.approx(8.248976500890643)


# ---------------------------------------------------------------------------
# Effective heat-transfer coefficient K_H
# ---------------------------------------------------------------------------


class TestComputeUEffective:
    """Effective area coefficient ``K_H`` [W/(m^2*K)] per the documented model."""

    @pytest.mark.unit
    def test_k_h_matches_documented_formula(self, geometry: LoopGeometry) -> None:
        """K_H = 1 / (1/U_pipe + R_screed) with spacing-corrected pipe wall.

        Hand-computed from the module docstring formulas for a 16 x 2 mm PE-X
        loop at 150 mm spacing, 146.67 m of pipe over 20 m^2 (K_PEX = 0.35):
        U_pipe_m = 2*pi*K_PEX / ln(d_outer/d_inner) = 7.6443 W/(m*K),
        f_spacing = 1 / (1 + spacing/(pi*d_outer)) = 0.2510, giving
        U_pipe = 14.0705 W/(m^2*K) and K_H = 5.8455 W/(m^2*K).
        """
        assert _compute_u_effective(geometry) == pytest.approx(5.845540613114904)


# ---------------------------------------------------------------------------
# Exact power values (pin the full EN 1264 reduced formula chain)
# ---------------------------------------------------------------------------


class TestLoopPowerExactValues:
    """Exact power [W] for pinned gradients, default return estimates."""

    @pytest.mark.unit
    def test_heating_default_return_exact(self, geometry: LoopGeometry) -> None:
        """Heating 35/24 degC: return estimate clamps to 30 degC (LMTD(11, 6)).

        Q = K_H * A * LMTD(11, 6)^1.1 = 5.8455 * 20 * 8.2490^1.1 [W].
        """
        q_w = loop_power(35.0, 24.0, geometry, "heating")
        assert q_w == pytest.approx(1190.953351544624)

    @pytest.mark.unit
    def test_cooling_default_return_exact(self, geometry: LoopGeometry) -> None:
        """Cooling 16/24 degC: return estimate clamps to 19 degC (-LMTD(8, 5)).

        Q = -K_H * A * LMTD(8, 5)^1.1 = -5.8455 * 20 * 6.3829^1.1 [W].
        """
        q_w = loop_power(16.0, 24.0, geometry, "cooling")
        assert q_w == pytest.approx(-898.2076000547686)

    @pytest.mark.unit
    def test_one_kelvin_log_delta_delivers_power(self, geometry: LoopGeometry) -> None:
        """A 1 K LMTD still transfers ``K_H * A * 1^1.1`` W (dt_log == 1.0)."""
        q_w = loop_power(25.0, 24.0, geometry, "heating", t_return_estimate=25.0)
        assert q_w == pytest.approx(116.9108122622981)

    @pytest.mark.unit
    def test_return_at_slab_gives_exactly_zero(self, geometry: LoopGeometry) -> None:
        """A return estimate at slab temperature collapses the LMTD to 0.0 W."""
        q_w = loop_power(35.0, 24.0, geometry, "heating", t_return_estimate=24.0)
        assert q_w == 0.0


# ---------------------------------------------------------------------------
# Valve-scaled power
# ---------------------------------------------------------------------------


class TestLoopPowerWithValve:
    """Valve-scaled power: clamping to ``[0, 1]`` and linear duty scaling."""

    @pytest.mark.unit
    def test_closed_valve_is_zero(self, geometry: LoopGeometry) -> None:
        """Valve position 0.0 returns exactly 0.0 W."""
        q_w = loop_power_with_valve(0.0, 35.0, 24.0, geometry, "heating")
        assert q_w == 0.0

    @pytest.mark.unit
    def test_negative_valve_clamped_to_zero(self, geometry: LoopGeometry) -> None:
        """A negative valve position is clamped to 0 -> exactly 0.0 W."""
        q_w = loop_power_with_valve(-0.5, 35.0, 24.0, geometry, "heating")
        assert q_w == 0.0

    @pytest.mark.unit
    def test_fully_open_valve_equals_unscaled_power(
        self, geometry: LoopGeometry
    ) -> None:
        """Valve position 1.0 returns the full unscaled loop power [W]."""
        full_w = loop_power(35.0, 24.0, geometry, "heating")
        q_w = loop_power_with_valve(1.0, 35.0, 24.0, geometry, "heating")
        assert full_w > 0.0
        assert q_w == pytest.approx(full_w)

    @pytest.mark.unit
    def test_valve_above_one_clamped_to_one(self, geometry: LoopGeometry) -> None:
        """A valve position > 1 is clamped to 1 (full unscaled power)."""
        q_w = loop_power_with_valve(1.5, 35.0, 24.0, geometry, "heating")
        full_w = loop_power(35.0, 24.0, geometry, "heating")
        assert q_w == pytest.approx(full_w)

    @pytest.mark.unit
    def test_half_valve_scales_linearly(self, geometry: LoopGeometry) -> None:
        """Valve position 0.5 returns half the unscaled loop power [W]."""
        full_w = loop_power(35.0, 24.0, geometry, "heating")
        q_w = loop_power_with_valve(0.5, 35.0, 24.0, geometry, "heating")
        assert q_w == pytest.approx(0.5 * full_w)

    @pytest.mark.unit
    def test_return_estimate_is_forwarded(self, geometry: LoopGeometry) -> None:
        """An explicit return estimate is forwarded to ``loop_power``."""
        full_w = loop_power(35.0, 24.0, geometry, "heating", t_return_estimate=28.0)
        q_w = loop_power_with_valve(0.5, 35.0, 24.0, geometry, "heating", 28.0)
        assert q_w == pytest.approx(0.5 * full_w)

    @pytest.mark.unit
    def test_cooling_valve_scales_negative_power(self, geometry: LoopGeometry) -> None:
        """Cooling power stays negative and scales with the valve position."""
        full_w = loop_power(16.0, 24.0, geometry, "cooling")
        q_w = loop_power_with_valve(0.5, 16.0, 24.0, geometry, "cooling")
        assert full_w < 0.0
        assert q_w == pytest.approx(0.5 * full_w)


# ---------------------------------------------------------------------------
# LoopGeometry validation boundaries and messages
# ---------------------------------------------------------------------------


class TestLoopGeometryValidation:
    """``__post_init__`` range checks: boundaries and documented messages."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("effective_pipe_length_m", 0.0, "effective_pipe_length_m must be > 0"),
            ("pipe_spacing_m", 0.0, "pipe_spacing_m must be > 0"),
            ("pipe_diameter_outer_mm", 0.0, "pipe_diameter_outer_mm must be > 0"),
            ("pipe_wall_thickness_mm", 0.0, "pipe_wall_thickness_mm must be > 0"),
            ("area_m2", 0.0, "area_m2 must be > 0"),
        ],
    )
    def test_zero_field_rejected(self, field: str, value: float, match: str) -> None:
        """Each geometry field must be strictly positive (0.0 rejected)."""
        kwargs = {
            "effective_pipe_length_m": 146.67,
            "pipe_spacing_m": 0.15,
            "pipe_diameter_outer_mm": 16.0,
            "pipe_wall_thickness_mm": 2.0,
            "area_m2": 20.0,
        }
        kwargs[field] = value
        with pytest.raises(ValueError, match=match):
            LoopGeometry(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("effective_pipe_length_m", 0.5),
            ("pipe_spacing_m", 0.05),
            ("area_m2", 0.5),
        ],
    )
    def test_small_positive_values_valid(self, field: str, value: float) -> None:
        """Small but strictly positive field values are legitimate geometry."""
        kwargs = {
            "effective_pipe_length_m": 146.67,
            "pipe_spacing_m": 0.15,
            "pipe_diameter_outer_mm": 16.0,
            "pipe_wall_thickness_mm": 2.0,
            "area_m2": 20.0,
        }
        kwargs[field] = value
        geometry = LoopGeometry(**kwargs)  # type: ignore[arg-type]
        assert getattr(geometry, field) == pytest.approx(value)

    @pytest.mark.unit
    def test_sub_millimetre_pipe_valid(self) -> None:
        """Sub-1 mm pipe diameter/wall are valid as long as 0 < wall < d/2."""
        geometry = LoopGeometry(
            effective_pipe_length_m=0.5,
            pipe_spacing_m=0.05,
            pipe_diameter_outer_mm=0.5,
            pipe_wall_thickness_mm=0.1,
            area_m2=0.5,
        )
        assert geometry.pipe_diameter_outer_mm == pytest.approx(0.5)
        assert geometry.pipe_wall_thickness_mm == pytest.approx(0.1)

    @pytest.mark.unit
    def test_wall_equal_half_diameter_rejected(self) -> None:
        """``wall == diameter / 2`` is rejected (strict inequality required)."""
        with pytest.raises(
            ValueError,
            match=r"pipe_wall_thickness_mm \(8\.0\) must be "
            r"< pipe_diameter_outer_mm / 2 \(8\.0\)",
        ):
            LoopGeometry(
                effective_pipe_length_m=146.67,
                pipe_spacing_m=0.15,
                pipe_diameter_outer_mm=16.0,
                pipe_wall_thickness_mm=8.0,
                area_m2=20.0,
            )

    @pytest.mark.unit
    def test_wall_below_half_diameter_valid(self) -> None:
        """A wall in ``[diameter / 3, diameter / 2)`` is a valid thick-wall pipe."""
        geometry = LoopGeometry(
            effective_pipe_length_m=146.67,
            pipe_spacing_m=0.15,
            pipe_diameter_outer_mm=16.0,
            pipe_wall_thickness_mm=6.0,
            area_m2=20.0,
        )
        assert geometry.pipe_wall_thickness_mm == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# LoopGeometry.from_room_config
# ---------------------------------------------------------------------------


def _room(
    name: str,
    *,
    area_m2: float = 20.0,
    loop_geometry: LoopGeometry | None = None,
) -> RoomConfig:
    """Return a minimal validated :class:`RoomConfig` for geometry tests.

    Args:
        name: Room identifier.
        area_m2: Floor area in square metres.
        loop_geometry: Optional explicit loop geometry override.

    Returns:
        A validated single-loop, no-fast-source room configuration.
    """
    return RoomConfig(
        name=name,
        area_m2=area_m2,
        params=RCParams(
            C_air=60_000.0,
            C_slab=3_250_000.0,
            R_sf=0.01,
            C_wall=1_500_000.0,
            R_wi=0.02,
            R_wo=0.03,
            R_ve=0.03,
            R_ins=0.01,
        ),
        loop_geometry=loop_geometry,
    )


class TestLoopGeometryFromRoomConfig:
    """Geometry estimation from a :class:`RoomConfig` (module defaults)."""

    @pytest.mark.unit
    def test_explicit_geometry_returned_unchanged(self, geometry: LoopGeometry) -> None:
        """A room with explicit ``loop_geometry`` gets it back unchanged."""
        room = _room("salon", loop_geometry=geometry)
        assert LoopGeometry.from_room_config(room) is geometry

    @pytest.mark.unit
    def test_estimated_from_area_uses_module_defaults(self) -> None:
        """Without explicit geometry, estimate length = area/spacing * 1.1.

        The estimate uses the documented module defaults: 150 mm spacing,
        16 x 2 mm pipe and the 1.1 bend/return margin factor.
        """
        room = _room("salon", area_m2=20.0)
        geometry = LoopGeometry.from_room_config(room)
        assert geometry.effective_pipe_length_m == pytest.approx(20.0 / 0.15 * 1.1)
        assert geometry.pipe_spacing_m == pytest.approx(0.15)
        assert geometry.pipe_diameter_outer_mm == pytest.approx(16.0)
        assert geometry.pipe_wall_thickness_mm == pytest.approx(2.0)
        assert geometry.area_m2 == pytest.approx(20.0)
