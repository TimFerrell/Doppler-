"""Candidate observer grids in a local azimuthal-equidistant frame."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geo import LocalProjection


@dataclass
class Grid:
    proj: LocalProjection
    x: np.ndarray  # metres east of the projection centre
    y: np.ndarray  # metres north
    step: float

    @property
    def lat(self) -> np.ndarray:
        return self.proj.inverse(self.x, self.y)[0]

    @property
    def lon(self) -> np.ndarray:
        return self.proj.inverse(self.x, self.y)[1]

    def latlon(self) -> tuple[np.ndarray, np.ndarray]:
        return self.proj.inverse(self.x, self.y)

    def __len__(self) -> int:
        return len(self.x)

    def subset(self, mask) -> "Grid":
        return Grid(self.proj, self.x[mask], self.y[mask], self.step)


def make_grid(center: tuple[float, float], radius_m: float, step_m: float) -> Grid:
    """Square-lattice points (spacing ``step_m``) within ``radius_m`` of ``center`` (lat, lon)."""
    proj = LocalProjection(float(center[0]), float(center[1]))
    n = int(np.floor(radius_m / step_m))
    a = np.arange(-n, n + 1) * step_m
    X, Y = np.meshgrid(a, a)
    keep = X ** 2 + Y ** 2 <= radius_m ** 2 + 1e-6
    return Grid(proj, X[keep].astype(float), Y[keep].astype(float), float(step_m))


def refine(proj: LocalProjection, x: np.ndarray, y: np.ndarray, step: float) -> Grid:
    """3x3 neighbourhoods at half the step around each survivor (deduplicated)."""
    h = step / 2.0
    o = np.array([-h, 0.0, h])
    ox, oy = np.meshgrid(o, o)
    X = (np.asarray(x)[:, None] + ox.ravel()[None, :]).ravel()
    Y = (np.asarray(y)[:, None] + oy.ravel()[None, :]).ravel()
    key = np.round(np.stack([X, Y], 1) / (h / 4)).astype(np.int64)
    _, idx = np.unique(key, axis=0, return_index=True)
    idx.sort()
    return Grid(proj, X[idx], Y[idx], h)
