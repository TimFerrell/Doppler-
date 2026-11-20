"""Synthetic scenarios: curved/accelerating prop tracks, rendered audio, ADS-B rows, truth.

The audio renderer deliberately does NOT use ``model.solve_retarded``: it inverts
t_h(t_e) = t_e + |r(t_e) - r_obs|/c on a dense emission-time grid (t_h is monotonic for
subsonic sources) and synthesises s(t_h) = sum_n a_n cos(2 pi n f0 t_e(t_h)), whose
instantaneous frequency is exactly f0 * dt_e/dt_h = f0 * c/(c - v_r(t_e)).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import soundfile as sf
import yaml

from . import adsb, geo


@dataclass
class Flight:
    icao24: str
    type_code: str | None
    t: np.ndarray  # abs unix s, fine grid
    lat: np.ndarray
    lon: np.ndarray
    alt: np.ndarray
    pos: np.ndarray  # ECEF
    vel: np.ndarray  # ECEF (finite-difference of pos)
    ve: np.ndarray
    vn: np.ndarray
    vu: np.ndarray
    f0: float = 0.0
    amps: tuple = (1.0, 0.5, 0.25)
    loudness: float = 1.0
    params: dict | None = None


def simulate_flight(proj: geo.LocalProjection, t_cpa: float, cpa_xy: tuple[float, float], heading_deg: float,
                    v_c: float, accel: float, turn_deg_s: float, alt_c: float, vz: float, t_start: float,
                    t_end: float, icao24: str, type_code: str | None, fine_dt: float = 0.05) -> Flight:
    t = np.arange(t_start, t_end + fine_dt / 2, fine_dt)
    s = t - t_cpa
    psi = np.radians(heading_deg + turn_deg_s * s)
    v = v_c + accel * s
    ve_p, vn_p = v * np.sin(psi), v * np.cos(psi)

    def integ(f):
        cum = np.concatenate([[0.0], np.cumsum(0.5 * (f[1:] + f[:-1]) * fine_dt)])
        return cum - np.interp(t_cpa, t, cum)

    x = cpa_xy[0] + integ(ve_p)
    y = cpa_xy[1] + integ(vn_p)
    alt = alt_c + vz * s
    lat, lon = proj.inverse(x, y)
    pos = geo.lla_to_ecef(lat, lon, alt)
    vel = np.gradient(pos, t, axis=0)
    ve, vn, vu = geo.ecef_vec_to_enu(lat, lon, vel)
    params = dict(t_cpa_design=float(t_cpa), heading_deg=float(heading_deg), v_c=float(v_c), accel=float(accel),
                  turn_deg_s=float(turn_deg_s), alt_c=float(alt_c), vz=float(vz))
    return Flight(icao24, type_code, t, lat, lon, alt, pos, vel, ve, vn, vu, params=params)


def emission_times(fl: Flight, r_obs: np.ndarray, t_h: np.ndarray, c: float):
    """Exact inversion of t_h(t_e) on the flight's fine grid. Returns t_e, D(t_e), valid mask."""
    D = np.linalg.norm(fl.pos - r_obs, axis=-1)
    th_grid = fl.t + D / c
    if np.any(np.diff(th_grid) <= 0):
        raise ValueError("non-monotonic hearing time: supersonic segment?")
    valid = (t_h >= th_grid[0]) & (t_h <= th_grid[-1])
    t_e = np.interp(t_h, th_grid, fl.t)
    De = np.interp(t_h, th_grid, D)
    return t_e, De, valid


def true_curve(fl: Flight, r_obs: np.ndarray, t_h: np.ndarray, c: float) -> np.ndarray:
    t_e, De, valid = emission_times(fl, r_obs, t_h, c)
    p = np.stack([np.interp(t_e, fl.t, fl.pos[:, k]) for k in range(3)], -1)
    v = np.stack([np.interp(t_e, fl.t, fl.vel[:, k]) for k in range(3)], -1)
    rel = r_obs - p
    v_r = (v * rel).sum(-1) / np.linalg.norm(rel, axis=-1)
    f = fl.f0 * c / (c - v_r)
    return np.where(valid, f, np.nan)


def cpa_truth(fl: Flight, r_obs: np.ndarray, c: float) -> dict:
    D = np.linalg.norm(fl.pos - r_obs, axis=-1)
    i = int(np.argmin(D))
    return {"t_cpa_abs": float(fl.t[i]), "d_cpa_m": float(D[i]), "t_heard_abs": float(fl.t[i] + D[i] / c),
            "speed_mps": float(np.linalg.norm(fl.vel[i]))}


