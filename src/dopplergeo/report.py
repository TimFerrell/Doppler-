"""Run reports: report.md, candidates.csv (QGIS-importable), PNG plots."""
from __future__ import annotations

import datetime as dt
import json
import platform
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


def utc(ts: float | None) -> str:
    if ts is None or not np.isfinite(ts):
        return "unknown"
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def write_candidates_csv(pts: pd.DataFrame, path: Path) -> None:
    cols = ["rank", "lat", "lon", "score"] + [c for c in pts.columns if c.startswith(("score_e", "df_e"))] + [
        "dt1", "combo", "stage", "step_m"]
    pts[cols].to_csv(path, index=False, float_format="%.7f")


def plot_overlay(spec, ev, curve_t, curve_hz, out: Path, title: str, truth_t=None, truth_hz=None, ridge=None):
    m = ev.meas
    sel_t = (spec.times >= m.t_lo) & (spec.times <= m.t_hi)
    finite = np.isfinite(curve_hz)
    lo = np.nanmin(curve_hz[finite]) if finite.any() else 80
    hi = np.nanmax(curve_hz[finite]) if finite.any() else 300
    pad = 0.15 * (hi - lo) + 5
    sel_f = (spec.freqs >= lo - pad) & (spec.freqs <= hi + pad)
    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    S = spec.db[np.ix_(sel_f, sel_t)]
    vmin, vmax = np.percentile(S, [5, 99.7])
    ax.imshow(S, origin="lower", aspect="auto", cmap="magma", vmin=vmin, vmax=vmax,
              extent=[spec.times[sel_t][0], spec.times[sel_t][-1], spec.freqs[sel_f][0], spec.freqs[sel_f][-1]])
    if ridge is not None:
        ax.plot(ridge[0], ridge[1], ".", ms=1.5, color="#9ad", alpha=0.6, label="ridge (per-column max)")
    if truth_t is not None:
        ax.plot(truth_t, truth_hz, "-", lw=2.4, color="#2ecc71", alpha=0.9, label="true curve")
    ax.plot(curve_t, curve_hz, "--", lw=1.4, color="#00e5ff", label="modeled (best candidate, retarded time)")
    ax.axvline(m.t_cpa_heard, color="w", lw=0.6, ls=":")
    ax.set_xlim(m.t_lo, m.t_hi)
    ax.set_ylim(spec.freqs[sel_f][0], spec.freqs[sel_f][-1])
    ax.set_xlabel("audio time (s)")
    ax.set_ylabel("Hz")
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def plot_heatmap(pts: pd.DataFrame, tracks_xy: list, out: Path, truth_xy=None, title="candidate scores"):
    fig, ax = plt.subplots(figsize=(8, 7.5), dpi=110)
    order = pts.sort_values("score")
    sc = ax.scatter(order["x"] / 1000, order["y"] / 1000, c=order["score"], s=np.where(order["stage"] > 0, 6, 14),
                    cmap="viridis", marker="s", linewidths=0)
    for name, (tx, ty) in tracks_xy:
        ax.plot(tx / 1000, ty / 1000, "-", color="#d35400", lw=1, alpha=0.8)
        if len(tx):
            ax.annotate(name, (tx[-1] / 1000, ty[-1] / 1000), fontsize=7, color="#d35400")
    best = pts.iloc[0]
    ax.plot(best["x"] / 1000, best["y"] / 1000, "o", mfc="none", mec="red", ms=14, mew=2, label="#1 candidate")
    if truth_xy is not None:
        ax.plot(truth_xy[0] / 1000, truth_xy[1] / 1000, "*", color="white", mec="k", ms=14, label="truth")
    xr = pts["x"] / 1000
    yr = pts["y"] / 1000
    ax.set_xlim(xr.min() - 0.5, xr.max() + 0.5)
    ax.set_ylim(yr.min() - 0.5, yr.max() + 0.5)
    ax.set_aspect("equal")
    ax.set_xlabel("east (km, local AEQD)")
    ax.set_ylabel("north (km)")
    ax.set_title(title)
    ax.legend(loc="lower right", fontsize=8)
    fig.colorbar(sc, ax=ax, label="summed brightness")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def plot_zoom(pts: pd.DataFrame, out: Path, truth_xy=None, half_m=1500.0):
    best = pts.iloc[0]
    near = pts[(np.abs(pts["x"] - best["x"]) < half_m) & (np.abs(pts["y"] - best["y"]) < half_m)
               & (pts["combo"] == best["combo"])].sort_values("score")
    fig, ax = plt.subplots(figsize=(6.5, 6), dpi=110)
    sc = ax.scatter(near["x"] - best["x"], near["y"] - best["y"], c=near["score"], s=np.where(near["stage"] > 0, 30, 70),
                    cmap="viridis", marker="s")
    ax.plot(0, 0, "o", mfc="none", mec="red", ms=14, mew=2, label="#1")
    if truth_xy is not None:
        ax.plot(truth_xy[0] - best["x"], truth_xy[1] - best["y"], "*", color="white", mec="k", ms=14, label="truth")
    ax.set_aspect("equal")
    ax.set_xlabel("m east of #1")
    ax.set_ylabel("m north of #1")
    ax.set_title("score around #1 (combo " + str(best["combo"]) + ")", fontsize=9)
    ax.legend(fontsize=8)
    fig.colorbar(sc, ax=ax)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def _md_table(df: pd.DataFrame, floatfmt: dict | None = None) -> str:
    floatfmt = floatfmt or {}
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, (float, np.floating)):
                cells.append(format(v, floatfmt.get(c, ".3f")))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write_report(out_dir: Path, ctx: dict) -> Path:
    """ctx keys: title, command, inputs[{name,path,sha256}], attestation, config, events[Measurement],
    detect_diag, stage_rows, timings, best(DataFrame), pts(DataFrame), runner_up, truth, plots, notes."""
    L = [f"# {ctx['title']}", ""]
    try:
        from .web.app import code_version

        ver = code_version()["label"]
    except Exception:  # noqa: BLE001
        from . import __version__ as ver
    L += [f"- generated: {utc(dt.datetime.now(dt.timezone.utc).timestamp())} by dopplergeo {ver}",
          f"- command: `{ctx.get('command', '')}`",
          f"- host: {platform.platform()}, python {platform.python_version()}", ""]
    L += ["## Authorization", "", ctx["attestation"], ""]
    L += ["## Inputs", "", "| input | path | sha256 |", "|---|---|---|"]
    for i in ctx["inputs"]:
        L.append(f"| {i['name']} | `{i['path']}` | `{i.get('sha256', '-')}` |")
    L += ["", f"- audio start (UTC): {utc(ctx.get('audio_start'))}",
          f"- time reference: `{ctx.get('time_ref')}`", f"- speed of sound: {ctx.get('c'):.2f} m/s",
          f"- elevation: {ctx.get('elevation')}", f"- region: {ctx.get('region')}", ""]
    L += ["## Measurements (Stage-0 straight-line fits)", ""]
    rows = []
    for k, m in enumerate(ctx["events"], 1):
        se = m.stderr or {}
        rows.append({"event": k, "manual": m.manual, "window_s": f"{m.t_lo:.1f}-{m.t_hi:.1f}",
                     "f0_hz": m.f0, "v_mps": m.v, "d_cpa_m": m.d, "t_cpa_heard_s": m.t_cpa_heard,
                     "rms_hz": m.rms_hz, "cols": m.n_cols, "inliers": m.inlier_frac,
                     "se(f0,v,d,t)": ", ".join(f"{se.get(n, np.nan):.3g}" for n in ("f0", "v", "d", "t_cpa"))})
    L += [_md_table(pd.DataFrame(rows)), ""]
    if ctx.get("detect_diag"):
        L += [f"Event detector: `{json.dumps(ctx['detect_diag'])}`", ""]
    L += ["## Candidate counts per stage", "", _md_table(pd.DataFrame(ctx["stage_rows"])), ""]
    best = ctx["best"]
    pts = ctx["pts"]
    L += ["## Ranking", "", "### Best point per flight combination (global rank)", ""]
    bcols = ["combo_rank", "combo", "lat", "lon", "score"] + [c for c in best.columns if c.startswith("score_e")] + [
        "dt1"] + [c for c in best.columns if c.startswith("df_e")]
    L += [_md_table(best[bcols].head(10), {"lat": ".6f", "lon": ".6f", "score": ".1f"}), ""]
    L += ["### Top-10 candidate points", ""]
    pcols = ["rank", "combo", "lat", "lon", "score", "dt1", "stage", "step_m"]
    L += [_md_table(pts[pcols].head(10), {"lat": ".6f", "lon": ".6f", "score": ".1f", "step_m": ".0f"}), ""]
    top = pts.iloc[0]
    if len(best) > 1:
        m2 = best.iloc[1]
        L.append(f"- margin vs #2 flight combination: {top['score'] - m2['score']:.1f} "
                 f"({100 * (top['score'] - m2['score']) / top['score']:.2f} %)")
    ru = ctx.get("runner_up")
    if ru is not None:
        sep = ctx.get("runner_up_sep_m")
        L.append(f"- margin vs best candidate >= {sep:.0f} m away: {top['score'] - ru['score']:.1f} "
                 f"({100 * (top['score'] - ru['score']) / top['score']:.2f} %) at {ru['lat']:.6f}, {ru['lon']:.6f}")
    L.append(f"- **#1: {top['lat']:.6f}, {top['lon']:.6f}** (combo {top['combo']})")
    L.append("")
    if ctx.get("truth"):
        t = ctx["truth"]
        L += ["## Truth comparison (synthetic/known-location run)", "",
              f"- true observer: {t['lat']:.6f}, {t['lon']:.6f}",
              f"- #1 error: **{t['err_m']:.1f} m**; true flight combination: {t.get('combo', '-')}",
              f"- rank of the scored point nearest the truth ({t['nearest_m']:.0f} m away): {t['nearest_rank']}",
              f"- score at exact truth: {t['score']:.1f} (vs #1 {top['score']:.1f})", ""]
    L += ["## Timings (wall, s)", "", _md_table(pd.DataFrame([{"stage": k, "seconds": v}
                                                              for k, v in ctx["timings"].items()]),
                                                {"seconds": ".2f"}), ""]
    L += ["## Plots", ""]
    for name, p in ctx.get("plots", []):
        L += [f"### {name}", "", f"![{name}]({p})", ""]
    if ctx.get("notes"):
        L += ["## Notes", ""] + [f"- {n}" for n in ctx["notes"]] + [""]
    L += ["## Configuration", "", "```json", json.dumps(ctx["config"], indent=1, default=str), "```", ""]
    path = out_dir / "report.md"
    path.write_text("\n".join(L))
    return path


