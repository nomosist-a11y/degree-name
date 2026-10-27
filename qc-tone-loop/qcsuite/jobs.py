"""Long runs that survive interruption: one folder per job, one JSONL line per item.

A sweep of 430 configurations takes a night (about 16 s each on the unit), and
a bulk reamp or a full backup can take an hour, so every long run is a job:

    ~/.qctone/jobs/<id>/
      spec.json       what was asked (kind + parameters)
      items.json      the full work list, fixed when the job starts
      results.jsonl   one line per finished item: {"key", "ok", "result" | "error", ...}
      state.json      status, counts, current item, timing - what progress bars read

Running a job again skips every key already in ``results.jsonl``, so a job
interrupted by Ctrl-C, a crash or a power cut picks up where it stopped. A
failing item is recorded with its error and the run carries on; failures are
never dropped, and a run of consecutive failures (``max_consecutive_failures``)
stops the job, because by then the unit is usually unreachable.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from pathlib import Path

from qcsuite import paths
from qcsuite.errors import DeviceUnavailable


class JobStopped(RuntimeError):
    """The job stopped before finishing (cancelled or too many failures)."""


class Job:
    def __init__(self, folder: Path):
        self.dir = folder
        self.id = folder.name

    # -- files -----------------------------------------------------------------

    @property
    def spec(self) -> dict:
        return json.loads((self.dir / "spec.json").read_text())

    @property
    def items(self) -> list:
        path = self.dir / "items.json"
        return json.loads(path.read_text()) if path.exists() else []

    def state(self) -> dict:
        path = self.dir / "state.json"
        return json.loads(path.read_text()) if path.exists() else {"status": "new"}

    def set_state(self, **fields):
        state = self.state()
        state.update(fields)
        state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(self.dir / "state.json")
        return state

    def results(self) -> list[dict]:
        path = self.dir / "results.jsonl"
        if not path.exists():
            return []
        out = []
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                # A torn last line from a crash mid-write: everything before it is
                # intact, and the item it belonged to will simply run again.
                if n != len(path.read_text().splitlines()):
                    raise
        return out

    def done_keys(self) -> set:
        return {r["key"] for r in self.results() if r.get("ok")}

    def append(self, record: dict):
        with open(self.dir / "results.jsonl", "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")

    def summary(self) -> dict:
        state = self.state()
        results = self.results()
        return {"id": self.id, "kind": self.spec.get("kind"), "title": self.spec.get("title", ""),
                **{k: state.get(k) for k in ("status", "total", "done", "failed", "current",
                                             "started", "updated", "eta_s", "message")},
                "ok": sum(1 for r in results if r.get("ok")),
                "errors": [r for r in results if not r.get("ok")][-5:]}


def _json_default(o):
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:  # pragma: no cover
        pass
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


def jobs_dir(mock: bool) -> Path:
    return paths.sub("jobs", mock)


def create(kind: str, spec: dict, items: list, mock: bool, job_id: str | None = None,
           title: str = "") -> Job:
    job_id = job_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{uuid.uuid4().hex[:4]}"
    folder = paths.child(jobs_dir(mock), job_id)
    if folder.exists():
        raise FileExistsError(f"ジョブ {job_id} はすでにあります(再開は --resume {job_id})")
    folder.mkdir(parents=True)
    job = Job(folder)
    (folder / "spec.json").write_text(json.dumps({"kind": kind, "title": title, **spec},
                                                 ensure_ascii=False, indent=2,
                                                 default=_json_default) + "\n")
    (folder / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1,
                                                  default=_json_default) + "\n")
    job.set_state(status="new", total=len(items), done=0, failed=0)
    return job


def open_job(job_id: str, mock: bool) -> Job:
    folder = paths.child(jobs_dir(mock), job_id)
    if not (folder / "spec.json").exists():
        raise FileNotFoundError(f"ジョブ {job_id} が見つかりません")
    return Job(folder)


def listing(mock: bool) -> list[dict]:
    out = []
    for folder in sorted(jobs_dir(mock).iterdir(), reverse=True):
        if (folder / "spec.json").exists():
            out.append(Job(folder).summary())
    return out


def run(job: Job, fn, *, key=lambda item: item["key"], cancel: threading.Event | None = None,
        on_progress=None, max_consecutive_failures: int = 5) -> dict:
    """Run ``fn(item)`` for every item not yet done; record each result."""
    items = job.items
    done = job.done_keys()
    failed = 0
    consecutive = 0
    times: list[float] = []
    job.set_state(status="running", total=len(items), done=len(done), failed=0,
                  started=time.strftime("%Y-%m-%d %H:%M:%S"), message="")
    try:
        for item in items:
            k = key(item)
            if k in done:
                continue
            if cancel is not None and cancel.is_set():
                job.set_state(status="cancelled", message="中断しました(再開できます)")
                raise JobStopped("cancelled")
            remaining = len(items) - len(done)
            eta = round(sum(times) / len(times) * remaining) if times else None
            job.set_state(current=str(k), eta_s=eta)
            if on_progress:
                on_progress(len(done), len(items), k)
            t0 = time.monotonic()
            try:
                result = fn(item)
            except (KeyboardInterrupt, DeviceUnavailable):
                raise
            except Exception as exc:
                failed += 1
                consecutive += 1
                job.append({"key": k, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc(limit=6),
                            "seconds": round(time.monotonic() - t0, 2)})
                job.set_state(failed=failed)
                if consecutive >= max_consecutive_failures:
                    job.set_state(status="failed",
                                  message=f"{consecutive} 件続けて失敗したため止めました: {exc}")
                    raise JobStopped(f"{consecutive} consecutive failures; last: {exc}") from exc
                continue
            consecutive = 0
            elapsed = time.monotonic() - t0
            times.append(elapsed)
            done.add(k)
            job.append({"key": k, "ok": True, "result": result, "seconds": round(elapsed, 2)})
            job.set_state(done=len(done))
    except KeyboardInterrupt:
        job.set_state(status="interrupted", message="中断しました(同じコマンドに --resume で再開)")
        raise
    except DeviceUnavailable as exc:
        job.set_state(status="failed", message=str(exc))
        raise
    final = "done" if failed == 0 else "done_with_errors"
    job.set_state(status=final, current=None, eta_s=0,
                  message="" if failed == 0 else f"{failed} 件が失敗しました(results.jsonl を参照)")
    return job.summary()


class JobRunner:
    """Background jobs for the web server: one at a time, cancellable."""

    def __init__(self):
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def busy(self) -> str | None:
        with self._lock:
            for job_id, t in self._threads.items():
                if t.is_alive():
                    return job_id
        return None

    def start(self, job: Job, target) -> str:
        """``target(job, cancel_event)`` runs in a thread; returns the job id."""
        running = self.busy()
        if running:
            raise RuntimeError(f"ジョブ {running} が実行中です。終わるか中断してから始めてください。")
        cancel = threading.Event()

        def body():
            try:
                target(job, cancel)
            except JobStopped:
                pass
            except Exception as exc:
                job.set_state(status="failed", message=f"{type(exc).__name__}: {exc}",
                              traceback=traceback.format_exc(limit=8))

        thread = threading.Thread(target=body, name=f"job-{job.id}", daemon=True)
        with self._lock:
            self._threads[job.id] = thread
            self._cancel[job.id] = cancel
        thread.start()
        return job.id

    def cancel(self, job_id: str) -> bool:
        event = self._cancel.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def wait(self, job_id: str, timeout: float | None = None):
        thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)
