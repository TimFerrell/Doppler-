import numpy as np
import pytest

from dopplergeo import config, geo
from dopplergeo.adsb import Track

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg():
    return config.load_config(overrides={"adsb": {"jet_types_csv": str(ROOT / "data" / "icao_jet_types.csv")}})


def straight_track(r0, v, t0=1.7e9, duration=400.0, dt=1.0, flight_id=1) -> Track:
    """Exactly straight, constant-velocity ECEF track."""
    t = t0 + np.arange(0.0, duration + dt / 2, dt)
    pos = r0[None, :] + (t - t0)[:, None] * v[None, :]
    vel = np.repeat(v[None, :], len(t), 0)
    lat, lon, alt = geo.ecef_to_lla(pos)
    return Track(flight_id, "abc123", "C172", 0, float(t[0]), dt, pos, vel, lat, lon, alt)


def straight_scenario(d=3000.0, speed=100.0, t_cpa_offset=400.0, lat=40.0, lon=-100.0, h=200.0):
    """Observer at (lat,lon,h); straight track passing at slant CPA distance d at t0 + t_cpa_offset."""
    r_obs = geo.lla_to_ecef(lat, lon, h)
    e, n, u = geo.enu_basis(lat, lon)
    perp = (0.6 * e + 0.8 * u)  # unit: CPA offset direction (east+up)
    v = speed * n
    t0 = 1.7e9
    r_cpa = r_obs + d * perp
    r0 = r_cpa - v * t_cpa_offset
    return r_obs, straight_track(r0, v, t0=t0, duration=2 * t_cpa_offset), t0 + t_cpa_offset
