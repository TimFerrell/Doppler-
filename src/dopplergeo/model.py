"""Forward acoustic model: retarded-time Doppler curves for a stationary observer.

Conventions (tests depend on them):
  * slant distance  D = |r_ac - r_obs|  (3-D)
  * radial velocity v_r = (v_ac . (r_obs - r_ac)) / |r_obs - r_ac|, positive when approaching
  * heard frequency f = f0 * c / (c - v_r)
  * emission time   t_e solves t_e = t_h - |r_ac(t_e) - r_obs| / c (fixed-point iteration)
"""
from __future__ import annotations

import numpy as np

from .adsb import Track


def doppler(f0, v_r, c):
    v_r = np.asarray(v_r, float)
    if np.any(np.abs(v_r[np.isfinite(v_r)]) >= c):
        raise ValueError("|v_r| >= c: moving-source Doppler formula invalid (supersonic)")
    return np.asarray(f0, float) * c / (c - v_r)


def radial_velocity(r_ac, v_ac, r_obs):
    rel = np.asarray(r_obs, float) - np.asarray(r_ac, float)
    d = np.linalg.norm(rel, axis=-1)
    return (np.asarray(v_ac, float) * rel).sum(-1) / d


def solve_retarded(track: Track, r_obs, t_h, c: float, tol: float = 1e-4, max_iter: int = 20):
    """Fixed-point solve of t_e = t_h - |r(t_e) - r_obs|/c.

    ``r_obs`` (...,3) and ``t_h`` (...) must broadcast. Returns (t_e, D, r_ac(t_e), n_iter).
    Values whose emission time falls outside the track coverage are NaN.
    """
    r_obs = np.asarray(r_obs, float)
    t_h = np.asarray(t_h, float)
    lo, hi = track.t0, track.t1
    # g(t_e) = t_h - D(t_e)/c is a contraction (|g'| = |v_r|/c < 1): clamping iterates to the track's
    # coverage keeps convergence when the solution is inside it, and stops an overshooting iterate from
    # turning into NaN for good.
    p = track.position(np.clip(t_h, lo, hi))
    D = np.linalg.norm(p - r_obs, axis=-1)
    t_e = t_h - D / c
    n_iter = 0
    step = np.full(np.shape(t_e), np.inf)
    for n_iter in range(1, max_iter + 1):
        p = track.position(np.clip(t_e, lo, hi))
        D = np.linalg.norm(p - r_obs, axis=-1)
        t_new = t_h - D / c
        step = np.abs(t_new - t_e)
        t_e = t_new
        if not np.any(step > tol):
            break
    bad = (t_e < lo - 1e-9) | (t_e > hi + 1e-9) | ~(step <= tol)
    t_e = np.where(bad, np.nan, t_e)
    p = track.position(t_e)
    D = np.linalg.norm(p - r_obs, axis=-1)
    return t_e, D, p, n_iter


def forward_curve(r_obs, track: Track, f0: float, hear_times, c: float, tol: float = 1e-4,
                  max_iter: int = 20, details: bool = False):
    """Heard frequency at ``hear_times`` (absolute unix s) for observer(s) ``r_obs`` (ECEF).

    Vectorized: ``r_obs`` of shape (M,1,3) with ``hear_times`` (T,) -> (M,T).
    """
    t_e, D, p, _ = solve_retarded(track, r_obs, hear_times, c, tol, max_iter)
    v = track.velocity(t_e)
    rel = r_obs - p
    v_r = (v * rel).sum(-1) / D
    f = f0 * c / (c - v_r)
    if details:
        return f, {"t_e": t_e, "D": D, "v_r": v_r}
    return f


def forward_curve_naive(r_obs, track: Track, f0: float, hear_times, c: float):
    """WRONG-by-design comparison model: uses aircraft state at the hearing time."""
    t = np.asarray(hear_times, float)
    p = track.position(t)
    v = track.velocity(t)
    rel = r_obs - p
    v_r = (v * rel).sum(-1) / np.linalg.norm(rel, axis=-1)
    return f0 * c / (c - v_r)


def straight_line_retarded_tau(t_h, t_c, v, d, c):
    """Closed-form emission time offset tau = t_e - t_c for a straight constant-velocity pass.

    Solves (c^2 - v^2) tau^2 - 2 c^2 (t_h - t_c) tau + (c^2 (t_h - t_c)^2 - d^2) = 0 and keeps the
    physical root (t_e <= t_h).
    """
    D = np.asarray(t_h, float) - t_c
    a = c * c - v * v
    disc = c * c * v * v * D * D + a * d * d
    return (c * c * D - np.sqrt(disc)) / a


def straight_line_curve(t_h, f0, v, d, t_c, c, retarded: bool = True):
    """Straight-line measurement model (Stage-0 fit).

    v_r(t) = -v^2 (t - t_c) / sqrt(d^2 + v^2 (t - t_c)^2), evaluated at the emission time
    (retarded=True, exact for a straight pass) or naively at the hearing time.
    """
    t_h = np.asarray(t_h, float)
    tau = straight_line_retarded_tau(t_h, t_c, v, d, c) if retarded else t_h - t_c
    v_r = -v * v * tau / np.sqrt(d * d + v * v * tau * tau)
    return f0 * c / (c - v_r)


def retarded_cpa(track: Track, r_obs, c: float, batch: int = 2048):
    """Closest point of approach of ``track`` to observers ``r_obs`` (M,3).

    Returns dict of arrays (M,): t_cpa (emission, abs), d_cpa (slant m), t_heard (= t_cpa + d/c),
    speed (m/s at CPA), edge (CPA at an end of the segment, i.e. not a true minimum).
    """
    r_obs = np.atleast_2d(np.asarray(r_obs, float))
    M = len(r_obs)
    out = {k: np.empty(M) for k in ("t_cpa", "d_cpa", "t_heard", "speed")}
    out["edge"] = np.zeros(M, bool)
    P = track.pos
    n = track.n
    for s in range(0, M, batch):
        ro = r_obs[s:s + batch]
        d2 = ((P[None, :, :] - ro[:, None, :]) ** 2).sum(-1)  # (B,N)
        i = np.argmin(d2, axis=1)
        best_t = np.empty(len(ro))
        best_d = np.full(len(ro), np.inf)
        for side in (-1, 0):  # segments (i-1,i) and (i,i+1)
            a = np.clip(i + side, 0, n - 2)
            pa, pb = P[a], P[a + 1]
            seg = pb - pa
            u = np.clip(((ro - pa) * seg).sum(-1) / (seg * seg).sum(-1), 0.0, 1.0)
            q = pa + u[:, None] * seg
            dd = np.linalg.norm(ro - q, axis=-1)
            better = dd < best_d
            best_d = np.where(better, dd, best_d)
            best_t = np.where(better, track.t0 + (a + u) * track.dt, best_t)
        sl = slice(s, s + len(ro))
        out["t_cpa"][sl] = best_t
        out["d_cpa"][sl] = best_d
        out["t_heard"][sl] = best_t + best_d / c
        out["speed"][sl] = np.linalg.norm(track.velocity(best_t), axis=-1)
        out["edge"][sl] = (i == 0) | (i == n - 1)
    return out
