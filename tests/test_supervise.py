"""Auto-update supervisor: fast-forward updates, refusal on local changes, rollback on failed self-test."""
import subprocess

import pytest

from dopplergeo import supervise


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        git(dev, "config", k, v)
    (dev / "app.txt").write_text("v1\n")
    git(dev, "add", "-A"); git(dev, "commit", "-qm", "v1"); git(dev, "push", "-q", "origin", "HEAD:main")
    app = tmp_path / "app"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(app)], check=True)
    return dev, app


def _sup(app, tmp_path, monkeypatch, smoke=False):
    s = supervise.Supervisor(app, ["serve"], interval=1, state_path=tmp_path / "state.json", smoke=smoke)
    restarts = []
    monkeypatch.setattr(s, "start_child", lambda: restarts.append("start"))
    monkeypatch.setattr(s, "stop_child", lambda timeout=30: restarts.append("stop"))
    return s, restarts


def test_fast_forward_update_restarts_web_app(repos, tmp_path, monkeypatch):
    dev, app = repos
    (dev / "app.txt").write_text("v2\n")
    git(dev, "commit", "-qam", "v2"); git(dev, "push", "-q", "origin", "HEAD:main")
    s, restarts = _sup(app, tmp_path, monkeypatch)
    target = s.remote_sha()
    assert target == git(dev, "rev-parse", "HEAD")
    assert s.try_update(target)
    assert (app / "app.txt").read_text() == "v2\n" and restarts == ["stop", "start"]
    assert s.state["status"] == "up to date"


def test_local_changes_block_update(repos, tmp_path, monkeypatch):
    dev, app = repos
    (dev / "app.txt").write_text("v2\n")
    git(dev, "commit", "-qam", "v2"); git(dev, "push", "-q", "origin", "HEAD:main")
    (app / "app.txt").write_text("edited on the server\n")
    s, restarts = _sup(app, tmp_path, monkeypatch)
    assert not s.try_update(s.remote_sha())
    assert s.state["status"] == "blocked" and (app / "app.txt").read_text() == "edited on the server\n"
    assert restarts == []


def test_failed_selftest_rolls_back(repos, tmp_path, monkeypatch):
    dev, app = repos
    (dev / "tests").mkdir()
    (dev / "tests" / "test_physics.py").write_text("def test_broken():\n    assert False\n")
    git(dev, "add", "-A"); git(dev, "commit", "-qm", "broken"); git(dev, "push", "-q", "origin", "HEAD:main")
    before = git(app, "rev-parse", "HEAD")
    s, restarts = _sup(app, tmp_path, monkeypatch, smoke=True)
    target = s.remote_sha()
    assert not s.try_update(target)
    assert git(app, "rev-parse", "HEAD") == before and target in s.bad and restarts == []
    assert "rolled back" in s.state["message"]
