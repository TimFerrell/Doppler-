"""Local web interface: ``dopplergeo serve``.

Binds to 127.0.0.1 by default. The pages load no third-party resources: the map (Leaflet, US Census
state/county outlines, Natural Earth places) is bundled. The server only contacts the network when you
explicitly ask it to (rehydrating from the adsb.lol archive, or a live point-API collector), and the
optional OpenStreetMap street layer is off unless you switch it on in the map.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import re
import time
import zipfile
from pathlib import Path

import markdown
import pandas as pd
import yaml
from flask import (Flask, abort, jsonify, redirect, render_template, request, send_file, send_from_directory,
                   url_for)
from werkzeug.utils import secure_filename

from .. import config as configmod
from .. import sources
from .jobs import JobManager

AUDIO_EXT = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aiff", ".aif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp", ".mts", ".m2ts"}
MEDIA_EXT = AUDIO_EXT | VIDEO_EXT
ADSB_EXT = {".parquet", ".json", ".jsonl", ".gz", ".zip"}
REC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.wav$")


class FormError(ValueError):
    pass


def job_progress(kind: str, state: str, log: str) -> dict:
    """Human-readable stages of a job, derived from its log."""
    def has(pat):
        return re.search(pat, log, re.M) is not None

    def last(pat):
        m = re.findall(pat, log, re.M)
        return m[-1] if m else None

    steps: list[dict] = []

    def step(label, done, active_detail=None, detail=None):
        steps.append({"label": label, "done": bool(done), "detail": detail, "active_detail": active_detail})

    pct = None
    if kind == "locate":
        if "ingest-adsb" in log:
            step("Read the uploaded flight logs", has(r"^\$ dopplergeo .* locate "),
                 detail=(f"{last(r'rows_out: (\d+)')} positions" if last(r"rows_out: (\d+)") else None))
        step("Load the recording and compute its spectrogram", has(r"\[spectrogram\]"),
             detail=last(r"\[spectrogram\] in=([\d.]+ s) audio") and f"{last(r'\[spectrogram\] in=([\d.]+) s audio')} s of audio")
        ev = last(r"\[events\] in=- out=(\d+)")
        step("Find the planes passing in the audio", ev is not None,
             detail=(f"{ev} flyover{'s' if ev != '1' else ''} found" if ev else None))
        rows = last(r"ADS-B rows in the search window: (\d+) \((\d+) aircraft\)")
        jet = re.findall(r"\[jet filter \(flights\)\] in=(\d+) out=(\d+)", log)
        step("Load flight data for this area and time, drop jets", bool(jet),
             detail=(f"{jet[-1][1]} of {jet[-1][0]} flights are not known jets" if jet else None))
        nco = last(r"\[flight combinations\] in=- out=(\d+)")
        done_co = len(re.findall(r": refine L\d+\]", log)) if nco else 0
        n_lvl = max(1, len(set(re.findall(r": refine (L\d+)\]", log))) or 1)
        tot = int(nco) if nco else 0
        finished = has(r"^#1: ") or has(r"^report: ")
        if tot:
            per = min(tot, len(re.findall(r": near-track points\]", log)))
            pct = int(100 * per / tot) if not finished else 100
        step("Score every candidate spot against every matching flight", finished,
             active_detail=(f"flight combination {min(tot, len(re.findall(r': near-track points]', log)))} of {tot}"
                            if tot else None),
             detail=(f"{tot} flight combination{'s' if tot != 1 else ''} tried" if tot else None))
        step("Rank the spots and write the report", finished and state == "done")
    elif kind == "rehydrate":
        dl = re.findall(r"downloaded ([\d.]+) / ([\d.]+) GB", log)
        days = re.findall(r"^day: '(\d{4}-\d{2}-\d{2})'", log, re.M)
        cur = last(r"adsb\.lol (\d{4}-\d{2}-\d{2}) \S+: \d+ part")
        if dl:
            pct = 100 if state == "done" else int(100 * float(dl[-1][0]) / float(dl[-1][1]))
        step("Find the day's archive on adsb.lol", cur is not None or bool(days))
        step("Download the whole day and keep only aircraft inside the area", state == "done",
             active_detail=(f"day {cur}: {dl[-1][0]} of {dl[-1][1]} GB" if dl and cur else None),
             detail=(f"{len(days)} day(s) done" if days else None))
        rows = re.findall(r"^rows_out: (\d+)", log, re.M)
        step("Save to your flight data", state == "done",
             detail=(f"{sum(int(r) for r in rows):,} positions saved" if rows else None))
    elif kind in ("import",):
        step("Read and convert the receiver logs", state == "done",
             detail=(f"{last(r'rows_out: (\d+)')} positions" if last(r"rows_out: (\d+)") else None))
    elif kind == "measure":
        step("Load the recording and compute its spectrogram", has(r"detect:") or has(r"^events:"))
        step("Find and measure each flyover", state == "done")
    elif kind == "synth":
        seeds = re.findall(r"^seed (\d+): #1 error ([\d.]+) m", log, re.M)
        step("Generate test scenarios and locate them", state == "done",
             detail=(", ".join(f"seed {a}: {b} m off" for a, b in seeds) if seeds else None))
    active = None
    if state in ("queued", "running"):
        for st in steps:
            if not st["done"]:
                st["active"] = True
                active = st
                break
    now = ("Waiting for the job ahead of it to finish." if state == "queued" else
           (active["label"] + (f" ({active['active_detail']})" if active and active.get("active_detail") else "") + "…"
            if active else "Finishing…") if state == "running" else None)
    return {"steps": steps, "percent": pct, "now": now}


def create_app(jobs_dir: str | Path = "data/out/web", base_config: str | Path | None = None,
               max_upload_mb: int = 4096, library_dir: str | Path = "data/adsb/library",
               recordings_dir: str | Path = "data/audio/recordings", scheduler: bool = True) -> Flask:
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
    cfg_full = configmod.load_config(overrides=base_raw)
    jobs = JobManager(jobs_dir)
    lib = sources.Library(Path(library_dir).resolve())
    collectors = sources.CollectorManager(lib, cfg_full)
    started_at = time.time()
    rec_dir = Path(recordings_dir).resolve()
    rec_dir.mkdir(parents=True, exist_ok=True)
    app.extensions["dopplergeo"] = {"jobs": jobs, "library": lib, "collectors": collectors, "recordings": rec_dir}

    def enqueue_archive_day(name: str, day: str, end_day: str | None = None) -> str:
        m = lib.meta(name)
        bb = m.get("request_bbox")
        label = day if not end_day or end_day == day else f"{day} → {end_day}"
        job_id, d = jobs.new_job("rehydrate", f"Rehydrate {label} from adsb.lol → {name}",
                                 {"date": day, "end_date": end_day, "bbox": bb, "dataset": name})
        cfgp = write_config(d, {})
        argv = ["--config", str(cfgp), "fetch-adsblol", "--date", day,
                "--bbox", ("world" if not bb else ",".join(str(v) for v in bb)), "--out", str(lib.path(name))]
        if m.get("skip_jets"):
            argv.append("--skip-jets")
        if end_day and end_day != day:
            argv += ["--end-date", end_day]
        jobs.submit(job_id, [{"argv": argv}])
        return job_id

    sched = sources.ArchiveScheduler(lib, lambda name, day: enqueue_archive_day(name, day))
    app.extensions["dopplergeo"]["scheduler"] = sched

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

    def save_adsb_uploads(files, src: Path) -> list[Path]:
        saved = [save_upload(f, src, ADSB_EXT) for f in files]
        for p in [p for p in saved if p.suffix.lower() == ".zip"]:
            if extract_zip(p, src) == 0:
                raise FormError(f"{p.name}: no .json/.jsonl/.gz/.parquet files inside")
        return saved

    def recordings() -> list[dict]:
        out = []
        for p in sorted(rec_dir.glob("*.wav"), key=lambda q: q.stat().st_mtime, reverse=True):
            meta = {}
            mp = p.with_suffix(".json")
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text())
                except json.JSONDecodeError:
                    meta = {}
            out.append({"name": p.name, "size_mb": p.stat().st_size / 1e6, **meta})
        return out

    def recording_path(name: str) -> Path:
        if not REC_RE.match(name or "") or not (rec_dir / name).is_file():
            raise FormError(f"unknown recording {name!r}")
        return rec_dir / name

    def recording_meta(name: str) -> dict:
        mp = (rec_dir / name).with_suffix(".json")
        return json.loads(mp.read_text()) if mp.exists() else {}

    def audio_input(d: Path) -> tuple[Path, dict]:
        """(audio path, recording metadata) from the library picker or an upload."""
        name = request.form.get("recording", "").strip()
        if name:
            return recording_path(name), recording_meta(name)
        fs = request.files.get("audio")
        if fs is None or not fs.filename:
            raise FormError("choose a recording from the library or upload an audio or video file")
        p = save_upload(fs, d / "inputs", MEDIA_EXT)
        meta = {}
        if p.suffix.lower() in VIDEO_EXT | {".m4a"}:
            from ..audio import media_info

            info = media_info(p)
            if not info["has_audio"]:
                raise FormError(f"{p.name} has no audio track")
            meta = {"start_utc": info["start_utc"], "time_source": info["time_source"]}
        return p, meta

    def datasets() -> list[dict]:
        out = []
        for m in lib.list():
            d = lib.path(m["name"])
            parts = list(d.rglob("*.parquet"))
            mt = (d / "meta.json").stat().st_mtime
            if parts and (m.get("rows", 0) == 0 or max(p.stat().st_mtime for p in parts) > mt + 1):
                try:
                    m = lib.refresh(m["name"])
                except Exception:  # noqa: BLE001 - a part still being written
                    pass
            m["collecting"] = m["name"] in collectors.active and collectors.active[m["name"]].is_alive()
            out.append(m)
        return out

    def page(error=None, tab=None, status=200):
        return render_template("index.html", jobs=jobs.list()[:50], cfg=cfg_preview, error=error,
                               form=request.form if error else {}, tab=tab or request.form.get("tab") or
                               request.args.get("tab") or "locate", recordings=recordings(), datasets=datasets(),
                               collectors=collectors.statuses(), today=dt.date.today().isoformat(),
                               yesterday=(dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=2)).isoformat(),
                               sched={"alive": sched.is_alive(), "last_check": sched.last_check,
                                      "last_error": sched.last_error}), status

    def form_error(msg, d=None, job_id=None):
        if d is not None and job_id is not None:
            import shutil

            shutil.rmtree(d, ignore_errors=True)
        return page(msg, status=400)

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return page()

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
            audio, _ = audio_input(d)
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
            audio, rmeta = audio_input(d)
            start = request.form.get("start_utc", "").strip() or rmeta.get("start_utc")
            if start:
                from ..cli import parse_time

                try:
                    start = parse_time(start)
                except ValueError as e:
                    raise FormError(str(e)) from None
            dsname = request.form.get("dataset", "").strip()
            max_gap = None
            steps = []
            if dsname:
                if not lib.exists(dsname):
                    raise FormError(f"unknown ADS-B dataset {dsname!r}")
                meta = lib.meta(dsname)
                if not any(lib.path(dsname).rglob("*.parquet")):
                    raise FormError(f"dataset {dsname} holds no data yet")
                adsb_path = lib.path(dsname)
                max_gap = meta.get("max_gap_s")
                adsb_desc = f"library dataset {dsname}"
            else:
                files = [f for f in request.files.getlist("adsb") if f and f.filename]
                if not files:
                    raise FormError("choose an ADS-B dataset from the library, or upload a unified .parquet or "
                                    "tar1090/readsb JSON logs (or a .zip of them)")
                src = d / "inputs" / "adsb"
                saved = save_adsb_uploads(files, src)
                adsb_desc = ", ".join(p.name for p in saved)
                parquets = sorted(src.glob("*.parquet"))
                if parquets and len(parquets) == len(list(src.iterdir())):
                    adsb_path = src
                else:
                    adsb_path = d / "inputs" / "adsb.parquet"
        except FormError as e:
            return form_error(str(e), d, job_id)

        over = configmod.deep_merge(over, {"grid": {"refine_levels": refine}, "score": {"dt_sweep_s": dt_sweep},
                                           "audio": {"start_utc": start}})
        if harmonics:
            over["score"]["harmonics"] = harmonics
        if max_gap:
            over["track"] = {"max_gap_s": float(max_gap)}
        if dem:
            over["elevation"] = {"dem_path": dem}
        elif elev is not None:
            over["elevation"] = {"constant_m": elev, "dem_path": None}
        try:
            cfgp = write_config(d, over)
        except (KeyError, ValueError) as e:
            return form_error(f"configuration error: {e}", d, job_id)
        if not dsname and adsb_path.suffix == ".parquet":
            steps.append({"argv": ["--config", str(cfgp), "ingest-adsb", "--src", str(d / "inputs" / "adsb"),
                                   "--out", str(adsb_path)]})
        argv = ["--config", str(cfgp), "locate", "--audio", str(audio), "--region",
                f"center={lat},{lon},radius_km={radius}", "--adsb", str(adsb_path), "--out", str(d / "out")]
        if ev:
            argv += ["--events", ev]
        if tlat is not None:
            argv += ["--truth-latlon", f"{tlat},{tlon}"]
        steps.append({"argv": argv})
        params = {"audio": audio.name, "adsb": adsb_desc, "center": [lat, lon], "radius_km": radius,
                  "start_utc": start, "events": ev, "truth": [tlat, tlon] if tlat is not None else None,
                  "refine_levels": refine, "dt_sweep_s": dt_sweep, "harmonics": harmonics}
        jobs._update(job_id, title=f"Locate: {audio.name}", params=params)
        jobs.submit(job_id, steps)
        return redirect(url_for("job_page", job_id=job_id))

    # ------------------------------------------------------------------ recordings (browser microphone)
    @app.post("/recordings")
    def upload_recording():
        fs = request.files.get("audio")
        if fs is None:
            return jsonify({"error": "no audio"}), 400
        start = request.form.get("start_utc", "").strip()
        try:
            t0 = float(start)
        except ValueError:
            return jsonify({"error": "start_utc must be unix seconds"}), 400
        stamp = dt.datetime.fromtimestamp(t0, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        label = secure_filename(request.form.get("label", "").strip())[:40]
        name = f"rec-{stamp}{'-' + label if label else ''}.wav"
        p = rec_dir / name
        fs.save(p)
        head = p.read_bytes()[:4]
        if head != b"RIFF":
            p.unlink()
            return jsonify({"error": "not a WAV file"}), 400
        import soundfile as sf

        info = sf.info(str(p))
        meta = {"start_utc": t0, "start_iso": dt.datetime.fromtimestamp(t0, dt.timezone.utc).isoformat(),
                "duration_s": info.duration, "sr": info.samplerate, "device": request.form.get("device", "")[:120],
                "label": request.form.get("label", "")[:120], "source": "browser microphone",
                "processing": "echoCancellation/noiseSuppression/autoGainControl requested off",
                "clock": "browser Date.now() at the first captured sample"}
        p.with_suffix(".json").write_text(json.dumps(meta, indent=1))
        return jsonify({"name": name, **meta})

    @app.post("/recordings/import")
    def import_recording():
        """Audio or video file -> recordings library (audio extracted with ffmpeg, time from metadata)."""
        from ..audio import extract_audio, media_info

        fs = request.files.get("media")
        tmp = rec_dir / ".incoming"
        try:
            if fs is None or not fs.filename:
                raise FormError("choose a video or audio file")
            src = save_upload(fs, tmp, MEDIA_EXT)
        except FormError as err:
            return page(str(err), "record", 400)
        try:
            info = media_info(src)
            if not info["has_audio"]:
                raise FormError(f"{src.name} has no audio track")
            override = request.form.get("start_utc", "").strip()
            t0, source = info["start_utc"], info["time_source"]
            if override:
                from ..cli import parse_time

                try:
                    t0, source = parse_time(override), "entered by you"
                except ValueError as e:
                    raise FormError(str(e)) from None
            elif t0 is not None and request.form.get("time_is_end") == "yes" and info["duration_s"]:
                t0, source = t0 - info["duration_s"], f"{source} (recording end) minus duration"
            stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(src.name).stem).strip("-.")[:50] or "video"
            stamp = (dt.datetime.fromtimestamp(t0, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") if t0 is not None
                     else time.strftime("import%Y%m%dT%H%M%S"))
            name = f"rec-{stamp}-{stem}.wav"
            out = extract_audio(src, rec_dir / name, int(cfg_preview["audio"]["target_sr"]))
            import soundfile as sf

            meta = {"start_utc": t0, "start_iso": (dt.datetime.fromtimestamp(t0, dt.timezone.utc).isoformat()
                                                   if t0 is not None else None),
                    "time_source": source or "none found — enter the start time in Locate", "duration_s": sf.info(str(out)).duration,
                    "sr": sf.info(str(out)).samplerate, "source": f"imported from {src.name}",
                    "label": request.form.get("label", "")[:120],
                    "note": "phone video audio is AAC-compressed and often high-passed; very low engine tones may be weak"}
            out.with_suffix(".json").write_text(json.dumps(meta, indent=1))
        except FormError as err:
            return page(str(err), "record", 400)
        except RuntimeError as err:
            return page(str(err), "record", 400)
        finally:
            src.unlink(missing_ok=True)
        return redirect(url_for("index", tab="record"))

    @app.get("/recordings/<name>")
    def get_recording(name):
        try:
            p = recording_path(name)
        except FormError:
            abort(404)
        return send_file(p, mimetype="audio/wav", as_attachment=request.args.get("download") is not None)

    @app.post("/recordings/<name>/delete")
    def delete_recording(name):
        try:
            p = recording_path(name)
        except FormError:
            abort(404)
        p.unlink()
        p.with_suffix(".json").unlink(missing_ok=True)
        return redirect(url_for("index", tab="record"))

    # ------------------------------------------------------------------ ADS-B data library
    def ds_name(default_prefix: str) -> str:
        name = request.form.get("dataset_name", "").strip() or default_prefix
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
        if not sources.NAME_RE.match(name):
            raise FormError("dataset names use letters, digits, '.', '_' and '-'")
        return name

    @app.post("/data/rehydrate")
    def data_rehydrate():
        try:
            day = dt.date.fromisoformat(request.form.get("date", "").strip())
            end_raw = request.form.get("end_date", "").strip()
            end = dt.date.fromisoformat(end_raw) if end_raw else day
        except ValueError:
            return page("dates must be YYYY-MM-DD", "data", 400)
        try:
            if end < day:
                raise FormError("the end date is before the start date")
            if (end - day).days > 31:
                raise FormError("at most 31 days per request (each day is a ~3.5 GB download)")
            if end >= dt.datetime.now(dt.timezone.utc).date():
                raise FormError("the archive only has complete past UTC days (published about a day later)")
            world = request.form.get("region_label", "").strip().lower() == "whole world"
            skip_jets = request.form.get("skip_jets") == "yes"
            if world:
                bbox = None
                region = "world"
            else:
                s, w, n, e = (fnum(k, lo, hi) for k, lo, hi in (("south", -90, 90), ("west", -180, 180),
                                                                 ("north", -90, 90), ("east", -180, 180)))
                if not (s < n and w < e):
                    raise FormError("the bounding box needs south < north and west < east")
                bbox = [s, w, n, e]
                region = re.sub(r"[^A-Za-z0-9]+", "-", request.form.get("region_label", "").strip()).strip("-").lower()
                region = region or f"{s:.1f}_{w:.1f}_{n:.1f}_{e:.1f}"
            name = ds_name(f"adsblol-{region}{'-props' if skip_jets else ''}")
            subscribe = request.form.get("subscribe") == "yes"
            if lib.exists(name):
                m = lib.meta(name)
                same_box = ((m.get("request_bbox") is None and bbox is None) or
                            (m.get("request_bbox") and bbox and
                             [round(v, 4) for v in m["request_bbox"]] == [round(v, 4) for v in bbox]))
                if m.get("kind") != "archive" or not same_box or bool(m.get("skip_jets")) != skip_jets:
                    raise FormError(f"dataset {name} exists with a different source or box; pick another name")
                if subscribe and not m.get("subscribe"):
                    lib.update_meta(name, subscribe=True, since=m.get("since") or day.isoformat())
        except FormError as err:
            return page(str(err), "data", 400)
        if not lib.exists(name):
            lib.create(name, {"source": "adsb.lol archive (ODbL 1.0)", "kind": "archive", "request_bbox": bbox,
                              "skip_jets": skip_jets,
                              "region": request.form.get("region_label", "").strip() or None, "days": [],
                              "subscribe": subscribe, "since": day.isoformat(), "max_gap_s": 120.0,
                              "license": "ODbL 1.0 — attribute adsb.lol; share derived databases alike"})
        job_id = enqueue_archive_day(name, day.isoformat(), end.isoformat())
        if subscribe:
            sched.queued.update((name, (day + dt.timedelta(days=k)).isoformat()) for k in range((end - day).days + 1))
        return redirect(url_for("job_page", job_id=job_id))

    @app.post("/data/<name>/subscribe")
    def data_subscribe(name):
        if not lib.exists(name) or lib.meta(name).get("kind") != "archive":
            abort(404)
        on = request.form.get("on") == "yes"
        lib.update_meta(name, subscribe=on)
        return redirect(url_for("index", tab="data"))

    @app.post("/data/import")
    def data_import():
        files = [f for f in request.files.getlist("adsb") if f and f.filename]
        try:
            if not files:
                raise FormError("choose receiver log files (or a .zip) to import")
            name = ds_name(f"import-{time.strftime('%Y%m%d-%H%M%S')}")
            if lib.exists(name):
                raise FormError(f"dataset {name} already exists")
        except FormError as err:
            return page(str(err), "data", 400)
        job_id, d = jobs.new_job("import", f"Import ADS-B logs → {name}", {"dataset": name})
        try:
            save_adsb_uploads(files, d / "inputs" / "adsb")
        except FormError as err:
            import shutil

            shutil.rmtree(d, ignore_errors=True)
            return page(str(err), "data", 400)
        lib.create(name, {"source": "imported receiver logs", "kind": "import", "max_gap_s": 30.0})
        cfgp = write_config(d, {})
        jobs.submit(job_id, [{"argv": ["--config", str(cfgp), "ingest-adsb", "--src", str(d / "inputs" / "adsb"),
                                       "--out", str(lib.path(name) / "part-import.parquet")]}])
        return redirect(url_for("job_page", job_id=job_id))

    @app.post("/data/collect/start")
    def collect_start():
        src = request.form.get("source", "")
        try:
            kw = {"interval_s": fnum("interval_s", 1, 3600, required=False, default=1.0), "flush_s": 30.0}
            if src == "receiver":
                url = request.form.get("url", "").strip()
                if not re.match(r"^https?://", url):
                    raise FormError("receiver URL must start with http:// or https://")
                kw["url"] = url
                default = "receiver-" + dt.date.today().isoformat()
            elif src in sources.POINT_APIS:
                kw.update(lat=fnum("lat", -90, 90), lon=fnum("lon", -180, 180),
                          radius_nm=fnum("radius_nm", 1, 250, required=False, default=25.0))
                default = f"{src.replace('.', '')}-{dt.date.today().isoformat()}"
            else:
                raise FormError("choose a source")
            name = ds_name(default)
            collectors.start(name, src, **kw)
        except (FormError, ValueError) as err:
            return page(str(err), "data", 400)
        return redirect(url_for("index", tab="data"))

    @app.post("/data/collect/<name>/stop")
    def collect_stop(name):
        collectors.stop(name)
        return redirect(url_for("index", tab="data"))

    @app.get("/data/status.json")
    def data_status():
        return jsonify({"collectors": collectors.statuses()})

    @app.post("/data/<name>/delete")
    def data_delete(name):
        try:
            if name in collectors.active and collectors.active[name].is_alive():
                raise FormError("stop the collector first")
            lib.delete(name)
        except (FormError, ValueError, FileNotFoundError) as err:
            return page(str(err), "data", 400)
        return redirect(url_for("index", tab="data"))

    @app.get("/data/<name>/tracks.json")
    def data_tracks(name):
        """Thinned per-aircraft polylines for previewing a dataset on the map."""
        if not lib.exists(name):
            abort(404)
        from .. import adsb

        meta = lib.meta(name)
        bbox = None
        try:
            bbox = tuple(float(v) for v in request.args["bbox"].split(",")) if request.args.get("bbox") else None
        except ValueError:
            abort(400)
        t_range = None
        big = meta.get("size_bytes", 0) > 200e6 or (meta.get("rows") or 0) > 3_000_000
        if big:
            if bbox is None or (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 150:
                return jsonify({"type": "FeatureCollection", "features": [], "total_flights": 0,
                                "note": "This dataset is large: zoom in to a region (under about 10° x 15°) to preview it."})
            if meta.get("t_max"):
                t_range = (meta["t_max"] - 86400, meta["t_max"])
        df = adsb.read_table(lib.path(name), bbox=bbox if big else None, t_range=t_range)
        feats = []
        groups = df.groupby(["icao24", "flight_id"])
        total = groups.ngroups
        for (icao, fid), g in groups:
            g = g.sort_values("time")
            step = max(1, len(g) // 200)
            g = g.iloc[::step]
            feats.append({"type": "Feature", "properties": {"icao24": icao, "type_code": g["type_code"].iloc[0]
                                                              if pd.notna(g["type_code"].iloc[0]) else None},
                          "geometry": {"type": "LineString", "coordinates": g[["lon", "lat"]].round(5).values.tolist()}})
            if len(feats) >= 3000:
                break
        return jsonify({"type": "FeatureCollection", "features": feats, "total_flights": int(total)})

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
                    "csvs": [p.name for p in sorted(run.glob("*.csv"))], "top": None, "truth_err": None,
                    "has_map": (run / "map.json").exists()}
            bp = run / "best_per_combination.csv"
            if bp.exists():
                b = pd.read_csv(bp)
                item["n_events"] = len([c for c in b.columns if c.startswith("score_e")])
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
        log = (d / "job.log").read_text(errors="replace") if (d / "job.log").exists() else ""
        return render_template("job.html", job=j, results=results(d), meas=meas,
                               progress=job_progress(j["kind"], j["state"], log))

    @app.get("/jobs/<job_id>/status.json")
    def job_status(job_id):
        j, d = get_job(job_id)
        log = d / "job.log"
        text = log.read_text(errors="replace") if log.exists() else ""
        lines = [ln for ln in text.splitlines() if "font_manager" not in ln]
        return jsonify({"state": j["state"], "error": j["error"], "warnings": j.get("warnings", []),
                        "log": "\n".join(lines[-400:]), "progress": job_progress(j["kind"], j["state"], text)})

    @app.get("/jobs/<job_id>/candidates.json")
    def job_candidates(job_id):
        j, d = get_job(job_id)
        rel = request.args.get("run", "out")
        run = (d / rel).resolve()
        if not run.is_relative_to(d.resolve()) or not (run / "candidates.csv").exists():
            abort(404)
        pts = pd.read_csv(run / "candidates.csv").head(int(request.args.get("limit", 6000)))
        mp = run / "map.json"
        extra = json.loads(mp.read_text()) if mp.exists() else {}
        truth = extra.get("truth")
        if truth is None:
            txt = (run / "report.md").read_text() if (run / "report.md").exists() else ""
            if "- true observer: " in txt:
                a, b = txt.split("- true observer: ")[1].split("\n")[0].split(", ")
                truth = {"lat": float(a), "lon": float(b)}
        return jsonify({"points": pts[["lat", "lon", "score", "combo", "rank"]].to_dict(orient="list"),
                        "truth": truth, "tracks": extra.get("tracks"), "region": extra.get("region")})

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
        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"dopplergeo-{job_id}.zip")

    @app.template_filter("ts")
    def ts(v):
        if not v:
            return "—"
        return dt.datetime.fromtimestamp(v, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    @app.template_filter("dur")
    def dur(j):
        if not j.get("started"):
            return "—"
        end = j.get("finished") or time.time()
        s = int(end - j["started"])
        return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"

    @app.template_filter("secs")
    def secs(v):
        v = float(v or 0)
        return f"{int(v // 60)}:{int(v % 60):02d}"

    # ------------------------------------------------------------------ status (used by the supervisor)
    @app.get("/api/status")
    def api_status():
        js = jobs.list()
        busy = [j["id"] for j in js if j["state"] in ("queued", "running")]
        return jsonify({"version": code_version(), "started": started_at, "busy_jobs": len(busy), "busy_ids": busy,
                        "collectors": [c["dataset"] for c in collectors.statuses() if c.get("running")],
                        "updater": updater_state()})

    @app.get("/data/progress.json")
    def data_progress():
        return jsonify({"downloads": sources.fetch_progress(lib.root)})

    @app.context_processor
    def inject_version():
        return {"code_version": code_version(), "updater": updater_state()}

    if scheduler:
        sched.start()  # last: the enqueue callback needs every helper above
        resumed = collectors.resume()
        if resumed:
            app.logger.info("resumed collectors: %s", resumed)
    return app


def code_version() -> dict:
    """Git commit of the running code (if it runs from a checkout)."""
    import subprocess

    root = Path(__file__).resolve().parents[3]
    try:
        out = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), "log", "-1", "--format=%h|%ct|%s"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        h, ct, subj = out.split("|", 2)
        return {"commit": h, "time": float(ct), "subject": subj}
    except Exception:  # noqa: BLE001 - not a checkout, or git missing
        from .. import __version__

        return {"commit": None, "time": None, "subject": f"dopplergeo {__version__}"}


def updater_state() -> dict | None:
    """State written by `dopplergeo supervise` (None when not supervised)."""
    import os

    p = os.environ.get("DOPPLERGEO_UPDATER_STATE")
    if not p or not Path(p).exists():
        return None
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None
