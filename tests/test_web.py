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

    app = create_app(tmp_path / "jobs")
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
    assert "I own this recording" in html and 'name="adsb"' in html
    assert "https://" not in html.split("<footer")[0]  # no third-party resources


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
    assert "Best location" in page and "Error vs known location" in page
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
