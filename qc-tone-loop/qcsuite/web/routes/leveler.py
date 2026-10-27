"""Web routes for the loudness leveler (see :mod:`qcsuite.features.leveler`).

Leveler plans run through this module's own executor
(:func:`qcsuite.features.leveler.execute`), because their copy steps may carry
Gain-block or amp-knob adjustments the shared plan runner does not apply.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request

from qcsuite import jobs
from qcsuite import plan as plan_mod
from qcsuite.errors import QcsuiteError
from qcsuite.features import leveler

router = APIRouter(prefix="/api/leveler")


def _state(request: Request):
    return request.app.state.qc


def _target(body):
    value = body.get("target")
    return None if value in (None, "") else float(value)


def _proposal(state, body) -> tuple[dict, str]:
    job_id = body.get("job") or leveler.latest_job(state.mock)
    if job_id is None:
        raise FileNotFoundError("測定結果がありません。先に測定してください。")
    proposal = leveler.propose(leveler.results(job_id, state.mock), _target(body),
                               body.get("method") or "lane")
    return proposal, job_id


@router.get("/jobs")
def measure_jobs(request: Request):
    """Measuring and verifying jobs, newest first."""
    mock = _state(request).mock
    out = []
    for kind in (leveler.MEASURE_KIND, leveler.VERIFY_KIND):
        for job in leveler.jobs_of(kind, mock):
            out.append({**job.summary(), "spec": job.spec})
    return sorted(out, key=lambda j: j["id"], reverse=True)


@router.post("/measure")
async def measure(request: Request):
    """Start a measuring job: ``{presets: [{folder, position}], folders, di, scenes}``."""
    state = _state(request)
    body = await request.json()
    rig = state.rig()
    job = leveler.start_measure(rig.device, presets=body.get("presets", []),
                                folders=body.get("folders", []), di=body.get("di") or None,
                                scenes=body.get("scenes") or None, mock=state.mock)
    return {"job": state.start_job(job, leveler.measure_work(rig, job))}


@router.get("/results/{job_id}")
def measure_results(job_id: str, request: Request):
    mock = _state(request).mock
    return {"job": jobs.open_job(job_id, mock).summary(), "results": leveler.results(job_id, mock)}


@router.post("/propose")
async def propose(request: Request):
    proposal, job_id = _proposal(_state(request), await request.json())
    return {"job": job_id, **proposal}


@router.post("/plan")
async def plan(request: Request):
    """The copy-with-correction plan. The page shows it and asks before running."""
    state = _state(request)
    body = await request.json()
    proposal, job_id = _proposal(state, body)
    device = state.device()
    di = jobs.open_job(job_id, state.mock).spec.get("di")
    p = leveler.build_plan(device, proposal, to=body.get("to") or None, di=di,
                           measure_job=job_id)
    return {**state.plan_response(p), "execute_url": f"/api/leveler/execute/{p.id}"}


@router.post("/execute/{plan_id}")
async def execute(plan_id: str, request: Request):
    """Run a leveler plan (after the player typed the confirmation word)."""
    from qcsuite.web.app import CONFIRM_WORD
    state = _state(request)
    body = await request.json()
    if body.get("confirm") != CONFIRM_WORD:
        raise QcsuiteError(f"確認のため「{CONFIRM_WORD}」と入力してください。")
    p = plan_mod.load_plan(plan_id, state.mock)
    job = jobs.create("plan", {"plan_id": p.id}, [{"key": p.id}], state.mock, title=p.title)

    def work(item):
        def progress(i, n, text):
            job.set_state(current=f"{i}/{n} {text}")
        result = leveler.execute(p, state.device(), confirm=True, on_progress=progress)
        return {"results": result["results"], "backups": result["backups"], "dir": result["dir"]}
    return {"job": state.start_job(job, work)}


@router.post("/check-lane")
async def check_lane(request: Request):
    """Does writing the row output VOLUME change the sound? (runs as a job)"""
    state = _state(request)
    body = await request.json()
    rig = state.rig()
    folder, position = body.get("folder") or None, body.get("position")
    job = jobs.create("level-check-lane", {"di": body.get("di"), "folder": folder,
                                           "position": position},
                      [{"key": "check"}], state.mock, title="出力レベルが効くかの確認")

    def work(item):
        return leveler.check_lane_volume_effect(
            rig, body.get("di") or leveler.di_mod.SYNTH_NAME, folder,
            None if position in (None, "") else int(position),
            float(body.get("delta_db", -6.0)))
    return {"job": state.start_job(job, work)}


@router.post("/verify")
async def verify(request: Request):
    state = _state(request)
    body = await request.json()
    session = leveler.load_session(body.get("session") or None, state.mock)
    rig = state.rig()
    job = leveler.start_verify(session, di=body.get("di") or None, mock=state.mock)
    return {"job": state.start_job(job, leveler.measure_work(rig, job))}


@router.get("/verify/{job_id}")
def verify_result(job_id: str, request: Request):
    return leveler.verify_summary(job_id, _state(request).mock)


@router.get("/sessions")
def sessions(request: Request):
    folder = leveler.sessions_dir(_state(request).mock)
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"), reverse=True)]
