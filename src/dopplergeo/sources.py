"""ADS-B data sources: dataset library, adsb.lol daily archive, live collectors.

Sources (see docs/adsb_sources.md):

* **Your own receiver** (readsb / tar1090 / dump1090-fa ``aircraft.json`` over HTTP): the best
  ongoing source for self-recorded audio -- full ~1 s cadence, no third party involved.
* **adsb.lol live API** ``/v2/point/{lat}/{lon}/{radius_nm}`` and **airplanes.live**
  ``/v2/point/...`` (1 request/s, non-commercial): ongoing collection for an area without a receiver.
  These reveal the polled area to the provider.
* **adsb.lol daily archive** (``github.com/adsblol/globe_history_<YYYY>``, ODbL 1.0): one release per
  day, whole sky, readsb ``trace_full`` files. Used to rehydrate a past day. Several GB per day:
  it is streamed and filtered to a bounding box, nothing but the filtered rows is kept.

Every dataset in the library is a directory ``<library>/<name>/`` holding ``part-*.parquet`` files
in the unified schema plus ``meta.json``.
"""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import logging
import re
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from . import adsb

log = logging.getLogger(__name__)
USER_AGENT = "dopplergeo/0.1 (+offline acoustic research tool)"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


# =============================================================================== library
class Library:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        if not NAME_RE.match(name or ""):
            raise ValueError(f"invalid dataset name {name!r}")
        return self.root / name

    def exists(self, name: str) -> bool:
        return (self.path(name) / "meta.json").exists()

    def create(self, name: str, meta: dict) -> Path:
        d = self.path(name)
        d.mkdir(parents=True, exist_ok=True)
        m = {"name": name, "created": time.time(), "rows": 0, "flights": 0, "t_min": None, "t_max": None,
             "bbox": None, **meta}
        (d / "meta.json").write_text(json.dumps(m, indent=1))
        return d

    def meta(self, name: str) -> dict:
        return json.loads((self.path(name) / "meta.json").read_text())

    def update_meta(self, name: str, **kw) -> dict:
        p = self.path(name) / "meta.json"
        m = json.loads(p.read_text())
        m.update(kw)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=1))
        tmp.replace(p)
        return m

    def append(self, name: str, df: pd.DataFrame) -> dict:
        """Append unified rows as a new part file and refresh the summary statistics."""
        d = self.path(name)
        if len(df):
            part = d / f"part-{time.strftime('%Y%m%dT%H%M%S')}-{int(time.time() * 1e6) % 1000000:06d}.parquet"
            adsb.validate(df).to_parquet(part, index=False)
        return self.refresh(name)

    def refresh(self, name: str) -> dict:
        d = self.path(name)
        parts = sorted(d.glob("*.parquet"))
        if not parts:
            return self.update_meta(name, rows=0, flights=0, t_min=None, t_max=None, bbox=None)
        import pyarrow.parquet as pq

        cols = ["time", "icao24", "lat", "lon"]
        t = pd.concat([pq.read_table(p, columns=cols).to_pandas() for p in parts], ignore_index=True)
        return self.update_meta(
            name, rows=int(len(t)), flights=int(t["icao24"].nunique()), t_min=float(t["time"].min()),
            t_max=float(t["time"].max()),
            bbox=[float(t["lat"].min()), float(t["lon"].min()), float(t["lat"].max()), float(t["lon"].max())],
            parts=len(parts))

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*/meta.json")):
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(out, key=lambda m: m.get("created", 0), reverse=True)

    def delete(self, name: str) -> None:
        import shutil

        shutil.rmtree(self.path(name))


# =============================================================================== traces
def parse_trace(obj: dict, stats: dict, bbox=None, t_range=None) -> list[dict]:
    """readsb ``trace_full`` JSON (adsb.lol / ADS-B Exchange globe_history) -> unified rows.

    Point layout (readsb README-json.md): [dt, lat, lon, alt_ft|"ground"|null, gs_kt, track_deg,
    flags, vrate_fpm, aircraft|null, source, geom_alt_ft, geom_vrate_fpm, ias, roll]. flags&8: the
    altitude is geometric; flags&4: the vertical rate is geometric. Points without speed, track or
    vertical rate are dropped (counted), as are ground points.
    """
    hexid = str(obj.get("icao", "")).lower().lstrip("~")
    base = float(obj.get("timestamp", 0.0))
    tc = obj.get("t")
    rows = []
    for p in obj.get("trace", []):
        stats["points"] += 1
        lat, lon = p[1], p[2]
        if lat is None or lon is None:
            continue
        if bbox is not None and not (bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]):
            continue
        t = base + float(p[0])
        if t_range is not None and not (t_range[0] <= t <= t_range[1]):
            continue
        alt = p[3]
        if alt == "ground":
            stats["on_ground"] += 1
            continue
        geom_alt = p[10] if len(p) > 10 else None
        if geom_alt is not None:
            alt = geom_alt
        gs, trk, vr = p[4], p[5], p[7]
        if len(p) > 11 and p[11] is not None and vr is None:
            vr = p[11]
        if alt is None or gs is None or trk is None or vr is None:
            stats["dropped_missing_fields"] += 1
            continue
        ve, vn, vu = adsb.velocity_from_gs_track(gs, trk, vr)
        rows.append({"time": t, "icao24": hexid, "lat": float(lat), "lon": float(lon),
                     "alt_msl_m": float(alt) * adsb.FT_TO_M, "vx_east": float(ve), "vy_north": float(vn),
                     "vz_up": float(vu), "type_code": (str(tc).upper() if tc else None)})
    return rows


