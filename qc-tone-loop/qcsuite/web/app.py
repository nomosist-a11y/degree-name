"""The local web app: ``qcsuite serve`` (add ``--mock`` to run without the unit).

FastAPI backend, plain HTML/JS frontend in ``static/`` (no build step), Japanese
UI. The server holds the device lock while it is connected, so a CLI command
run alongside it gets a clear "in use" message instead of a tangle of USB
traffic. It binds to 127.0.0.1 only: there is no login, so it is never
exposed to the network.

Every feature adds its routes from ``qcsuite/web/routes/<name>.py`` (a module
with ``router``); this file holds what they share - the rig, background jobs,
plans and their confirmation, notices, file serving and the DI library.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import threading
import traceback
import webbrowser
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from qcsuite import __version__, jobs, paths
from qcsuite import plan as plan_mod
from qcsuite.audio import di as di_mod
from qcsuite.audio.render import MONITOR_WARNING
from qcsuite.device.session import open_rig, state_path
from qcsuite.errors import DeviceBusy, QcsuiteError
from qcsuite.features import MODULES
from qctone import device as qdevice

log = logging.getLogger("qcsuite.web")
STATIC = Path(__file__).parent / "static"
CONFIRM_WORD = "実行"
HOST = "127.0.0.1"


class AppState:
    """What every route shares: one rig, one job runner, the plans in flight."""

    def __init__(self, mock: bool, allow_folders=(), fake=None):
        self.mock = mock
        self.allow = set(allow_folders)
        self.jobs = jobs.JobRunner()
        self._fake = fake
        self._rig = None
        self._stack: ExitStack | None = None
        self._lock = threading.RLock()

    # -- the rig ---------------------------------------------------------------

    def rig(self):
        with self._lock:
            if self._rig is None:
                stack = ExitStack()
                self._rig = stack.enter_context(open_rig(
                    self.mock, allow_folders=self.allow, what="qcsuite serve",
                    fake=self._fake, lock_timeout=2.0))
                self._stack = stack
            return self._rig

    def device(self):
        return self.rig().device

    @property
    def connected(self) -> bool:
        return self._rig is not None

    def close(self):
        with self._lock:
            if self._stack is not None:
                self._stack.close()
            self._rig, self._stack = None, None

    def checkpoint(self):
        """Persist the fake unit's state so a restart sees the same storage."""
        if self.mock and self._rig is not None and self._fake is None:
            self._rig.fake.save_state(state_path())

    # -- plans -------------------------------------------------------------------

    def plan_response(self, plan: plan_mod.Plan) -> dict:
        """Check a plan against the rules and store it; nothing is executed."""
        problems = plan_mod.check(plan, self.device())
        plan_mod.save_plan(plan, self.mock)
        return {**plan.to_dict(), "refused": problems,
                "confirm_word": CONFIRM_WORD if plan.destructive or plan.changes_sound else None}

    def start_plan(self, plan_id: str, confirm: str) -> str:
        plan = plan_mod.load_plan(plan_id, self.mock)
        if confirm != CONFIRM_WORD:
            raise QcsuiteError(f"確認のため「{CONFIRM_WORD}」と入力してください。")
        job = jobs.create("plan", {"plan_id": plan.id}, [{"key": plan.id}], self.mock,
                          title=plan.title)

        def target(job, cancel):
            def work(item):
                def progress(i, n, text):
                    job.set_state(current=f"{i}/{n} {text}")
                result = plan_mod.execute(plan, self.device(), confirm=True, on_progress=progress)
                self.checkpoint()
                return {"results": result["results"], "backups": result["backups"],
                        "dir": result["dir"]}
            jobs.run(job, work, cancel=cancel, max_consecutive_failures=1)
        return self.jobs.start(job, target)

    def start_job(self, job: jobs.Job, work, key=lambda item: item["key"]) -> str:
        """Run ``work(item)`` for each item of ``job`` in the background."""
        def target(job, cancel):
            try:
                jobs.run(job, work, key=key, cancel=cancel)
            finally:
                self.checkpoint()
        return self.jobs.start(job, target)


def _error(status: int, message: str):
    return JSONResponse(status_code=status, content={"error": message})


