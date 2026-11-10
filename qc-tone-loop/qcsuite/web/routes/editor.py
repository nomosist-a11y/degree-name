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
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from qcsuite.features import editor, photos

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
    try:
        return await run_in_threadpool(state.editor.move, state.device(),
                                      str(body.get("gesture", "")), body)
    except editor.EditorConflict as exc:
        return _conflict(exc)


@router.post("/end")
async def end(request: Request):
    body = await _body(request)
    state = _state(request)
    try:
        result = await run_in_threadpool(state.editor.end, state.device(),
                                        str(body.get("gesture", "")), body)
    except editor.EditorConflict as exc:
        return _conflict(exc)
    state.checkpoint()
    return result


@router.get("/catalog")
def catalog(request: Request):
    return editor.catalog_listing(_state(request).device())


@router.post("/replace")
async def replace(request: Request):
    body = await _body(request)
    state = _state(request)
    try:
        result = await run_in_threadpool(state.editor.replace, state.device(), body)
    except editor.EditorConflict as exc:
        return _conflict(exc)
    state.checkpoint()
    return result


# --- grid layout: move / delete / paste / duplicate / reset / port / split ---

@router.post("/layout/{action}")
async def layout(action: str, request: Request):
    body = await _body(request)
    state = _state(request)
    try:
        result = await run_in_threadpool(state.editor.layout, state.device(), action, body)
    except editor.EditorConflict as exc:
        return _conflict(exc)
    state.checkpoint()
    return result


@router.get("/block")
def block(request: Request, row: int, col: int):
    return editor.block_copy(_state(request).device(), row, col)


@router.post("/scene")
async def scene(request: Request):
    body = await _body(request)
    state = _state(request)
    return await run_in_threadpool(state.editor.scene, state.device(), body)


# --- photos of the real gear (display only; nothing here touches the unit) ---

@router.get("/photo")
async def photo(name: str, based_on: str = "", plugin: bool = False, fetch: bool = True):
    """Origin and photo for one QC device. ``fetch=0`` answers from the cache
    only (hover previews), so a sweep of the mouse never searches Commons."""
    return await run_in_threadpool(photos.lookup, name, based_on, plugin, fetch)


@router.get("/photo/file/{kind}/{filename}")
def photo_file(kind: str, filename: str):
    return FileResponse(photos.photo_file(kind, filename),
                        headers={"Cache-Control": "private, max-age=3600"})


@router.post("/photo")
async def photo_upload(request: Request):
    """Register the player's own picture: ``{"name": QC name, "data": data URL}``."""
    try:
        size = int(request.headers.get("content-length") or 0)
    except ValueError:
        size = 0
    if size > photos.MAX_UPLOAD * 4 // 3 + 4096:
        raise photos.PhotoError("画像が大きすぎます(12 MB まで)。")
    body = await _body(request)
    path = await run_in_threadpool(photos.save_user_photo, str(body.get("name", "")),
                                   body.get("data"))
    return {"file": path.name, "url": photos.photo_url("user", path.name)}
