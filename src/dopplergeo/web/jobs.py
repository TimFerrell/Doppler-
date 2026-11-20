"""Background job queue for the web interface.

Every job is a list of ``dopplergeo`` CLI invocations run in-process, one job at a time, so the
web interface behaves exactly like the command line (same config validation, same authorization
gate, same reports). Each job lives in its own directory under the jobs dir::

    <jobs_dir>/<job_id>/job.json     state, parameters, timestamps
    <jobs_dir>/<job_id>/job.log      captured log + stdout
    <jobs_dir>/<job_id>/config.yaml  the run config handed to the CLI
    <jobs_dir>/<job_id>/inputs/...   uploads
    <jobs_dir>/<job_id>/out/...      reports, CSVs, plots
"""
from __future__ import annotations

import contextlib
import json
import logging
import queue
import secrets
import threading
import time
import traceback
from pathlib import Path

from .. import cli

STATES = ("queued", "running", "done", "failed")


class JobManager:
    def __init__(self, jobs_dir: str | Path):
        self.root = Path(jobs_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._q: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        # jobs interrupted by a server restart can never finish: mark them failed
        for j in self.list():
            if j["state"] in ("queued", "running"):
                self._update(j["id"], state="failed", error="interrupted by a server restart")
        self._worker = threading.Thread(target=self._run_forever, name="dopplergeo-jobs", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ state
    def job_dir(self, job_id: str) -> Path:
        if not job_id.isalnum():
            raise KeyError(job_id)
        d = self.root / job_id
        if not (d / "job.json").exists():
            raise KeyError(job_id)
        return d

    def get(self, job_id: str) -> dict:
        return json.loads((self.job_dir(job_id) / "job.json").read_text())

    def list(self) -> list[dict]:
        out = []
        for p in self.root.glob("*/job.json"):
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(out, key=lambda j: j["created"], reverse=True)

    def _update(self, job_id: str, **kw) -> dict:
        with self._lock:
            p = self.root / job_id / "job.json"
            j = json.loads(p.read_text())
            j.update(kw)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(j, indent=1))
            tmp.replace(p)
            return j

    def new_job(self, kind: str, title: str, params: dict) -> tuple[str, Path]:
        job_id = time.strftime("%Y%m%d%H%M%S") + secrets.token_hex(3)
        d = self.root / job_id
        (d / "inputs").mkdir(parents=True)
        (d / "out").mkdir()
        j = {"id": job_id, "kind": kind, "title": title, "params": params, "state": "new",
             "created": time.time(), "started": None, "finished": None, "error": None, "steps": []}
        (d / "job.json").write_text(json.dumps(j, indent=1))
        return job_id, d

    def submit(self, job_id: str, steps: list[dict]) -> None:
        """``steps``: [{"argv": [...], "warn": {exit_code: message}}], run in order.

        The job fails at the first non-zero exit code, except codes listed in ``warn``, which are
        recorded as warnings (e.g. synth-demo exits 1 when a seed misses the 150 m target).
        """
        self._update(job_id, state="queued", steps=steps)
        self._q.put(job_id)

    # ------------------------------------------------------------------ worker
    def _run_forever(self) -> None:
        while True:
            job_id = self._q.get()
            try:
                self._run(job_id)
            except Exception:  # noqa: BLE001 - never let the worker die
                logging.getLogger(__name__).exception("job %s crashed", job_id)

    def _run(self, job_id: str) -> None:
        d = self.root / job_id
        j = self._update(job_id, state="running", started=time.time())
        log_path = d / "job.log"
        handler = logging.FileHandler(log_path)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.setLevel(logging.INFO)
        root = logging.getLogger()
        root.addHandler(handler)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        error = None
        warnings: list[str] = []
        try:
            with open(log_path, "a", buffering=1) as fh, contextlib.redirect_stdout(fh):
                for step in j["steps"]:
                    argv = step["argv"]
                    print(f"$ dopplergeo {' '.join(argv)}", flush=True)
                    try:
                        rc = cli.main(argv)
                    except SystemExit as e:  # authorization refusals and gating use sys.exit(msg)
                        if e.code not in (0, None):
                            print(str(e.code), flush=True)
                        rc = 0 if e.code in (0, None) else 1
                    warn = (step.get("warn") or {}).get(str(rc))
                    if rc != 0 and warn:
                        warnings.append(warn)
                        print(f"warning: {warn}", flush=True)
                    elif rc != 0:
                        error = f"step failed (exit {rc}): dopplergeo {' '.join(argv)}"
                        break
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            with open(log_path, "a") as fh:
                fh.write(traceback.format_exc())
        finally:
            root.removeHandler(handler)
            handler.close()
        self._update(job_id, state="failed" if error else "done", error=error, warnings=warnings,
                     finished=time.time())
