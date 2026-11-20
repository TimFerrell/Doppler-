"""Stage-0 measurement: ridge extraction, straight-line Doppler fit, flyover detection."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.optimize import least_squares

from .audio import Spectrogram
from .model import straight_line_curve

log = logging.getLogger(__name__)


@dataclass
class Ridge:
    times: np.ndarray
    freq: np.ndarray  # Hz, sub-bin (parabolic) interpolated per-column maximum
    snr_db: np.ndarray  # peak dB minus column median within the band


@dataclass
class Measurement:
    """Fitted straight-line pass. Times are audio-relative seconds."""

    f0: float
    v: float
    d: float
    t_cpa: float  # emission-time CPA (audio-relative)
    t_cpa_heard: float  # = t_cpa + d/c: when the CPA sound (f == f0) is heard
    t_lo: float  # fit window
    t_hi: float
    rms_hz: float
    n_cols: int
    inlier_frac: float
    spread_hz: float
    stderr: dict = field(default_factory=dict)
    success: bool = True
    manual: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (float(v) if isinstance(v, (np.floating, float)) else v) for k, v in d.items()}


def extract_ridge(spec: Spectrogram, band: tuple[float, float], t_lo: float | None = None,
                  t_hi: float | None = None) -> Ridge:
    fsel = (spec.freqs >= band[0]) & (spec.freqs <= band[1])
    tsel = np.ones(len(spec.times), bool)
    if t_lo is not None:
        tsel &= spec.times >= t_lo
    if t_hi is not None:
        tsel &= spec.times <= t_hi
    S = spec.db[np.ix_(fsel, tsel)]
    f = spec.freqs[fsel]
    k = np.argmax(S, axis=0)
    j = np.arange(S.shape[1])
    kc = np.clip(k, 1, len(f) - 2)
    a, b, cc = S[kc - 1, j], S[kc, j], S[kc + 1, j]
    den = a - 2 * b + cc
    off = np.where((k == kc) & (den < 0), 0.5 * (a - cc) / np.where(den == 0, 1, den), 0.0)
    off = np.clip(off, -0.5, 0.5)
    freq = f[kc] + off * (f[1] - f[0])
    freq = np.where(k == kc, freq, f[k])
    snr = S[k, j] - np.median(S, axis=0)
    return Ridge(spec.times[tsel], freq, snr)


def _initial_guesses(t, f, c):
    fh, fl = np.percentile(f, 95), np.percentile(f, 5)
    f0 = 2 * fh * fl / (fh + fl)
    v = np.clip(c * (fh - fl) / (fh + fl), 30, 200)
    # CPA guess: where the (median-smoothed) ridge crosses f0 going down
    k = max(3, len(f) // 30)
    fs = np.convolve(f, np.ones(k) / k, mode="same")
    cross = np.where((fs[:-1] >= f0) & (fs[1:] < f0))[0]
    tcs = [t[i] for i in cross] if len(cross) else []
    tcs.append(t[np.argmin(np.abs(fs - f0))])
    return f0, v, tcs


def fit_ridge(t: np.ndarray, f: np.ndarray, cfg: dict, c: float, band: tuple[float, float], t_lo: float,
              t_hi: float) -> Measurement:
    """Robust least-squares fit of the straight-line model to ridge points (t audio-relative s, f Hz)."""
    m = cfg["measure"]
    if len(t) < 8:
        return Measurement(np.nan, np.nan, np.nan, np.nan, np.nan, t_lo, t_hi, np.inf, int(len(t)), 0.0, 0.0,
                           success=False)
    vb, db = m["v_bounds_mps"], m["d_bounds_m"]
    lo = [band[0], vb[0], db[0], t_lo - 120.0]
    hi = [band[1], vb[1], db[1], t_hi + 120.0]

    def resid(p):
        return straight_line_curve(t, p[0], p[1], p[2], p[3], c) - f

    f0g, vg, tcs = _initial_guesses(t, f, c)
    best = None
    for tcg in tcs[:4]:
        for dg in (800.0, 2000.0, 4000.0, 8000.0):
            x0 = np.clip([f0g, vg, dg, tcg - dg / c], np.array(lo) + 1e-6, np.array(hi) - 1e-6)
            r = least_squares(resid, x0, bounds=(lo, hi), loss=m["loss"], f_scale=float(m["f_scale_hz"]),
                              x_scale=[1.0, 10.0, 500.0, 1.0], xtol=1e-12, ftol=1e-12, gtol=1e-12)
            if best is None or r.cost < best.cost:
                best = r
    p = best.x
    res = resid(p)
    inl = np.abs(res) < 3 * float(m["f_scale_hz"])
    rms = float(np.sqrt(np.mean(res[inl] ** 2))) if inl.any() else np.inf
    stderr = {}
    try:
        J = best.jac[inl] if inl.sum() > 4 else best.jac
        cov = np.linalg.inv(J.T @ J) * (rms ** 2)
        stderr = dict(zip(("f0", "v", "d", "t_cpa"), np.sqrt(np.clip(np.diag(cov), 0, None)).tolist()))
    except np.linalg.LinAlgError:
        pass
    f_model = straight_line_curve(np.array([t_lo, t_hi]), p[0], p[1], p[2], p[3], c)
    return Measurement(
        f0=float(p[0]), v=float(p[1]), d=float(p[2]), t_cpa=float(p[3]), t_cpa_heard=float(p[3] + p[2] / c),
        t_lo=t_lo, t_hi=t_hi, rms_hz=rms, n_cols=int(len(t)), inlier_frac=float(inl.mean()),
        spread_hz=float(f_model[0] - f_model[1]), stderr=stderr,
        success=bool(best.success and rms <= float(m["max_rms_hz"])),
    )


def fit_curve(spec: Spectrogram, cfg: dict, c: float, t_lo: float | None = None, t_hi: float | None = None,
              band: tuple[float, float] | None = None) -> tuple[Measurement, Ridge]:
    """Extract the ridge (per-column max in the band) and fit the straight-line model -> (f0, v, d, t_cpa)."""
    band = tuple(band or cfg["measure"]["band_hz"])
    ridge = extract_ridge(spec, band, t_lo, t_hi)
    good = ridge.snr_db >= float(cfg["measure"]["min_column_snr_db"])
    t_lo = float(ridge.times[0]) if t_lo is None else float(t_lo)
    t_hi = float(ridge.times[-1]) if t_hi is None else float(t_hi)
    return fit_ridge(ridge.times[good], ridge.freq[good], cfg, c, band, t_lo, t_hi), ridge


def detect_events(spec: Spectrogram, cfg: dict, c: float) -> tuple[list[Measurement], dict]:
    """Ridge-energy flyover detector with S-curve morphology test.

    Slides a window over the recording; a window is a candidate if enough columns have a
    prominent ridge in the detection band AND a straight-line Doppler fit is good, descending
    (approach -> recede), and its CPA lies in the window's central 60 %. Candidates within
    ``merge_s`` are merged, and each event is refit over ``measure.fit_window_s``.
    """
    d = cfg["detect"]
    band = tuple(d["band_hz"])
    W, step = float(d["window_s"]), float(d["step_s"])
    t0, t1 = float(spec.times[0]), float(spec.times[-1])
    diag = {"windows": 0, "energetic": 0, "s_curve": 0, "duration_s": round(t1 - t0, 1), "rejected": []}
    cands: list[Measurement] = []
    short = t1 - t0 <= W
    starts = np.arange(t0, max(t0, t1 - W) + 1e-9, step) if not short else np.array([t0])
    # a clip shorter than one window is fitted whole; its closest approach may sit nearer the edges
    margin = 0.1 if short else 0.2
    for s in starts:
        diag["windows"] += 1
        e = min(s + W, t1)
        ridge = extract_ridge(spec, band, s, e)
        cover = float(np.mean(ridge.snr_db >= cfg["measure"]["min_column_snr_db"]))
        rec = {"window_s": [round(float(s), 1), round(float(e), 1)], "tone_coverage": round(cover, 2), "reasons": []}
        if cover < float(d["min_coverage"]):
            rec["reasons"].append(f"a clear engine tone is present in only {cover:.0%} of the window "
                                  f"(needs {float(d['min_coverage']):.0%})")
            diag["rejected"].append(rec)
            continue
        diag["energetic"] += 1
        meas, _ = fit_curve(spec, cfg, c, s, e, band)
        lo, hi = s + margin * (e - s), e - margin * (e - s)
        rec["fit"] = {k: (round(float(getattr(meas, k)), 2) if np.isfinite(getattr(meas, k)) else None)
                      for k in ("f0", "v", "d", "t_cpa_heard", "rms_hz", "spread_hz", "inlier_frac")}
        if not np.isfinite(meas.rms_hz) or meas.rms_hz > float(d["max_rms_hz"]):
            rec["reasons"].append(f"the tone does not follow a single plane's Doppler curve "
                                  f"(fit error {meas.rms_hz:.2f} Hz, limit {float(d['max_rms_hz'])} Hz)")
        if meas.inlier_frac < float(d["min_inlier_frac"]):
            rec["reasons"].append(f"only {meas.inlier_frac:.0%} of the tone points fit the curve")
        if meas.spread_hz < float(d["min_spread_hz"]):
            rec["reasons"].append(f"the pitch barely changes ({meas.spread_hz:.1f} Hz drop, needs "
                                  f"{float(d['min_spread_hz'])} Hz): no plane passing, or far away")
        if not lo <= meas.t_cpa_heard <= hi:
            rec["reasons"].append(f"the closest approach (fitted at {meas.t_cpa_heard:.1f} s) is not inside the "
                                  f"recording's middle part ({lo:.1f}–{hi:.1f} s): the clip misses the moment the plane "
                                  "was nearest")
        if rec["reasons"]:
            diag["rejected"].append(rec)
            continue
        diag["s_curve"] += 1
        cands.append(meas)
    diag["rejected"] = diag["rejected"][:12]
    cands.sort(key=lambda m: m.t_cpa_heard)
    merged: list[list[Measurement]] = []
    for m in cands:
        if merged and m.t_cpa_heard - merged[-1][-1].t_cpa_heard <= float(d["merge_s"]):
            merged[-1].append(m)
        else:
            merged.append([m])
    events = []
    for grp in merged:
        tc = float(np.median([m.t_cpa_heard for m in grp]))
        events.append(fit_event(spec, cfg, c, tc))
    diag["events"] = len(events)
    log.info("detect: %s", diag)
    return events, diag


def fit_event(spec: Spectrogram, cfg: dict, c: float, t_center: float, manual: bool = False) -> Measurement:
    half = float(cfg["measure"]["fit_window_s"]) / 2
    lo = max(float(spec.times[0]), t_center - half)
    hi = min(float(spec.times[-1]), t_center + half)
    meas, _ = fit_curve(spec, cfg, c, lo, hi)
    meas.manual = manual
    return meas