def rows_to_frame(rows: list[dict], gap_s: float = 1800.0) -> pd.DataFrame:
    if not rows:
        return adsb.coerce(pd.DataFrame({c: pd.Series(dtype=t) for c, t in adsb.SCHEMA.items()}))
    df = pd.DataFrame.from_records(rows)
    df["flight_id"] = 0
    df = adsb.coerce(df)
    df["_k"] = np.round(df["time"].to_numpy(float), 1)
    df = df.drop_duplicates(["icao24", "_k"]).drop(columns="_k")
    return adsb.assign_flight_ids(df, gap_s)


class _ConcatHTTP(io.RawIOBase):
    """Sequential read over several HTTP downloads (split tar parts .tar.aa, .tar.ab, ...)."""

    def __init__(self, urls: list[str], progress=None, timeout=120):
        self.urls = list(urls)
        self.resp = None
        self.progress = progress
        self.timeout = timeout
        self.read_bytes = 0
        self.total = 0

    def readable(self):
        return True

    def _next(self):
        if self.resp is not None:
            self.resp.close()
        if not self.urls:
            self.resp = None
            return False
        url = self.urls.pop(0)
        self.resp = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
                                           timeout=self.timeout)
        return True

    def readinto(self, b):
        while True:
            if self.resp is None and not self._next():
                return 0
            n = self.resp.readinto(b)
            if n:
                self.read_bytes += n
                if self.progress:
                    self.progress(self.read_bytes, self.total)
                return n
            if not self._next():
                return 0


def _http_size(url: str, timeout=60) -> int | None:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def adsblol_part_urls(day: dt.date, variant: str = "prod-0", max_parts: int = 12) -> list[tuple[str, int]]:
    """Asset URLs (+ sizes) of one day's release: v<Y.m.d>-planes-readsb-<variant>.tar.aa, .ab, ..."""
    tag = f"v{day:%Y.%m.%d}-planes-readsb-{variant}"
    base = f"https://github.com/adsblol/globe_history_{day:%Y}/releases/download/{tag}/{tag}.tar."
    out = []
    for i in range(max_parts):
        suffix = "a" + "abcdefghijklmnopqrstuvwxyz"[i]
        size = _http_size(base + suffix)
        if size is None:
            break
        out.append((base + suffix, size))
    return out


def _trace_may_touch(obj: dict, bbox, pad: float = 1.0, stride: int = 10) -> bool:
    """Cheap pre-check on every ``stride``-th point against a bbox padded by ``pad`` degrees.

    Archive points are at most a few minutes apart, so an aircraft that enters the box comes within
    ``pad`` degrees of it on a sampled point. Skips the exact per-point pass for ~95 % of traces.
    """
    s, w, n, e = bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad
    tr = obj.get("trace") or ()
    for p in tr[::stride]:
        lat, lon = p[1], p[2]
        if lat is not None and s <= lat <= n and w <= lon <= e:
            return True
    if tr:
        p = tr[-1]
        return p[1] is not None and s <= p[1] <= n and w <= p[2] <= e
    return False


