"""End-to-end orchestration: measure -> candidate flights -> grid -> locked scoring -> ranking."""
from __future__ import annotations

import itertools
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import adsb, filters, geo, grid as gridmod, model
from .config import speed_of_sound
from .audio import Spectrogram
from .measure import Measurement
from .score import EventScorer

log = logging.getLogger(__name__)


@dataclass
class Event:
    meas: Measurement
    scorer: EventScorer


@dataclass
class StageLog:
    rows: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)

    def count(self, stage: str, n_in, n_out, note: str = "") -> None:
        self.rows.append({"stage": stage, "in": n_in, "out": n_out, "note": note})
        log.info("[%s] in=%s out=%s %s", stage, n_in, n_out, note)

    def timer(self, name: str):
        log_ = self

        class _T:
            def __enter__(self):
                self.t = time.perf_counter()

            def __exit__(self, *a):
                log_.timings[name] = log_.timings.get(name, 0.0) + time.perf_counter() - self.t

        return _T()


def make_events(spec: Spectrogram, meas: list[Measurement], cfg: dict) -> list[Event]:
    return [Event(m, EventScorer(spec, m.t_lo, m.t_hi, cfg, m.f0)) for m in meas]


# ----------------------------------------------------------------------------- scoring
def _score_batch(r_obs, tracks, events, c, cfg, time_ref, audio_start):
    phys = cfg["physics"]
    tol, it = float(phys["retarded_tol_s"]), int(phys["retarded_max_iter"])
    B = len(r_obs)
    n = len(events)
    if time_ref == "absolute":
        T0 = np.full(B, float(audio_start))
    else:  # locked to the measured CPA of event 1
        rc = model.retarded_cpa(tracks[0], r_obs, c)
        T0 = rc["t_heard"] - events[0].meas.t_cpa_heard
    per = np.zeros((B, n))
    dfs = np.zeros((B, n))
    dt1 = np.zeros(B)
    for i, (tr, ev) in enumerate(zip(tracks, events)):
        t_abs = ev.scorer.tf[None, :] + T0[:, None]
        curves = model.forward_curve(r_obs[:, None, :], tr, ev.meas.f0, t_abs, c, tol, it)
        res = ev.scorer.score(curves, sweep_dt=(i == 0))
        per[:, i] = res.score
        dfs[:, i] = res.df
        if i == 0:
            dt1 = res.dt
    return per, dfs, dt1, T0


_WORKER: dict = {}


def _worker_init(payload):
    _WORKER.update(payload)


def _worker_run(r_obs):
    w = _WORKER
    return _score_batch(r_obs, w["tracks"], w["events"], w["c"], w["cfg"], w["time_ref"], w["audio_start"])