def create_app(mock: bool = False, allow_folders=(), fake=None) -> FastAPI:
    state = AppState(mock, allow_folders, fake=fake)

    @asynccontextmanager
    async def lifespan(app):
        yield
        watcher = getattr(state, "watch_bg", None)       # the edit watcher's poll thread
        if watcher is not None and watcher.running:
            watcher.stop()
        state.checkpoint()
        state.close()

    app = FastAPI(title="qcsuite", version=__version__, lifespan=lifespan)
    app.state.qc = state

    @app.exception_handler(DeviceBusy)
    async def busy_handler(request: Request, exc: DeviceBusy):
        return _error(409, str(exc))

    @app.exception_handler(QcsuiteError)
    async def qcsuite_handler(request: Request, exc: QcsuiteError):
        return _error(400, str(exc))

    @app.exception_handler(qdevice.PatchError)
    async def patch_handler(request: Request, exc: qdevice.PatchError):
        return _error(400, f"パッチを拒否しました(何も送っていません): {exc}")

    @app.exception_handler(FileNotFoundError)
    async def missing_handler(request: Request, exc: FileNotFoundError):
        return _error(404, str(exc))

    @app.exception_handler(Exception)
    async def crash_handler(request: Request, exc: Exception):
        log.error("unhandled error on %s:\n%s", request.url.path, traceback.format_exc())
        return _error(500, f"{type(exc).__name__}: {exc}")

    # -- core API ------------------------------------------------------------------

    @app.get("/api/status")
    def status(connect: bool = False):
        info = None
        if connect or state.connected:
            info = state.device().version()
        device = state.device() if state.connected else None
        return {
            "version": __version__, "mode": "mock" if mock else "hardware",
            "connected": state.connected, "device": info,
            "policy": device.policy.describe() if device else None,
            "busy_job": state.jobs.busy(), "monitor_warning": MONITOR_WARNING,
            "notices": [n.to_dict() for n in (device.notices[-30:] if device else [])],
        }

    @app.post("/api/connect")
    def connect():
        return {"device": state.device().version()}

    @app.post("/api/disconnect")
    def disconnect():
        if state.jobs.busy():
            raise QcsuiteError("ジョブの実行中は切断できません。")
        state.checkpoint()
        state.close()
        return {"connected": False}

    @app.get("/api/folders")
    def folders():
        return state.device().setlists()

    @app.get("/api/presets")
    def presets(folder: str):
        return state.device().presets(folder)

    @app.get("/api/grid")
    def grid():
        return state.device().grid()

    @app.post("/api/apply")
    async def apply(request: Request):
        body = await request.json()
        result = state.device().apply_patch(body.get("patch", body))
        return result

    @app.post("/api/policy/allow")
    async def policy_allow(request: Request):
        body = await request.json()
        device = state.device()
        name = body["folder"]
        if body.get("allowed", True):
            device.policy.allow(name)
            state.allow.add(name)
        else:
            key = qdevice_key(name)
            device.policy.allowed.discard(key)
            state.allow.discard(name)
        return device.policy.describe()

    @app.get("/api/jobs")
    def job_list():
        return jobs.listing(mock)

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str, tail: int = 50):
        job = jobs.open_job(job_id, mock)
        return {"summary": job.summary(), "spec": job.spec, "results": job.results()[-tail:]}

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: str):
        return {"cancelled": state.jobs.cancel(job_id)}

    @app.get("/api/plans/{plan_id}")
    def plan_get(plan_id: str):
        return plan_mod.load_plan(plan_id, mock).to_dict()

    @app.post("/api/plans/{plan_id}/execute")
    async def plan_execute(plan_id: str, request: Request):
        body = await request.json()
        job_id = state.start_plan(plan_id, body.get("confirm", ""))
        return {"job": job_id}

    @app.post("/api/mock/reset")
    def mock_reset():
        if not mock:
            raise QcsuiteError("モックのときだけ使えます。")
        if state.jobs.busy():
            raise QcsuiteError("ジョブの実行中は初期化できません。")
        state.close()
        if state_path().exists():
            state_path().unlink()
        return {"reset": True}

    @app.get("/api/files")
    def files(path: str):
        root = paths.home(mock).resolve()
        target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if root not in target.parents and target != root:
            raise HTTPException(403, "qcsuite のデータフォルダ以外は開けません")
        if not target.is_file():
            raise HTTPException(404, f"{path} がありません")
        return FileResponse(target)

    @app.get("/api/di")
    def di_list():
        di_mod.ensure_synthetic(mock)
        return di_mod.listing(mock)

    @app.post("/api/di/import")
    async def di_import(file: UploadFile = File(...), name: str = Form("")):
        tmp = paths.sub("tmp", mock) / Path(file.filename or "upload.wav").name
        tmp.write_bytes(await file.read())
        try:
            return di_mod.import_file(tmp, name or tmp.stem, mock)
        finally:
            tmp.unlink(missing_ok=True)

    @app.post("/api/di/record")
    async def di_record(request: Request):
        from qcsuite.features import dilib
        body = await request.json()
        return dilib.record(state.rig(), body["name"], float(body.get("seconds", 12)),
                            body.get("note", ""))

    for name in MODULES:
        if importlib.util.find_spec(f"qcsuite.web.routes.{name}") is not None:
            module = importlib.import_module(f"qcsuite.web.routes.{name}")
            app.include_router(module.router)

    @app.get("/")
    def index():
        return RedirectResponse("/static/index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def qdevice_key(name: str) -> str:
    from qcsuite.device.guard import folder_key
    return folder_key(name)


def serve(mock: bool, allow_folders=(), port: int = 8765, open_browser: bool = True):
    """Run the app on 127.0.0.1 only. There is no login, so it is never exposed
    to the network (LAN access is a future idea, not built)."""
    import uvicorn
    url = f"http://{HOST}:{port}/"
    print(f"qcsuite {'(モック)' if mock else '(実機)'}: {url}  終了は Ctrl-C")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(mock, allow_folders), host=HOST, port=port, log_level="warning")
