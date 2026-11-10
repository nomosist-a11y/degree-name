"""Web routes for the editor's extra settings (qcsuite/features/editor_extras.py).

``POST /api/editor/extra/<action>`` with a JSON body. Answers
``{"id", "warnings", "note", ...read-back}``; 409 ``{"error", "conflict": true}``
when the unit no longer showed what the page showed (nothing written); 400
``{"error"}`` for refusals. The work runs in the thread pool, one edit at a time
with the editor's other edits (the EditorSession's lock).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from qcsuite.features import editor, editor_extras

router = APIRouter(prefix="/api/editor")


def _state(request: Request):
    state = request.app.state.qc
    if getattr(state, "editor", None) is None:
        state.editor = editor.EditorSession()
    return state


def _write(session: editor.EditorSession, device, action: str, body: dict) -> dict:
    with session._edit:
        session._wait_idle(device)
        return editor_extras.write(device, action, body)


@router.post("/extra/{action}")
async def extra(action: str, request: Request):
    if action not in editor_extras.ACTIONS:
        raise editor.EditorError(f"知らない操作です: {action}")
    body = await request.json()
    if not isinstance(body, dict):
        raise editor.EditorError("本文は JSON のオブジェクトで送ってください。")
    state = _state(request)
    try:
        result = await run_in_threadpool(_write, state.editor, state.device(), action, body)
    except editor.EditorConflict as exc:
        return JSONResponse(status_code=409, content={"error": str(exc), "conflict": True})
    state.checkpoint()
    return result
