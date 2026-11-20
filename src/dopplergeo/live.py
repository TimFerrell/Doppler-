"""Live event stream of a locate run (``<out>/live.jsonl``) for the animated web view.

Each line is one JSON event: ``{"t": unix, "type": ..., ...}``. The web UI tails the file while the job
runs and replays it afterwards. Writing is best-effort: a failure to write never affects the analysis.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np


class LiveFeed:
    def __init__(self, out_dir: str | Path | None):
        self.path = Path(out_dir) / "live.jsonl" if out_dir is not None else None
        self._fh = None
        self._pending: list[tuple] = []
        self._pending_t = 0.0
        self.best_score = -np.inf
        self.scored = 0
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self.path, "w", buffering=1)
            except OSError:
                self._fh = None

    def emit(self, type_: str, **data) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps({"t": round(time.time(), 3), "type": type_, **data}, default=_jsonable) + "\n")
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ helpers
    def spectrogram(self, spec, cfg, out_dir: Path) -> None:
        """Save a compact band image (40–400 Hz) for the live view and announce it."""
        if self._fh is None:
            return
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        f_lo, f_hi = 40.0, max(400.0, float(cfg["detect"]["band_hz"][1]) + 60.0)
        sel = (spec.freqs >= f_lo) & (spec.freqs <= f_hi)
        S = spec.db[sel]
        step = max(1, S.shape[0] // 360)  # ~360 rows is plenty for display
        S = S[::step]
        cstep = max(1, S.shape[1] // 2400)
        S = S[:, ::cstep]
        vmin, vmax = np.percentile(S, [8, 99.7])
        plt.imsave(out_dir / "live_spectrogram.png", np.clip((S - vmin) / max(1e-6, vmax - vmin), 0, 1), cmap="magma",
                   origin="lower")
        self.emit("spectrogram", image="live_spectrogram.png", t0=float(spec.times[0]), t1=float(spec.times[-1]),
                  f0=float(spec.freqs[sel][0]), f1=float(spec.freqs[sel][-1]),
                  duration=float(spec.times[-1] + spec.window / spec.sr / 2))

    def ridge(self, ridge, min_snr: float) -> None:
        good = ridge.snr_db >= min_snr
        t, f, s = ridge.times[good], ridge.freq[good], ridge.snr_db[good]
        k = max(1, len(t) // 1500)
        self.emit("ridge", t=np.round(t[::k], 2), f=np.round(f[::k], 2), snr=np.round(s[::k], 1))

    def flyover(self, i: int, meas, c: float) -> None:
        from .model import straight_line_curve

        tt = np.linspace(meas.t_lo, meas.t_hi, 240)
        ff = straight_line_curve(tt, meas.f0, meas.v, meas.d, meas.t_cpa, c)
        self.emit("flyover", i=i, t=np.round(tt, 2), f=np.round(ff, 2), t_cpa=meas.t_cpa_heard, f0=meas.f0, v=meas.v,
                  d=meas.d, rms=meas.rms_hz, t_lo=meas.t_lo, t_hi=meas.t_hi)

    def tracks(self, per_event) -> None:
        feats = {}
        for i, lst in enumerate(per_event):
            for tr, lo, hi in lst:
                k = tr.key
                if k not in feats:
                    sub = tr.subset(lo, hi) if lo is not None else tr
                    sub = sub or tr
                    idx = np.linspace(0, sub.n - 1, min(sub.n, 160)).astype(int)
                    feats[k] = {"key": k, "icao24": tr.icao24, "type_code": tr.type_code, "events": [],
                                "lat": np.round(sub.lat[idx], 5), "lon": np.round(sub.lon[idx], 5)}
                feats[k]["events"].append(i)
        self.emit("tracks", tracks=list(feats.values()))

    def scores(self, combo: int, lat, lon, score, final: bool = False) -> None:
        """Buffer scored spots and emit them in chunks (~every 0.4 s), plus 'best' when it improves."""
        self._pending.append((combo, np.asarray(lat), np.asarray(lon), np.asarray(score)))
        self.scored += len(np.atleast_1d(lat))
        i = int(np.argmax(score)) if len(np.atleast_1d(score)) else None
        if i is not None and score[i] > self.best_score:
            self.best_score = float(score[i])
            self._flush()
            self.emit("best", lat=float(lat[i]), lon=float(lon[i]), score=float(score[i]), combo=combo)
        if final or time.time() - self._pending_t > 0.4:
            self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        for combo in sorted({p[0] for p in self._pending}):
            parts = [p for p in self._pending if p[0] == combo]
            self.emit("scores", combo=combo, lat=np.round(np.concatenate([p[1] for p in parts]), 6),
                      lon=np.round(np.concatenate([p[2] for p in parts]), 6),
                      score=np.round(np.concatenate([p[3] for p in parts]), 1), total=self.scored)
        self._pending = []
        self._pending_t = time.time()

    def close(self) -> None:
        self._flush()
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def _jsonable(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def read_events(path: str | Path, pos: int = 0, max_bytes: int = 4_000_000) -> tuple[list[dict], int]:
    """Events appended after byte offset ``pos`` (complete lines only) and the new offset."""
    p = Path(path)
    if not p.exists():
        return [], pos
    with open(p, "rb") as fh:
        fh.seek(pos)
        data = fh.read(max_bytes)
    end = data.rfind(b"\n")
    if end < 0:
        return [], pos
    out = []
    for line in data[:end].splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out, pos + end + 1