def score_points(lat, lon, elev: geo.Elevation, tracks: list[adsb.Track], events: list[Event], c: float, cfg: dict,
                 time_ref: str, audio_start: float | None = None, on_batch=None) -> pd.DataFrame:
    """Locked multi-event score for each candidate point.

    Event 1: dt (+-dt_sweep) and df0 sweeps. Events i>1: placed at
    modeled_retarded_cpa_i + offset (offset fixed by event 1's geometry), df0 sweep only.
    """
    lat = np.asarray(lat, float)
    lon = np.asarray(lon, float)
    r_obs = geo.lla_to_ecef(lat, lon, elev(lat, lon))
    bs = int(cfg["score"]["batch_size"])
    batches = [r_obs[s:s + bs] for s in range(0, len(r_obs), bs)]
    workers = int(cfg["score"]["workers"])
    if workers == 0:
        workers = min(os.cpu_count() or 1, 8) if len(r_obs) >= 4 * bs else 1
    if workers > 1 and len(batches) > 1:
        payload = dict(tracks=tracks, events=events, c=c, cfg=cfg, time_ref=time_ref, audio_start=audio_start)
        import multiprocessing as mp
        import warnings

        # Linux: fork (workers inherit the payload without pickling; callers need no __main__ guard;
        # workers only run numpy on private arrays, so the fork-with-threads caveat does not bite).
        # macOS/Windows: spawn -- forking after Apple system frameworks are loaded can crash workers.
        method = "fork" if sys.platform.startswith("linux") else "spawn"
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*fork.*")
            with ProcessPoolExecutor(workers, mp_context=mp.get_context(method), initializer=_worker_init,
                                     initargs=(payload,)) as ex:
                outs = []
                for o in ex.map(_worker_run, batches):  # results arrive in order: stream them as they finish
                    k0 = len(outs) * bs
                    outs.append(o)
                    if on_batch:
                        on_batch(lat[k0:k0 + len(o[0])], lon[k0:k0 + len(o[0])], o[0].sum(1))
    else:
        outs = []
        for k, b in enumerate(batches):
            o = _score_batch(b, tracks, events, c, cfg, time_ref, audio_start)
            outs.append(o)
            if on_batch:
                on_batch(lat[k * bs:k * bs + len(b)], lon[k * bs:k * bs + len(b)], o[0].sum(1))
    per = np.concatenate([o[0] for o in outs])
    dfs = np.concatenate([o[1] for o in outs])
    dt1 = np.concatenate([o[2] for o in outs])
    T0 = np.concatenate([o[3] for o in outs])
    df = pd.DataFrame({"lat": lat, "lon": lon, "score": per.sum(1), "dt1": dt1, "T0": T0})
    for i in range(len(events)):
        df[f"score_e{i + 1}"] = per[:, i]
        df[f"df_e{i + 1}"] = dfs[:, i]
    return df


# ----------------------------------------------------------------------------- search
def candidate_tracks_per_event(tracks, events, proj, region_radius_m, cfg, audio_start):
    audible = float(cfg["grid"]["audible_radius_m"])
    tol = float(cfg["locate"]["time_tolerance_s"])
    out = []
    for ev in events:
        if audio_start is not None:
            lo = audio_start + ev.meas.t_lo - tol - audible / speed_of_sound(cfg) - 60.0
            hi = audio_start + ev.meas.t_hi + tol
        else:
            lo = hi = None
        q = filters.qualifying_tracks(tracks, proj, region_radius_m, audible, lo, hi)
        if lo is not None and np.isfinite(ev.meas.v):
            # cheap speed screen: the plane's ground speed around the event must be close to the fitted speed
            tol_v = max(float(cfg["locate"]["speed_tolerance_mps"]), float(cfg["locate"]["speed_tolerance_frac"]) * ev.meas.v)
            keep = []
            for tr in q:
                sub = tr.subset(lo, hi)
                if sub is None:
                    continue
                spd = np.linalg.norm(sub.vel, axis=1)
                if np.any(np.abs(spd - ev.meas.v) <= tol_v):
                    keep.append(tr)
            q = keep
        out.append([(tr, lo, hi) for tr in q])
    return out


