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


def load_audio(path: str | Path, target_sr: int) -> tuple[np.ndarray, int]:
    """Load mono float32 audio resampled to ``target_sr``. WAV/FLAC via soundfile, else librosa."""
    path = Path(path)
    try:
        import soundfile as sf

        x, sr = sf.read(str(path), dtype="float32", always_2d=True)
        x = x.mean(axis=1)
    except Exception:
        import librosa

        x, sr = librosa.load(str(path), sr=None, mono=True)
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
