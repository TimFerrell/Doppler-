"""ADS-B sources (trace parsing, archive downloader, scheduler, library) and video audio extraction."""
import datetime as dt
import gzip
import io
import json
import subprocess
import tarfile

import numpy as np
import pandas as pd
import pytest

from dopplergeo import adsb, audio, sources


def _trace(hexid, t0, pts, t="C172"):
    # [dt, lat, lon, alt, gs, track, flags, vrate, aircraft, source, geom_alt, geom_vrate, ias, roll]
    return {"icao": hexid, "t": t, "timestamp": t0,
            "trace": [[k * 10.0, la, lo, alt, 100.0, 90.0, 0, 0, None, "adsb_icao", None, None, None, None]
                      for k, (la, lo, alt) in enumerate(pts)]}


def test_parse_trace_fields_and_filters():
    st = {"points": 0, "on_ground": 0, "dropped_missing_fields": 0}
    obj = _trace("ABC123", 1.7e9, [(27.0, -81.0, 3000), (27.0, -80.99, "ground"), (40.0, -100.0, 3000)])
    obj["trace"].append([40.0, 27.1, -81.1, 3000, None, 90.0, 0, 0, None, None, None, None, None, None])
    obj["trace"].append([50.0, 27.2, -81.2, 3000, 100.0, 90.0, 8, 640, None, None, 3100, None, None, None])
    rows = sources.parse_trace(obj, st, bbox=(24, -88, 31, -79))
    assert [r["time"] for r in rows] == [1.7e9, 1.7e9 + 50.0]
    assert st["on_ground"] == 1 and st["dropped_missing_fields"] == 1
    r0, r1 = rows
    assert r0["icao24"] == "abc123" and r0["type_code"] == "C172"
    assert r0["vx_east"] == pytest.approx(51.4444, abs=1e-3) and r0["alt_msl_m"] == pytest.approx(914.4)
    assert r1["alt_msl_m"] == pytest.approx(3100 * 0.3048)  # geometric altitude preferred when present
    assert r1["vz_up"] == pytest.approx(640 * 0.00508)


def test_prefilter_keeps_border_crossers():
    inside = _trace("a", 0, [(35.0, -90.0, 1000)] * 30 + [(28.0, -82.0, 1000)])
    outside = _trace("b", 0, [(45.0, -120.0, 1000)] * 31)
    assert sources._trace_may_touch(inside, (24, -88, 31, -79))
    assert not sources._trace_may_touch(outside, (24, -88, 31, -79))


def _split_tar(tmp_path, traces):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        readme = b"readme"
        ti = tarfile.TarInfo("./README.txt"); ti.size = len(readme); tar.addfile(ti, io.BytesIO(readme))
        for obj in traces:
            data = gzip.compress(json.dumps(obj).encode())
            ti = tarfile.TarInfo(f"./traces/{obj['icao'][-2:]}/trace_full_{obj['icao']}.json")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    half = len(raw) // 2
    urls = []
    for k, chunk in enumerate((raw[:half], raw[half:])):
        p = tmp_path / f"day.tar.a{'ab'[k]}"
        p.write_bytes(chunk)
        urls.append((p.as_uri(), len(chunk)))
    return urls


def test_fetch_day_streams_split_tar_and_filters(tmp_path, monkeypatch):
    traces = [_trace(f"a{k:05x}", 1.7e9, [(27.0 + 0.01 * j, -81.0, 2000) for j in range(50)]) for k in range(30)]
    traces += [_trace("b00001", 1.7e9, [(45.0, -120.0, 2000)] * 50)]
    urls = _split_tar(tmp_path, traces)
    monkeypatch.setattr(sources, "adsblol_part_urls", lambda day, variant="prod-0", max_parts=12: urls)
    out = tmp_path / "ds"
    st = sources.fetch_adsblol_day(dt.date(2026, 1, 2), (24, -88, 31, -79), out, chunk_rows=400)
    assert st["trace_files"] == 31 and st["traces_prefiltered_out"] == 1 and st["aircraft_in_bbox"] == 30
    assert st["rows_out"] == 1500 and st["files_out"] >= 3  # written in chunks
    df = adsb.read_table(out)
    assert len(df) == 1500 and df["flight_id"].nunique() == 30  # ids re-derived across part files
    sub = adsb.read_table(out, bbox=(27.0, -81.5, 27.1, -80.5), t_range=(1.7e9, 1.7e9 + 200))
    assert len(sub) == 30 * 11 and sub["lat"].max() <= 27.1