def search(events: list[Event], tracks: list[adsb.Track], center: tuple[float, float], radius_m: float,
           elev: geo.Elevation, c: float, cfg: dict, time_ref: str, audio_start: float | None,
           slog: StageLog, live=None) -> tuple[pd.DataFrame, gridmod.Grid]:
    from .live import LiveFeed

    live = live or LiveFeed(None)
    g = cfg["grid"]
    step = float(g["step_m"])
    audible = float(g["audible_radius_m"])
    levels = int(g["refine_levels"])
    # Large search areas start on a coarser lattice (at most grid.max_points spots) and refine down
    # to the normal resolution: each refinement level halves the step around the best spots.
    n_full = np.pi * (radius_m / step) ** 2
    extra = 0
    while n_full / 4 ** extra > float(g["max_points"]):
        extra += 1
    step *= 2 ** extra
    levels += extra
    base = gridmod.make_grid(center, radius_m, step)
    proj = base.proj
    slog.count("grid", "-", len(base), f"{step:.0f} m step, radius {radius_m / 1000:.1f} km"
               + (f" (coarse start for a large area; {levels} refinement levels)" if extra else ""))
    live.emit("search", lat=center[0], lon=center[1], radius_m=radius_m, step_m=step, points=len(base), levels=levels)
    per_event = candidate_tracks_per_event(tracks, events, proj, radius_m, cfg, audio_start)
    live.tracks(per_event)
    for i, lst in enumerate(per_event):
        slog.count(f"qualifying tracks e{i + 1}", len(tracks), len(lst),
                   ", ".join(f"{t.icao24}/{t.key}" for t, _, _ in lst))
    combos = [cb for cb in itertools.product(*per_event) if len({t.key for t, _, _ in cb}) == len(cb)]
    slog.count("flight combinations", "-", len(combos))
    live.emit("combos", n=len(combos))
    if not combos:
        raise RuntimeError("no qualifying flight combination for the detected events")
    results = []
    for ci, cb in enumerate(combos):
        name = "+".join(f"{t.icao24}/{t.key}" for t, _, _ in cb)
        mask = np.ones(len(base), bool)
        sub_tracks = []
        for tr, lo, hi in cb:
            mask &= filters.near_track_mask(base.x, base.y, tr, proj, audible, lo, hi)
            st = tr.subset(lo, hi) if lo is not None else tr
            sub_tracks.append(st if st is not None else tr)
        pts = base.subset(mask)
        slog.count(f"combo {name}: near-track points", len(base), len(pts), f"within {audible / 1000:.0f} km")
        live.emit("combo", i=ci, n=len(combos), name=name, keys=[t.key for t, _, _ in cb], points=len(pts), step_m=step)
        if len(pts) == 0:
            continue

        def on_batch(la_, lo__, sc_, _ci=ci):
            live.scores(_ci, la_, lo__, sc_)

        with slog.timer("scoring"):
            la, lo_ = pts.latlon()
            res = score_points(la, lo_, elev, sub_tracks, events, c, cfg, time_ref, audio_start, on_batch=on_batch)
        res["x"], res["y"], res["stage"], res["step_m"] = pts.x, pts.y, 0, step
        res["combo"] = name
        stage_res = [res]
        cur = res
        cur_step = step
        for lvl in range(levels):
            top = cur.nlargest(int(g["refine_top_n"]), "score")
            rg = gridmod.refine(proj, top["x"].to_numpy(), top["y"].to_numpy(), cur_step)
            cur_step = rg.step
            live.emit("refine", combo=ci, level=lvl + 1, levels=levels, step_m=cur_step, points=len(rg))
            with slog.timer("scoring"):
                la, lo_ = rg.latlon()
                r2 = score_points(la, lo_, elev, sub_tracks, events, c, cfg, time_ref, audio_start, on_batch=on_batch)
            r2["x"], r2["y"], r2["stage"], r2["step_m"] = rg.x, rg.y, lvl + 1, cur_step
            r2["combo"] = name
            slog.count(f"combo {name}: refine L{lvl + 1}", len(top), len(rg), f"{cur_step:.0f} m step")
            stage_res.append(r2)
            cur = pd.concat([cur.nlargest(int(g["refine_top_n"]), "score"), r2], ignore_index=True)
        results.append(pd.concat(stage_res, ignore_index=True))
    allres = pd.concat(results, ignore_index=True)
    allres["dist_center_m"] = np.hypot(allres["x"], allres["y"])
    return allres, base


