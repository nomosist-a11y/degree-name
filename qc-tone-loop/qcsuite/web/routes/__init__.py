"""One module per feature, each exposing ``router`` (a FastAPI APIRouter).

Routes reach the shared state through ``request.app.state.qc``
(:class:`qcsuite.web.app.AppState`): ``.device()``, ``.rig()``,
``.plan_response(plan)``, ``.start_job(job, work)``, ``.mock``.
"""
