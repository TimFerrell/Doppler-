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
    band: list = field(default_factory=list)  # frequency band of the ridge this was fitted in

    @property
    def is_high(self) -> bool:
        """Fitted in the high (jet-tone) band."""
        return bool(self.band) and self.band[0] >= 250

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (float(v) if isinstance(v, (np.floating, float)) else v) for k, v in d.items()}


def _track_from(S, f, j0, k0, dt, max_slope_frac, max_jump_frac, max_jump_hz):
    n_f, n_t = S.shape
    k = np.empty(n_t, np.int64)
    k[j0] = k0
    df = f[1] - f[0]
    for order in (range(j0 + 1, n_t), range(j0 - 1, -1, -1)):
        prev = k0
        for j in order:
            if dt is not None:  # physical limit: a passing aircraft's tone slides at most ~4 %/s
                w = int(np.ceil((max_slope_frac * f[prev] * dt) / df)) + 1
            else:
                w = int(np.ceil((max_jump_frac * f[prev] + max_jump_hz) / df))
            a, b = max(0, prev - w), min(n_f, prev + w + 1)
            prev = a + int(np.argmax(S[a:b, j]))
            k[j] = prev
    return k


def track_ridge(S: np.ndarray, f: np.ndarray, max_jump_frac: float = 0.02, max_jump_hz: float = 4.0,
                dt: float | None = None, max_slope_frac: float = 0.04, n_seeds: int = 6) -> np.ndarray:
    """Follow one tonal line through a spectrogram (rows = freqs, cols = time).

    Started from the ``n_seeds`` strongest cells (in different parts of the recording); from each, column
    by column (forward and backward), the peak is searched only within the slope an aircraft can produce
    (or +-(max_jump_frac*f + max_jump_hz) when ``dt`` is unknown). Unlike a per-column maximum this cannot
    hop between an engine tone and its harmonics. The path with the highest mean level wins.
    Returns row indices per column.
    """
    n_f, n_t = S.shape
    colmax = S.max(axis=0)
    order = np.argsort(colmax)[::-1]
    seeds = []
    for j in order:  # strongest columns, at least ~5 % of the recording apart
        if all(abs(int(j) - s_) > max(3, n_t // 20) for s_ in seeds):
            seeds.append(int(j))
        if len(seeds) >= n_seeds:
            break
    best, best_score = None, -np.inf
    for j0 in seeds:
        k = _track_from(S, f, j0, int(np.argmax(S[:, j0])), dt, max_slope_frac, max_jump_frac, max_jump_hz)
        score = float(np.mean(S[k, np.arange(n_t)]))
        if score > best_score:
            best, best_score = k, score
    return best


def extract_ridge(spec: Spectrogram, band: tuple[float, float], t_lo: float | None = None,
                  t_hi: float | None = None, track: bool = False) -> Ridge:
    fsel = (spec.freqs >= band[0]) & (spec.freqs <= band[1])
    tsel = np.ones(len(spec.times), bool)
    if t_lo is not None:
        tsel &= spec.times >= t_lo
    if t_hi is not None:
        tsel &= spec.times <= t_hi
    S = spec.db[np.ix_(fsel, tsel)]
    f = spec.freqs[fsel]
    if track:
        # whiten rows (a broadband hump cannot capture the tracker) and smooth ~0.25 s in time (weak tones
        # stand out of the noise); the tracked rows are then read from the unsmoothed spectrogram
        from scipy.ndimage import uniform_filter1d

        dt_col = float(spec.times[1] - spec.times[0]) if len(spec.times) > 1 else 0.1
        W = S - np.median(S, axis=1, keepdims=True)
        W = uniform_filter1d(W, size=max(1, int(round(0.25 / dt_col))), axis=1)
        k = track_ridge(W, f, dt=dt_col)
    else:
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
              t_hi: float, rel: bool = False) -> Measurement:
    """Robust least-squares fit of the straight-line model to ridge points (t audio-relative s, f Hz)."""
    m = cfg["measure"]
    if len(t) < 8:
        return Measurement(np.nan, np.nan, np.nan, np.nan, np.nan, t_lo, t_hi, np.inf, int(len(t)), 0.0, 0.0,
                           success=False, band=[float(band[0]), float(band[1])])
    vb, db = m["v_bounds_mps"], m["d_bounds_m"]
    lo = [band[0], vb[0], db[0], t_lo - 120.0]
    hi = [band[1], vb[1], db[1], t_hi + 120.0]

    def resid(p):
        return straight_line_curve(t, p[0], p[1], p[2], p[3], c) - f

    f0g, vg, tcs = _initial_guesses(t, f, c)
    # tolerances in Hz scale with the tone for the jet band (the same timing noise is more Hz at 1.5 kHz)
    fscale = float(m["f_scale_hz"]) * (max(1.0, f0g / 150.0) if rel else 1.0)
    max_rms = float(m["max_rms_hz"]) * (max(1.0, f0g / 150.0) if rel else 1.0)
    best = None
    for tcg in tcs[:4]:
        for dg in (800.0, 2000.0, 4000.0, 8000.0):
            x0 = np.clip([f0g, vg, dg, tcg - dg / c], np.array(lo) + 1e-6, np.array(hi) - 1e-6)
            r = least_squares(resid, x0, bounds=(lo, hi), loss=m["loss"], f_scale=fscale,
                              x_scale=[1.0, 10.0, 500.0, 1.0], xtol=1e-12, ftol=1e-12, gtol=1e-12)
            if best is None or r.cost < best.cost:
                best = r
    p = best.x
    res = resid(p)
    inl = np.abs(res) < 3 * fscale
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
        band=[float(band[0]), float(band[1])],
        f0=float(p[0]), v=float(p[1]), d=float(p[2]), t_cpa=float(p[3]), t_cpa_heard=float(p[3] + p[2] / c),
        t_lo=t_lo, t_hi=t_hi, rms_hz=rms, n_cols=int(len(t)), inlier_frac=float(inl.mean()),
        spread_hz=float(f_model[0] - f_model[1]), stderr=stderr,
        success=bool(best.success and rms <= max_rms),
    )


