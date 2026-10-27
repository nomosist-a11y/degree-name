"""Web routes for sweeps (feature 6) and the sensitivity map (idea M)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request

from qcsuite import jobs
from qcsuite.errors import QcsuiteError
from qcsuite.features import sweep

router = APIRouter(prefix="/api/sweep")


def _state(request: Request):
    return request.app.state.qc


def _start(state, job, base_grid=None) -> str:
    rig = state.rig()

    def target(job, cancel):
        try:
            sweep.run(rig, job, base_grid=base_grid, cancel=cancel)
        finally:
            state.checkpoint()
    return state.jobs.start(job, target)


@router.post("/preview")
async def preview(request: Request):
    body = await request.json()
    state = _state(request)
    spec = sweep.load_spec(body.get("spec", body))
    rig = state.rig()
    needs_base = any(not c.get("models") for c in spec.get("cells", []))
    base = sweep.establish_base(rig, spec) if needs_base else None
    expanded = sweep.expand(spec, rig.device.catalog, base)
    data, sr = sweep.load_di(spec, rig.mock)
    count = len(expanded["configs"])
    return {"count": count, "estimate_s": sweep.estimate_seconds(count, len(data) / sr, rig.mock),
            "large": count > sweep.LARGE_SWEEP, "notes": expanded["notes"],
            "configs": expanded["configs"][:100]}


@router.post("/run")
async def run(request: Request):
    body = await request.json()
    state = _state(request)
    if state.jobs.busy():
        raise QcsuiteError(f"ジョブ {state.jobs.busy()} が実行中です。")
    spec = sweep.load_spec(body.get("spec", body))
    job, info = sweep.create(state.rig(), spec, allow_large=bool(body.get("confirm_large")))
    base = json.loads((job.dir / "base_grid.json").read_text())
    return {**info, "job": _start(state, job, base)}


@router.post("/{job_id}/resume")
def resume(request: Request, job_id: str):
    state = _state(request)
    return {"job": _start(state, jobs.open_job(job_id, state.mock))}


@router.get("/jobs")
def job_list(request: Request):
    return [j for j in jobs.listing(_state(request).mock) if j.get("kind") == "sweep"]


@router.get("/{job_id}/results")
def results(request: Request, job_id: str, sort: str = "key", include_excluded: bool = True):
    return sweep.results(jobs.open_job(job_id, _state(request).mock), sort, include_excluded)


@router.post("/{job_id}/tournament")
async def to_tournament(request: Request, job_id: str):
    body = await request.json()
    state = _state(request)
    return sweep.to_tournament(jobs.open_job(job_id, state.mock), state.mock,
                               body.get("top"), body.get("keys") or None)


@router.post("/sensitivity")
async def sensitivity_run(request: Request):
    body = await request.json()
    state = _state(request)
    if state.jobs.busy():
        raise QcsuiteError(f"ジョブ {state.jobs.busy()} が実行中です。")
    spec = sweep.sensitivity_spec(int(body["row"]), int(body["col"]), body["param"],
                                  int(body.get("steps", 7)), body.get("base", "current"),
                                  body.get("di") or None, body.get("model") or None,
                                  body.get("seconds"))
    job, info = sweep.create(state.rig(), spec)
    base = json.loads((job.dir / "base_grid.json").read_text())
    return {**info, "job": _start(state, job, base)}


@router.get("/{job_id}/sensitivity")
def sensitivity(request: Request, job_id: str):
    return sweep.sensitivity(jobs.open_job(job_id, _state(request).mock))
