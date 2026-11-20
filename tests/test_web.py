"""Web interface: forms, authorization gate, file serving safety, and a full job through HTTP."""
import io
import time

import pytest

from conftest import ROOT

flask = pytest.importorskip("flask")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)  # the default jet table path is repository-relative
    from dopplergeo.web import create_app

    app = create_app(tmp_path / "jobs", library_dir=tmp_path / "lib", recordings_dir=tmp_path / "rec", scheduler=False)
    app.testing = True
    return app.test_client(), tmp_path / "jobs"


def _wait(c, job_id, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/jobs/{job_id}/status.json").get_json()
        if s["state"] in ("done", "failed"):
            return s
        time.sleep(0.5)
    raise TimeoutError(job_id)


def test_index_renders(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "I made this recording" in html and "Get flight data" in html and 'name="adsb"' in html
    import re

    # no third-party resources: every script/stylesheet/image is served by this app
    srcs = re.findall(r'<(?:script|img)[^>]+src="([^"]+)"', html) + re.findall(r'<link[^>]+href="([^"]+)"', html)
    assert srcs and all(u.startswith("/") for u in srcs), srcs


def test_locate_requires_attestation_and_leaves_no_job(client):
    c, jobs = client
    data = {"center_lat": "40", "center_lon": "-100", "radius_km": "5",
            "audio": (io.BytesIO(b"RIFF"), "a.wav"), "adsb": (io.BytesIO(b"{}"), "aircraft.json")}
    r = c.post("/jobs/locate", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    assert "authorized" in r.get_data(as_text=True)
    assert not any(jobs.iterdir())


def test_locate_validates_inputs(client):
    c, _ = client
    data = {"attest": "yes", "center_lat": "91", "center_lon": "-100", "radius_km": "5",
            "audio": (io.BytesIO(b"RIFF"), "a.wav"), "adsb": (io.BytesIO(b"{}"), "aircraft.json")}
    r = c.post("/jobs/locate", data=data, content_type="multipart/form-data")
    assert r.status_code == 400 and "center lat" in r.get_data(as_text=True)
    data = {"attest": "yes", "center_lat": "40", "center_lon": "-100", "radius_km": "5",
            "audio": (io.BytesIO(b"x"), "evil.exe"), "adsb": (io.BytesIO(b"{}"), "aircraft.json")}
    r = c.post("/jobs/locate", data=data, content_type="multipart/form-data")
    assert r.status_code == 400 and "unsupported file type" in r.get_data(as_text=True)


def test_synth_job_end_to_end_and_file_safety(client):
    c, jobs = client
    r = c.post("/jobs/synth", data={"seeds": "1"})
    assert r.status_code == 302
    job_id = r.headers["Location"].rstrip("/").split("/")[-1]
    s = _wait(c, job_id)
    assert s["state"] == "done", s
    page = c.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Most likely location" in page and "Distance from the true location" in page and "What this means" in page
    rep = c.get(f"/jobs/{job_id}/files/out/report.md")
    assert rep.status_code == 200 and "<table>" in rep.get_data(as_text=True)
    assert c.get(f"/jobs/{job_id}/files/out/heatmap.png").status_code == 200
    cand = c.get(f"/jobs/{job_id}/candidates.json?run=out").get_json()
    assert len(cand["points"]["lat"]) > 100 and cand["truth"] is not None
    assert c.get(f"/jobs/{job_id}/bundle.zip").status_code == 200
    # path traversal and unknown jobs
    assert c.get(f"/jobs/{job_id}/files/../../etc/passwd").status_code == 404
    assert c.get(f"/jobs/{job_id}/files/%2e%2e/%2e%2e/config.yaml").status_code == 404
    assert c.get(f"/jobs/{job_id}/candidates.json?run=../..").status_code == 404
    assert c.get("/jobs/nope/status.json").status_code == 404


def test_recording_upload_and_video_import(client, tmp_path):
    import numpy as np
    import soundfile as sf

    c, _ = client
    buf = io.BytesIO()
    sf.write(buf, np.zeros(16000, np.float32), 16000, format="WAV")
    r = c.post("/recordings", data={"audio": (io.BytesIO(buf.getvalue()), "r.wav"), "start_utc": "1725009530.25",
                                    "label": "yard"}, content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 200 and j["name"] == "rec-20240830T091850Z-yard.wav" and j["duration_s"] == 1.0
    assert c.get(f"/recordings/{j['name']}").status_code == 200
    assert c.post("/recordings", data={"audio": (io.BytesIO(b"nope"), "r.wav"), "start_utc": "1"},
                  content_type="multipart/form-data").status_code == 400
    assert c.get("/recordings/..%2fx.wav").status_code == 404
    page = c.get("/?tab=record").get_data(as_text=True)
    assert "rec-20240830T091850Z-yard.wav" in page


def test_rehydrate_form_validation(client):
    c, _ = client
    import datetime as dt

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    base = {"south": "24.2", "west": "-87.8", "north": "31.2", "east": "-79.8", "region_label": "Florida"}
    r = c.post("/data/rehydrate", data={**base, "date": today})
    assert r.status_code == 400 and "past UTC days" in r.get_data(as_text=True)
    r = c.post("/data/rehydrate", data={**base, "date": "2026-01-01", "south": "31", "north": "24"})
    assert r.status_code == 400 and "south &lt; north" in r.get_data(as_text=True)
    r = c.post("/data/rehydrate", data={**base, "date": "2026-01-05", "end_date": "2026-01-01"})
    assert r.status_code == 400


def test_locate_refuses_recording_outside_flight_data(client, tmp_path):
    import numpy as np
    import soundfile as sf

    from dopplergeo import sources

    c, _ = client
    lib = sources.Library(tmp_path / "lib")
    lib.create("us", {"source": "test", "kind": "archive", "max_gap_s": 120.0})
    dd = lib.path("us") / "day=2026-10-21"
    dd.mkdir()
    sources.rows_to_frame([{"time": 1792540800.0 + k, "icao24": "abc", "lat": 34.0, "lon": -84.4, "alt_msl_m": 900.0,
                            "vx_east": 50.0, "vy_north": 0.0, "vz_up": 0.0, "type_code": "C172"}
                           for k in range(10)]).to_parquet(dd / "tile_+30_-090.parquet")
    lib.refresh("us")
    buf = io.BytesIO()
    sf.write(buf, np.zeros(16000, np.float32), 16000, format="WAV")
    rec = c.post("/recordings", data={"audio": (io.BytesIO(buf.getvalue()), "r.wav"), "start_utc": "1474200000",
                                      "label": "old"}, content_type="multipart/form-data").get_json()["name"]
    r = c.post("/jobs/locate", data={"attest": "yes", "recording": rec, "dataset": "us", "center_lat": "33.96",
                                     "center_lon": "-84.46", "radius_km": "200"})
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and "2016-09-18" in html and "2026-10-21" in html


def _locate_job(c, tmp_path):
    """A locate job from uploads that fails fast (no flyover in silence)."""
    import numpy as np
    import soundfile as sf

    from dopplergeo import sources

    wav = io.BytesIO()
    sf.write(wav, np.zeros(16000 * 20, np.float32), 16000, format="WAV")
    pq = io.BytesIO()
    sources.rows_to_frame([{"time": 1.7e9 + k, "icao24": "abc", "lat": 34.0, "lon": -84.4, "alt_msl_m": 900.0,
                            "vx_east": 50.0, "vy_north": 0.0, "vz_up": 0.0, "type_code": "C172"}
                           for k in range(30)]).to_parquet(pq)
    r = c.post("/jobs/locate", data={"attest": "yes", "operator": "me", "center_lat": "34.0", "center_lon": "-84.4",
                                     "radius_km": "7", "events": "", "truth_lat": "34.01", "truth_lon": "-84.41",
                                     "audio": (io.BytesIO(wav.getvalue()), "clip.wav"),
                                     "adsb": (io.BytesIO(pq.getvalue()), "flights.parquet")},
               content_type="multipart/form-data")
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    return r.headers["Location"].rstrip("/").split("/")[-1]


def test_retry_and_edit_rerun(client, tmp_path):
    c, jobs_dir = client
    jid = _locate_job(c, tmp_path)
    assert _wait(c, jid)["state"] == "failed"
    # retry: same files and settings, new job
    r = c.post(f"/jobs/{jid}/retry")
    assert r.status_code == 302
    rid = r.headers["Location"].rstrip("/").split("/")[-1]
    assert rid != jid and _wait(c, rid)["state"] == "failed"
    rj = c.get(f"/jobs/{rid}").get_data(as_text=True)
    assert "(retry 1)" in rj and "retry of" in rj
    assert (jobs_dir / rid / "inputs" / "clip.wav").exists()
    # edit & run again: form prefilled, files reused when nothing new is chosen
    page = c.get(f"/?from={jid}").get_data(as_text=True)
    assert 'value="34.0"' in page and 'name="reuse_job"' in page and "clip.wav" in page and 'value="7"' in page
    r = c.post("/jobs/locate", data={"attest": "yes", "reuse_job": jid, "center_lat": "34.0", "center_lon": "-84.4",
                                     "radius_km": "9"})
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    eid = r.headers["Location"].rstrip("/").split("/")[-1]
    _wait(c, eid)
    assert (jobs_dir / eid / "inputs" / "clip.wav").exists() and (jobs_dir / eid / "inputs" / "adsb").exists()
    assert "radius_km=9" in " ".join(c.get(f"/jobs/{eid}/status.json").get_json()["log"].split())
    assert c.post("/jobs/locate", data={"attest": "yes", "reuse_job": "nope", "center_lat": "1", "center_lon": "1",
                                        "radius_km": "1"}).status_code == 400
