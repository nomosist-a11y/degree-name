"""Web routes for the edit watcher (idea D) and linked takes (idea H).

One background watcher per server, kept on the shared state. Saving a session
into the sandbox has its own execute endpoint because it runs the plan with the
watcher's rebuild hook (recall the source preset, replay the changes) - the
generic ``/api/plans/{id}/execute`` would rebuild from JSON instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from qcsuite import jobs
from qcsuite import plan as plan_mod
from qcsuite.errors import QcsuiteError
from qcsuite.features import watch
from qcsuite.web.app import CONFIRM_WORD

router = APIRouter(prefix="/api/watch")


def _state(request: Request):
    return request.app.state.qc


def _bg(state, create: bool = False, interval: float = watch.DEFAULT_INTERVAL):
    bg = getattr(state, "watch_bg", None)
    if bg is None and create:
        bg = watch.BackgroundWatch(state, interval)
        state.watch_bg = bg
    return bg


@router.get("/status")
def status(request: Request, tail: int = 40):
    state = _state(request)
    bg = _bg(state)
    if bg is None:
        return {"running": False, "session": None, "entries": [], "pending": []}
    out = bg.status()
    if bg.watcher and bg.watcher.session:
        out["entries"] = bg.watcher.session.entries()[-tail:]
    return out


@router.post("/start")
async def start(request: Request):
    body = await request.json() if await request.body() else {}
    state = _state(request)
    bg = _bg(state)
    if bg is not None and bg.running:
        raise QcsuiteError("記録はすでに動いています。")
    interval = float(body.get("interval", watch.DEFAULT_INTERVAL))
    bg = watch.BackgroundWatch(state, interval)
    state.watch_bg = bg
    return bg.start()


@router.post("/stop")
def stop(request: Request):
    bg = _bg(_state(request))
    if bg is None or bg.watcher is None:
        raise QcsuiteError("記録は動いていません。")
    return bg.stop()


@router.post("/poll")
def poll(request: Request):
    """Read once now (the background thread does this on its own interval)."""
    bg = _bg(_state(request))
    if bg is None or not bg.running:
        raise QcsuiteError("記録は動いていません。")
    return {"entries": bg.tick(), **bg.status()}


@router.post("/note")
async def note(request: Request):
    body = await request.json()
    state = _state(request)
    bg = _bg(state)
    text = body.get("text", "").strip()
    if not text:
        raise QcsuiteError("メモが空です。")
    if bg is not None and bg.running:
        return bg.note(text)
    return watch.add_note(body.get("session") or "latest", text, state.mock)


@router.post("/take")
async def take(request: Request):
    body = await request.json()
    state = _state(request)
    bg = _bg(state)
    name = body.get("name", "").strip()
    if not name:
        raise QcsuiteError("録音の名前を入れてください。")
    session = None
    if not (bg and bg.running) and body.get("session"):
        session = watch.open_session(body["session"], state.mock)
    try:
        return watch.take(state.rig(), name, float(body.get("seconds", 20)),
                          watcher=bg.watcher if bg and bg.running else None,
                          session=session, note=body.get("note", ""))
    except RuntimeError as exc:
        raise QcsuiteError(str(exc)) from exc


@router.get("/takes")
def take_list(request: Request):
    return watch.takes(_state(request).mock)


@router.get("/sessions")
def sessions(request: Request):
    return [s.summary() for s in watch.sessions(_state(request).mock)]


@router.get("/sessions/{session_id}")
def session_detail(request: Request, session_id: str):
    return watch.show(watch.open_session(session_id, _state(request).mock))


@router.get("/sessions/{session_id}/markdown", response_class=PlainTextResponse)
def session_markdown(request: Request, session_id: str):
    return watch.markdown(watch.open_session(session_id, _state(request).mock))


@router.get("/sessions/{session_id}/patch")
def session_patch(request: Request, session_id: str, segment: int | None = None):
    return watch.replay_for(watch.open_session(session_id, _state(request).mock), segment)


@router.post("/sessions/{session_id}/apply")
async def session_apply(request: Request, session_id: str):
    body = await request.json() if await request.body() else {}
    state = _state(request)
    s = watch.open_session(session_id, state.mock)
    return watch.apply_replay(state.device(), s, body.get("segment"))


@router.post("/sessions/{session_id}/to-sandbox")
async def session_to_sandbox(request: Request, session_id: str):
    body = await request.json() if await request.body() else {}
    state = _state(request)
    s = watch.open_session(session_id, state.mock)
    source = watch.parse_source(body["source"]) if body.get("source") else None
    plan = watch.to_sandbox_plan(state.device(), s, segment=body.get("segment"), source=source,
                                 slot=body.get("slot") or None, name=body.get("name") or None)
    return state.plan_response(plan)


@router.post("/plans/{plan_id}/execute")
async def plan_execute(request: Request, plan_id: str):
    body = await request.json()
    state = _state(request)
    if body.get("confirm", "") != CONFIRM_WORD:
        raise QcsuiteError(f"確認のため「{CONFIRM_WORD}」と入力してください。")
    plan = plan_mod.load_plan(plan_id, state.mock)
    job = jobs.create("plan", {"plan_id": plan.id}, [{"key": plan.id}], state.mock,
                      title=plan.title)

    def target(job, cancel):
        def work(item):
            def progress(i, n, text):
                job.set_state(current=f"{i}/{n} {text}")
            result = watch.execute_plan(plan, state.device(), confirm=True, on_progress=progress)
            state.checkpoint()
            return {"results": result["results"], "backups": result["backups"],
                    "dir": result["dir"]}
        jobs.run(job, work, cancel=cancel, max_consecutive_failures=1)
    return {"job": state.jobs.start(job, target)}
