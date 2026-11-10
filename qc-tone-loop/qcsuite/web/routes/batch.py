"""Web routes for bulk reamps / stems (feature 9) and the sound map (idea K)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from qcsuite import jobs
from qcsuite.errors import QcsuiteError
from qcsuite.features import batch

router = APIRouter(prefix="/api/batch")


def _state(request: Request):
    return request.app.state.qc


def _start(state, job) -> str:
    rig = state.rig()

    def target(job, cancel):
        try:
            batch.run(rig, job, cancel=cancel)
        finally:
            state.checkpoint()
    return state.jobs.start(job, target)


@router.post("/run")
async def run(request: Request):
    body = await request.json()
    state = _state(request)
    if state.jobs.busy():
        raise QcsuiteError(f"ジョブ {state.jobs.busy()} が実行中です。")
    job = batch.create(state.rig(), body.get("dis") or ["synthetic-riff"],
                       body.get("presets") or None, body.get("folder") or None,
                       body.get("format", "wav"))
    return {"job": _start(state, job), "items": len(job.items)}


@router.post("/{job_id}/resume")
def resume(request: Request, job_id: str):
    state = _state(request)
    return {"job": _start(state, jobs.open_job(job_id, state.mock))}


@router.get("/jobs")
def job_list(request: Request):
    return [j for j in jobs.listing(_state(request).mock) if j.get("kind") == "batch"]


@router.get("/{job_id}")
def show(request: Request, job_id: str):
    state = _state(request)
    return batch.show(jobs.open_job(job_id, state.mock), state.mock)


@router.get("/{job_id}/map")
def sound_map(request: Request, job_id: str, di: str = ""):
    return batch.sound_map(jobs.open_job(job_id, _state(request).mock), di or None)


@router.get("/{job_id}/near")
def near(request: Request, job_id: str, name: str, di: str = ""):
    return batch.nearest(jobs.open_job(job_id, _state(request).mock), name, di or None)
