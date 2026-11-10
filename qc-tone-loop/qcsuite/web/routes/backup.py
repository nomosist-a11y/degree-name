"""Web routes for backup, history and restore (feature 2)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from qcsuite import jobs
from qcsuite.features import backup

router = APIRouter(prefix="/api/backup")


def _state(request: Request):
    return request.app.state.qc


@router.get("/status")
def status(request: Request):
    return backup.status(_state(request).mock)


@router.post("/run")
async def run(request: Request):
    """Start (or resume) a backup job. The page has already shown the recall warning."""
    body = await request.json()
    state = _state(request)
    device = state.device()
    if body.get("resume"):
        job = jobs.open_job(body["resume"], state.mock)
    else:
        job = backup.create_job(device, body.get("folders") or None,
                                bool(body.get("include_factory")), state.mock)

    def target(job, cancel):
        try:
            backup.run_job(state.device(), job, cancel=cancel)
        finally:
            state.checkpoint()

    return {"job": state.jobs.start(job, target)}


@router.get("/log")
def log(request: Request, preset: str = "", limit: int = 50):
    mock = _state(request).mock
    return backup.preset_log(mock, preset) if preset else backup.log(mock, limit=limit)


@router.get("/diff")
def diff(request: Request, a: str = "", b: str = "", preset: str = ""):
    d = backup.diff(_state(request).mock, a or None, b or None, preset or None)
    return {**d, "text": backup.diff_report(d)}


@router.get("/presets")
def presets(request: Request):
    """The latest backup's presets (summary rows, no knob data)."""
    return [{"file": n["_file"], "folder": n["folder"], "position": n["position"],
             "slot": n["slot"], "name": n["name"], "fingerprint": n["fingerprint"],
             "blocks": len(n["blocks"])}
            for n in backup.load_latest(_state(request).mock)]


@router.get("/show")
def show(request: Request, ref: str, commit: str = ""):
    rel, n = backup.find(_state(request).mock, ref, commit or None)
    from qcsuite import presets as presets_mod
    return {"file": rel, "preset": n, "chain": presets_mod.chain_text(n)}


@router.post("/restore")
async def restore(request: Request):
    body = await request.json()
    state = _state(request)
    _, n = backup.find(state.mock, body["ref"], body.get("commit") or None)
    plan = backup.restore_plan(state.device(), n, to=body.get("to") or None,
                               slot=body.get("slot") or None, name=body.get("name") or None)
    return state.plan_response(plan)
