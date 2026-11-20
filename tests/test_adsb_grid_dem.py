import json

import numpy as np
import pandas as pd
import pytest
import yaml

from dopplergeo import adsb, config, filters, geo, grid
from conftest import ROOT


def _snap(now, acs):
    return {"now": now, "messages": 10, "aircraft": acs}


def _ac(hexid="a1b2c3", lat=40.0, lon=-100.0, **kw):
    d = {"hex": hexid, "lat": lat, "lon": lon, "alt_baro": 5000, "alt_geom": 5100, "gs": 100.0, "track": 90.0,
         "baro_rate": 640, "seen_pos": 0.5, "t": "C172"}
    d.update(kw)
    return d


def test_config_example_in_sync_with_defaults():
    with open(ROOT / "config.example.yaml") as fh:
        assert yaml.safe_load(fh) == config.DEFAULTS


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("score:\n  dt_swep_s: 2\n")
    with pytest.raises(KeyError):
        config.load_config(p)


def test_ingest_readsb_snapshot_series(tmp_path, cfg):
    d = tmp_path / "readsb"
    d.mkdir()
    for i in range(5):
        acs = [_ac(lon=-100.0 + 0.001 * i, seen_pos=0.2), {"hex": "ffffff", "seen": 1.0},
               _ac("b00b00", alt_baro="ground", alt_geom=None)]
        (d / f"aircraft_{i:03d}.json").write_text(json.dumps(_snap(1.7e9 + i, acs)))
    df, st = adsb.ingest(d, cfg)
    assert list(df.columns) == adsb.REQUIRED
    assert len(df) == 5 and st["no_position"] == 5 and st["on_ground"] == 5
    r = df.iloc[0]
    assert r.time == pytest.approx(1.7e9 - 0.2)
    assert r.alt_msl_m == pytest.approx(5100 * 0.3048, rel=1e-6)  # geometric preferred
    assert r.vx_east == pytest.approx(51.4444, abs=1e-3) and abs(r.vy_north) < 1e-4
    assert r.vz_up == pytest.approx(640 * 0.00508, rel=1e-5)
    assert r.type_code == "C172"
    assert str(df["time"].dtype) == "float64" and str(df["alt_msl_m"].dtype) == "float32"


def test_ingest_tar1090_history_and_dedupe(tmp_path, cfg):
    d = tmp_path / "tar1090"
    d.mkdir()
    # the same fix reported in two snapshots (seen_pos grows) must be deduplicated
    (d / "history_0.json").write_text(json.dumps(_snap(1000.0, [_ac(seen_pos=0.0)])))
    (d / "history_1.json").write_text(json.dumps(_snap(1001.0, [_ac(seen_pos=1.0)])))
    (d / "history_2.json").write_text(json.dumps(_snap(1002.0, [_ac(lon=-99.99, seen_pos=0.0)])))
    df, st = adsb.ingest(d, cfg)
    assert len(df) == 2 and st["duplicates"] == 1


def test_missing_velocity_fails_loudly(tmp_path, cfg):
    p = tmp_path / "aircraft.json"
    p.write_text(json.dumps(_snap(1000.0, [_ac(gs=None)])))
    with pytest.raises(adsb.ADSBError, match="gs"):
        adsb.ingest(p, cfg)
    cfg["adsb"]["on_missing_velocity"] = "drop"
    with pytest.raises(adsb.ADSBError, match="no ADS-B position rows"):
        adsb.ingest(p, cfg)


def test_schema_validation_fails_on_missing_column():
    df = pd.DataFrame({"time": [1.0], "icao24": ["a"], "lat": [1.0], "lon": [2.0]})
    with pytest.raises(adsb.ADSBError, match="missing required column"):
        adsb.validate(df)