def fit_curve(spec: Spectrogram, cfg: dict, c: float, t_lo: float | None = None, t_hi: float | None = None,
              band: tuple[float, float] | None = None) -> tuple[Measurement, Ridge]:
    """Extract the ridge (per-column max in the band) and fit the straight-line model -> (f0, v, d, t_cpa)."""
    band = tuple(band or cfg["measure"]["band_hz"])
    high = band[0] >= 250  # jet-engine band: follow one line, scale tolerances with frequency
    ridge = extract_ridge(spec, band, t_lo, t_hi, track=high)
    good = ridge.snr_db >= float(cfg["measure"]["min_column_snr_db"])
    t_lo = float(ridge.times[0]) if t_lo is None else float(t_lo)
    t_hi = float(ridge.times[-1]) if t_hi is None else float(t_hi)
    return fit_ridge(ridge.times[good], ridge.freq[good], cfg, c, band, t_lo, t_hi, rel=high), ridge


def detect_events(spec: Spectrogram, cfg: dict, c: float, band=None, min_spread=None,
                  on_window=None) -> tuple[list[Measurement], dict]:
    """Ridge-energy flyover detector with S-curve morphology test.

    Slides a window over the recording; a window is a candidate if enough columns have a
    prominent ridge in the detection band AND a straight-line Doppler fit is good, descending
    (approach -> recede), and its CPA lies in the window's central 60 %. Candidates within
    ``merge_s`` are merged, and each event is refit over ``measure.fit_window_s``.
    """
    d = cfg["detect"]
    band = tuple(band or d["band_hz"])
    min_spread = float(min_spread if min_spread is not None else d["min_spread_hz"])
    W, step = float(d["window_s"]), float(d["step_s"])
    t0, t1 = float(spec.times[0]), float(spec.times[-1])
    diag = {"windows": 0, "energetic": 0, "s_curve": 0, "duration_s": round(t1 - t0, 1), "rejected": []}
    cands: list[Measurement] = []
    short = t1 - t0 <= W
    starts = np.arange(t0, max(t0, t1 - W) + 1e-9, step) if not short else np.array([t0])
    # a clip shorter than one window is fitted whole; its closest approach may sit nearer the edges
    margin = 0.1 if short else 0.2
    for wi, s in enumerate(starts):
        diag["windows"] += 1
        e = min(s + W, t1)
        ridge = extract_ridge(spec, band, s, e, track=band[0] >= 250)
        cover = float(np.mean(ridge.snr_db >= cfg["measure"]["min_column_snr_db"]))
        rec = {"window_s": [round(float(s), 1), round(float(e), 1)], "tone_coverage": round(cover, 2), "reasons": [],
               "band_hz": [band[0], band[1]]}
        if cover < float(d["min_coverage"]):
            rec["reasons"].append(f"a clear engine tone is present in only {cover:.0%} of the window "
                                  f"(needs {float(d['min_coverage']):.0%})")
            diag["rejected"].append(rec)
            if on_window:
                on_window(wi, len(starts), rec, None, False)
            continue
        diag["energetic"] += 1
        meas, _ = fit_curve(spec, cfg, c, s, e, band)
        lo, hi = s + margin * (e - s), e - margin * (e - s)
        rec["fit"] = {k: (round(float(getattr(meas, k)), 2) if np.isfinite(getattr(meas, k)) else None)
                      for k in ("f0", "v", "d", "t_cpa_heard", "rms_hz", "spread_hz", "inlier_frac")}
        rms_lim = float(d["max_rms_hz"]) * (max(1.0, meas.f0 / 150.0) if band[0] >= 250 and np.isfinite(meas.f0) else 1.0)
        if not np.isfinite(meas.rms_hz) or meas.rms_hz > rms_lim:
            rec["reasons"].append(f"the tone does not follow a single plane's Doppler curve "
                                  f"(fit error {meas.rms_hz:.2f} Hz, limit {rms_lim:.1f} Hz)")
        if meas.inlier_frac < float(d["min_inlier_frac"]):
            rec["reasons"].append(f"only {meas.inlier_frac:.0%} of the tone points fit the curve")
        if meas.spread_hz < min_spread:
            rec["reasons"].append(f"the pitch barely changes ({meas.spread_hz:.1f} Hz drop, needs "
                                  f"{min_spread:g} Hz): no plane passing, or far away")
        if not lo <= meas.t_cpa_heard <= hi:
            rec["reasons"].append(f"the closest approach (fitted at {meas.t_cpa_heard:.1f} s) is not inside the "
                                  f"recording's middle part ({lo:.1f}–{hi:.1f} s): the clip misses the moment the plane "
                                  "was nearest")
        if on_window:
            on_window(wi, len(starts), rec, meas, not rec["reasons"])
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
        events.append(fit_event(spec, cfg, c, tc, band=band))
    diag["events"] = len(events)
    log.info("detect: %s", diag)
    return events, diag


