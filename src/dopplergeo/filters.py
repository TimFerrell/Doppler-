"""Candidate filters: jet removal, flight qualification, track proximity."""
from __future__ import annotations

import csv
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import shapely

from .adsb import Track
from .geo import LocalProjection

log = logging.getLogger(__name__)


def load_jet_types(path: str | Path) -> set[str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"jet type table not found: {p} (config adsb.jet_types_csv)")
    with open(p) as fh:
        rows = list(csv.DictReader(fh))
    if not rows or "type_code" not in rows[0]:
        raise ValueError(f"{p}: expected a CSV with a 'type_code' column")
    return {r["type_code"].strip().upper() for r in rows if r["type_code"].strip()}


def drop_jets(df: pd.DataFrame, jets: set[str]) -> tuple[pd.DataFrame, dict]:
    """Drop rows of KNOWN jet types; keep props and unknown/null type codes (lookup has gaps)."""
    tc = df["type_code"].astype("string").str.upper()
    is_jet = tc.isin(jets).fillna(False).to_numpy(bool)
    stats = {
        "flights_in": int(df["flight_id"].nunique()),
        "flights_jet_dropped": int(df.loc[is_jet, "flight_id"].nunique()),
        "flights_unknown_type_kept": int(df.loc[tc.isna().to_numpy(bool), "flight_id"].nunique()),
    }
    out = df[~is_jet].reset_index(drop=True)
    stats["flights_out"] = int(out["flight_id"].nunique())
    log.info("jet filter: %s", stats)
    return out, stats


def track_xy(track: Track, proj: LocalProjection, t_lo: float | None = None, t_hi: float | None = None):
    t = track.t
    sel = np.ones(track.n, bool)
    if t_lo is not None:
        sel &= t >= t_lo
    if t_hi is not None:
        sel &= t <= t_hi
    x, y = proj.forward(track.lat[sel], track.lon[sel])
    return x, y


def near_track_mask(x: np.ndarray, y: np.ndarray, track: Track, proj: LocalProjection, radius_m: float,
                    t_lo: float | None = None, t_hi: float | None = None) -> np.ndarray:
    """Points whose ground distance to the track polyline is <= radius_m."""
    tx, ty = track_xy(track, proj, t_lo, t_hi)
    if len(tx) == 0:
        return np.zeros(len(x), bool)
    geom = shapely.LineString(np.stack([tx, ty], 1)) if len(tx) > 1 else shapely.Point(tx[0], ty[0])
    pts = shapely.points(x, y)
    return shapely.dwithin(pts, geom, radius_m)


def qualifying_tracks(tracks: list[Track], proj: LocalProjection, region_radius_m: float, audible_m: float,
                      t_lo: float | None = None, t_hi: float | None = None) -> list[Track]:
    """Track segments that come within region_radius + audible radius of the region centre in [t_lo,t_hi]."""
    out = []
    for tr in tracks:
        tx, ty = track_xy(tr, proj, t_lo, t_hi)
        if len(tx) and np.min(np.hypot(tx, ty)) <= region_radius_m + audible_m:
            out.append(tr)
    return out
