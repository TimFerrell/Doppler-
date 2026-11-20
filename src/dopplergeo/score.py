"""Spectrogram brightness scoring of modeled Doppler curves.

The score of a curve is the summed brightness (dB above the per-row noise floor, linearly
interpolated in frequency) under the curve over the event's spectrogram columns, maximised
over a time shift dt and an f0 shift df (the curve is rescaled by (f0+df)/f0, since the
heard frequency is proportional to f0). The sweep is done coarse-to-fine (coarse grid, then
the full-resolution grid in a neighbourhood of the coarse optimum), which is exact whenever
the score is unimodal on the coarse step scale (window ~1 s, bins ~1 Hz).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audio import Spectrogram


def _sweep(half: float, step: float) -> np.ndarray:
    n = int(round(half / step))
    return np.arange(-n, n + 1) * step


@dataclass
class ScoreResult:
    score: np.ndarray  # (B,)
    dt: np.ndarray
    df: np.ndarray


class EventScorer:
    """Scores batches of curves against one event window of a spectrogram.

    Curves are supplied sampled on ``self.tf`` (audio-relative seconds, uniform ``curve_step_s``),
    which covers the window plus the dt sweep margin.
    """

    def __init__(self, spec: Spectrogram, t_lo: float, t_hi: float, cfg: dict, f0: float):
        s = cfg["score"]
        self.f0 = float(f0)  # f0 the supplied curves were computed with; df scales them by (f0+df)/f0
        cols = (spec.times >= t_lo) & (spec.times <= t_hi)
        if cols.sum() < 2:
            raise ValueError(f"event window [{t_lo}, {t_hi}] holds <2 spectrogram columns")
        self.tj = spec.times[cols].astype(np.float64)
        Bm = spec.brightness[:, cols]
        self.nF, self.J = Bm.shape
        self.Bflat = np.ascontiguousarray(Bm).ravel()
        self.f_first = float(spec.freqs[0])
        self.bin = spec.bin_hz
        self.harm = [int(h) for h in s["harmonics"]]
        self.dt_half, self.dt_step = float(s["dt_sweep_s"]), float(s["dt_step_s"])
        self.df_half, self.df_step = float(s["df_sweep_hz"]), float(s["df_step_hz"])
        self.cdt, self.cdf = float(s["coarse_dt_step_s"]), float(s["coarse_df_step_hz"])
        self.step = float(s["curve_step_s"])
        self.quadratic = s["freq_interp"] == "quadratic"
        self.polish_div = int(s["polish_divisions"])
        margin = self.dt_half + 2 * self.step
        self.tf = np.arange(self.tj[0] - margin, self.tj[-1] + margin + self.step, self.step)
        self.jidx = np.arange(self.J)

    # ------------------------------------------------------------------ core
    def _eval(self, curves: np.ndarray, dts: np.ndarray, dfs: np.ndarray) -> np.ndarray:
        """curves (B,Nf); dts (B,K); dfs (B,L) f0 shifts -> summed brightness (B,K,L)."""
        B = curves.shape[0]
        u = (self.tj[None, None, :] - dts[:, :, None] - self.tf[0]) / self.step  # (B,K,J)
        i = np.clip(np.floor(u).astype(np.int64), 0, len(self.tf) - 2)
        w = u - i
        flat = i.reshape(B, -1)
        c0 = np.take_along_axis(curves, flat, axis=1).reshape(i.shape)
        c1 = np.take_along_axis(curves, flat + 1, axis=1).reshape(i.shape)
        F = c0 * (1 - w) + c1 * w  # (B,K,J) Hz
        total = np.zeros((B, dts.shape[1], dfs.shape[1]))
        for h in self.harm:
            scale = 1.0 + dfs[:, None, :, None] / self.f0  # f0 -> f0 + df scales the whole curve
            p = (h * F[:, :, None, :] * scale - self.f_first) / self.bin  # (B,K,L,J)
            if self.quadratic:
                # 3-point Lagrange interpolation around the nearest bin (O(h^3) error: no bin-centre bias)
                ok = np.isfinite(p) & (p >= 1) & (p <= self.nF - 2)
                p = np.where(ok, p, 1.0)
                k = np.rint(p).astype(np.int64)
                x = (p - k).astype(np.float32)
                idx = k * self.J + self.jidx
                ym, y0, yp = self.Bflat[idx - self.J], self.Bflat[idx], self.Bflat[idx + self.J]
                v = y0 + 0.5 * x * (yp - ym) + 0.5 * x * x * (yp - 2 * y0 + ym)
            else:
                ok = np.isfinite(p) & (p >= 0) & (p < self.nF - 1)
                p = np.where(ok, p, 0.0)
                k = p.astype(np.int64)
                fr = (p - k).astype(np.float32)
                idx = k * self.J + self.jidx
                v = self.Bflat[idx] * (1 - fr) + self.Bflat[idx + self.J] * fr
            total += np.where(ok, v, 0.0).sum(-1)
        return total

    def score(self, curves: np.ndarray, sweep_dt: bool = True) -> ScoreResult:
        curves = np.asarray(curves, np.float64)
        B = curves.shape[0]
        # coarse pass
        cdt = _sweep(self.dt_half, self.cdt) if sweep_dt else np.zeros(1)
        cdf = _sweep(self.df_half, self.cdf)
        tot = self._eval(curves, np.broadcast_to(cdt, (B, len(cdt))), np.broadcast_to(cdf, (B, len(cdf))))
        flat = tot.reshape(B, -1).argmax(1)
        kb, lb = np.unravel_index(flat, tot.shape[1:])
        dt0, df0 = cdt[kb], cdf[lb]
        # fine pass around the coarse optimum, on the full-resolution sweep lattice
        if sweep_dt:
            ndt = int(round(self.cdt / self.dt_step))
            fdt = dt0[:, None] + np.arange(-ndt, ndt + 1)[None, :] * self.dt_step
            fdt = np.clip(np.round(fdt / self.dt_step) * self.dt_step, -self.dt_half, self.dt_half)
        else:
            fdt = np.zeros((B, 1))
        ndf = int(round(self.cdf / self.df_step))
        fdf = df0[:, None] + np.arange(-ndf, ndf + 1)[None, :] * self.df_step
        fdf = np.clip(np.round(fdf / self.df_step) * self.df_step, -self.df_half, self.df_half)
        tot = self._eval(curves, fdt, fdf)
        flat = tot.reshape(B, -1).argmax(1)
        kb, lb = np.unravel_index(flat, tot.shape[1:])
        r = np.arange(B)
        best, bdt, bdf = tot.reshape(B, -1)[r, flat], fdt[r, kb], fdf[r, lb]
        if self.polish_div > 1:
            best, bdt, bdf = self._polish(curves, best, bdt, bdf, sweep_dt)
        return ScoreResult(best, bdt, bdf)

    def _polish(self, curves, best, bdt, bdf, sweep_dt):
        """2-D sub-lattice polish: +-1 lattice step around the optimum at step/polish_divisions.

        The score peak in (dt, df) is a narrow diagonal ridge (~0.1 s x 0.1 Hz across, since a
        time shift on the steep part of the curve trades against an f0 shift), so the spec's
        0.1 s / 0.1 Hz lattice alone leaves a quantisation error of a few % of the score -- the
        same order as the score difference between the true point and a point 100 m away.
        """
        n = self.polish_div
        offs_t = np.arange(-n, n + 1) * (self.dt_step / n) if sweep_dt else np.zeros(1)
        offs_f = np.arange(-n, n + 1) * (self.df_step / n)
        fdt = np.clip(bdt[:, None] + offs_t[None, :], -self.dt_half, self.dt_half)
        fdf = np.clip(bdf[:, None] + offs_f[None, :], -self.df_half, self.df_half)
        tot = self._eval(curves, fdt, fdf)
        B = len(best)
        flat = tot.reshape(B, -1).argmax(1)
        kb, lb = np.unravel_index(flat, tot.shape[1:])
        r = np.arange(B)
        val = tot.reshape(B, -1)[r, flat]
        imp = val > best
        return (np.where(imp, val, best), np.where(imp, fdt[r, kb], bdt), np.where(imp, fdf[r, lb], bdf))

    def score_exhaustive(self, curves: np.ndarray, sweep_dt: bool = True) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Full-resolution sweep (for tests/diagnostics). Returns (table (B,K,L), dts, dfs)."""
        curves = np.asarray(curves, np.float64)
        B = curves.shape[0]
        dts = _sweep(self.dt_half, self.dt_step) if sweep_dt else np.zeros(1)
        dfs = _sweep(self.df_half, self.df_step)
        return self._eval(curves, np.broadcast_to(dts, (B, len(dts))), np.broadcast_to(dfs, (B, len(dfs)))), dts, dfs


def brightness_score(spec: Spectrogram, curve_times: np.ndarray, curve_hz: np.ndarray, f0: float, cfg: dict,
                     t_lo: float | None = None, t_hi: float | None = None, sweep_dt: bool = True):
    """Convenience single-curve scorer: max summed brightness over the dt/df sweeps.

    ``curve_times`` are audio-relative seconds; the curve is resampled onto the scorer grid.
    Returns (score, dt, df).
    """
    t_lo = float(spec.times[0]) if t_lo is None else t_lo
    t_hi = float(spec.times[-1]) if t_hi is None else t_hi
    sc = EventScorer(spec, t_lo, t_hi, cfg, f0)
    c = np.interp(sc.tf, curve_times, curve_hz, left=np.nan, right=np.nan)[None, :]
    r = sc.score(c, sweep_dt)
    return float(r.score[0]), float(r.dt[0]), float(r.df[0])
