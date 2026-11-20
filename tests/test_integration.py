"""Integration: Phase 0 synthetic end-to-end and Phase A file-based (WAV + tar1090 JSON) pipeline."""
import numpy as np
import pandas as pd
import pytest
import yaml

from dopplergeo import adsb, audio, cli, config, geo, model, pipeline, score, synth
from conftest import ROOT

JETS = str(ROOT / "data" / "icao_jet_types.csv")


@pytest.fixture(scope="module")
def phase0(tmp_path_factory):
    cfg = config.load_config(overrides={"adsb": {"jet_types_csv": JETS}})
    c = config.speed_of_sound(cfg)
    d = tmp_path_factory.mktemp("p0")
    truth = synth.make_phase0_fixture(d / "fx", 1, cfg, c)
    return cfg, c, d, truth


def test_phase0_end_to_end(phase0):
    cfg, c, d, truth = phase0
    df, _ = adsb.ingest(d / "fx" / "adsb.jsonl", cfg)
    ob = truth["observer"]
    rcfg = config.deep_merge(cfg, {"elevation": {"constant_m": ob["elev_m"]}})
    res = pipeline.run_locate(d / "fx" / "audio.wav", df, (truth["search"]["center_lat"], truth["search"]["center_lon"]),
                              truth["search"]["radius_m"], rcfg, d / "out", c, events_spec="whole",
                              time_ref="absolute", audio_start=truth["audio"]["start_unix"],
                              truth={"lat": ob["lat"], "lon": ob["lon"]}, attestation="synthetic")
    t = res["truth"]
    assert t["err_m"] <= 150.0
    assert t["score"] >= res["top"]["score"] * (1 - 1e-3)  # exact truth is (essentially) the global maximum
    for f in ("report.md", "candidates.csv", "overlay_e1.png", "heatmap.png"):
        assert (d / "out" / f).exists()
    rep = (d / "out" / "report.md").read_text()
    for section in ("## Inputs", "## Candidate counts per stage", "### Top-10 candidate points", "## Timings"):
        assert section in rep


def test_naive_model_fails_scoring(phase0):
    """Scoring against the non-retarded curve must fail at the TRUE location."""
    cfg, c, d, truth = phase0
    x, sr = audio.load_audio(d / "fx" / "audio.wav", 16000)
    spec = audio.spectrogram_from_config(x, sr, cfg)
    df, _ = adsb.ingest(d / "fx" / "adsb.jsonl", cfg)
    tr = adsb.tracks_from_table(df)[0]
    ob = truth["observer"]
    r = geo.lla_to_ecef(ob["lat"], ob["lon"], ob["elev_m"])
    f0 = truth["flights"][0]["f0_hz"]
    sc = score.EventScorer(spec, spec.times[0], spec.times[-1], cfg, f0)
    T0 = truth["audio"]["start_unix"]
    good = sc.score(model.forward_curve(r, tr, f0, sc.tf + T0, c)[None, :])
    naive = sc.score(model.forward_curve_naive(r, tr, f0, sc.tf + T0, c)[None, :])
    assert abs(good.dt[0]) < 0.3
    assert naive.score[0] < 0.5 * good.score[0]


def test_phase_a_file_based(tmp_path, capsys):
    cfg = config.load_config(overrides={"adsb": {"jet_types_csv": JETS}})
    c = config.speed_of_sound(cfg)
    fx = tmp_path / "fx"
    truth = synth.make_phase_a_fixture(fx, 7, cfg, c)
    # the fixture's run config + the jet table path
    rc = yaml.safe_load((fx / "config.yaml").read_text())
    rc["adsb"] = {"jet_types_csv": JETS}
    (fx / "config.yaml").write_text(yaml.safe_dump(rc))
    assert cli.main(["ingest-adsb", "--src", str(fx / "tar1090"), "--out", str(fx / "adsb.parquet")]) == 0
    tab = adsb.read_table(fx / "adsb.parquet")
    assert tab["flight_id"].nunique() == 4
    ob = truth["observer"]
    region = f"center={truth['region']['lat']},{truth['region']['lon']},radius_km=4"
    # authorization gate
    with pytest.raises(SystemExit):
        cli.main(["locate", "--audio", str(fx / "audio.wav"), "--region", region, "--adsb", str(fx / "adsb.parquet"),
                  "--out", str(tmp_path / "x")])
    rcode = cli.main(["--config", str(fx / "config.yaml"), "locate", "--audio", str(fx / "audio.wav"), "--region",
                      region, "--adsb", str(fx / "adsb.parquet"), "--out", str(tmp_path / "run"),
                      "--truth-latlon", f"{ob['lat']},{ob['lon']}"])
    assert rcode == 0
    pts = pd.read_csv(tmp_path / "run" / "candidates.csv")
    top = pts.iloc[0]
    err = geo.ground_distance(top["lat"], top["lon"], ob["lat"], ob["lon"])
    assert err <= 150.0
    audible = [f["icao24"] for f in truth["flights"] if f["audible"]]
    assert top["combo"].split("+")[0].startswith(audible[0]) and top["combo"].split("+")[1].startswith(audible[1])
    jet = [f["icao24"] for f in truth["flights"] if f["type_code"] == "B738"][0]
    assert not pts["combo"].str.contains(jet).any()  # known jet removed before candidate generation
    best = pd.read_csv(tmp_path / "run" / "best_per_combination.csv")
    assert best["score"].iloc[0] > 1.5 * best["score"].iloc[1]  # clear margin over the wrong flight pairs


def test_no_flight_data_still_shows_what_was_heard(phase0):
    """Without flight data the run still analyses the audio (spectrogram + flyovers) before stopping."""
    from dopplergeo import live

    cfg, c, d, truth = phase0
    empty = adsb.coerce(pd.DataFrame({k: pd.Series(dtype=t) for k, t in adsb.SCHEMA.items()}))
    with pytest.raises(RuntimeError, match="No flight data"):
        pipeline.run_locate(d / "fx" / "audio.wav", empty, (truth["search"]["center_lat"], truth["search"]["center_lon"]),
                            5000.0, cfg, d / "nodata", c, events_spec="whole", time_ref="absolute",
                            audio_start=truth["audio"]["start_unix"], no_data_reason="No flight data for that time")
    ev, _ = live.read_events(d / "nodata" / "live.jsonl")
    kinds = [e["type"] for e in ev]
    assert "spectrogram" in kinds and "flyover" in kinds and "nodata" in kinds and kinds[-1] == "failed"
    assert (d / "nodata" / "recording_spectrogram.png").exists() and "stopped before the search" in (d / "nodata" / "report.md").read_text()