def fit_event(spec: Spectrogram, cfg: dict, c: float, t_center: float, manual: bool = False,
              band=None) -> Measurement:
    half = float(cfg["measure"]["fit_window_s"]) / 2
    lo = max(float(spec.times[0]), t_center - half)
    hi = min(float(spec.times[-1]), t_center + half)
    meas, _ = fit_curve(spec, cfg, c, lo, hi, band)
    meas.manual = manual
    return meas


def detect_all(low: Spectrogram, high: Spectrogram | None, cfg: dict, c: float,
               on_window=None) -> tuple[list[Measurement], dict]:
    """Flyovers in the prop band (low spectrogram) and, if enabled, the jet-tone band (high spectrogram).

    Events are returned in time order; a jet-band event at (nearly) the same moment as a prop-band one
    is kept too (it may be a different aircraft, or a harmonic: the search decides).
    """
    cb = (lambda band: (lambda *a: on_window(band, *a))) if on_window else (lambda band: None)
    events, diag = detect_events(low, cfg, c, on_window=cb("prop"))
    diag = {"prop_band": diag}
    jb = cfg["detect"].get("jet_band_hz")
    if high is not None and jb:
        jev, jdiag = detect_events(high, cfg, c, band=jb, min_spread=cfg["detect"]["jet_min_spread_hz"],
                                   on_window=cb("jet"))
        diag["jet_band"] = jdiag
        events = events + jev
    events.sort(key=lambda m: m.t_cpa_heard)
    diag["events"] = len(events)
    diag["rejected"] = (diag["prop_band"].get("rejected", []) + diag.get("jet_band", {}).get("rejected", []))[:16]
    return events, diag


def fit_manual(low: Spectrogram, high: Spectrogram | None, cfg: dict, c: float, t: float) -> Measurement:
    """A manually given flyover time: fit it in the prop band, else in the jet band, keeping the better fit."""
    m = fit_event(low, cfg, c, t, manual=True)
    jb = cfg["detect"].get("jet_band_hz")
    if high is not None and jb:
        mj = fit_event(high, cfg, c, t, manual=True, band=jb)
        # compare relative fit error (Hz scales with frequency)
        rel = (m.rms_hz / m.f0) if (m.success and np.isfinite(m.f0)) else np.inf
        relj = (mj.rms_hz / mj.f0) if (mj.success and np.isfinite(mj.f0)) else np.inf
        if relj < rel:
            m = mj
    return m