def fetch_adsblol_day(day: dt.date, bbox: tuple[float, float, float, float], out_dir: str | Path,
                      variants=("prod-0", "staging-0"), t_range=None, progress=None, gap_s=1800.0,
                      chunk_rows: int = 400_000) -> dict:
    """Stream one day of the adsb.lol archive into ``out_dir`` (part files), keeping only points in bbox.

    Memory stays bounded for state-sized boxes: rows are written every ``chunk_rows``. Flight ids are
    re-derived across part files when the dataset is read (``adsb.read_table`` on the directory).
    """
    parts = []
    used = None
    for v in variants:
        parts = adsblol_part_urls(day, v)
        if parts:
            used = v
            break
    if not parts:
        raise FileNotFoundError(f"no adsb.lol archive release found for {day} (tried {', '.join(variants)}); "
                                "releases appear about a day later")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"part-archive-{day.isoformat()}-*.parquet"):  # re-fetch replaces the day
        old.unlink()
    stats = {"day": day.isoformat(), "variant": used, "parts": len(parts), "bytes": sum(s for _, s in parts),
             "trace_files": 0, "traces_prefiltered_out": 0, "aircraft_in_bbox": 0, "points": 0, "on_ground": 0,
             "dropped_missing_fields": 0, "rows_out": 0, "files_out": 0}
    log.info("adsb.lol %s %s: %d part(s), %.2f GB", day, used, len(parts), stats["bytes"] / 1e9)
    stream = _ConcatHTTP([u for u, _ in parts], progress)
    stream.total = stats["bytes"]
    rows: list[dict] = []
    aircraft: set[str] = set()

    def flush():
        if not rows:
            return
        df = rows_to_frame(rows, gap_s)
        df.to_parquet(out_dir / f"part-archive-{day.isoformat()}-{stats['files_out']:03d}.parquet", index=False)
        stats["rows_out"] += len(df)
        stats["files_out"] += 1
        rows.clear()

    with tarfile.open(fileobj=io.BufferedReader(stream, 1 << 20), mode="r|") as tar:
        for m in tar:
            if not m.isfile() or "trace_full" not in m.name:
                continue
            stats["trace_files"] += 1
            raw = tar.extractfile(m).read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not _trace_may_touch(obj, bbox):
                stats["traces_prefiltered_out"] += 1
                continue
            got = parse_trace(obj, stats, bbox, t_range)
            if got:
                aircraft.add(got[0]["icao24"])
                rows += got
                if len(rows) >= chunk_rows:
                    flush()
    flush()
    stats["aircraft_in_bbox"] = len(aircraft)
    log.info("adsb.lol %s: %s", day, stats)
    return stats