def test_flight_ids_and_track_segments():
    t = np.concatenate([np.arange(0, 100, 1.5), np.arange(160, 260, 1.5), np.arange(5000, 5100, 1.5)])
    n = len(t)
    lat = 40.0 + np.arange(n) * 1e-4
    df = pd.DataFrame({"time": 1.7e9 + t, "icao24": "abc", "flight_id": 0, "lat": lat, "lon": -100.0,
                       "alt_msl_m": 1000.0, "vx_east": 0.0, "vy_north": 10.0, "vz_up": 0.0, "type_code": None})
    df = adsb.assign_flight_ids(adsb.coerce(df), gap_s=1800.0)
    assert df["flight_id"].nunique() == 2
    tracks = adsb.tracks_from_table(df, dt=1.0, max_gap_s=30.0)
    assert len(tracks) == 3  # 60 s gap splits flight 1
    tr = tracks[0]
    assert np.allclose(np.diff(tr.t), 1.0) and tr.t0 == np.ceil(tr.t0)
    # linear interpolation in ECEF reproduces the reported fixes
    p = tr.position(df["time"].iloc[3])
    assert np.allclose(p, geo.lla_to_ecef(lat[3], -100.0, 1000.0), atol=0.05)
    assert np.isnan(tr.position(tr.t1 + 5)).all()


def test_jet_filter_keeps_unknown(cfg):
    jets = filters.load_jet_types(cfg["adsb"]["jet_types_csv"])
    assert {"B738", "A320", "E175", "CRJ9", "B77W", "A359", "MD88"} <= jets
    assert "C172" not in jets and "DH8D" not in jets
    df = pd.DataFrame({"flight_id": [1, 2, 3, 4], "type_code": ["B738", "C208", None, "a320"]})
    out, st = filters.drop_jets(df, jets)
    assert sorted(out["flight_id"]) == [2, 3]
    assert st["flights_unknown_type_kept"] == 1


def test_grid_and_refine():
    g = grid.make_grid((40.0, -100.0), 1000.0, 200.0)
    assert np.all(np.hypot(g.x, g.y) <= 1000.0 + 1e-6)
    assert len(g) == 81  # lattice points (i,j) in [-5,5]^2 with i^2 + j^2 <= 25
    lat, lon = g.latlon()
    i = np.where((g.x == 0) & (g.y == 0))[0][0]
    j = np.where((g.x == 200) & (g.y == 0))[0][0]
    d = geo.ground_distance(lat[i], lon[i], lat[j], lon[j])
    assert d == pytest.approx(200.0, abs=0.5)
    r = grid.refine(g.proj, np.array([0.0, 200.0]), np.array([0.0, 0.0]), 200.0)
    assert r.step == 100.0 and len(r) == 15  # two overlapping 3x3 neighbourhoods


def test_dem_geotiff_sampler(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    path = tmp_path / "dem.tif"
    data = (np.arange(100, dtype=np.float32).reshape(10, 10) * 10.0)
    with rasterio.open(path, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32", crs="EPSG:4326",
                       transform=from_origin(-100.0, 41.0, 0.1, 0.1), nodata=-9999.0) as ds:
        ds.write(data, 1)
    e = geo.Elevation(dem_path=str(path))
    assert e(40.95, -99.95) == pytest.approx(0.0)
    assert e(40.85, -99.75) == pytest.approx(120.0)  # row 1, col 2
    with pytest.raises(ValueError):
        e(30.0, -99.0)  # outside the raster


def test_large_search_area_starts_coarse(cfg):
    import math

    step, levels = float(cfg["grid"]["step_m"]), int(cfg["grid"]["refine_levels"])
    n_full = math.pi * (100_000 / step) ** 2
    extra = 0
    while n_full / 4 ** extra > cfg["grid"]["max_points"]:
        extra += 1
    g = grid.make_grid((28.6, -81.4), 100_000, step * 2 ** extra)
    assert len(g) <= cfg["grid"]["max_points"] * 1.05
    assert step * 2 ** extra / 2 ** (levels + extra) <= step / 2  # refinement still ends at the normal resolution