def test_archive_scheduler_queues_new_days(tmp_path, monkeypatch):
    lib = sources.Library(tmp_path)
    lib.create("fl", {"kind": "archive", "subscribe": True, "request_bbox": [24, -88, 31, -79], "days": ["2026-01-01"],
                      "since": "2026-01-01"})
    lib.create("other", {"kind": "archive", "subscribe": False, "request_bbox": [1, 2, 3, 4]})
    monkeypatch.setattr(sources, "latest_archive_day", lambda: dt.date(2026, 1, 3))
    got = []
    sch = sources.ArchiveScheduler(lib, lambda n, d: got.append((n, d)), max_backfill=5)
    sch.check_once()
    assert sorted(got) == [("fl", "2026-01-02"), ("fl", "2026-01-03")]
    sch.check_once()
    assert len(got) == 2  # already queued


def test_normalise_v2_snapshot_ms():
    s = sources.normalise_snapshot({"now": 1795187753500, "ac": [{"hex": "a"}]})
    assert s["now"] == pytest.approx(1795187753.5) and s["aircraft"] == [{"hex": "a"}]


def test_library_append_refresh(tmp_path):
    lib = sources.Library(tmp_path)
    lib.create("x", {"source": "test"})
    df = sources.rows_to_frame([{"time": 1.0 + k, "icao24": "abc", "lat": 30.0, "lon": -80.0, "alt_msl_m": 100.0,
                                 "vx_east": 1.0, "vy_north": 0.0, "vz_up": 0.0, "type_code": None} for k in range(5)])
    m = lib.append("x", df)
    assert m["rows"] == 5 and m["flights"] == 1 and m["bbox"] == [30.0, -80.0, 30.0, -80.0]
    with pytest.raises(ValueError):
        lib.path("../etc")


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    d = tmp_path_factory.mktemp("vid")
    t = np.arange(16000 * 4) / 16000
    import soundfile as sf

    sf.write(d / "tone.wav", (0.5 * np.sin(2 * np.pi * 140 * t)).astype(np.float32), 16000)
    out = d / "clip.mov"
    subprocess.run([audio.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=64x64:r=5", "-i", str(d / "tone.wav"), "-shortest", "-c:v", "libx264", "-c:a", "aac",
                    "-ar", "48000", "-ac", "2", "-metadata", "creation_time=2024-08-30T09:18:50Z",
                    "-metadata", "com.apple.quicktime.creationdate=2024-08-30T05:18:49-0400",
                    "-movflags", "use_metadata_tags", str(out)], check=True)
    return out


def test_video_audio_extraction_and_metadata(video, tmp_path):
    info = audio.media_info(video)
    assert info["has_audio"] and info["has_video"]
    # Apple's creation date (start of recording, local time with offset) wins over creation_time
    assert info["time_source"] == "com.apple.quicktime.creationdate"
    assert info["start_utc"] == dt.datetime(2024, 8, 30, 9, 18, 49, tzinfo=dt.timezone.utc).timestamp()
    wav = audio.extract_audio(video, tmp_path / "a.wav", 16000)
    x, sr = audio.load_audio(wav, 16000)
    assert sr == 16000 and abs(len(x) / sr - 4.0) < 0.1
    spec = np.abs(np.fft.rfft(x[:16000]))
    assert abs(np.argmax(spec) - 140) <= 1  # the tone survives AAC + extraction
    x2, _ = audio.load_audio(video, 16000)  # direct load of the video path
    assert abs(len(x2) - len(x)) < 1600