def latest_archive_day(today: dt.date | None = None, lookback: int = 4) -> dt.date | None:
    """Most recent UTC day whose adsb.lol release exists (checks today-1 back to today-lookback)."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    for k in range(1, lookback + 1):
        d = today - dt.timedelta(days=k)
        if adsblol_part_urls(d, "prod-0", max_parts=1) or adsblol_part_urls(d, "staging-0", max_parts=1):
            return d
    return None


class ArchiveScheduler(threading.Thread):
    """Keeps 'subscribed' archive datasets current: once an hour, queue any newly published days.

    ``enqueue(dataset, day)`` is supplied by the host (the web server queues a normal job, so it shows
    in the job list). Days already present in ``meta['days']`` or already queued are skipped.
    """

    def __init__(self, library: Library, enqueue, interval_s: float = 3600.0, max_backfill: int = 3):
        super().__init__(daemon=True, name="archive-scheduler")
        self.library, self.enqueue, self.interval, self.max_backfill = library, enqueue, interval_s, max_backfill
        self._stop = threading.Event()
        self.queued: set[tuple[str, str]] = set()
        self.last_check: float | None = None
        self.last_error: str | None = None

    def stop(self):
        self._stop.set()

    def check_once(self) -> list[tuple[str, str]]:
        subs = [m for m in self.library.list() if m.get("subscribe") and m.get("request_bbox")]
        if not subs:
            return []
        latest = latest_archive_day()
        out = []
        if latest is None:
            return out
        for m in subs:
            have = set(m.get("days", []))
            for k in range(self.max_backfill):
                d = (latest - dt.timedelta(days=k)).isoformat()
                if m.get("since") and d < m["since"]:
                    continue
                key = (m["name"], d)
                if d not in have and key not in self.queued:
                    self.queued.add(key)
                    self.enqueue(m["name"], d)
                    out.append(key)
        return out

    def run(self):
        while not self._stop.is_set():
            try:
                self.check_once()
                self.last_error = None
            except Exception as e:  # noqa: BLE001 - network hiccups must not kill the scheduler
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("archive scheduler: %s", e)
            self.last_check = time.time()
            self._stop.wait(self.interval)


# =============================================================================== live
POINT_APIS = {
    "adsb.lol": "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm}",
    "airplanes.live": "https://api.airplanes.live/v2/point/{lat}/{lon}/{radius_nm}",
}


def normalise_snapshot(obj: dict) -> dict:
    """readsb aircraft.json / v2 API response -> {'now': unix s, 'aircraft': [...]}."""
    ac = obj.get("aircraft", obj.get("ac"))
    if ac is None or "now" not in obj:
        raise adsb.ADSBError("response is not an aircraft snapshot (needs 'now' and 'aircraft'/'ac')")
    now = float(obj["now"])
    if now > 1e11:  # v2 APIs report milliseconds
        now /= 1000.0
    return {"now": now, "aircraft": ac}


class Collector(threading.Thread):
    """Polls a source and appends unified rows to a library dataset every ``flush_s`` seconds."""

    def __init__(self, library: Library, dataset: str, source: str, cfg: dict, url: str | None = None,
                 lat: float | None = None, lon: float | None = None, radius_nm: float = 25.0,
                 interval_s: float = 1.0, flush_s: float = 30.0):
        super().__init__(daemon=True, name=f"collector-{dataset}")
        if source == "receiver":
            if not url:
                raise ValueError("receiver source needs the aircraft.json URL")
            self.url = url
        elif source in POINT_APIS:
            self.url = POINT_APIS[source].format(lat=f"{lat:.4f}", lon=f"{lon:.4f}", radius_nm=int(radius_nm))
            # provider limits: airplanes.live documents 1 req/s; adsb.lol's are dynamic (429 seen at 2 s)
            interval_s = max(interval_s, 1.0 if source == "airplanes.live" else 5.0)
        else:
            raise ValueError(f"unknown source {source!r}")
        self.library, self.dataset, self.source, self.cfg = library, dataset, source, cfg
        self.interval, self.flush_s = float(interval_s), float(flush_s)
        self._stop = threading.Event()
        self.status = {"dataset": dataset, "source": source, "url": self.url, "started": time.time(),
                       "polls": 0, "errors": 0, "last_error": None, "rows_buffered": 0, "rows_written": 0,
                       "last_poll": None, "aircraft_last": 0, "running": True}
        self._seen: dict[str, float] = {}

    def stop(self):
        self._stop.set()

    def _poll(self) -> list[dict]:
        req = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            obj = json.loads(r.read())
        snap = normalise_snapshot(obj)
        st = {k: 0 for k in ("aircraft_entries", "no_position", "stale_position", "on_ground",
                             "dropped_missing_fields")}
        cfg = {**self.cfg, "adsb": {**self.cfg["adsb"], "on_missing_velocity": "drop"}}
        rows = adsb.parse_snapshot(snap, cfg, st, self.url)
        self.status["aircraft_last"] = len(snap["aircraft"])
        fresh = []
        for r in rows:  # snapshots repeat a fix until a new one arrives
            k = r["icao24"]
            if self._seen.get(k) is None or r["time"] > self._seen[k] + 0.05:
                self._seen[k] = r["time"]
                fresh.append(r)
        return fresh

    def run(self):
        buf: list[dict] = []
        last_flush = time.time()
        backoff = 0.0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                buf += self._poll()
                self.status["polls"] += 1
                self.status["last_poll"] = t0
                backoff = 0.0
            except Exception as e:  # noqa: BLE001 - keep collecting through transient errors
                self.status["errors"] += 1
                self.status["last_error"] = f"{type(e).__name__}: {e}"
                log.warning("collector %s: %s", self.dataset, e)
                if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    backoff = float(ra) if ra and ra.isdigit() else min(60.0, max(5.0, 2 * backoff))
                    self._stop.wait(backoff)
            self.status["rows_buffered"] = len(buf)
            if buf and time.time() - last_flush >= self.flush_s:
                self._flush(buf)
                buf, last_flush = [], time.time()
            self._stop.wait(max(0.0, self.interval - (time.time() - t0)))
        if buf:
            self._flush(buf)
        self.status["running"] = False
        self.status["rows_buffered"] = 0

    def _flush(self, buf):
        df = rows_to_frame(buf)
        self.library.append(self.dataset, df)
        self.status["rows_written"] += len(df)


class CollectorManager:
    def __init__(self, library: Library, cfg: dict):
        self.library, self.cfg = library, cfg
        self.active: dict[str, Collector] = {}

    def start(self, dataset: str, source: str, **kw) -> Collector:
        if dataset in self.active and self.active[dataset].is_alive():
            raise ValueError(f"already collecting into {dataset}")
        if not self.library.exists(dataset):
            self.library.create(dataset, {"source": source, "kind": "live", "params": {k: v for k, v in kw.items()},
                                          "max_gap_s": 30.0})
        c = Collector(self.library, dataset, source, self.cfg, **kw)
        self.active[dataset] = c
        c.start()
        return c

    def stop(self, dataset: str) -> None:
        c = self.active.get(dataset)
        if c:
            c.stop()
            c.join(timeout=15)

    def statuses(self) -> list[dict]:
        return [c.status | {"running": c.is_alive()} for c in self.active.values()]

    def stop_all(self) -> None:
        for name in list(self.active):
            self.stop(name)
