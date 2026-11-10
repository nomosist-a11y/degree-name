"""Web routes for the health check (idea A) and the pre-gig check (idea B)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from qcsuite import jobs
from qcsuite.errors import QcsuiteError
from qcsuite.features import health
from qcsuite.features import sheets_sources as src

router = APIRouter(prefix="/api/health")


def _state(request: Request):
    return request.app.state.qc


@router.post("/scan")
async def scan(request: Request):
    """``source``: backup (no recall), current (no side effects), refs, or folder
    (a background job; presets not in the backup are recalled one by one)."""
    body = await request.json()
    state = _state(request)
    source = body.get("source", "backup")
    device = state.device()
    if source == "folder":
        job = health.create_folder_job(device, body["folder"], state.mock)

        def work(item):
            return src.read(state.device(), item["folder"], item["position"], mock=state.mock)
        return {"job": state.start_job(job, work)}
    if source == "backup":
        items = src.collect(device, mock=state.mock, from_backup=True, folder=body.get("folder") or None)
        if not items:
            raise QcsuiteError("バックアップがありません。先に「バックアップ」でバックアップを取ってください。")
        label = "バックアップ"
    elif source == "current":
        items, label = [src.current(device)], "現在のグリッド"
    else:
        refs = [src.parse_ref(r) for r in body.get("refs") or []]
        items, label = src.collect(device, mock=state.mock, refs=refs), "指定のプリセット"
    report = health.scan(items, label)
    health.save_report(report, state.mock)
    return {"report": report, "markdown": health.report_markdown(report)}


@router.get("/job/{job_id}")
def job_report(request: Request, job_id: str):
    state = _state(request)
    report = health.finish_folder_job(jobs.open_job(job_id, state.mock), state.mock)
    return {"report": report, "markdown": health.report_markdown(report)}


@router.get("/last")
def last(request: Request):
    """The previous report, or ``{"report": null}`` before the first scan."""
    try:
        report = health.last_report(_state(request).mock)
    except FileNotFoundError:
        return {"report": None, "markdown": ""}
    return {"report": report, "markdown": health.report_markdown(report)}


@router.get("/last.md")
def last_md(request: Request):
    return PlainTextResponse(health.report_markdown(health.last_report(_state(request).mock)),
                             media_type="text/markdown; charset=utf-8")


@router.get("/gig/{name}")
def gig(request: Request, name: str, no_read: bool = True):
    state = _state(request)
    g = health.gig_check(state.device(), name, mock=state.mock, read_missing=not no_read)
    return {**g, "markdown": health.gig_markdown(g)}
