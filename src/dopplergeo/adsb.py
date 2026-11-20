"""ADS-B: unified parquet schema, receiver-log ingest, track resampling.

Unified schema (one row per position report)::

    time       float64  unix seconds UTC (time of the position fix)
    icao24     str      lowercase hex
    flight_id  int64    surrogate: icao24 + continuity (gap > adsb.flight_gap_s => new flight)
    lat, lon   float64  degrees WGS84
    alt_msl_m  float32  metres (geometric altitude preferred, else barometric)
    vx_east, vy_north, vz_up  float32  m/s
    type_code  str|null ICAO Doc 8643 type designator
"""
from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import geo

log = logging.getLogger(__name__)

KT_TO_MPS = 0.514444  # brief: 0.51444
FPM_TO_MPS = 0.00508
FT_TO_M = 0.3048

SCHEMA = {
    "time": "float64",
    "icao24": "string",
    "flight_id": "int64",
    "lat": "float64",
    "lon": "float64",
    "alt_msl_m": "float32",
    "vx_east": "float32",
    "vy_north": "float32",
    "vz_up": "float32",
    "type_code": "string",
}
REQUIRED = list(SCHEMA)


class ADSBError(ValueError):
    pass


# --------------------------------------------------------------------------- units
def velocity_from_gs_track(gs_kt, track_deg, baro_rate_fpm):
    gs = np.asarray(gs_kt, float) * KT_TO_MPS
    tr = np.radians(np.asarray(track_deg, float))
    return gs * np.sin(tr), gs * np.cos(tr), np.asarray(baro_rate_fpm, float) * FPM_TO_MPS


# --------------------------------------------------------------------------- schema
def validate(df: pd.DataFrame, where: str = "ADS-B table") -> pd.DataFrame:
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ADSBError(f"{where}: missing required column(s) {missing}; have {list(df.columns)}")
    for c in ("time", "lat", "lon", "alt_msl_m", "vx_east", "vy_north", "vz_up"):
        n_bad = int((~np.isfinite(df[c].to_numpy(float))).sum())
        if n_bad:
            raise ADSBError(f"{where}: column {c!r} has {n_bad} null/non-finite values")
    if df["icao24"].isna().any():
        raise ADSBError(f"{where}: null icao24")
    return coerce(df)


