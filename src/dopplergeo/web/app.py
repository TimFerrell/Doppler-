"""Local web interface: ``dopplergeo serve``.

Binds to 127.0.0.1 by default. The server makes no outbound network calls and the pages load no
third-party resources (no CDN scripts, no map tiles), so recording locations never leave the machine.
"""
from __future__ import annotations

import io
import json
import math
import zipfile
from pathlib import Path

import markdown
import pandas as pd
import yaml
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from .. import config as configmod
from .jobs import JobManager

AUDIO_EXT = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aiff", ".aif"}
ADSB_EXT = {".parquet", ".json", ".jsonl", ".gz", ".zip"}


class FormError(ValueError):
    pass


def create_app(jobs_dir: str | Path = "data/out/web", base_config: str | Path | None = None,
               max_upload_mb: int = 4096) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 1024 * 1024
    base_raw = {}
    if base_config:
        base_raw = yaml.safe_load(Path(base_config).read_text()) or {}
        configmod.load_config(base_config)  # validate now, not at the first job
    cfg_preview = configmod.load_config(overrides=base_raw)
    jets = Path(cfg_preview["adsb"]["jet_types_csv"])
    if not jets.is_absolute():
        jets = (Path.cwd() / jets).resolve()
    if not jets.exists():
        raise FileNotFoundError(f"jet type table {jets} not found: run `dopplergeo serve` from the repository root "
                                "or set adsb.jet_types_csv to an absolute path in --config")
    base_raw = configmod.deep_merge(base_raw, {"adsb": {"jet_types_csv": str(jets)}})
    jobs = JobManager(jobs_dir)
    app.extensions["dopplergeo_jobs"] = jobs

    # ------------------------------------------------------------------ helpers
    def write_config(d: Path, overrides: dict) -> Path:
        merged = configmod.deep_merge(base_raw, overrides)
        p = d / "config.yaml"
        p.write_text(yaml.safe_dump(merged, sort_keys=False))
        configmod.load_config(p)  # raises on unknown keys / bad YAML before queueing
        return p

    def fnum(name, lo=None, hi=None, required=True, default=None):
        v = request.form.get(name, "").strip()
        if not v:
            if required:
                raise FormError(f"{name.replace('_', ' ')} is required")
            return default
        try:
            x = float(v)
        except ValueError:
            raise FormError(f"{name.replace('_', ' ')} must be a number") from None
        if not math.isfinite(x) or (lo is not None and x < lo) or (hi is not None and x > hi):
            raise FormError(f"{name.replace('_', ' ')} must be between {lo} and {hi}")
        return x

    def events_field():
        v = request.form.get("events", "").strip()
        if not v:
            return None
        try:
            return ",".join(str(float(t)) for t in v.replace(" ", "").split(",") if t)
        except ValueError:
            raise FormError("event times must be comma-separated seconds, e.g. 61.5, 981") from None

    def attestation():
        if request.form.get("attest") != "yes":
            raise FormError("You must confirm the recording is yours or that you are authorized to analyse it.")
        who = request.form.get("operator", "").strip()[:120] or None
        return {"authorization": {"attest_owned_or_authorized": True, "operator": who}}

    def save_upload(fs, dest_dir: Path, allowed: set[str]) -> Path:
        name = secure_filename(fs.filename or "")
        if not name:
            raise FormError("missing file name")
        ext = "".join(Path(name).suffixes[-2:]) if name.endswith(".gz") else Path(name).suffix
        if Path(name).suffix.lower() not in allowed and ext.lower() not in allowed:
            raise FormError(f"{name}: unsupported file type (allowed: {', '.join(sorted(allowed))})")
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / name
        fs.save(p)
        return p

    def extract_zip(p: Path, dest: Path) -> int:
        n = 0
        with zipfile.ZipFile(p) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = secure_filename(Path(info.filename).name)
                if not name or Path(name).suffix.lower() not in {".json", ".jsonl", ".gz", ".parquet"}:
                    continue
                target = dest / f"{n:06d}_{name}"  # flattened: no paths from the archive are used
                with z.open(info) as src, open(target, "wb") as out:
                    out.write(src.read())
                n += 1
        p.unlink()
        return n

    def audio_upload(d: Path) -> Path:
        fs = request.files.get("audio")
        if fs is None or not fs.filename:
            raise FormError("choose an audio file")
        return save_upload(fs, d / "inputs", AUDIO_EXT)

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return render_template("index.html", jobs=jobs.list()[:50], cfg=cfg_preview, error=None, form={})

    def form_error(msg, d=None, job_id=None):
        if d is not None and job_id is not None:
            import shutil

            shutil.rmtree(d, ignore_errors=True)
        return render_template("index.html", jobs=jobs.list()[:50], cfg=cfg_preview, error=msg,
                               form=request.form, tab=request.form.get("tab", "locate")), 400

    @app.post("/jobs/synth")
    def submit_synth():
        try:
            seeds = [int(s) for s in request.form.get("seeds", "1").replace(" ", "").split(",") if s]
            if not seeds or len(seeds) > 20:
                raise FormError("give 1-20 integer seeds")
        except ValueError:
            return form_error("seeds must be comma-separated integers")
        except FormError as e:
            return form_error(str(e))
        job_id, d = jobs.new_job("synth", f"Synthetic demo (seeds {','.join(map(str, seeds))})", {"seeds": seeds})
        cfgp = write_config(d, {})
        jobs.submit(job_id, [{"argv": ["--config", str(cfgp), "synth-demo", "--out", str(d / "out"),
                                       "--seeds", ",".join(map(str, seeds))],
                              "warn": {"1": "at least one seed missed the 150 m target (see the reports)"}}])
        return redirect(url_for("job_page", job_id=job_id))

    @app.post("/jobs/measure")
    def submit_measure():
        job_id, d = jobs.new_job("measure", "Measure flyovers", {})
        try:
            over = attestation()
            audio = audio_upload(d)
            ev = events_field()
        except FormError as e:
            return form_error(str(e), d, job_id)
        cfgp = write_config(d, over)
        argv = ["--config", str(cfgp), "measure", "--audio", str(audio), "--out", str(d / "out" / "measurements.yaml")]
        if ev:
            argv += ["--events", ev]
        jobs._update(job_id, title=f"Measure: {audio.name}", params={"audio": audio.name, "events": ev})
        jobs.submit(job_id, [{"argv": argv}])
        return redirect(url_for("job_page", job_id=job_id))

    @app.post("/jobs/locate")
    def submit_locate():
        job_id, d = jobs.new_job("locate", "Locate", {})
        try:
            over = attestation()
            lat = fnum("center_lat", -90, 90)
            lon = fnum("center_lon", -180, 180)
            radius = fnum("radius_km", 0.5, 200)
            elev = fnum("elevation_m", -500, 9000, required=False)
            dem = request.form.get("dem_path", "").strip() or None
            if dem and not Path(dem).is_file():
                raise FormError(f"DEM file not found on the server: {dem}")
            start = request.form.get("start_utc", "").strip() or None
            if start:
                from ..cli import parse_time

                try:
                    start = parse_time(start)
                except ValueError as e:
                    raise FormError(str(e)) from None
            tlat = fnum("truth_lat", -90, 90, required=False)
            tlon = fnum("truth_lon", -180, 180, required=False)
            if (tlat is None) != (tlon is None):
                raise FormError("give both known latitude and longitude, or neither")
            refine = int(fnum("refine_levels", 0, 5, required=False, default=cfg_preview["grid"]["refine_levels"]))
            dt_sweep = fnum("dt_sweep_s", 0.1, 30, required=False, default=cfg_preview["score"]["dt_sweep_s"])
            harm = request.form.get("harmonics", "").strip()
            harmonics = [int(h) for h in harm.replace(" ", "").split(",") if h] if harm else None
            if harmonics is not None and (not harmonics or any(h < 1 or h > 8 for h in harmonics)):
                raise FormError("harmonics must be integers 1-8, e.g. 1,2,3")
            ev = events_field()
            audio = audio_upload(d)
            files = [f for f in request.files.getlist("adsb") if f and f.filename]
            if not files:
                raise FormError("add ADS-B data: a unified .parquet, or tar1090/readsb JSON logs (or a .zip of them)")
            src = d / "inputs" / "adsb"
            saved = [save_upload(f, src, ADSB_EXT) for f in files]
            for p in [p for p in saved if p.suffix.lower() == ".zip"]:
                if extract_zip(p, src) == 0:
                    raise FormError(f"{p.name}: no .json/.jsonl/.gz/.parquet files inside")
        except FormError as e:
            return form_error(str(e), d, job_id)

        over = configmod.deep_merge(over, {"grid": {"refine_levels": refine}, "score": {"dt_sweep_s": dt_sweep},
                                           "audio": {"start_utc": start}})
        if harmonics:
            over["score"]["harmonics"] = harmonics
        if dem:
            over["elevation"] = {"dem_path": dem}
        elif elev is not None:
            over["elevation"] = {"constant_m": elev, "dem_path": None}
        try:
            cfgp = write_config(d, over)
        except (KeyError, ValueError) as e:
            return form_error(f"configuration error: {e}", d, job_id)
        parquets = sorted(src.glob("*.parquet"))
        steps = []
        if parquets and len(parquets) == len(list(src.iterdir())):
            adsb_path = src
        else:
            adsb_path = d / "inputs" / "adsb.parquet"
            steps.append({"argv": ["--config", str(cfgp), "ingest-adsb", "--src", str(src), "--out", str(adsb_path)]})
        argv = ["--config", str(cfgp), "locate", "--audio", str(audio), "--region",
                f"center={lat},{lon},radius_km={radius}", "--adsb", str(adsb_path), "--out", str(d / "out")]
        if ev:
            argv += ["--events", ev]
        if tlat is not None:
            argv += ["--truth-latlon", f"{tlat},{tlon}"]
        steps.append({"argv": argv})
        params = {"audio": audio.name, "adsb_files": [p.name for p in saved], "center": [lat, lon],
                  "radius_km": radius, "start_utc": start, "events": ev, "truth": [tlat, tlon] if tlat is not None else None,
                  "refine_levels": refine, "dt_sweep_s": dt_sweep, "harmonics": harmonics}
        jobs._update(job_id, title=f"Locate: {audio.name}", params=params)
        jobs.submit(job_id, steps)
        return redirect(url_for("job_page", job_id=job_id))

    # ------------------------------------------------------------------ job views
    def get_job(job_id):
        try:
            return jobs.get(job_id), jobs.job_dir(job_id)
        except KeyError:
            abort(404)

    def results(d: Path) -> list[dict]:
        out = []
        for rp in sorted((d / "out").rglob("report.md")):
            run = rp.parent
            rel = run.relative_to(d).as_posix()
            item = {"rel": rel, "name": run.relative_to(d / "out").as_posix() or "run",
                    "images": [p.name for p in sorted(run.glob("*.png"))],
                    "csvs": [p.name for p in sorted(run.glob("*.csv"))], "top": None, "truth_err": None}
            bp = run / "best_per_combination.csv"
            if bp.exists():
                b = pd.read_csv(bp)
                if len(b):
                    r = b.iloc[0]
                    item["top"] = {"lat": float(r["lat"]), "lon": float(r["lon"]), "score": float(r["score"]),
                                   "combo": str(r["combo"]),
                                   "margin": (float((b["score"].iloc[0] - b["score"].iloc[1]) / b["score"].iloc[0])
                                              if len(b) > 1 else None)}
            txt = rp.read_text()
            if "#1 error: **" in txt:
                item["truth_err"] = txt.split("#1 error: **")[1].split(" m**")[0]
            out.append(item)
        return out

    @app.get("/jobs/<job_id>")
    def job_page(job_id):
        j, d = get_job(job_id)
        meas = None
        mp = d / "out" / "measurements.yaml"
        if mp.exists():
            meas = yaml.safe_load(mp.read_text())
        return render_template("job.html", job=j, results=results(d), meas=meas)

    @app.get("/jobs/<job_id>/status.json")
    def job_status(job_id):
        j, d = get_job(job_id)
        log = d / "job.log"
        text = log.read_text(errors="replace") if log.exists() else ""
        lines = [ln for ln in text.splitlines() if "font_manager" not in ln]
        return jsonify({"state": j["state"], "error": j["error"], "warnings": j.get("warnings", []),
                        "log": "\n".join(lines[-400:])})

    @app.get("/jobs/<job_id>/candidates.json")
    def job_candidates(job_id):
        j, d = get_job(job_id)
        rel = request.args.get("run", "out")
        run = (d / rel).resolve()
        if not run.is_relative_to(d.resolve()) or not (run / "candidates.csv").exists():
            abort(404)
        pts = pd.read_csv(run / "candidates.csv").head(int(request.args.get("limit", 6000)))
        truth = None
        txt = (run / "report.md").read_text() if (run / "report.md").exists() else ""
        if "- true observer: " in txt:
            a, b = txt.split("- true observer: ")[1].split("\n")[0].split(", ")
            truth = [float(a), float(b)]
        return jsonify({"points": pts[["lat", "lon", "score", "combo", "rank"]].to_dict(orient="list"),
                        "truth": truth})

    @app.get("/jobs/<job_id>/files/<path:relpath>")
    def job_file(job_id, relpath):
        j, d = get_job(job_id)
        target = (d / relpath).resolve()
        if not target.is_relative_to(d.resolve()) or not target.is_file():
            abort(404)
        if target.suffix == ".md" and request.args.get("raw") is None:
            html = markdown.markdown(target.read_text(), extensions=["tables", "fenced_code"])
            return render_template("report.html", job=j, body=html, relpath=relpath)
        return send_from_directory(d, relpath, as_attachment=request.args.get("download") is not None)

    @app.get("/jobs/<job_id>/bundle.zip")
    def job_zip(job_id):
        j, d = get_job(job_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted((d / "out").rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(d).as_posix())
            for name in ("job.json", "job.log", "config.yaml"):
                if (d / name).exists():
                    z.write(d / name, name)
        buf.seek(0)
        from flask import send_file

        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"dopplergeo-{job_id}.zip")

    @app.template_filter("ts")
    def ts(v):
        import datetime as dt

        if not v:
            return "—"
        return dt.datetime.fromtimestamp(v, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    @app.template_filter("dur")
    def dur(j):
        if not j.get("started"):
            return "—"
        import time

        end = j.get("finished") or time.time()
        s = int(end - j["started"])
        return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"

    @app.template_filter("tojson_pretty")
    def tojson_pretty(v):
        return json.dumps(v, indent=1, default=str)

    return app
