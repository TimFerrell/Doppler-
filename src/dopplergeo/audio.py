"""Audio I/O and spectrogram."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp", ".mts", ".m2ts"}
NATIVE_EXT = {".wav", ".flac", ".ogg", ".mp3", ".aiff", ".aif"}


def ffmpeg_exe() -> str:
    """Bundled ffmpeg (imageio-ffmpeg wheel), else one on PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        import shutil

        exe = shutil.which("ffmpeg")
        if not exe:
            raise RuntimeError("ffmpeg not found: pip install imageio-ffmpeg (or install ffmpeg)") from None
        return exe


def _parse_time(v: str) -> float | None:
    import datetime as dt
    import re

    v = v.strip()
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v)  # -0400 -> -04:00
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def media_info(path: str | Path) -> dict:
    """Duration, audio presence and recording time from a media file's container metadata.

    ``start_utc`` prefers Apple's ``com.apple.quicktime.creationdate`` (local time with offset, the
    moment recording started); otherwise the generic ``creation_time`` (UTC), which some Android
    phones write when recording *stops* -- callers should let the user confirm it.
    """
    import re
    import subprocess

    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    err = r.stderr
    info = {"duration_s": None, "has_audio": " Audio:" in err, "has_video": " Video:" in err,
            "start_utc": None, "time_source": None}
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    if m:
        info["duration_s"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"com\.apple\.quicktime\.creationdate\s*:\s*(\S+)", err)
    if m and _parse_time(m.group(1)) is not None:
        info["start_utc"], info["time_source"] = _parse_time(m.group(1)), "com.apple.quicktime.creationdate"
    else:
        m = re.search(r"creation_time\s*:\s*(\S+)", err)
        if m and _parse_time(m.group(1)) is not None:
            info["start_utc"], info["time_source"] = _parse_time(m.group(1)), "creation_time"
    return info


def extract_audio(src: str | Path, out_wav: str | Path, sr: int = 16000) -> Path:
    """First audio stream of a video (or any media file) -> mono 16-bit WAV at ``sr``."""
    import subprocess

    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-map", "0:a:0",
           "-ac", "1", "-ar", str(int(sr)), "-c:a", "pcm_s16le", str(out_wav)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out_wav.exists():
        raise RuntimeError(f"ffmpeg could not extract audio from {src}: {r.stderr.strip()[-500:]}")
    return out_wav


