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

from tortoise_ufh.const import DEFAULT_DT_COOLING, DEFAULT_DT_HEATING
from tortoise_ufh.ufh_loop import LoopGeometry, loop_power

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
        # output; the reduced EN 1264 model omits screed spreading losses,
        # so the ceiling is generous.
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