def expected_overlays(spec, cfg, diag, c: float = 343.0) -> dict:
    """What the detector expected to see, for drawing on the recording's spectrogram.

    - ``expected``: an illustrative prop flyover (pitch curve) placed mid-recording at the strongest tone
      actually present (or 120 Hz), with typical light-aircraft speed and distance.
    - ``near_misses``: the curve the detector tried in each rejected stretch, with its reasons.
    - ``band``: the frequency band searched.
    """
    from .measure import extract_ridge
    from .model import straight_line_curve

    lo, hi = (float(v) for v in cfg["detect"]["band_hz"])
    rg = extract_ridge(spec, (lo, hi))
    good = rg.snr_db >= float(cfg["measure"]["min_column_snr_db"])
    f0 = float(np.median(rg.freq[good])) if good.sum() >= 5 else 120.0
    f0 = min(max(f0, lo + 25), hi - 45)
    t_a, t_b = float(spec.times[0]), float(spec.times[-1])
    t_mid = 0.5 * (t_a + t_b)
    v, d = 60.0, 600.0  # a close, typical light-aircraft pass: makes the S-shape unmistakable
    tt = np.linspace(t_a, t_b, 300)
    ff = straight_line_curve(tt, f0, v, d, t_mid - d / c, c)
    near = []
    for r in (diag or {}).get("rejected", []):
        fit = r.get("fit") or {}
        if not all(fit.get(k) is not None for k in ("f0", "v", "d", "t_cpa_heard")):
            continue
        a, b = r["window_s"]
        t = np.linspace(a, b, 200)
        f = straight_line_curve(t, fit["f0"], fit["v"], fit["d"], fit["t_cpa_heard"] - fit["d"] / c, c)
        near.append({"t": np.round(t, 2), "f": np.round(f, 2), "reasons": r["reasons"], "window_s": r["window_s"]})
    return {"band": [lo, hi], "expected": {"t": np.round(tt, 2), "f": np.round(ff, 2), "f0": f0, "t_cpa": t_mid,
                                           "v": v, "d": d},
            "near_misses": near}


