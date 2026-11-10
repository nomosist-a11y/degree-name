"""Web routes for the editor's DIRECTORY (qcsuite/features/editor_directory.py).

Reads: ``GET /api/editor/directory`` (setlists with what may be done in each,
one setlist's presets), ``GET /api/editor/directory/captures`` and
``GET /api/editor/directory/midi`` - on request only. Writes are only ever
PLANNED here (``POST /api/editor/directory/plan``): the answer is a stored
dry-run plan, run later through ``POST /api/plans/{id}/execute`` with the
confirm word. Nothing in this module sends a write to the unit.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from qcsuite.features import editor_directory as directory
from qcsuite.web.app import CONFIRM_WORD

router = APIRouter(prefix="/api/editor")


@router.get("/directory")
async def listing(request: Request, folder: str = ""):
    state = request.app.state.qc
    result = await run_in_threadpool(directory.directory, state.device(), folder or None)
    result["confirm_word"] = CONFIRM_WORD
    return result


@router.post("/directory/plan")
async def plan(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise directory.DirectoryError("本文は JSON のオブジェクトで送ってください。")
    state = request.app.state.qc
    device = state.device()
    built = await run_in_threadpool(directory.build_plan, device, body)
    response = await run_in_threadpool(state.plan_response, built)
    # The page always asks for the word, even for a plan that only creates a folder.
    response["confirm_word"] = CONFIRM_WORD
    response["backup_dir"] = directory.backup_dir(built, state.mock)
    response["plan_dir"] = str(directory.plan_mod.plans_dir(state.mock) / built.id)
    return response


@router.post("/directory/finished")
async def finished(request: Request):
    """The page saw a directory plan finish: stop the undo history here."""
    body = await request.json()
    plan_id = body.get("plan") if isinstance(body, dict) else None
    if not isinstance(plan_id, str) or not plan_id:
        raise directory.DirectoryError("plan を指定してください。")
    return directory.plan_finished(request.app.state.qc.device(), plan_id)


@router.get("/directory/captures")
async def captures(request: Request):
    return await run_in_threadpool(directory.captures_listing, request.app.state.qc.device())


@router.get("/directory/midi")
async def midi(request: Request):
    return await run_in_threadpool(directory.midi_listing, request.app.state.qc.device())
