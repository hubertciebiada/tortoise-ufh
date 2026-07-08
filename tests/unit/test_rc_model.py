"""Unit tests for :mod:`tortoise_ufh.rc_model` (RCParams, ModelOrder, RCModel).

Covers, in the spirit of the BUILD_SPEC (11) test contract:

* ``RCParams`` / ``RCModel`` validation raising ``ValueError`` (with ``match``).
* 3R3C SISO matrix dimensions.
* Analytic steady state (residual == 0) plus a hand-derived 2R2C closed form.
* The ``C_slab / C_air`` ratio physical invariant.
* ``step`` purity (no mutation of the input state vector).

Units are those of the core: resistances K/W, capacitances J/K, temperatures
degC, heat flows W, time (``dt``) seconds. These tests import only the pure
core (numpy/scipy) and never ``homeassistant``.
"""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from custom_components.tortoise_ufh.core.rc_model import ModelOrder, RCModel, RCParams

# Physically realistic reference parameters (BUILD_SPEC 8 / blueprint 13):
# ~20 m^2 room, C_air ~ 60 kJ/K, C_slab ~ 3.25 MJ/K (80 mm screed).
_C_AIR = 60_000.0
_C_SLAB = 3_250_000.0
_C_WALL = 1_500_000.0


def _params_3r3c() -> RCParams:
    """Return valid 3R3C parameters (SISO, no fast source)."""
    return RCParams(
        C_air=_C_AIR,
        C_slab=_C_SLAB,
        R_sf=0.01,
        C_wall=_C_WALL,
        R_wi=0.02,
        R_wo=0.03,
        R_ve=0.03,
        R_ins=0.01,
    )


def _params_2r2c() -> RCParams:
    """Return valid 2R2C parameters (SISO, no fast source)."""
    return RCParams(C_air=_C_AIR, C_slab=_C_SLAB, R_sf=0.01, R_env=0.03)


@pytest.fixture
def params_3r3c() -> RCParams:
    """Valid 3R3C parameters."""
    return _params_3r3c()


@pytest.fixture
def params_2r2c() -> RCParams:
    """Valid 2R2C parameters."""
    return _params_2r2c()


@pytest.fixture
def model_3r3c(params_3r3c: RCParams) -> RCModel:
    """3R3C SISO model discretized at dt=60 s."""
    return RCModel(params_3r3c, ModelOrder.THREE, dt=60.0)


@pytest.fixture
def model_2r2c(params_2r2c: RCParams) -> RCModel:
    """2R2C SISO model discretized at dt=60 s."""
    return RCModel(params_2r2c, ModelOrder.TWO, dt=60.0)