def render_audio(flights: list[Flight], r_obs: np.ndarray, t_start: float, duration: float, sr: int, c: float,
                 snr_db: float, rng: np.random.Generator, spreading: bool = True) -> np.ndarray:
    n = int(round(duration * sr))
    t_rel = np.arange(n) / sr
    t_h = t_start + t_rel
    x = np.zeros(n)
    for fl in flights:
        t_e, De, valid = emission_times(fl, r_obs, t_h, c)
        te_rel = t_e - t_start  # keep phases well-conditioned
        d_ref = float(np.min(np.linalg.norm(fl.pos - r_obs, axis=-1)))
        env = fl.loudness * (d_ref / De if spreading else np.ones(n))
        env = np.where(valid, env, 0.0)
        sig = np.zeros(n)
        for k, a in enumerate(fl.amps, start=1):
            phi = rng.uniform(0, 2 * np.pi)
            sig += a * np.cos(2 * np.pi * k * fl.f0 * te_rel + phi)
        x += env * sig
    sigma = np.sqrt(0.5 / 10 ** (snr_db / 10.0))
    x += rng.normal(0.0, sigma, n)
    peak = np.max(np.abs(x))
    return (x / peak * 0.9).astype(np.float32)


def adsb_rows(fl: Flight, rng: np.random.Generator, flight_id: int, t_lo: float | None = None,
              t_hi: float | None = None) -> pd.DataFrame:
    """Irregular-cadence (1-2 s) unified-schema rows sampled from the fine track."""
    t_lo = fl.t[0] if t_lo is None else t_lo
    t_hi = fl.t[-1] if t_hi is None else t_hi
    ts = [t_lo + rng.uniform(0, 1)]
    while ts[-1] < t_hi:
        ts.append(ts[-1] + rng.uniform(1.0, 2.0))
    ts = np.array([t for t in ts if t <= t_hi])
    f = lambda a: np.interp(ts, fl.t, a)  # noqa: E731
    return pd.DataFrame({
        "time": ts, "icao24": fl.icao24, "flight_id": flight_id,
        "lat": np.round(f(fl.lat), 6), "lon": np.round(f(fl.lon), 6), "alt_msl_m": f(fl.alt),
        "vx_east": f(fl.ve), "vy_north": f(fl.vn), "vz_up": f(fl.vu), "type_code": fl.type_code,
    })


def _write_jsonl(df: pd.DataFrame, path: Path) -> None:
    with open(path, "w") as fh:
        for rec in df.to_dict(orient="records"):
            rec = {k: (None if (isinstance(v, float) and np.isnan(v)) else v) for k, v in rec.items()}
            fh.write(json.dumps(rec) + "\n")


def write_true_curve(fl: Flight, r_obs, audio_start: float, duration: float, c: float, path: Path) -> None:
    t_rel = np.arange(0.0, duration + 1e-9, 0.1)
    f = true_curve(fl, r_obs, audio_start + t_rel, c)
    pd.DataFrame({"t_rel": t_rel, "f_hz": f}).to_csv(path, index=False)


def _rand_observer(rng):
    return float(rng.uniform(30, 45)), float(rng.uniform(-110, -85)), float(rng.uniform(50, 500))


