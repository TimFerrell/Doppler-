"""Configuration: defaults, YAML loading, derived constants.

Every path and physical/algorithmic constant used by the pipeline lives here and
can be overridden from a YAML file (see ``config.example.yaml``, which must stay
key-for-key in sync with ``DEFAULTS``; a unit test enforces that).
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    # Hard gate: only recordings owned by, or explicitly authorized to, the
    # operator may be analysed. Real-audio commands refuse to run unless this is
    # true or --attest-authorized is passed; the attestation is written to every
    # report.
    "authorization": {
        "attest_owned_or_authorized": False,
        "operator": None,
    },
    "physics": {
        "speed_of_sound": 343.0,  # m/s
        "temperature_c": None,  # if set: c = 331.3*sqrt(1 + T/273.15), overrides speed_of_sound
        "retarded_tol_s": 1.0e-4,
        "retarded_max_iter": 20,
    },
    "track": {
        "resample_dt_s": 1.0,
        "max_gap_s": 30.0,
    },
    "elevation": {
        "dem_path": None,  # single GeoTIFF mosaic (MSL metres); null -> constant_m
        "constant_m": 0.0,
        "dem_band": 1,
    },
    "audio": {
        "target_sr": 16000,
        "start_utc": None,  # ISO-8601 or unix seconds of the first sample; optional
    },
    "spectrogram": {
        "window": 16384,
        "hop_divisor": 8,
        "pad_factor": 4,  # zero-padding factor of the FFT (frequency interpolation only)
        "fmin_hz": 5.0,
        "fmax_hz": 800.0,
        "high_fmax_hz": 3000.0,  # second spectrogram for jet tones (fan whine, buzz-saw), 300 Hz .. this
        "high_fmin_hz": 250.0,
        "high_pad_factor": 2,
        "high_window": 4096,  # shorter window: jet tones move ~10 Hz/s near the closest point
    },
    "measure": {
        "band_hz": [80.0, 300.0],
        "min_column_snr_db": 6.0,
        "fit_window_s": 90.0,
        "loss": "soft_l1",
        "f_scale_hz": 1.0,
        "v_bounds_mps": [20.0, 320.0],
        "d_bounds_m": [100.0, 30000.0],
        "max_rms_hz": 2.0,
    },
    "detect": {
        "band_hz": [60.0, 300.0],
        "window_s": 60.0,
        "step_s": 10.0,
        "min_coverage": 0.5,
        "min_spread_hz": 4.0,
        "max_rms_hz": 1.5,
        "min_inlier_frac": 0.6,
        "merge_s": 20.0,
        "manual_times_s": None,  # e.g. [61.5, 981.0]: overrides auto-detection
        "jet_band_hz": [300.0, 3000.0],  # jet engine tones are searched here too ([] disables)
        "jet_min_spread_hz": 15.0,
    },
    "score": {
        "dt_sweep_s": 3.0,
        "dt_step_s": 0.1,
        "df_sweep_hz": 1.5,
        "df_step_hz": 0.1,
        "coarse_dt_step_s": 0.5,
        "coarse_df_step_hz": 0.5,
        "harmonics": [1],
        "brightness": "snr",  # snr (linear power / row median) | db (dB above row median)
        "brightness_clip": None,
        "curve_step_s": 0.1,
        "freq_interp": "quadratic",  # quadratic | linear (between spectrogram bins)
        "polish_divisions": 5,  # sub-lattice polish of the dt/df optimum; 1 disables
        "batch_size": 48,
        "workers": 0,  # 0 = auto (multiprocessing when a pass is large)
    },
    "grid": {
        "step_m": 200.0,
        "audible_radius_m": 10000.0,
        "refine_top_n": 50,
        "refine_levels": 1,
        "max_points": 40000,  # larger areas start coarser and get extra refinement levels
    },
    "adsb": {
        "jet_types_csv": "data/icao_jet_types.csv",
        "drop_jets": False,  # jets are searched too; true restores props-only
        "flight_gap_s": 1800.0,
        "max_seen_pos_s": 15.0,
        "on_missing_velocity": "error",  # error | drop
        "prefer_geometric_alt": True,
    },
    "locate": {
        "time_reference": "measured_cpa",  # measured_cpa (locked, Phase A) | absolute
        "time_tolerance_s": 300.0,
        "speed_tolerance_mps": 15.0,  # flight speed screen vs the fitted speed: max(abs, frac * v)
        "speed_tolerance_frac": 0.25,
        "top_k": 10,
    },
    "synth": {
        "duration_s": 120.0,
        "sr": 16000,
        "f0_hz": 140.0,
        "harmonic_amps": [1.0, 0.5, 0.25],
        "snr_db": -22.0,  # fundamental power (at CPA) / white-noise power, full band
        "spherical_spreading": True,
        "search_radius_m": 6000.0,
        "center_offset_max_m": 2000.0,
    },
    "search_mode": "local",  # local | area (Phase B, feature-flagged)
    "paths": {
        "out_dir": "data/out",
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path is not None:
        with open(path) as fh:
            user = yaml.safe_load(fh) or {}
        unknown = _unknown_keys(DEFAULTS, user)
        if unknown:
            raise KeyError(f"unknown config keys in {path}: {unknown}")
        cfg = deep_merge(cfg, user)
    if overrides:
        cfg = deep_merge(cfg, overrides)
    return cfg


def _unknown_keys(ref: dict, user: dict, prefix: str = "") -> list[str]:
    bad = []
    for k, v in user.items():
        if k not in ref:
            bad.append(prefix + k)
        elif isinstance(v, dict) and isinstance(ref[k], dict):
            bad += _unknown_keys(ref[k], v, prefix + k + ".")
    return bad


def speed_of_sound(cfg: dict) -> float:
    t = cfg["physics"].get("temperature_c")
    if t is not None:
        return 331.3 * math.sqrt(1.0 + float(t) / 273.15)
    return float(cfg["physics"]["speed_of_sound"])