def coerce(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({c: df[c] for c in REQUIRED})
    for c, t in SCHEMA.items():
        out[c] = out[c].astype(t)
    return out.reset_index(drop=True)


def assign_flight_ids(df: pd.DataFrame, gap_s: float) -> pd.DataFrame:
    df = df.sort_values(["icao24", "time"], kind="stable").reset_index(drop=True)
    icao = df["icao24"].to_numpy()
    t = df["time"].to_numpy(float)
    new = np.ones(len(df), bool)
    if len(df) > 1:
        new[1:] = (icao[1:] != icao[:-1]) | (np.diff(t) > gap_s)
    df["flight_id"] = np.cumsum(new).astype("int64")
    return df


# --------------------------------------------------------------------------- ingest
def _iter_json_files(src: Path):
    if src.is_file():
        yield src
        return
    pats = ("*.json", "*.json.gz", "*.jsonl", "*.jsonl.gz")
    files = sorted({p for pat in pats for p in src.rglob(pat)})
    if not files:
        raise ADSBError(f"no *.json / *.jsonl(.gz) files under {src}")
    yield from files


def _open_text(p: Path):
    return gzip.open(p, "rt") if p.suffix == ".gz" else open(p)


def _first(d: dict, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def parse_snapshot(obj: dict, cfg: dict, stats: dict, origin: str) -> list[dict]:
    """Parse one readsb ``aircraft.json`` / tar1090 ``history_N.json`` snapshot."""
    if "now" not in obj or "aircraft" not in obj:
        raise ADSBError(f"{origin}: not an aircraft snapshot (needs 'now' and 'aircraft')")
    now = float(obj["now"])
    max_seen = float(cfg["adsb"]["max_seen_pos_s"])
    prefer_geom = bool(cfg["adsb"]["prefer_geometric_alt"])
    policy = cfg["adsb"]["on_missing_velocity"]
    rows = []
    for ac in obj["aircraft"]:
        stats["aircraft_entries"] += 1
        if ac.get("lat") is None or ac.get("lon") is None:
            stats["no_position"] += 1
            continue
        hexid = ac.get("hex")
        if not hexid:
            raise ADSBError(f"{origin}: aircraft entry with position but no 'hex'")
        seen_pos = _first(ac, "seen_pos", "seen")
        seen_pos = 0.0 if seen_pos is None else float(seen_pos)
        if seen_pos > max_seen:
            stats["stale_position"] += 1
            continue
        alt_keys = ("alt_geom", "alt_baro", "altitude") if prefer_geom else ("alt_baro", "alt_geom", "altitude")
        alt = _first(ac, *alt_keys)
        if alt == "ground":
            stats["on_ground"] += 1
            continue
        gs = _first(ac, "gs", "speed")
        trk = _first(ac, "track", "true_heading")
        vr = _first(ac, "baro_rate", "geom_rate", "vert_rate")
        missing = [n for n, v in (("altitude", alt), ("gs", gs), ("track", trk), ("baro_rate", vr)) if v is None]
        if missing:
            if policy == "error":
                raise ADSBError(
                    f"{origin}: aircraft {hexid} has a position but no {missing}. Required ADS-B fields "
                    "are missing; set adsb.on_missing_velocity: drop to discard such rows explicitly."
                )
            stats["dropped_missing_fields"] += 1
            continue
        ve, vn, vu = velocity_from_gs_track(gs, trk, vr)
        tc = ac.get("t")  # readsb/tar1090 type designator (NOT 'type', which is the message source)
        rows.append({
            "time": now - seen_pos,
            "icao24": str(hexid).lower().strip(),
            "lat": float(ac["lat"]),
            "lon": float(ac["lon"]),
            "alt_msl_m": float(alt) * FT_TO_M,
            "vx_east": float(ve),
            "vy_north": float(vn),
            "vz_up": float(vu),
            "type_code": (str(tc).upper() if tc else None),
        })
    return rows


def ingest(src: str | Path, cfg: dict, type_db: dict[str, str] | None = None) -> tuple[pd.DataFrame, dict]:
    """Read tar1090 history_*.json, readsb aircraft.json snapshots, or unified JSONL/parquet."""
    src = Path(src)
    stats = {k: 0 for k in ("files", "snapshots", "aircraft_entries", "no_position", "stale_position",
                            "on_ground", "dropped_missing_fields", "unified_rows", "duplicates")}
    frames: list[pd.DataFrame] = []
    rows: list[dict] = []
    if src.is_file() and src.suffix == ".parquet" or src.is_dir() and any(src.glob("*.parquet")):
        files = [src] if src.is_file() else sorted(src.glob("*.parquet"))
        for f in files:
            frames.append(validate(pd.read_parquet(f), str(f)))
            stats["files"] += 1
    else:
        for p in _iter_json_files(src):
            stats["files"] += 1
            with _open_text(p) as fh:
                text = fh.read()
            if ".jsonl" in p.name:
                recs = [json.loads(line) for line in text.splitlines() if line.strip()]
                stats["unified_rows"] += len(recs)
                df = pd.DataFrame.from_records(recs)
                if "flight_id" not in df.columns:
                    df["flight_id"] = 0
                if "type_code" not in df.columns:
                    df["type_code"] = None
                frames.append(validate(df, str(p)))
                continue
            obj = json.loads(text)
            snaps = obj if isinstance(obj, list) else [obj]
            for s in snaps:
                stats["snapshots"] += 1
                rows += parse_snapshot(s, cfg, stats, str(p))
    if rows:
        df = pd.DataFrame.from_records(rows)
        df["flight_id"] = 0
        frames.append(coerce(df))
    if not frames:
        raise ADSBError(f"no ADS-B position rows found in {src}")
    df = pd.concat(frames, ignore_index=True)
    n0 = len(df)
    # snapshots repeat the same fix while seen_pos grows; dedupe on (icao, fix time)
    df["_tkey"] = np.round(df["time"].to_numpy(float), 1)
    df = df.drop_duplicates(["icao24", "_tkey"]).drop(columns="_tkey")
    stats["duplicates"] = n0 - len(df)
    if type_db:
        fill = df["icao24"].map(type_db)
        df["type_code"] = df["type_code"].fillna(fill)
    df = assign_flight_ids(df, float(cfg["adsb"]["flight_gap_s"]))
    df = coerce(df)
    stats["rows_out"] = len(df)
    stats["flights_out"] = int(df["flight_id"].nunique())
    log.info("ingest %s: %s", src, stats)
    return df, stats


def write_parquet(df: pd.DataFrame, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    validate(df).to_parquet(path, index=False)


def read_table(path: str | Path, flight_gap_s: float = 1800.0, bbox=None, t_range=None) -> pd.DataFrame:
    """Unified parquet file, or a directory of part files (flight ids are re-derived across parts).

    ``bbox`` (S, W, N, E) and ``t_range`` (t0, t1) are pushed down into the parquet scan, so a
    state-sized multi-day dataset can be read for one small region without loading all of it.
    """
    p = Path(path)
    if not (p.suffix == ".parquet" or p.is_dir()):
        raise ADSBError(f"expected a unified parquet file/dir, got {p} (run `dopplergeo ingest-adsb` first)")
    files = sorted(p.glob("*.parquet")) if p.is_dir() else [p]
    if not files:
        raise ADSBError(f"no parquet files in {p}")
    import pyarrow.dataset as ds

    dset = ds.dataset([str(f) for f in files], format="parquet")
    missing = [c for c in REQUIRED if c not in dset.schema.names]
    if missing:
        raise ADSBError(f"{p}: missing required column(s) {missing}; have {dset.schema.names}")
    flt = None
    if bbox is not None:
        s, w, n, e = bbox
        flt = (ds.field("lat") >= s) & (ds.field("lat") <= n) & (ds.field("lon") >= w) & (ds.field("lon") <= e)
    if t_range is not None:
        ft = (ds.field("time") >= float(t_range[0])) & (ds.field("time") <= float(t_range[1]))
        flt = ft if flt is None else flt & ft
    df = dset.to_table(filter=flt).to_pandas()
    df = validate(df, str(p))
    if len(files) > 1 or bbox is not None or t_range is not None:
        df["_k"] = np.round(df["time"].to_numpy(float), 1)
        df = df.drop_duplicates(["icao24", "_k"]).drop(columns="_k")
        df = coerce(assign_flight_ids(df, flight_gap_s))
    return df


# --------------------------------------------------------------------------- tracks
@dataclass
class Track:
    """A gap-free track segment resampled to a uniform time grid, in ECEF."""

    flight_id: int
    icao24: str
    type_code: str | None
    segment: int
    t0: float
    dt: float
    pos: np.ndarray  # (N,3) ECEF m
    vel: np.ndarray  # (N,3) ECEF m/s
    lat: np.ndarray
    lon: np.ndarray
    alt: np.ndarray

    @property
    def n(self) -> int:
        return len(self.pos)

    @property
    def t(self) -> np.ndarray:
        return self.t0 + self.dt * np.arange(self.n)

    @property
    def t1(self) -> float:
        return self.t0 + self.dt * (self.n - 1)

    @property
    def key(self) -> str:
        return f"{self.flight_id}.{self.segment}"

    def _interp(self, arr: np.ndarray, t) -> np.ndarray:
        t = np.asarray(t, float)
        u = (t - self.t0) / self.dt
        ok = (u >= 0) & (u <= self.n - 1)
        uc = np.where(ok, u, 0.0)
        i = np.minimum(np.floor(uc).astype(np.int64), self.n - 2)
        w = (uc - i)[..., None]
        out = arr[i] * (1.0 - w) + arr[i + 1] * w
        out[~ok] = np.nan
        return out

    def position(self, t) -> np.ndarray:
        return self._interp(self.pos, t)

    def velocity(self, t) -> np.ndarray:
        return self._interp(self.vel, t)

    def subset(self, t_lo: float, t_hi: float) -> "Track | None":
        i0 = max(0, int(np.floor((t_lo - self.t0) / self.dt)))
        i1 = min(self.n - 1, int(np.ceil((t_hi - self.t0) / self.dt)))
        if i1 - i0 < 1:
            return None
        sl = slice(i0, i1 + 1)
        return Track(self.flight_id, self.icao24, self.type_code, self.segment, self.t0 + i0 * self.dt, self.dt,
                     self.pos[sl], self.vel[sl], self.lat[sl], self.lon[sl], self.alt[sl])


def tracks_from_table(df: pd.DataFrame, dt: float = 1.0, max_gap_s: float = 30.0) -> list[Track]:
    """Resample each flight to a uniform ``dt`` grid by linear time interpolation in ECEF.

    Gaps larger than ``max_gap_s`` split a flight into separate segments.
    """
    tracks: list[Track] = []
    for fid, g in df.sort_values("time").groupby("flight_id", sort=True):
        t = g["time"].to_numpy(float)
        keep = np.concatenate([[True], np.diff(t) > 1e-6])
        g = g[keep]
        t = t[keep]
        lat, lon, alt = (g[c].to_numpy(float) for c in ("lat", "lon", "alt_msl_m"))
        p = geo.lla_to_ecef(lat, lon, alt)
        v = geo.enu_to_ecef_vec(lat, lon, g["vx_east"].to_numpy(float), g["vy_north"].to_numpy(float),
                                g["vz_up"].to_numpy(float))
        brk = np.where(np.diff(t) > max_gap_s)[0]
        starts = np.concatenate([[0], brk + 1])
        ends = np.concatenate([brk + 1, [len(t)]])
        tc = g["type_code"].iloc[0]
        tc = None if pd.isna(tc) else str(tc)
        for seg, (a, b) in enumerate(zip(starts, ends)):
            if b - a < 2:
                continue
            ts = t[a:b]
            tu = np.arange(np.ceil(ts[0] / dt) * dt, ts[-1] + 1e-9, dt)
            if len(tu) < 2:
                continue
            P = np.stack([np.interp(tu, ts, p[a:b, k]) for k in range(3)], -1)
            V = np.stack([np.interp(tu, ts, v[a:b, k]) for k in range(3)], -1)
            la, lo, al = geo.ecef_to_lla(P)
            tracks.append(Track(int(fid), str(g["icao24"].iloc[0]), tc, seg, float(tu[0]), dt, P, V, la, lo, al))
    return tracks