# ------------------------------------------------------------------------------- Phase 0
def make_phase0_fixture(out_dir: str | Path, seed: int, cfg: dict, c: float) -> dict:
    """Single curved, accelerating prop pass; writes audio.wav, adsb.jsonl, truth.yaml."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    s = cfg["synth"]
    rng = np.random.default_rng(seed)
    lat0, lon0, h0 = _rand_observer(rng)
    proj = geo.LocalProjection(lat0, lon0)
    r_obs = geo.lla_to_ecef(lat0, lon0, h0)
    dur = float(s["duration_s"])
    audio_start = float(1.70e9 + rng.integers(0, 30_000_000))
    g = rng.uniform(1500, 4000)  # horizontal CPA offset
    alt_c = rng.uniform(2600, 3400)
    D = np.hypot(g, alt_c - h0)
    t_heard_rel = rng.uniform(45, 75)
    t_cpa = audio_start + t_heard_rel - D / c
    hdg = rng.uniform(0, 360)
    side = rng.choice([-1, 1])
    psi = np.radians(hdg)
    cpa_xy = (side * g * np.cos(psi), -side * g * np.sin(psi))
    fl = simulate_flight(proj, t_cpa, cpa_xy, hdg, v_c=rng.uniform(103, 107), accel=rng.uniform(0.04, 0.07),
                         turn_deg_s=rng.choice([-1, 1]) * rng.uniform(0.1, 0.25), alt_c=alt_c,
                         vz=rng.uniform(-0.8, 0.8), t_start=t_cpa - 110, t_end=t_cpa + 100,
                         icao24=f"{rng.integers(0xA00000, 0xAFFFFF):06x}", type_code="C208")
    fl.f0 = float(s["f0_hz"])
    fl.amps = tuple(s["harmonic_amps"])
    x = render_audio([fl], r_obs, audio_start, dur, int(s["sr"]), c, float(s["snr_db"]), rng,
                     bool(s["spherical_spreading"]))
    sf.write(out / "audio.wav", x, int(s["sr"]), subtype="PCM_16")
    rows = adsb_rows(fl, rng, 1)
    _write_jsonl(rows, out / "adsb.jsonl")
    write_true_curve(fl, r_obs, audio_start, dur, c, out / f"true_curve_{fl.icao24}.csv")
    ang = rng.uniform(0, 2 * np.pi)
    rr = float(s["center_offset_max_m"]) * np.sqrt(rng.uniform())
    clat, clon = proj.inverse(rr * np.cos(ang), rr * np.sin(ang))
    truth = {
        "seed": int(seed),
        "observer": {"lat": lat0, "lon": lon0, "elev_m": h0},
        "audio": {"path": "audio.wav", "start_unix": audio_start, "duration_s": dur, "sr": int(s["sr"]),
                  "snr_db": float(s["snr_db"])},
        "speed_of_sound": float(c),
        "search": {"center_lat": float(clat), "center_lon": float(clon), "radius_m": float(s["search_radius_m"])},
        "flights": [{"icao24": fl.icao24, "type_code": fl.type_code, "f0_hz": fl.f0, "horizontal_cpa_m": float(g),
                     **fl.params, **cpa_truth(fl, r_obs, c)}],
    }
    with open(out / "truth.yaml", "w") as fh:
        yaml.safe_dump(truth, fh, sort_keys=False)
    return truth


# ------------------------------------------------------------------------------- Phase A
def _snapshot_files(flights: list[Flight], fix_tables: list[pd.DataFrame], t0: float, t1: float, cadence: float):
    """readsb/tar1090-style aircraft snapshots (feet/knots/fpm, quantised like real receivers)."""
    snaps = []
    for now in np.arange(t0, t1, cadence):
        acs = []
        for fl, fx in zip(flights, fix_tables):
            k = np.searchsorted(fx["time"].to_numpy(), now, side="right") - 1
            if k < 0 or now - fx["time"].iloc[k] > 10:
                continue
            r = fx.iloc[k]
            gs = np.hypot(r.vx_east, r.vy_north) / adsb.KT_TO_MPS
            trk = np.degrees(np.arctan2(r.vx_east, r.vy_north)) % 360
            ac = {"hex": fl.icao24, "lat": round(float(r.lat), 6), "lon": round(float(r.lon), 6),
                  "alt_baro": int(round(r.alt_msl_m / adsb.FT_TO_M / 25.0) * 25),
                  "alt_geom": int(round(r.alt_msl_m / adsb.FT_TO_M / 25.0) * 25),
                  "gs": round(float(gs), 1), "track": round(float(trk), 2),
                  "baro_rate": int(round(r.vz_up / adsb.FPM_TO_MPS / 64.0) * 64),
                  "seen_pos": round(float(now - r.time), 1), "seen": 0.1, "messages": 100, "rssi": -20.0}
            if fl.type_code:
                ac["t"] = fl.type_code
            acs.append(ac)
        # one entry without position, as real snapshots contain
        acs.append({"hex": "abcdef", "seen": 3.0, "messages": 5})
        snaps.append({"now": round(float(now), 1), "messages": 1000, "aircraft": acs})
    return snaps


def make_phase_a_fixture(out_dir: str | Path, seed: int, cfg: dict, c: float, duration: float = 330.0,
                         snr_db: float | None = None) -> dict:
    """File-based multi-event fixture: WAV + tar1090 history_*.json + truth.yaml + locate config.

    Two audible prop passes (CPA heard ~200 s apart), a distant non-audible prop (a wrong
    combination candidate), and a jet (must be removed by the type filter).
    """
    out = Path(out_dir)
    (out / "tar1090").mkdir(parents=True, exist_ok=True)
    s = cfg["synth"]
    rng = np.random.default_rng(seed)
    lat0, lon0, h0 = _rand_observer(rng)
    proj = geo.LocalProjection(lat0, lon0)
    r_obs = geo.lla_to_ecef(lat0, lon0, h0)
    audio_start = float(1.70e9 + rng.integers(0, 30_000_000))
    sr = int(s["sr"])

    def pass_(t_heard_rel, g, alt, tc, f0, icao, loud=1.0, span=(-160, 140), v_c=None):
        D = np.hypot(g, alt - h0)
        t_cpa = audio_start + t_heard_rel - D / c
        hdg = rng.uniform(0, 360)
        side = rng.choice([-1, 1])
        psi = np.radians(hdg)
        fl = simulate_flight(proj, t_cpa, (side * g * np.cos(psi), -side * g * np.sin(psi)), hdg,
                             v_c=v_c or rng.uniform(70, 95), accel=rng.uniform(-0.05, 0.05),
                             turn_deg_s=rng.choice([-1, 1]) * rng.uniform(0.05, 0.2), alt_c=alt, vz=rng.uniform(-1, 1),
                             t_start=t_cpa + span[0], t_end=t_cpa + span[1], icao24=icao, type_code=tc)
        fl.f0, fl.loudness = f0, loud
        fl.amps = tuple(s["harmonic_amps"])
        return fl

    f1 = pass_(rng.uniform(70, 90), rng.uniform(1500, 3500), rng.uniform(1200, 2500), "PA28", rng.uniform(100, 130),
               "a1b2c3")
    f2 = pass_(f1.params["t_cpa_design"] - audio_start + rng.uniform(190, 220), rng.uniform(1500, 3500),
               rng.uniform(1500, 3000), None, rng.uniform(150, 190), "a4d5e6")  # unknown type: must be kept
    f3 = pass_(rng.uniform(80, 120), rng.uniform(13000, 15000), 2000.0, "C172", 120.0, "a7f8a9")  # inaudible
    jet = pass_(rng.uniform(140, 170), 2500.0, 9000.0, "B738", 300.0, "a0b0c0", v_c=200.0)
    audible = [f1, f2]
    x = render_audio(audible, r_obs, audio_start, duration, sr, c, float(snr_db if snr_db is not None else s["snr_db"]),
                     rng, True)
    sf.write(out / "audio.wav", x, sr, subtype="PCM_16")
    for fl in audible:
        write_true_curve(fl, r_obs, audio_start, duration, c, out / f"true_curve_{fl.icao24}.csv")
    flights = [f1, f2, f3, jet]
    fixes = [adsb_rows(fl, rng, i + 1) for i, fl in enumerate(flights)]
    t0 = min(fl.t[0] for fl in flights)
    t1 = max(fl.t[-1] for fl in flights)
    snaps = _snapshot_files(flights, fixes, np.ceil(t0) + 1, t1, 1.0)
    for i, snap in enumerate(snaps):
        with open(out / "tar1090" / f"history_{i}.json", "w") as fh:
            json.dump(snap, fh)
    truth = {
        "seed": int(seed),
        "observer": {"lat": lat0, "lon": lon0, "elev_m": h0},
        "audio": {"path": "audio.wav", "start_unix": audio_start, "duration_s": duration, "sr": sr},
        "speed_of_sound": float(c),
        "flights": [{"icao24": fl.icao24, "type_code": fl.type_code, "f0_hz": float(fl.f0),
                     "audible": fl in audible, **fl.params, **cpa_truth(fl, r_obs, c)} for fl in flights],
    }
    with open(out / "truth.yaml", "w") as fh:
        yaml.safe_dump(truth, fh, sort_keys=False)
    ang = rng.uniform(0, 2 * np.pi)
    rr = 2000.0 * np.sqrt(rng.uniform())
    clat, clon = proj.inverse(rr * np.cos(ang), rr * np.sin(ang))
    run_cfg = {
        "authorization": {"attest_owned_or_authorized": True, "operator": "synthetic fixture"},
        "elevation": {"constant_m": float(h0)},
        "audio": {"start_utc": audio_start},
    }
    with open(out / "config.yaml", "w") as fh:
        yaml.safe_dump(run_cfg, fh, sort_keys=False)
    truth["region"] = {"lat": float(clat), "lon": float(clon)}
    with open(out / "truth.yaml", "w") as fh:
        yaml.safe_dump(truth, fh, sort_keys=False)
    return truth
