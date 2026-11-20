import numpy as np
import pytest

from dopplergeo import adsb, config, geo


def test_velocity_from_gs_track_and_rate():
    ve, vn, vu = adsb.velocity_from_gs_track(100.0, 90.0, 1000.0)
    assert ve == pytest.approx(51.4444, abs=1e-3)
    assert vn == pytest.approx(0.0, abs=1e-9)
    assert vu == pytest.approx(5.08)
    ve, vn, vu = adsb.velocity_from_gs_track(200.0, 0.0, -500.0)
    assert ve == pytest.approx(0.0, abs=1e-9) and vn == pytest.approx(102.8888, abs=1e-3)
    assert vu == pytest.approx(-2.54)
    ve, vn, _ = adsb.velocity_from_gs_track(100.0, 225.0, 0.0)
    assert ve == pytest.approx(-51.4444 / np.sqrt(2), abs=1e-3) and vn == pytest.approx(ve)


def test_ecef_known_points_and_roundtrip():
    assert np.allclose(geo.lla_to_ecef(0, 0, 0), [6378137.0, 0, 0], atol=1e-6)
    assert np.allclose(geo.lla_to_ecef(90, 0, 0), [0, 0, 6356752.314245], atol=1e-3)
    p = geo.lla_to_ecef(37.5, -122.3, 1234.5)
    lat, lon, h = geo.ecef_to_lla(p)
    assert (lat, lon, h) == pytest.approx((37.5, -122.3, 1234.5), abs=1e-7)


def test_enu_roundtrip_and_orthonormal():
    e, n, u = geo.enu_basis(41.0, -73.0)
    M = np.stack([e, n, u])
    assert np.allclose(M @ M.T, np.eye(3), atol=1e-12)
    v = geo.enu_to_ecef_vec(41.0, -73.0, 10.0, -20.0, 3.0)
    assert np.allclose(geo.ecef_vec_to_enu(41.0, -73.0, v), (10.0, -20.0, 3.0))


def test_speed_of_sound_temperature():
    cfg = config.load_config()
    assert config.speed_of_sound(cfg) == 343.0
    cfg["physics"]["temperature_c"] = 20.0
    assert config.speed_of_sound(cfg) == pytest.approx(343.2, abs=0.1)


def test_parse_region_and_time():
    from dopplergeo import cli

    assert cli.parse_region("center=40.5,-100.25,radius_km=12") == ((40.5, -100.25), 12000.0)
    with pytest.raises(ValueError):
        cli.parse_region("40,-100,12")
    assert cli.parse_time("2024-01-01T00:00:00Z") == 1704067200.0
    assert cli.parse_time(1.5) == 1.5
    with pytest.raises(ValueError):
        cli.parse_time("2024-01-01T00:00:00")  # naive timestamps are rejected (timezone discipline)