def write_no_events_report(out_dir, spec, cfg, diag, meas, inputs, attestation, title, c: float = 343.0) -> Path:
    """Diagnostic report when no usable flyover was found: spectrogram + reasons, in plain language."""
    from .measure import extract_ridge

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lo, hi = cfg["detect"]["band_hz"]
    sel = (spec.freqs >= max(20.0, lo - 20)) & (spec.freqs <= hi + 100)
    S = spec.db[sel]
    fig, ax = plt.subplots(figsize=(11, 5), dpi=110)
    vmin, vmax = np.percentile(S, [5, 99.7])
    ax.imshow(S, origin="lower", aspect="auto", cmap="magma", vmin=vmin, vmax=vmax,
              extent=[spec.times[0], spec.times[-1], spec.freqs[sel][0], spec.freqs[sel][-1]])
    rg = extract_ridge(spec, (lo, hi))
    good = rg.snr_db >= cfg["measure"]["min_column_snr_db"]
    ax.plot(rg.times[good], rg.freq[good], ".", ms=2.5, color="#5fd3ff", alpha=0.8,
            label="strongest tone per moment (clear ones only)")
    ov = expected_overlays(spec, cfg, diag, c)
    ax.axhline(lo, color="w", lw=0.8, ls=":", alpha=0.6)
    ax.axhline(hi, color="w", lw=0.8, ls=":", alpha=0.6)
    ax.text(spec.times[0], hi, f" searched band {lo:.0f}–{hi:.0f} Hz", color="w", fontsize=8, va="bottom", alpha=0.8)
    e = ov["expected"]
    ax.plot(e["t"], e["f"], "--", color="white", lw=2, alpha=0.85,
            label="what a passing prop plane would look like here (illustration)")
    ax.plot([e["t_cpa"]], [e["f0"]], "o", color="white", ms=7, mfc="none", mew=2)
    ax.annotate("steepest = moment the plane is nearest", (e["t_cpa"], e["f0"]), xytext=(10, 18),
                textcoords="offset points", color="white", fontsize=8,
                arrowprops={"arrowstyle": "->", "color": "white", "lw": 0.8})
    for k, nm in enumerate(ov["near_misses"]):
        ax.plot(nm["t"], nm["f"], "--", color="#ff6b6b", lw=1.6, alpha=0.9,
                label="closest attempt, rejected" if k == 0 else None)
    ax.set_xlabel("seconds into the recording")
    ax.set_ylabel("Hz")
    ax.set_title("What the recording contains (40–400 Hz): a passing prop plane shows as a line that slides down in pitch")
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "recording_spectrogram.png")
    plt.close(fig)
    dur = float(spec.times[-1] - spec.times[0] + spec.window / spec.sr)
    L = [f"# {title}: no usable flyover found", "", "## Authorization", "", attestation, "", "## Inputs", ""]
    for i in inputs or []:
        L.append(f"- {i['name']}: `{i['path']}` (sha256 `{i.get('sha256', '-')}`)")
    L += ["", "## What happened", "",
          f"The recording is {dur:.0f} s long. The tool looks for the pitch curve of a propeller plane passing by: the "
          "engine hum (roughly 60–300 Hz) slides from a higher to a lower pitch as the plane comes closer and then "
          "moves away, steepest at the moment it is nearest. No part of this recording showed a complete curve "
          "like that, so there was nothing to match against the flight data.", ""]
    rej = (diag or {}).get("rejected") or []
    if rej:
        L += ["### Why each part was rejected", ""]
        for r in rej:
            w = r["window_s"]
            L.append(f"- **{w[0]:.0f}–{w[1]:.0f} s**: " + "; ".join(r["reasons"]))
        L.append("")
    if meas:
        L += ["### Flyovers given or found but not fitted", ""]
        for k, m in enumerate(meas, 1):
            L.append(f"- event {k} around {m.t_cpa_heard:.1f} s: fit error {m.rms_hz:.2f} Hz, {m.n_cols} usable moments")
        L.append("")
    L += ["## What to try", "",
          "- Look at the spectrogram below. If you can see a line sliding down in pitch, note the time where it "
          "drops fastest and enter it under *When the planes passed* in the Locate form.",
          "- Record longer: about a minute before and after the loudest moment of a flyover. The tool needs the "
          "moment the plane is nearest to be inside the recording, ideally in the middle."
          + (f" This recording is only {dur:.0f} s long." if dur < 90 else ""),
          "- Propeller planes only: jets and helicopters do not produce a usable steady engine tone.",
          "- Wind, talking and traffic noise can drown the engine tone; a sheltered spot helps.", "",
          "## Plots", "", "### The recording", "", "![recording spectrogram](recording_spectrogram.png)", ""]
    if diag:
        L += ["## Detector details", "", "```json", json.dumps(diag, indent=1, default=str), "```", ""]
    p = out_dir / "report.md"
    p.write_text("\n".join(L))
    return p
