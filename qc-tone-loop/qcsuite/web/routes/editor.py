"""Web routes for エディター (qcsuite/features/editor.py).

The device work runs in the thread pool (not on the event loop), so a slow
write does not freeze the other requests and the server can drop a drag's
stale moves. Reads: ``GET /api/editor/grid`` (one read of the live preset). Writes go
through the feature module, which uses the Device and the panel's history; a
409 with ``conflict: true`` means the unit no longer showed what the page
showed and nothing was written.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from qcsuite.features import editor

router = APIRouter(prefix="/api/editor")


def _state(request: Request):
    state = request.app.state.qc
    if getattr(state, "editor", None) is None:
        state.editor = editor.EditorSession()
    return state


def _conflict(exc: editor.EditorConflict):
    return JSONResponse(status_code=409, content={"error": str(exc), "conflict": True})


@router.get("/grid")
def grid(request: Request):
    return editor.view(_state(request).device())


async def _body(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise editor.EditorError("本文は JSON のオブジェクトで送ってください。")
    return body


@router.post("/set")
async def set_one(request: Request):
    body = await _body(request)
    state = _state(request)
    try:
        result = await run_in_threadpool(state.editor.set, state.device(), body)
    except editor.EditorConflict as exc:
        return _conflict(exc)
    state.checkpoint()
    return result


@router.post("/begin")
async def begin(request: Request):
    body = await _body(request)
    state = _state(request)
    try:
        return await run_in_threadpool(state.editor.begin, state.device(), body)
    except editor.EditorConflict as exc:
        return _conflict(exc)


@router.post("/move")
async def move(request: Request):
    body = await _body(request)
    state = _state(request)
    return await run_in_threadpool(state.editor.move, state.device(),
                                  str(body.get("gesture", "")), body)


@router.post("/end")
async def end(request: Request):
    body = await _body(request)
    state = _state(request)
    result = await run_in_threadpool(state.editor.end, state.device(),
                                    str(body.get("gesture", "")), body)
    state.checkpoint()
    return result