def rank(allres: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(best point per flight combination ranked globally, all points ranked)."""
    # refinement neighbourhoods include already-scored points: keep one row per (combo, location)
    key = [allres["combo"], np.round(allres["x"], 1), np.round(allres["y"], 1)]
    allres = allres.loc[~pd.DataFrame({"c": key[0], "x": key[1], "y": key[2]}).duplicated()]
    pts = allres.sort_values("score", ascending=False).reset_index(drop=True)
    pts["rank"] = np.arange(1, len(pts) + 1)
    best = pts.groupby("combo", sort=False).head(1).reset_index(drop=True)
    best["combo_rank"] = np.arange(1, len(best) + 1)
    return best, pts


def runner_up_distinct(pts: pd.DataFrame, min_sep_m: float) -> pd.Series | None:
    """Best point of any combination at least ``min_sep_m`` from the #1 point."""
    top = pts.iloc[0]
    far = np.hypot(pts["x"] - top["x"], pts["y"] - top["y"]) >= min_sep_m
    far |= pts["combo"] != top["combo"]
    cand = pts[far]
    return None if cand.empty else cand.iloc[0]


# ----------------------------------------------------------------------------- full run
def run_locate(audio_path, adsb_df: pd.DataFrame, center: tuple[float, float], radius_m: float, cfg: dict,
               out_dir, c: float, events_spec="auto", time_ref: str | None = None, audio_start: float | None = None,
               truth: dict | None = None, command: str = "", attestation: str = "", inputs: list | None = None,
               title: str = "dopplergeo locate") -> dict:
    """Audio + unified ADS-B table -> ranked candidate locations, report.md, CSV and plots.

    Also streams ``<out>/live.jsonl`` (see live.py) for the animated web view.
    """
    from .live import LiveFeed

    live = LiveFeed(out_dir)
    live.emit("start", title=title, center=list(center), radius_m=radius_m,
              truth=({"lat": truth["lat"], "lon": truth["lon"]} if truth else None))
    try:
        res = _run_locate(live, audio_path, adsb_df, center, radius_m, cfg, out_dir, c, events_spec, time_ref,
                          audio_start, truth, command, attestation, inputs, title)
        live.emit("done")
        return res
    except BaseException as e:
        live.emit("failed", error=f"{e}"[:500])
        raise
    finally:
        live.close()


def _run_locate(live, audio_path, adsb_df, center, radius_m, cfg, out_dir, c, events_spec, time_ref, audio_start,
                truth, command, attestation, inputs, title) -> dict:
    from pathlib import Path

    from . import audio as audiomod
    from . import measure, report

    t_all = time.perf_counter()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    slog = StageLog()
    time_ref = time_ref or cfg["locate"]["time_reference"]
    if time_ref == "absolute" and audio_start is None:
        raise ValueError("time_reference=absolute requires the audio start time (audio.start_utc)")
    inputs = list(inputs or [])
    inputs.insert(0, {"name": "audio", "path": str(audio_path), "sha256": audiomod.sha256(audio_path)})

    live.emit("stage", stage="audio")
    with slog.timer("audio+spectrogram"):
        x, sr = audiomod.load_audio(audio_path, int(cfg["audio"]["target_sr"]))
        spec = audiomod.spectrogram_from_config(x, sr, cfg)
    live.spectrogram(spec, cfg, out)
    live.emit("stage", stage="detect")
    live.ridge(measure.extract_ridge(spec, tuple(cfg["detect"]["band_hz"])), float(cfg["measure"]["min_column_snr_db"]))
    slog.count("spectrogram", f"{len(x) / sr:.1f} s audio", f"{spec.db.shape[1]} cols x {spec.db.shape[0]} bins")

    detect_diag = None
    with slog.timer("measure"):
        if events_spec == "whole":
            meas = [measure.fit_curve(spec, cfg, c)[0]]
        elif events_spec == "auto":
            meas, detect_diag = measure.detect_events(spec, cfg, c)
        else:
            meas = [measure.fit_event(spec, cfg, c, float(t), manual=True) for t in events_spec]
    bad = [i + 1 for i, m in enumerate(meas) if not m.success]
    slog.count("events", "-", len(meas), f"failed fits: {bad}" if bad else "all fits ok")
    if detect_diag:
        live.emit("rejected", items=detect_diag.get("rejected", []))
    for i, m in enumerate(meas):
        if np.isfinite(m.f0):
            live.flyover(i, m, c)
    if not meas or bad:
        from . import report as _report

        ov = _report.expected_overlays(spec, cfg, detect_diag, c)
        live.emit("expected", **ov)
        _report.write_no_events_report(out, spec, cfg, detect_diag, meas, inputs, attestation, title, c)
        if not meas:
            raise RuntimeError("no plane flyover was found in the recording; see the report for what was heard and "
                               "why each part was rejected (or give flyover times explicitly)")
        raise RuntimeError(f"curve fit failed for event(s) {bad}: {[meas[i - 1].to_dict() for i in bad]}")
    events = make_events(spec, meas, cfg)

    live.emit("stage", stage="flights")
    with slog.timer("adsb+tracks"):
        n_rows = len(adsb_df)
        if cfg["adsb"]["drop_jets"]:
            jets = filters.load_jet_types(cfg["adsb"]["jet_types_csv"])
            adsb_df, jstats = filters.drop_jets(adsb_df, jets)
            slog.count("jet filter (flights)", jstats["flights_in"], jstats["flights_out"],
                       f"dropped {jstats['flights_jet_dropped']} known-jet flights; kept "
                       f"{jstats['flights_unknown_type_kept']} unknown-type")
        tracks = adsb.tracks_from_table(adsb_df, float(cfg["track"]["resample_dt_s"]), float(cfg["track"]["max_gap_s"]))
        slog.count("track segments", f"{n_rows} rows", len(tracks))
    live.emit("flights", rows=n_rows, segments=len(tracks))

    elev = geo.Elevation.from_config(cfg)
    live.emit("stage", stage="search")
    with slog.timer("search"):
        allres, base = search(events, tracks, center, radius_m, elev, c, cfg, time_ref, audio_start, slog, live)
    live.scores(-1, [], [], np.array([]), final=True)
    live.emit("stage", stage="rank")
    best, pts = rank(allres)
    _t = pts.iloc[0]
    live.emit("result", lat=float(_t["lat"]), lon=float(_t["lon"]), score=float(_t["score"]), combo=str(_t["combo"]),
              margin=(float((best["score"].iloc[0] - best["score"].iloc[1]) / best["score"].iloc[0]) if len(best) > 1 else None),
              scored=int(len(pts)))
    step = float(cfg["grid"]["step_m"])
    ru = runner_up_distinct(pts, 3 * step)

    truth_out = None
    proj = base.proj
    if truth is not None:
        tx, ty = proj.forward(truth["lat"], truth["lon"])
        top = pts.iloc[0]
        dists = np.hypot(pts["x"] - tx, pts["y"] - ty)
        if truth.get("combo"):
            dists = dists.where(pts["combo"] == truth["combo"], np.inf)
        k = int(np.argmin(dists.to_numpy()))
        combo_tracks = _combo_tracks(top["combo"], tracks, events, proj, radius_m, cfg, audio_start)
        ts = score_points([truth["lat"]], [truth["lon"]], elev, combo_tracks, events, c, cfg, time_ref, audio_start)
        truth_out = {"lat": truth["lat"], "lon": truth["lon"], "x": float(tx), "y": float(ty),
                     "err_m": float(np.hypot(top["x"] - tx, top["y"] - ty)), "nearest_m": float(dists.iloc[k]),
                     "nearest_rank": int(pts["rank"].iloc[k]), "score": float(ts["score"].iloc[0]),
                     "combo": truth.get("combo"), "combo_correct": (truth.get("combo") in (None, top["combo"]))}

    # plots
    plots = []
    top = pts.iloc[0]
    combo_tracks = _combo_tracks(top["combo"], tracks, events, proj, radius_m, cfg, audio_start)
    r_top = geo.lla_to_ecef(top["lat"], top["lon"], elev(top["lat"], top["lon"]))
    for i, ev in enumerate(events):
        dt_i = top["dt1"] if i == 0 else 0.0
        f0_i = ev.meas.f0 + top[f"df_e{i + 1}"]
        t_rel = ev.scorer.tf
        curve = model.forward_curve(r_top, combo_tracks[i], f0_i, t_rel - dt_i + top["T0"], c)
        tt = th = None
        if truth is not None and truth.get("curves"):
            tt, th = truth["curves"][i]
        rg = measure.extract_ridge(spec, tuple(cfg["measure"]["band_hz"]), ev.meas.t_lo, ev.meas.t_hi)
        good = rg.snr_db >= cfg["measure"]["min_column_snr_db"]
        fn = f"overlay_e{i + 1}.png"
        report.plot_overlay(spec, ev, t_rel, curve, out / fn, f"event {i + 1}: {combo_tracks[i].icao24} "
                            f"f0={f0_i:.2f} Hz dt={dt_i:+.2f} s", tt, th, (rg.times[good], rg.freq[good]))
        plots.append((f"event {i + 1} overlay (true vs modeled curve)", fn))
    txy = []
    feats = []
    best_keys = {nm.split("/", 1)[1] for nm in str(top["combo"]).split("+")}
    for tr in {t.key: t for t in tracks}.values():
        x_, y_ = filters.track_xy(tr, proj)
        near = np.hypot(x_, y_) < radius_m + float(cfg["grid"]["audible_radius_m"]) * 1.5
        if near.any():
            txy.append((tr.icao24, (x_[near], y_[near])))
            idx = np.where(near)[0][::3]
            feats.append({"type": "Feature", "properties": {
                "icao24": tr.icao24, "key": tr.key, "type_code": tr.type_code, "in_best": tr.key in best_keys,
                "t0": float(tr.t[idx[0]]), "t1": float(tr.t[idx[-1]])},
                "geometry": {"type": "LineString",
                             "coordinates": [[round(float(tr.lon[i]), 6), round(float(tr.lat[i]), 6)] for i in idx]}})
    import json as _json

    (out / "map.json").write_text(_json.dumps({
        "tracks": {"type": "FeatureCollection", "features": feats},
        "region": {"lat": center[0], "lon": center[1], "radius_m": radius_m},
        "best": {"lat": float(top["lat"]), "lon": float(top["lon"]), "combo": str(top["combo"])},
        "truth": ({"lat": truth_out["lat"], "lon": truth_out["lon"]} if truth_out else None)}))
    truth_xy = (truth_out["x"], truth_out["y"]) if truth_out else None
    report.plot_heatmap(pts, txy, out / "heatmap.png", truth_xy, "summed-brightness score (all scored points)")
    report.plot_zoom(pts, out / "heatmap_zoom.png", truth_xy)
    plots += [("score heatmap", "heatmap.png"), ("score around #1", "heatmap_zoom.png")]
    report.write_candidates_csv(pts, out / "candidates.csv")
    best.to_csv(out / "best_per_combination.csv", index=False, float_format="%.7f")
    slog.timings["total"] = time.perf_counter() - t_all
    ctx = {
        "title": title, "command": command, "inputs": inputs, "attestation": attestation, "config": cfg,
        "events": meas, "detect_diag": detect_diag, "stage_rows": slog.rows, "timings": slog.timings,
        "best": best, "pts": pts, "runner_up": ru, "runner_up_sep_m": 3 * step, "truth": truth_out, "plots": plots,
        "audio_start": audio_start, "time_ref": time_ref, "c": c, "elevation": elev.describe(),
        "region": f"center {center[0]:.6f},{center[1]:.6f} radius {radius_m / 1000:.2f} km",
        "notes": [
            "Scores are summed whitened spectrogram brightness under the retarded-time model curve.",
            "Event 1 sweeps dt and df0; events i>1 are time-locked to event 1's geometry and sweep df0 only.",
        ],
    }
    rp = report.write_report(out, ctx)
    return {"report": str(rp), "top": pts.iloc[0].to_dict(), "best": best, "pts": pts, "truth": truth_out,
            "events": meas, "timings": slog.timings, "stages": slog.rows}


def _combo_tracks(combo: str, tracks, events, proj, radius_m, cfg, audio_start):
    per_event = candidate_tracks_per_event(tracks, events, proj, radius_m, cfg, audio_start)
    names = combo.split("+")
    out = []
    for i, nm in enumerate(names):
        for tr, lo, hi in per_event[i]:
            if f"{tr.icao24}/{tr.key}" == nm:
                st = tr.subset(lo, hi) if lo is not None else tr
                out.append(st if st is not None else tr)
                break
        else:
            raise KeyError(f"track {nm} not found for event {i + 1}")
    return out
