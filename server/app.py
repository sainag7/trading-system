"""FastAPI app — the web dashboard's backend.

A thin API over the existing Python backend. Binds to localhost only (it can place
real orders). Serves the built React app (``web/dist``) as static files at ``/`` and
the JSON API under ``/api``. No order-placement logic lives here — trading routes
drive ``Orchestrator``/``Executor`` and their guardrails/kill-switch.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from config import REPO_ROOT, load_config
from server import config_io, data, engine
from server.jobs import EngineBusy, install_stdout_router, manager

app = FastAPI(title="Trading System", docs_url="/api/docs", openapi_url="/api/openapi.json")

# Same-origin in production; allow the Vite dev server during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    install_stdout_router()


# ==========================================================================
# Read-only data
# ==========================================================================
@app.get("/api/status")
def api_status() -> dict:
    return data.status()


@app.get("/api/ideas")
def api_ideas() -> dict:
    return data.ideas()


@app.get("/api/ideas/{ticker}")
def api_idea_detail(ticker: str) -> dict:
    return data.idea_detail(ticker)


@app.get("/api/deepdive/tickers")
def api_deepdive_tickers() -> dict:
    return data.deepdive_tickers()


@app.get("/api/deepdive/{ticker}/reports")
def api_deepdive_reports(ticker: str) -> dict:
    return {"ticker": ticker.upper(), "reports": data.deepdive_reports(ticker)}


@app.get("/api/portfolio")
def api_portfolio(account: str | None = None) -> dict:
    return data.portfolio(account)


@app.get("/api/activity")
def api_activity() -> dict:
    return data.activity()


@app.get("/api/config")
def api_config_get() -> dict:
    return config_io.config_view()


# ==========================================================================
# Config editing (writes config.local.yaml)
# ==========================================================================
@app.put("/api/config")
def api_config_put(patch: dict = Body(...)) -> dict:
    result = config_io.apply_overrides(patch)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "invalid config"))
    return {"ok": True, "config": config_io.config_view()}


@app.post("/api/config/reset")
def api_config_reset() -> dict:
    config_io.reset_overrides()
    return {"ok": True, "config": config_io.config_view()}


# ==========================================================================
# Kill switch (file-based engage/release)
# ==========================================================================
def _kill_switch_path() -> Path:
    cfg = load_config()
    return REPO_ROOT / cfg.raw.get("kill_switch_file", "KILL_SWITCH")


@app.post("/api/killswitch")
def api_killswitch_engage() -> dict:
    ks = _kill_switch_path()
    ks.write_text(
        f"kill switch engaged via web app {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
    return {"engaged": True}


@app.delete("/api/killswitch")
def api_killswitch_release() -> dict:
    cfg = load_config()
    if bool(cfg.raw.get("kill_switch", False)):
        raise HTTPException(
            status_code=409,
            detail="Kill switch is pinned by `kill_switch: true` in config — release it there.")
    _kill_switch_path().unlink(missing_ok=True)
    return {"engaged": False}


# ==========================================================================
# Deep-dive remove (reversible hide) / restore
# ==========================================================================
@app.delete("/api/deepdive/{ticker}")
def api_deepdive_hide(ticker: str) -> dict:
    return {"hidden": config_io.hide_ticker(ticker)}


@app.post("/api/deepdive/{ticker}/unhide")
def api_deepdive_unhide(ticker: str) -> dict:
    return {"hidden": config_io.unhide_ticker(ticker)}


# ==========================================================================
# Engine jobs — scan (recommend) / research (explain)
# ==========================================================================
@app.post("/api/scan")
async def api_scan(body: dict = Body(default={})) -> dict:
    profile = body.get("profile")
    account = body.get("account")

    async def factory(job):
        return await engine.run_scan(profile, account)

    try:
        job = manager.start_engine("scan", f"{profile or 'default'} scan", factory)
    except EngineBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    return job.status_dict()


@app.post("/api/deepdive/research")
async def api_research(body: dict = Body(...)) -> dict:
    ticker = (body.get("ticker") or "").strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="enter a ticker symbol (e.g. NVDA)")

    async def factory(job):
        return await engine.run_research(ticker)

    try:
        job = manager.start_engine("research", f"deep research {ticker.upper()}", factory)
    except EngineBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    return job.status_dict()


# ==========================================================================
# Trading — plan → approve → execute, and one-click live
# ==========================================================================
@app.post("/api/trade/plan")
async def api_trade_plan(body: dict = Body(default={})) -> dict:
    from server import trading
    mode = (body.get("mode") or "preview").lower()
    if mode not in ("preview", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'preview' or 'live'")
    profile, account = body.get("profile"), body.get("account")

    async def factory(job):
        return await trading.build(mode, profile, account)

    try:
        job = manager.start_engine("plan", f"{mode} plan", factory)
    except EngineBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    return job.status_dict()


@app.post("/api/trade/execute")
async def api_trade_execute(body: dict = Body(...)) -> dict:
    from server import trading
    plan_id = body.get("plan_id")
    indices = body.get("approved_indices")
    if not plan_id or not isinstance(indices, list):
        raise HTTPException(status_code=400,
                            detail="plan_id and approved_indices[] are required")

    async def factory(job):
        return await trading.execute(plan_id, [int(i) for i in indices])

    try:
        job = manager.start_engine("execute", "placing approved orders", factory)
    except EngineBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    return job.status_dict()


@app.post("/api/trade/live")
async def api_trade_live(body: dict = Body(default={})) -> dict:
    from server import trading
    # Require the explicit typed confirmation — the web equivalent of the CLI's
    # "I UNDERSTAND" live gate. Without it, refuse to place unattended live orders.
    if str(body.get("confirm", "")).strip().upper() != "I UNDERSTAND":
        raise HTTPException(
            status_code=400,
            detail="live trading requires confirm='I UNDERSTAND'")
    profile, account = body.get("profile"), body.get("account")

    async def factory(job):
        return await trading.live(profile, account)

    try:
        job = manager.start_engine("live", "placing LIVE orders", factory)
    except EngineBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    return job.status_dict()


@app.post("/api/trade/discard")
def api_trade_discard(body: dict = Body(...)) -> dict:
    from server import trading
    return trading.discard(body.get("plan_id", ""))


@app.get("/api/trade/pending")
def api_trade_pending() -> dict:
    from server import trading
    return trading.pending_summary()


# ==========================================================================
# Automation — daily schedule (launchd) + diagnostics
# ==========================================================================
@app.get("/api/schedule")
def api_schedule() -> dict:
    from server import schedule
    return schedule.status()


@app.post("/api/schedule/install")
def api_schedule_install(body: dict = Body(default={})) -> dict:
    from server import schedule
    return schedule.install(enable_live=bool(body.get("enable_live", False)))


@app.delete("/api/schedule")
def api_schedule_uninstall() -> dict:
    from server import schedule
    return schedule.uninstall()


@app.get("/api/diagnostics")
def api_diagnostics_list() -> dict:
    from server import diagnostics
    return {"checks": diagnostics.names()}


@app.post("/api/diagnostics/{name}")
async def api_diagnostics_run(name: str) -> dict:
    from server import diagnostics
    if name not in {c["name"] for c in diagnostics.names()}:
        raise HTTPException(status_code=404, detail=f"unknown diagnostic {name!r}")

    async def factory(job):
        return await diagnostics.run(name)

    job = manager.start_aux("diagnostic", f"diagnostic: {name}", factory)
    return job.status_dict()


# ==========================================================================
# Job status + live log stream (SSE)
# ==========================================================================
@app.get("/api/jobs/current")
def api_job_current() -> dict:
    job = manager.engine_job()
    return job.status_dict() if job else {"status": "idle"}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    d = job.status_dict()
    d["log"] = job.log
    return d


@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(job_id: str):
    if manager.get(job_id) is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return EventSourceResponse(manager.stream(job_id))


# ==========================================================================
# Static frontend (built React app). Registered LAST so /api wins.
# ==========================================================================
_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
else:
    @app.get("/")
    def _no_build() -> dict:
        return {"detail": "Frontend not built yet. Run `npm run build` in web/ "
                "(or use start.command). API is live under /api."}
