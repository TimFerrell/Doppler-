"""Geodesy: WGS84 LLA <-> ECEF, local ENU bases, local projections, elevation.

Altitude convention: ADS-B altitudes and DEM elevations are both treated as
heights above the WGS84 ellipsoid. Both are in reality ~MSL (orthometric); the
geoid undulation is common to observer and aircraft and only shifts both by the
same few tens of metres, which changes slant ranges by centimetres.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from pyproj import CRS, Transformer

_LLA2ECEF = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
_ECEF2LLA = Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)


def lla_to_ecef(lat, lon, h) -> np.ndarray:
    lat, lon, h = np.broadcast_arrays(np.asarray(lat, float), np.asarray(lon, float), np.asarray(h, float))
    x, y, z = _LLA2ECEF.transform(lon, lat, h)
    return np.stack([np.asarray(x), np.asarray(y), np.asarray(z)], axis=-1)


def ecef_to_lla(xyz) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    xyz = np.asarray(xyz, float)
    lon, lat, h = _ECEF2LLA.transform(xyz[..., 0], xyz[..., 1], xyz[..., 2])
    return np.asarray(lat), np.asarray(lon), np.asarray(h)


def enu_basis(lat, lon) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Unit east/north/up vectors (in ECEF) at geodetic lat/lon (deg). Shapes (...,3)."""
    phi = np.radians(np.asarray(lat, float))
    lam = np.radians(np.asarray(lon, float))
    sp, cp, sl, cl = np.sin(phi), np.cos(phi), np.sin(lam), np.cos(lam)
    e = np.stack([-sl, cl, np.zeros_like(sl)], axis=-1)
    n = np.stack([-sp * cl, -sp * sl, cp], axis=-1)
    u = np.stack([cp * cl, cp * sl, sp], axis=-1)
    return e, n, u


def enu_to_ecef_vec(lat, lon, ve, vn, vu) -> np.ndarray:
    e, n, u = enu_basis(lat, lon)
    return (np.asarray(ve, float)[..., None] * e + np.asarray(vn, float)[..., None] * n
            + np.asarray(vu, float)[..., None] * u)


def ecef_vec_to_enu(lat, lon, v) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    e, n, u = enu_basis(lat, lon)
    v = np.asarray(v, float)
    return (v * e).sum(-1), (v * n).sum(-1), (v * u).sum(-1)


@lru_cache(maxsize=64)
def _aeqd(lat0: float, lon0: float) -> tuple[Transformer, Transformer]:
    crs = CRS.from_proj4(f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m +no_defs")
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    return fwd, inv


@dataclass(frozen=True)
class LocalProjection:
    """Azimuthal-equidistant projection centred on (lat0, lon0); metres."""

    lat0: float
    lon0: float

    def forward(self, lat, lon) -> tuple[np.ndarray, np.ndarray]:
        fwd, _ = _aeqd(float(self.lat0), float(self.lon0))
        x, y = fwd.transform(np.asarray(lon, float), np.asarray(lat, float))
        return np.asarray(x), np.asarray(y)

    def inverse(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        _, inv = _aeqd(float(self.lat0), float(self.lon0))
        lon, lat = inv.transform(np.asarray(x, float), np.asarray(y, float))
        return np.asarray(lat), np.asarray(lon)


def ground_distance(lat1, lon1, lat2, lon2) -> np.ndarray:
    from pyproj import Geod

    g = Geod(ellps="WGS84")
    lat1, lon1, lat2, lon2 = np.broadcast_arrays(*(np.asarray(a, float) for a in (lat1, lon1, lat2, lon2)))
    _, _, d = g.inv(lon1, lat1, lon2, lat2)
    return np.asarray(d)


class Elevation:
    """Ground elevation provider: a constant, or a GeoTIFF sampled with rasterio."""

    def __init__(self, constant_m: float = 0.0, dem_path: str | None = None, band: int = 1):
        self.constant_m = float(constant_m)
        self.dem_path = dem_path
        self.band = band
        self._ds = None
        self._to_ds = None
        if dem_path:
            import rasterio

            self._ds = rasterio.open(dem_path)
            self._to_ds = Transformer.from_crs("EPSG:4326", self._ds.crs, always_xy=True)

    @classmethod
    def from_config(cls, cfg: dict) -> "Elevation":
        e = cfg["elevation"]
        return cls(e.get("constant_m", 0.0), e.get("dem_path"), e.get("dem_band", 1))

    def describe(self) -> str:
        return f"DEM {self.dem_path}" if self._ds is not None else f"constant {self.constant_m:.1f} m"

    def __call__(self, lat, lon) -> np.ndarray:
        lat = np.asarray(lat, float)
        lon = np.asarray(lon, float)
        if self._ds is None:
            return np.full(np.broadcast(lat, lon).shape, self.constant_m)
        lat_b, lon_b = np.broadcast_arrays(lat, lon)
        xs, ys = self._to_ds.transform(lon_b.ravel(), lat_b.ravel())
        vals = np.array([v[0] for v in self._ds.sample(zip(xs, ys), indexes=self.band)], float)
        nod = self._ds.nodata
        bad = ~np.isfinite(vals)
        if nod is not None:
            bad |= vals == nod
        # Points outside the raster also return nodata/0; treat nodata as an error
        # rather than silently using sea level.
        if bad.any():
            raise ValueError(f"DEM {self.dem_path} has no data at {int(bad.sum())} requested points")
        return vals.reshape(lat_b.shape)
