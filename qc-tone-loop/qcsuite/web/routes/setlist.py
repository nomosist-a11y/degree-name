"""Web routes for setlists and the MIDI table (feature 8)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from qcsuite.features import setlist as sl
from qcsuite.features import sheets_sources as src

router = APIRouter(prefix="/api/setlist")


def _state(request: Request):
    return request.app.state.qc


@router.get("/list")
def setlists(request: Request):
    return sl.listing(_state(request).mock)


@router.get("/midi-config")
def midi_config(request: Request):
    return {"mapping": sl.midi_config(_state(request).mock), "notes": sl.MIDI_NOTES}


@router.post("/midi-config")
async def set_midi_config(request: Request):
    body = await request.json()
    state = _state(request)
    mapping = {k: v for k, v in (body.get("mapping") or {}).items() if v not in (None, "")}
    return {"mapping": sl.set_midi_config(mapping, state.mock), "notes": sl.MIDI_NOTES}


@router.post("/build")
async def build(request: Request):
    body = await request.json()
    state = _state(request)
    setlist = sl.build(body["name"], body["text"], state.device(), mock=state.mock,
                       prefer_folder=body.get("prefer_folder") or None)
    return {**setlist, "problems": sl.problems(setlist)}


@router.get("/{name}")
def show(request: Request, name: str):
    setlist = sl.load_setlist(name, _state(request).mock)
    return {**setlist, "problems": sl.problems(setlist)}


@router.post("/{name}/choose")
async def choose(request: Request, name: str):
    body = await request.json()
    state = _state(request)
    setlist = sl.load_setlist(name, state.mock)
    key, position = src.parse_ref(body["preset"])
    sl.choose(setlist, int(body["n"]), key, position, state.device())
    sl.save_setlist(setlist, state.mock)
    return {**setlist, "problems": sl.problems(setlist)}


@router.post("/{name}/plan")
async def plan(request: Request, name: str):
    body = await request.json()
    state = _state(request)
    setlist = sl.load_setlist(name, state.mock)
    p = sl.plan_placement(state.device(), setlist, body.get("folder") or None,
                          body.get("start") or "1A", bool(body.get("overwrite")),
                          body.get("naming") or "preset")
    sl.save_setlist(setlist, state.mock)
    return state.plan_response(p)


@router.post("/{name}/map")
def use_existing(request: Request, name: str):
    state = _state(request)
    setlist = sl.use_existing(sl.load_setlist(name, state.mock))
    sl.save_setlist(setlist, state.mock)
    return {**setlist, "problems": sl.problems(setlist)}


@router.post("/{name}/refresh")
def refresh(request: Request, name: str):
    state = _state(request)
    setlist = sl.refresh_targets(state.device(), sl.load_setlist(name, state.mock))
    sl.save_setlist(setlist, state.mock)
    return {**setlist, "problems": sl.problems(setlist)}


@router.get("/{name}/midi")
def midi(request: Request, name: str, format: str = "json", channel: int = 1):
    state = _state(request)
    setlist = sl.load_setlist(name, state.mock)
    mapping = sl.midi_config(state.mock)
    if format == "json":
        return {"rows": sl.midi_rows(setlist, mapping, channel), "notes": sl.MIDI_NOTES}
    media = "text/csv; charset=utf-8" if format == "csv" else "text/markdown; charset=utf-8"
    return PlainTextResponse(sl.midi_export(setlist, mapping, format, channel), media_type=media)