class TestRCParamsValidation:
    """RCParams and RCModel construction reject invalid inputs with ValueError."""

    @pytest.mark.unit
    def test_valid_params_do_not_raise(self, params_3r3c: RCParams) -> None:
        """Valid 3R3C parameters construct without raising."""
        assert params_3r3c.C_air == _C_AIR
        assert params_3r3c.C_slab == _C_SLAB

    @pytest.mark.unit
    def test_negative_r_sf_rejected(self) -> None:
        """A non-positive slab-to-air resistance is rejected."""
        with pytest.raises(ValueError, match="R_sf must be positive"):
            RCParams(C_air=_C_AIR, C_slab=_C_SLAB, R_sf=-0.01)

    @pytest.mark.unit
    def test_zero_c_air_rejected(self) -> None:
        """A zero air capacitance is rejected."""
        with pytest.raises(ValueError, match="C_air must be positive"):
            RCParams(C_air=0.0, C_slab=_C_SLAB, R_sf=0.01)

    @pytest.mark.unit
    def test_negative_c_slab_rejected(self) -> None:
        """A negative slab capacitance is rejected."""
        with pytest.raises(ValueError, match="C_slab must be positive"):
            RCParams(C_air=_C_AIR, C_slab=-1.0, R_sf=0.01)

    @pytest.mark.unit
    def test_negative_solar_fraction_rejected(self) -> None:
        """A negative solar fraction is rejected."""
        with pytest.raises(ValueError, match="Solar fractions must be non-negative"):
            RCParams(C_air=_C_AIR, C_slab=_C_SLAB, R_sf=0.01, f_conv=-0.1, f_rad=0.4)

    @pytest.mark.unit
    def test_solar_fraction_sum_above_one_rejected(self) -> None:
        """Solar fractions summing above 1.0 are rejected."""
        with pytest.raises(ValueError, match=r"f_conv \+ f_rad must be <= 1.0"):
            RCParams(C_air=_C_AIR, C_slab=_C_SLAB, R_sf=0.01, f_conv=0.7, f_rad=0.4)

    @pytest.mark.unit
    def test_solar_fraction_sum_equals_one_accepted(self) -> None:
        """Solar fractions summing exactly to 1.0 are accepted."""
        params = RCParams(
            C_air=_C_AIR, C_slab=_C_SLAB, R_sf=0.01, f_conv=0.6, f_rad=0.4
        )
        assert params.f_conv + params.f_rad == 1.0

    @pytest.mark.unit
    def test_validate_for_order_3r3c_missing_c_wall(self) -> None:
        """3R3C validation requires C_wall to be present."""
        params = RCParams(
            C_air=_C_AIR,
            C_slab=_C_SLAB,
            R_sf=0.01,
            R_wi=0.02,
            R_wo=0.03,
            R_ve=0.03,
            R_ins=0.01,
        )
        with pytest.raises(ValueError, match="C_wall is required"):
            params.validate_for_order(ModelOrder.THREE)

    @pytest.mark.unit
    def test_validate_for_order_3r3c_negative_r_ins(self) -> None:
        """3R3C validation rejects a non-positive R_ins."""
        params = RCParams(
            C_air=_C_AIR,
            C_slab=_C_SLAB,
            R_sf=0.01,
            C_wall=_C_WALL,
            R_wi=0.02,
            R_wo=0.03,
            R_ve=0.03,
            R_ins=-0.01,
        )
        with pytest.raises(ValueError, match="R_ins must be positive"):
            params.validate_for_order(ModelOrder.THREE)

    @pytest.mark.unit
    def test_validate_for_order_2r2c_missing_r_env(self, params_3r3c: RCParams) -> None:
        """2R2C validation requires R_env to be present."""
        with pytest.raises(ValueError, match="R_env is required"):
            params_3r3c.validate_for_order(ModelOrder.TWO)

    @pytest.mark.unit
    def test_missing_3r3c_params_raise_on_construction(self) -> None:
        """Building a 3R3C model without wall parameters raises via validation."""
        params = RCParams(C_air=_C_AIR, C_slab=_C_SLAB, R_sf=0.01)
        with pytest.raises(ValueError, match="required for the 3R3C model"):
            RCModel(params, ModelOrder.THREE, dt=60.0)

    @pytest.mark.unit
    def test_frozen_dataclass_rejects_mutation(self, params_3r3c: RCParams) -> None:
        """RCParams is frozen and rejects attribute assignment."""
        with pytest.raises((AttributeError, TypeError)):
            params_3r3c.C_air = 1.0  # type: ignore[misc]

    @pytest.mark.unit
    def test_zero_dt_rejected(self, params_3r3c: RCParams) -> None:
        """A zero discretization step is rejected."""
        with pytest.raises(ValueError, match="dt must be positive"):
            RCModel(params_3r3c, ModelOrder.THREE, dt=0.0)

    @pytest.mark.unit
    def test_negative_dt_rejected(self, params_3r3c: RCParams) -> None:
        """A negative discretization step is rejected."""
        with pytest.raises(ValueError, match="dt must be positive"):
            RCModel(params_3r3c, ModelOrder.THREE, dt=-1.0)


class TestRCModelDimensions:
    """3R3C SISO matrix dimensions match the state-space contract."""

    @pytest.mark.unit
    def test_3r3c_siso_matrix_shapes(self, model_3r3c: RCModel) -> None:
        """3R3C SISO: A(3,3), B(3,1), E(3,3), b(3,) for both C and D forms."""
        m = model_3r3c.get_matrices()
        assert m["A_c"].shape == (3, 3)
        assert m["B_c"].shape == (3, 1)
        assert m["E_c"].shape == (3, 3)
        assert m["b_c"].shape == (3,)
        assert m["A_d"].shape == (3, 3)
        assert m["B_d"].shape == (3, 1)
        assert m["E_d"].shape == (3, 3)
        assert m["b_d"].shape == (3,)

    @pytest.mark.unit
    def test_3r3c_siso_properties(self, model_3r3c: RCModel) -> None:
        """3R3C SISO exposes 3 states, 1 input, 3 disturbances."""
        assert model_3r3c.n_states == 3
        assert model_3r3c.n_inputs == 1
        assert model_3r3c.n_disturbances == 3
        assert model_3r3c.state_names == ["T_air", "T_slab", "T_wall"]

    @pytest.mark.unit
    def test_3r3c_b_c_siso_structure(self, model_3r3c: RCModel) -> None:
        """SISO B_c couples Q_floor only into the slab node."""
        b_c: NDArray[np.float64] = model_3r3c.get_matrices()["B_c"]
        assert b_c[0, 0] == 0.0
        assert b_c[1, 0] > 0.0
        assert b_c[2, 0] == 0.0


