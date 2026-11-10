"""Web routes for preset management (feature 1, ideas J, O, P, R)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from qcsuite.device import guard
from qcsuite.features import backup, library

router = APIRouter(prefix="/api/library")


def _state(request: Request):
    return request.app.state.qc


@router.get("/index")
def index(request: Request, q: str = "", folder: str = "", sort: str = "folder",
          include_factory: bool = False):
    rows = library.index(_state(request).device(), include_factory)
    return library.search(rows, q, folder or None, sort)


@router.get("/dupes")
def dupes(request: Request):
    state = _state(request)
    names = library.name_dupes(library.index(state.device(), include_factory=True))
    contents = library.content_dupes(state.mock)
    return {"names": names, "contents": contents,
            "markdown": library.cleanup_markdown(names, contents)}


@router.post("/copy-plan")
async def copy_plan(request: Request):
    body = await request.json()
    state = _state(request)
    plan = library.copy_plan(state.device(), body.get("items", []), body.get("to") or None,
                             body.get("names") or None)
    return state.plan_response(plan)


@router.get("/show")
def show(request: Request, folder: str = "", position: int = 0, mode: str = "backup"):
    """``backup`` or ``current``; loading onto the unit is the POST below."""
    if mode == "load":
        mode = "backup"
    device = _state(request).device()
    if mode == "backup" and (guard.folder_key(folder), position) not in \
            backup.latest_by_key(device.mock):
        return {"missing": True,
                "message": "このプリセットはバックアップにありません。「本体に読み込んで表示」"
                           "(音が切り替わります)か、バックアップを実行してください。"}
    view = library.show(device, folder, position, mode)
    return {**view, "text": library.show_text(view)}


@router.post("/load")
async def load(request: Request):
    body = await request.json()
    view = library.show(_state(request).device(), body["folder"], int(body["position"]), "load")
    return {**view, "text": library.show_text(view)}


@router.get("/captures")
def captures(request: Request, q: str = "", limit: int = 100):
    return library.search_captures(_state(request).device(), q, limit)


@router.get("/irs")
def irs(request: Request, q: str = "", folder: str = ""):
    return library.search_irs(_state(request).device(), q, folder or None)


@router.get("/catalog")
def catalog(request: Request, q: str = "", category: str = ""):
    return library.search_catalog(_state(request).device(), q, category)


@router.post("/candidates")
async def candidates(request: Request):
    body = await request.json()
    fmt = body.get("format", "md")
    text = library.candidates(body.get("items", []), fmt)
    media = "text/csv" if fmt == "csv" else "text/markdown"
    return PlainTextResponse(text, media_type=f"{media}; charset=utf-8")


@router.get("/usage")
def usage(request: Request):
    return library.usage(_state(request).device())


@router.get("/favorites")
def favorites(request: Request, folder: str = library.FAVORITE_FOLDER):
    c = library.favorites_compare(_state(request).device(), folder)
    return {**c, "markdown": library.favorites_markdown(c)}
