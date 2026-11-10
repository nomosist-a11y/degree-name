"""Web routes for 言葉で操作するパネル (qcsuite/features/panel.py).

Preview never touches the unit; apply re-validates the previewed patch against
the live grid before sending it. Saving goes through a plan the page shows
with ``showPlan``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from qcsuite.features import panel

router = APIRouter(prefix="/api/panel")


def _state(request: Request):
    return request.app.state.qc


@router.get("/grid")
def grid(request: Request):
    return panel.grid_view(_state(request).device())


@router.get("/engines")
def engines():
    return panel.engines()


@router.post("/preview")
async def preview(request: Request):
    body = await request.json()
    return panel.preview(_state(request).device(), body.get("instruction", ""),
                         engine=body.get("engine", "rules"))


@router.post("/apply")
async def apply(request: Request):
    body = await request.json()
    result = panel.apply(_state(request).device(), body.get("patch") or {},
                         instruction=body.get("instruction", ""),
                         engine=body.get("engine", "rules"), summary=body.get("summary"))
    _state(request).checkpoint()
    return result


@router.post("/undo")
def undo(request: Request):
    result = panel.undo(_state(request).device())
    _state(request).checkpoint()
    return result


@router.post("/redo")
def redo(request: Request):
    result = panel.redo(_state(request).device())
    _state(request).checkpoint()
    return result


@router.get("/history")
def history(request: Request, limit: int = 30):
    return panel.history_view(_state(request).mock, limit)


@router.get("/ab")
def ab(request: Request):
    return panel.ab_summary(_state(request).mock)


@router.post("/ab/capture")
async def ab_capture(request: Request):
    body = await request.json()
    return panel.ab_capture(_state(request).device(), body.get("slot", "A"), body.get("label", ""))


@router.post("/ab/last")
def ab_last(request: Request):
    return panel.ab_from_last(_state(request).device())


@router.post("/ab/switch")
async def ab_switch(request: Request):
    body = await request.json()
    result = panel.ab_switch(_state(request).device(), body.get("slot", "A"))
    _state(request).checkpoint()
    return result


@router.post("/save")
async def save(request: Request):
    body = await request.json()
    state = _state(request)
    plan = panel.save_plan(state.device(), body.get("name", ""), body.get("folder") or None)
    return state.plan_response(plan)