class TestRCModelSteadyState:
    """Analytic steady state: residual is zero and matches a hand derivation."""

    @pytest.mark.unit
    def test_3r3c_steady_state_residual_zero(self, model_3r3c: RCModel) -> None:
        """steady_state satisfies A_c x + B_c u + E_c d + b_c == 0 (3R3C)."""
        u = np.array([2000.0])
        d = np.array([-10.0, 300.0, 150.0])
        x_ss = model_3r3c.steady_state(u, d)

        m = model_3r3c.get_matrices()
        residual = m["A_c"] @ x_ss + m["B_c"] @ u + m["E_c"] @ d + m["b_c"]
        np.testing.assert_allclose(residual, 0.0, atol=1e-9)

    @pytest.mark.unit
    def test_3r3c_steady_state_physically_ordered(self, model_3r3c: RCModel) -> None:
        """Under floor heating the slab is warmer than the air, above outdoor."""
        u = np.array([2000.0])
        d = np.array([-10.0, 0.0, 0.0])
        x_ss = model_3r3c.steady_state(u, d)
        assert x_ss[1] > x_ss[0] > -10.0

    @pytest.mark.unit
    def test_2r2c_steady_state_hand_derived(self, model_2r2c: RCModel) -> None:
        """2R2C heating equilibrium matches the closed-form series-resistance result.

        With Q_sol=0, all floor heat Q flows air->outdoor through R_env, so
        ``T_air = T_out + Q * R_env`` and the surface drop gives
        ``T_slab = T_air + Q * R_sf``.
        """
        q_floor = 1000.0
        t_out = 0.0
        u = np.array([q_floor])
        d = np.array([t_out, 0.0])

        x_ss = model_2r2c.steady_state(u, d)

        p = model_2r2c.params
        assert p.R_env is not None
        t_air_expected = t_out + q_floor * p.R_env
        t_slab_expected = t_air_expected + q_floor * p.R_sf
        np.testing.assert_allclose(x_ss[0], t_air_expected, atol=1e-6)
        np.testing.assert_allclose(x_ss[1], t_slab_expected, atol=1e-6)

    @pytest.mark.unit
    def test_step_converges_to_steady_state(self, model_3r3c: RCModel) -> None:
        """Repeated step from 20 degC converges to the analytic steady state."""
        u = np.array([1500.0])
        d = np.array([-5.0, 100.0, 200.0])
        x_ss = model_3r3c.steady_state(u, d)

        x = np.full(3, 20.0)
        for _ in range(10_080):  # 7 days at dt=60 s (>> slab time constant)
            x = model_3r3c.step(x, u, d)
        np.testing.assert_allclose(x, x_ss, atol=0.01)


class TestRCParamsInvariants:
    """Physical invariants of the reference parameter set."""

    @pytest.mark.unit
    def test_c_slab_c_air_ratio(self, params_3r3c: RCParams) -> None:
        """The slab-to-air heat-capacity ratio is ~54.17 (high thermal mass)."""
        ratio = params_3r3c.C_slab / params_3r3c.C_air
        assert abs(ratio - 54.1667) < 0.01

    @pytest.mark.unit
    def test_c_slab_c_air_ratio_matches_2r2c(
        self, params_3r3c: RCParams, params_2r2c: RCParams
    ) -> None:
        """The capacity ratio is identical across model orders (same masses)."""
        ratio_3 = params_3r3c.C_slab / params_3r3c.C_air
        ratio_2 = params_2r2c.C_slab / params_2r2c.C_air
        assert ratio_3 == ratio_2


class TestRCModelStepPurity:
    """step is a pure function: it never mutates its inputs."""

    @pytest.mark.unit
    def test_step_does_not_mutate_state(self, model_3r3c: RCModel) -> None:
        """step leaves the caller's state vector unchanged."""
        x = np.array([20.0, 20.0, 20.0])
        x_before = x.copy()
        u = np.array([1000.0])
        d = np.array([5.0, 0.0, 0.0])
        model_3r3c.step(x, u, d)
        np.testing.assert_array_equal(x, x_before)

    @pytest.mark.unit
    def test_step_returns_new_array(self, model_3r3c: RCModel) -> None:
        """step returns a freshly allocated vector distinct from the input."""
        x = np.array([20.0, 20.0, 20.0])
        u = np.array([500.0])
        d = np.array([0.0, 0.0, 0.0])
        x_next = model_3r3c.step(x, u, d)
        assert x_next is not x
        assert x_next.shape == (3,)

    @pytest.mark.unit
    def test_step_does_not_mutate_inputs_u_d(self, model_3r3c: RCModel) -> None:
        """step leaves the control and disturbance vectors unchanged."""
        x = np.array([20.0, 20.0, 20.0])
        u = np.array([1000.0])
        d = np.array([5.0, 100.0, 50.0])
        u_before = u.copy()
        d_before = d.copy()
        model_3r3c.step(x, u, d)
        np.testing.assert_array_equal(u, u_before)
        np.testing.assert_array_equal(d, d_before)