def load_audio(path: str | Path, target_sr: int) -> tuple[np.ndarray, int]:
    """Load mono float32 audio resampled to ``target_sr``.

    WAV/FLAC/OGG/MP3 via soundfile; video files and other containers (MOV, MP4, M4A, ...) via ffmpeg.
    """
    path = Path(path)
    if path.suffix.lower() not in NATIVE_EXT:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            wav = extract_audio(path, Path(td) / "audio.wav", target_sr)
            return load_audio(wav, target_sr)
    import soundfile as sf

    try:
        x, sr = sf.read(str(path), dtype="float32", always_2d=True)
        x = x.mean(axis=1)
    except Exception:  # noqa: BLE001 - e.g. an MP3 variant libsndfile cannot read
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            wav = extract_audio(path, Path(td) / "audio.wav", target_sr)
            x, sr = sf.read(str(wav), dtype="float32", always_2d=True)
            x = x.mean(axis=1)
    if sr != target_sr:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(int(sr), int(target_sr))
        x = resample_poly(x, target_sr // g, int(sr) // g).astype(np.float32)
        sr = target_sr
    return np.asarray(x, np.float32), int(sr)


@dataclass
class Spectrogram:
    times: np.ndarray  # (J,) column-centre times, seconds relative to the first audio sample
    freqs: np.ndarray  # (F,) Hz, uniform
    db: np.ndarray  # (F,J) power dB
    window: int
    hop: int
    sr: int

    @property
    def bin_hz(self) -> float:
        return float(self.freqs[1] - self.freqs[0])

    brightness_mode: str = "snr"
    brightness_clip: float | None = None

    @property
    def brightness(self) -> np.ndarray:
        """Noise-whitened brightness, per frequency row.

        ``snr`` (default): linear power / row median (incoherent matched-filter statistic;
        weights high-SNR columns most, where the geometry is best constrained).
        ``db``: dB above the row median. ``brightness_clip`` caps values (transient robustness).
        """
        if getattr(self, "_bright_key", None) != (self.brightness_mode, self.brightness_clip):
            med = np.median(self.db, axis=1, keepdims=True)
            rel = self.db - med
            b = 10.0 ** (rel / 10.0) if self.brightness_mode == "snr" else rel
            if self.brightness_clip is not None:
                b = np.minimum(b, self.brightness_clip)
            self._bright = np.ascontiguousarray(b.astype(np.float32))
            self._bright_key = (self.brightness_mode, self.brightness_clip)
        return self._bright

    def band(self, f_lo: float, f_hi: float) -> tuple[np.ndarray, np.ndarray]:
        sel = (self.freqs >= f_lo) & (self.freqs <= f_hi)
        return self.freqs[sel], self.db[sel]


def spectrogram(x: np.ndarray, sr: int, window: int = 16384, hop: int | None = None, fmin: float = 5.0,
                fmax: float = 800.0, pad_factor: int = 1, chunk_frames: int = 256) -> Spectrogram:
    """Hamming STFT power in dB, restricted to [fmin, fmax]. Hop defaults to window/8."""
    hop = hop or window // 8
    x = np.asarray(x, np.float64)
    if len(x) < window:
        raise ValueError(f"audio shorter than one STFT window ({len(x)} < {window} samples)")
    n_frames = 1 + (len(x) - window) // hop
    nfft = window * int(pad_factor)
    freqs_all = np.fft.rfftfreq(nfft, 1.0 / sr)
    sel = np.where((freqs_all >= fmin) & (freqs_all <= fmax))[0]
    k0, k1 = sel[0], sel[-1] + 1
    w = np.hamming(window)
    frames = np.lib.stride_tricks.sliding_window_view(x, window)[::hop][:n_frames]
    out = np.empty((k1 - k0, n_frames), np.float32)
    scale = 1.0 / (w.sum() ** 2)
    for s in range(0, n_frames, chunk_frames):
        X = np.fft.rfft(frames[s:s + chunk_frames] * w, n=nfft, axis=1)[:, k0:k1]
        p = (X.real ** 2 + X.imag ** 2) * scale
        out[:, s:s + chunk_frames] = (10.0 * np.log10(p + 1e-20)).T
    times = (np.arange(n_frames) * hop + window / 2.0) / sr
    return Spectrogram(times, freqs_all[k0:k1], out, window, hop, sr)


def spectrogram_from_config(x: np.ndarray, sr: int, cfg: dict) -> Spectrogram:
    s = cfg["spectrogram"]
    sp = spectrogram(x, sr, int(s["window"]), int(s["window"]) // int(s["hop_divisor"]), float(s["fmin_hz"]),
                     float(s["fmax_hz"]), int(s["pad_factor"]))
    sp.brightness_mode = cfg["score"]["brightness"]
    sp.brightness_clip = cfg["score"]["brightness_clip"]
    return sp


def high_spectrogram_from_config(x: np.ndarray, sr: int, cfg: dict) -> Spectrogram | None:
    """Second, higher band (jet engine tones). None if disabled or above Nyquist."""
    s = cfg["spectrogram"]
    if not cfg["detect"].get("jet_band_hz"):
        return None
    f1 = min(float(s["high_fmax_hz"]), sr / 2 - 50)
    if f1 <= float(s["high_fmin_hz"]) + 50:
        return None
    w = int(s["high_window"])
    sp = spectrogram(x, sr, w, w // int(s["hop_divisor"]), float(s["high_fmin_hz"]), f1, int(s["high_pad_factor"]))
    sp.brightness_mode = cfg["score"]["brightness"]
    sp.brightness_clip = cfg["score"]["brightness_clip"]
    return sp


def log_display(low: Spectrogram, high: Spectrogram | None, f_lo: float = 40.0, f_hi: float = 3000.0,
                rows: int = 420, max_cols: int = 3000) -> tuple[np.ndarray, float, float]:
    """Spectrogram resampled to log-spaced frequency rows (prop hums and jet whines on one picture).

    Each row is normalised to its own median (frequency response flattened) before display scaling.
    Returns (image 0..1 [rows, cols] with row 0 = lowest frequency, f_lo, f_hi actually covered).
    """
    f_hi = min(f_hi, float(high.freqs[-1]) if high is not None else float(low.freqs[-1]))
    fr = np.geomspace(f_lo, f_hi, rows)
    cstep = max(1, low.db.shape[1] // max_cols)
    out = np.empty((rows, low.db[:, ::cstep].shape[1]), np.float32)
    split = float(low.freqs[-1]) - 50 if high is not None else np.inf
    for src, sel in ((low, fr < split), (high, fr >= split)):
        if src is None or not sel.any():
            continue
        D = src.db[:, ::cstep]
        D = D - np.median(D, axis=1, keepdims=True)
        idx = np.clip(np.searchsorted(src.freqs, fr[sel]), 0, len(src.freqs) - 1)
        out[sel] = D[idx, :out.shape[1]]
    lo, hi = np.percentile(out, [8, 99.7])
    return np.clip((out - lo) / max(1e-6, hi - lo), 0, 1), float(fr[0]), float(fr[-1])
