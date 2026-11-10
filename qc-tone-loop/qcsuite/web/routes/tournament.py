"""Web routes for blind comparisons (see :mod:`qcsuite.features.tournament`)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from qcsuite import jobs, paths
from qcsuite.errors import QcsuiteError
from qcsuite.features import tournament

router = APIRouter(prefix="/api/tournament")


def _state(request: Request):
    return request.app.state.qc


def _options(body: dict) -> dict:
    rounds = body.get("rounds")
    return {"format": body.get("format") or "swiss",
            "rounds": None if rounds in (None, "") else int(rounds),
            "consistency_checks": int(body.get("checks") or 0)}


@router.get("")
def listing(request: Request):
    return tournament.listing(_state(request).mock)


@router.post("/files")
async def from_files(request: Request):
    """Candidates that are already rendered (sweeps, batch reamps) - inside the home only."""
    state = _state(request)
    body = await request.json()
    home = paths.home(state.mock).resolve()
    candidates = []
    for c in body.get("candidates", []):
        path = Path(c["file"])
        path = (home / path).resolve() if not path.is_absolute() else path.resolve()
        if home not in path.parents:
            raise QcsuiteError(f"qcsuite のデータフォルダ以外のファイルは使えません: {c['file']}")
        candidates.append({**c, "file": str(path)})
    target = body.get("target_lufs")
    return tournament.create(candidates, mock=state.mock, title=body.get("title") or "比較",
                             source=body.get("source") or "files",
                             target_lufs=None if target in (None, "") else float(target),
                             **_options(body))


@router.post("/presets")
async def from_presets(request: Request):
    """Render presets in the background, then start the comparison."""
    state = _state(request)
    body = await request.json()
    rig = state.rig()
    job = tournament.start_render_job(body.get("presets", []), di=body.get("di") or None,
                                      mock=state.mock, title=body.get("title") or "",
                                      options=_options(body))
    work = tournament.render_job_work(rig, job)

    def target(job, cancel):
        try:
            jobs.run(job, work, cancel=cancel)
            t = tournament.finish_render_job(job, state.mock)
            job.set_state(tournament=t["id"], message=f"比較 {t['id']} を作りました")
        finally:
            state.checkpoint()
    return {"job": state.jobs.start(job, target)}


@router.get("/by-job/{job_id}")
def by_job(job_id: str, request: Request):
    tid = jobs.open_job(job_id, _state(request).mock).state().get("tournament")
    if not tid:
        raise FileNotFoundError("このジョブからはまだ比較ができていません。")
    return tournament.view(tid, _state(request).mock)


@router.get("/{tid}")
def view(tid: str, request: Request):
    return tournament.view(tid, _state(request).mock)


@router.get("/{tid}/next")
def next_pair(tid: str, request: Request):
    return tournament.next_pair(tid, _state(request).mock)


@router.post("/{tid}/vote")
async def vote(tid: str, request: Request):
    body = await request.json()
    return tournament.vote(tid, _state(request).mock, str(body["choice"]), body.get("pair_id"))


@router.post("/{tid}/reveal")
def reveal(tid: str, request: Request):
    return tournament.reveal(tid, _state(request).mock)


@router.get("/{tid}/export")
def export(tid: str, request: Request, fmt: str = "md"):
    text = tournament.export_results(tid, _state(request).mock, fmt)
    media = "application/json" if fmt == "json" else "text/markdown"
    return PlainTextResponse(text, media_type=f"{media}; charset=utf-8")
