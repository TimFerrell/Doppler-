import numpy as np
import pytest

from dopplergeo import audio, measure, model, score

C = 343.0
SR = 16000
TRUE = dict(f0=140.0, v=100.0, d=3200.0, t_c=55.0)


def _tone_audio(duration=110.0, noise=0.0, seed=0):
    """Tone whose instantaneous frequency follows the straight-line retarded model exactly."""
    t = np.arange(int(duration * SR)) / SR
    # emission time per sample, closed form: s(t_h) = cos(2 pi f0 t_e(t_h))
    tau = model.straight_line_retarded_tau(t, TRUE["t_c"], TRUE["v"], TRUE["d"], C)
    t_e = TRUE["t_c"] + tau
    x = np.cos(2 * np.pi * TRUE["f0"] * t_e)
    if noise:
        x = x + np.random.default_rng(seed).normal(0, noise, len(x))
    return x.astype(np.float32)


@pytest.fixture(scope="module")
def tone_spec():
    from dopplergeo import config

    cfg = config.load_config()
    return audio.spectrogram_from_config(_tone_audio(noise=0.3), SR, cfg), cfg


def test_fit_ridge_exact_on_noiseless_model_points(cfg):
    t = np.linspace(0, 110, 800)
    f = model.straight_line_curve(t, TRUE["f0"], TRUE["v"], TRUE["d"], TRUE["t_c"], C)
    m = measure.fit_ridge(t, f, cfg, C, (80, 300), 0.0, 110.0)
    assert m.success
    assert m.f0 == pytest.approx(TRUE["f0"], rel=1e-7)
    assert m.v == pytest.approx(TRUE["v"], rel=1e-6)
    assert m.d == pytest.approx(TRUE["d"], rel=1e-6)
    assert m.t_cpa == pytest.approx(TRUE["t_c"], abs=1e-5)
    assert m.t_cpa_heard == pytest.approx(TRUE["t_c"] + TRUE["d"] / C, abs=1e-5)


def test_fit_curve_on_rendered_audio(tone_spec):
    spec, cfg = tone_spec
    m, _ = measure.fit_curve(spec, cfg, C)
    assert m.success
    assert m.f0 == pytest.approx(TRUE["f0"], abs=0.05)
    assert m.v == pytest.approx(TRUE["v"], abs=0.5)
    assert m.d == pytest.approx(TRUE["d"], rel=0.02)
    assert m.t_cpa_heard == pytest.approx(TRUE["t_c"] + TRUE["d"] / C, abs=0.1)


def test_scorer_peaks_at_zero_shift_for_true_curve(tone_spec):
    spec, cfg = tone_spec
    sc = score.EventScorer(spec, spec.times[0], spec.times[-1], cfg, TRUE["f0"])
    curve = model.straight_line_curve(sc.tf, TRUE["f0"], TRUE["v"], TRUE["d"], TRUE["t_c"], C)[None, :]
    tab, dts, dfs = sc.score_exhaustive(curve)
    k, l = np.unravel_index(tab[0].argmax(), tab[0].shape)
    assert dts[k] == pytest.approx(0.0, abs=1e-9)
    assert dfs[l] == pytest.approx(0.0, abs=1e-9)
    # the fast coarse-to-fine sweep finds the same optimum
    r = sc.score(curve)
    assert r.score[0] == pytest.approx(tab[0].max(), rel=1e-6)
    assert abs(r.dt[0]) <= 0.02 and abs(r.df[0]) <= 0.02


def test_scorer_recovers_known_shift(tone_spec):
    spec, cfg = tone_spec
    sc = score.EventScorer(spec, spec.times[0], spec.times[-1], cfg, TRUE["f0"] - 0.6)
    # model 1.2 s early and with f0 0.6 Hz low: the sweep must undo both
    curve = model.straight_line_curve(sc.tf + 1.2, TRUE["f0"] - 0.6, TRUE["v"], TRUE["d"], TRUE["t_c"], C)[None, :]
    r = sc.score(curve)
    assert r.dt[0] == pytest.approx(1.2, abs=0.05)
    assert r.df[0] == pytest.approx(0.6, abs=0.05)


def test_ridge_snr_and_band(tone_spec):
    spec, _ = tone_spec
    rg = measure.extract_ridge(spec, (80, 300))
    assert np.all((rg.freq >= 80) & (rg.freq <= 300))
    assert np.median(rg.snr_db) > 10
