import numpy as np
import pytest

from dopplergeo import model
from conftest import straight_scenario

C = 343.0


def test_doppler_limits():
    assert model.doppler(140.0, 0.0, C) == pytest.approx(140.0)
    assert model.doppler(140.0, 50.0, C) > 140.0  # approaching
    assert model.doppler(140.0, -50.0, C) < 140.0  # receding
    assert model.doppler(140.0, 100.0, C) == pytest.approx(140.0 * C / (C - 100.0))
    with pytest.raises(ValueError):
        model.doppler(140.0, 400.0, C)


def test_radial_velocity_sign():
    r_obs = np.zeros(3)
    r_ac = np.array([1000.0, 0, 0])
    assert model.radial_velocity(r_ac, np.array([-100.0, 0, 0]), r_obs) == pytest.approx(100.0)  # approaching
    assert model.radial_velocity(r_ac, np.array([100.0, 0, 0]), r_obs) == pytest.approx(-100.0)
    assert model.radial_velocity(r_ac, np.array([0, 100.0, 0]), r_obs) == pytest.approx(0.0)


@pytest.mark.parametrize("d,speed", [(1500.0, 60.0), (3000.0, 100.0), (8000.0, 150.0)])
def test_retarded_solver_matches_closed_form(d, speed):
    r_obs, tr, t_c = straight_scenario(d=d, speed=speed)
    t_h = np.linspace(t_c - 120, t_c + 150, 2001)
    t_e, D, _, n_iter = model.solve_retarded(tr, r_obs, t_h, C, tol=1e-4, max_iter=20)
    tau = model.straight_line_retarded_tau(t_h, t_c, speed, d, C)
    assert np.all(np.isfinite(t_e))
    assert np.max(np.abs(t_e - (t_c + tau))) < 1e-3  # < 1 ms
    assert np.all(t_e <= t_h)
    assert n_iter <= 20
    # emission-time identity holds
    assert np.max(np.abs(t_h - t_e - D / C)) < 1e-3


def test_closed_form_root_is_physical():
    tau = model.straight_line_retarded_tau(np.array([-50.0, 0.0, 50.0]), 0.0, 100.0, 3000.0, C)
    assert np.all(tau <= np.array([-50.0, 0.0, 50.0]))
    # at t_h = t_c + d/c... heard-CPA: the sound emitted at CPA arrives d/c later
    assert model.straight_line_retarded_tau(3000.0 / C, 0.0, 100.0, 3000.0, C) == pytest.approx(0.0, abs=1e-9)


def test_forward_curve_matches_straight_line_model():
    d, speed = 3000.0, 100.0
    r_obs, tr, t_c = straight_scenario(d=d, speed=speed)
    t_h = np.linspace(t_c - 100, t_c + 120, 500)
    f_full = model.forward_curve(r_obs, tr, 140.0, t_h, C)
    f_sl = model.straight_line_curve(t_h, 140.0, speed, d, t_c, C)
    assert np.max(np.abs(f_full - f_sl)) < 1e-3
    # f == f0 exactly when the CPA sound arrives
    assert model.forward_curve(r_obs, tr, 140.0, np.array([t_c + d / C]), C)[0] == pytest.approx(140.0, abs=1e-3)


def test_naive_curve_is_wrong_shape_not_just_shifted():
    d, speed = 3500.0, 105.0
    r_obs, tr, t_c = straight_scenario(d=d, speed=speed)
    t_h = np.arange(t_c - 50, t_c + 70, 0.1)
    f_ret = model.forward_curve(r_obs, tr, 140.0, t_h, C)
    f_naive = model.forward_curve_naive(r_obs, tr, 140.0, t_h, C)
    best = np.inf
    for shift in np.arange(-30, 30.01, 0.05):  # best pure time shift of the naive curve
        g = np.interp(t_h, t_h + shift, f_naive, left=np.nan, right=np.nan)
        ok = np.isfinite(g)
        best = min(best, np.max(np.abs(g[ok] - f_ret[ok])))
    assert best > 1.0  # Hz: shape error survives any time shift
