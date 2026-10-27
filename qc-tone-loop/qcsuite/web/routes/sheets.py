"""Web routes for preset sheets (feature 7), scene maps (C) and share packs (N).

Files are written under the qcsuite home and opened through ``/api/files``.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Request

from qcsuite import paths
from qcsuite.features import sheets
from qcsuite.features import sheets_sources as src

router = APIRouter(prefix="/api/sheets")


def _state(request: Request):
    return request.app.state.qc


def _items(state, body: dict) -> list[dict]:
    refs = [src.parse_ref(r) for r in body.get("refs") or []]
    items = src.collect(state.device(), mock=state.mock, refs=refs, folder=body.get("folder") or None,
                        current_grid=bool(body.get("current")),
                        from_backup=bool(body.get("from_backup")))
    if not items:
        raise FileNotFoundError("対象のプリセットがありません(バックアップから作る場合は、先にバックアップを)")
    return items


def _rel(state, path) -> str:
    return str(path.relative_to(paths.home(state.mock)))


def _stem(items):
    return sheets._stem(items) + time.strftime("-%H%M%S")


@router.get("/sources")
def sources(request: Request):
    state = _state(request)
    backup = [{"ref": src.ref_text(n.get("folder"), n.get("position")), "name": n.get("name")}
              for n in src.backup_presets(state.mock)]
    return {"backup": backup, "folders": [f["name"] for f in state.device().setlists()]}


@router.post("/recalls")
async def recalls(request: Request):
    """How many presets the selection would recall on the unit (0 = no sound change)."""
    body = await request.json()
    state = _state(request)
    if body.get("from_backup"):
        return {"recalls": 0}
    refs = [src.parse_ref(r) for r in body.get("refs") or []]
    if body.get("folder"):
        refs += src.folder_refs(state.device(), body["folder"])
    return {"recalls": src.needs_recall(refs, state.mock)}


@router.post("/html")
async def html(request: Request):
    body = await request.json()
    state = _state(request)
    items = _items(state, body)
    path = paths.sub("sheets", state.mock) / f"{_stem(items)}.html"
    path.write_text(sheets.sheet_html(items, body.get("title") or "プリセット資料"))
    return {"file": _rel(state, path), "presets": len(items)}


@router.post("/pdf")
async def pdf(request: Request):
    body = await request.json()
    state = _state(request)
    items = _items(state, body)
    info = sheets.sheet_pdf(items, paths.sub("sheets", state.mock) / f"{_stem(items)}.pdf",
                            body.get("title") or "プリセット資料")
    return {**info, "file": _rel(state, Path(info["file"]))}


@router.post("/pack")
async def pack(request: Request):
    body = await request.json()
    state = _state(request)
    items = _items(state, body)
    out_dir = paths.sub("sheets", state.mock)
    renders = []
    if body.get("render_di"):
        refs = [(n["folder"], n["position"]) for n in items
                if n.get("folder") is not None and n.get("position") is not None]
        renders = sheets.render_for_pack(state.rig(), refs, body["render_di"], state.mock,
                                         paths.ensure(out_dir / f"renders-{time.strftime('%Y%m%d-%H%M%S')}"))
    info = sheets.share_pack(items, out_dir / f"{_stem(items)}-pack.zip", pdf=bool(body.get("pdf")),
                             renders=renders, title=body.get("title") or "共有パック")
    return {**info, "file": _rel(state, Path(info["file"]))}
