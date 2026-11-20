"""Keep a running dopplergeo deployment in sync with its git branch.

``dopplergeo supervise -- serve --host 0.0.0.0 --port 8765`` runs the web app as a child process and,
every ``--interval`` seconds, compares the checkout's HEAD with the remote branch (``git ls-remote``,
which is not subject to GitHub's REST API rate limit). When the remote has moved:

1. it waits until the web app reports no queued/running jobs (``/api/status``);
2. fast-forwards the checkout (never merges, never discards local changes: a diverged or dirty
   checkout is reported and left alone);
3. reinstalls dependencies only if requirements.txt / pyproject.toml changed;
4. runs a quick offline self-test; if it fails, the checkout is reset back and that commit is skipped;
5. restarts only the web-app child process. The container keeps running, so command-line work
   started with ``docker exec`` (e.g. archive backfills) is not interrupted.

Changes to Dockerfile.local cannot be applied from inside the container; they are reported in the UI
("image rebuild recommended"). State is written to ``--state`` (shown in the web UI footer).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("dopplergeo.supervise")

SMOKE_TESTS = ["tests/test_physics.py", "tests/test_units.py", "tests/test_measure_score.py",
               "tests/test_adsb_grid_dem.py", "tests/test_sources_video.py"]


class Supervisor:
    def __init__(self, repo: Path, serve_args: list[str], interval: float = 30.0, branch: str | None = None,
                 remote: str = "origin", state_path: Path | None = None, port: int = 8765,
                 token_file: str | None = None, smoke: bool = True, auto_update: bool = True):
        self.repo = Path(repo).resolve()
        self.serve_args = serve_args
        self.interval = interval
        self.remote = remote
        self.branch = branch or self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.state_path = Path(state_path or self.repo / "data" / "out" / "updater.json")
        self.port = port
        self.token_file = token_file
        self.smoke = smoke
        self.auto_update = auto_update
        self.child: subprocess.Popen | None = None
        self.bad: set[str] = set()
        self.stopping = False
        self.child_starts: list[float] = []
        self.state = {"branch": self.branch, "status": "starting", "message": "", "local": None, "remote": None,
                      "last_check": None, "last_update": None, "rebuild_recommended": False, "history": []}

    # ------------------------------------------------------------------ git helpers
    def _auth(self) -> list[str]:
        if not self.token_file:
            return []
        tok = Path(self.token_file).read_text().strip()
        b = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
        return ["-c", f"http.extraHeader=Authorization: Basic {b}"]

    def git(self, *args, check=True, auth=False) -> str:
        cmd = ["git", "-c", "safe.directory=*", *(self._auth() if auth else []), "-C", str(self.repo), *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[-400:]}")
        return r.stdout

    def remote_sha(self) -> str:
        out = self.git("ls-remote", self.remote, f"refs/heads/{self.branch}", auth=True)
        if not out.strip():
            raise RuntimeError(f"branch {self.branch} not found on {self.remote}")
        return out.split()[0]

    # ------------------------------------------------------------------ state
    def save(self, **kw):
        self.state.update(kw)
        self.state["pid"] = os.getpid()
        self.state["updated"] = time.time()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.state_path)

    def event(self, msg: str, level=logging.INFO):
        log.log(level, msg)
        self.state["history"] = ([{"t": time.time(), "msg": msg}] + self.state.get("history", []))[:30]

    # ------------------------------------------------------------------ child
    def start_child(self):
        env = {**os.environ, "DOPPLERGEO_UPDATER_STATE": str(self.state_path)}
        self.child = subprocess.Popen([sys.executable, "-m", "dopplergeo.cli", *self.serve_args], cwd=str(self.repo),
                                      env=env)
        self.child_starts = [t for t in self.child_starts if time.time() - t < 600] + [time.time()]
        self.event(f"web app started (pid {self.child.pid}) at {self.git('rev-parse', '--short', 'HEAD').strip()}")

    def stop_child(self, timeout=30):
        if self.child and self.child.poll() is None:
            self.child.send_signal(signal.SIGTERM)
            try:
                self.child.wait(timeout)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(10)

    def busy(self) -> int | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=5) as r:
                return int(json.loads(r.read()).get("busy_jobs", 0))
        except Exception:  # noqa: BLE001 - app starting up / not answering
            return None

    # ------------------------------------------------------------------ update
    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""

    def try_update(self, target: str) -> bool:
        local = self.git("rev-parse", "HEAD").strip()
        dirty = [ln for ln in self.git("status", "--porcelain", "--untracked-files=no").splitlines() if ln.strip()]
        if dirty:
            self.save(status="blocked", message=f"local changes in the checkout ({len(dirty)} file(s)); not updating")
            return False
        self.git("fetch", "--quiet", self.remote, self.branch, auth=True)
        if subprocess.run(["git", "-c", "safe.directory=*", "-C", str(self.repo), "merge-base", "--is-ancestor",
                           local, target]).returncode != 0:
            self.save(status="blocked", message="the checkout has commits that are not on GitHub (diverged); not updating")
            return False
        before = {n: self._hash(self.repo / n) for n in ("requirements.txt", "pyproject.toml", "Dockerfile.local")}
        self.save(status="updating", message=f"fast-forwarding to {target[:7]}")
        self.git("merge", "--ff-only", "--quiet", target)
        after = {n: self._hash(self.repo / n) for n in before}
        try:
            if after["requirements.txt"] != before["requirements.txt"] or after["pyproject.toml"] != before["pyproject.toml"]:
                self.event("dependencies changed: installing")
                subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                               cwd=self.repo, check=True, timeout=1800)
                subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "-e", "."],
                               cwd=self.repo, check=True, timeout=600)
            if self.smoke:
                tests = [t for t in SMOKE_TESTS if (self.repo / t).exists()]
                r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests],
                                   cwd=self.repo, capture_output=True, text=True, timeout=900)
                if r.returncode != 0:
                    raise RuntimeError("self-test failed: " + (r.stdout.strip().splitlines() or ["?"])[-1])
        except Exception as e:  # noqa: BLE001
            self.git("reset", "--keep", local)
            self.bad.add(target)
            self.event(f"update to {target[:7]} rejected and rolled back: {e}", logging.WARNING)
            self.save(status="error", message=f"update to {target[:7]} rolled back: {e}", local=local)
            return False
        if after["Dockerfile.local"] != before["Dockerfile.local"]:
            self.state["rebuild_recommended"] = True
            self.event("Dockerfile.local changed: rebuild the image when convenient", logging.WARNING)
        self.stop_child()
        self.start_child()
        self.event(f"updated {local[:7]} -> {target[:7]}")
        self.save(status="up to date", message="", local=target, last_update=time.time())
        return True

    # ------------------------------------------------------------------ main loop
    def run(self):
        def _term(signum, frame):
            self.stopping = True

        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
        self.start_child()
        self.save(status="starting", local=self.git("rev-parse", "HEAD").strip())
        next_check = time.time() + 5
        while not self.stopping:
            time.sleep(1)
            if self.child and self.child.poll() is not None:
                code = self.child.returncode
                if len([t for t in self.child_starts if time.time() - t < 600]) >= 5:
                    self.save(status="error", message=f"web app keeps exiting (code {code}); waiting 60 s")
                    time.sleep(60)
                self.event(f"web app exited with code {code}; restarting", logging.WARNING)
                self.start_child()
            if time.time() < next_check:
                continue
            next_check = time.time() + self.interval
            try:
                local = self.git("rev-parse", "HEAD").strip()
                target = self.remote_sha()
                self.save(local=local, remote=target, last_check=time.time())
                if target == local:
                    self.save(status="up to date", message="")
                    continue
                if target in self.bad:
                    self.save(status="error", message=f"skipping {target[:7]} (failed its self-test earlier)")
                    continue
                if not self.auto_update:
                    self.save(status="update available", message=f"{target[:7]} is on GitHub (auto-update off)")
                    continue
                b = self.busy()
                if b:
                    self.save(status="update pending", message=f"{target[:7]} waits for {b} running/queued job(s)")
                    continue
                self.try_update(target)
            except Exception as e:  # noqa: BLE001 - network blips must not kill the supervisor
                self.save(status="check failed", message=str(e)[-300:])
        self.save(status="stopped", message="")
        self.stop_child()


def main(args) -> int:
    repo = Path(args.repo or Path.cwd())
    serve = list(args.serve_args or [])
    if serve and serve[0] == "--":
        serve = serve[1:]
    if not serve:
        serve = ["serve", "--host", "0.0.0.0", "--port", str(args.port)]
    Supervisor(repo, serve, interval=args.interval, branch=args.branch, state_path=args.state, port=args.port,
               token_file=args.token_file, smoke=not args.no_selftest, auto_update=not args.no_auto_update).run()
    return 0
