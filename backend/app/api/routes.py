"""All dashboard routes: full-page renders plus a handful of HTMX partial
endpoints (trade table filtering, backtest progress polling, manual refresh).
"""
from __future__ import annotations

import datetime as dt
import os
import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api import queries
from app.auth import SESSION_KEY, verify_credentials
from app.backtest.runner import run_backtest
from app.config import BACKEND_DIR, settings
from app.data.calendar import is_trading_day, now_ist, trading_days
from app.live import control as live_control
from app.live.monitor import poll_once
from app.live.scheduler import get_scheduler
from app.models.db import get_session
from app.models.schema import BacktestRun
from app.strategy.types import Direction, EntryType, TradeStatus

router = APIRouter()
auth_router = APIRouter()
templates = Jinja2Templates(directory=str(BACKEND_DIR.parent / "frontend" / "templates"))

# run_id -> {"current": int, "total": int, "day": str}  (in-memory, single-process)
_backtest_progress: dict[int, dict] = {}
_backtest_lock = threading.Lock()


@auth_router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if request.session.get(SESSION_KEY):
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "error": None})


@auth_router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    if verify_credentials(username, password):
        request.session[SESSION_KEY] = True
        request.session["username"] = username.strip()
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "error": "Incorrect username or password."},
        status_code=401,
    )


@auth_router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


def _nav_ctx(active: str) -> dict:
    return {
        "active": active,
        "now": now_ist(),
        "is_trading_day": is_trading_day(now_ist().date()),
        "settings": settings,
        "structure_intervals": live_control.VALID_INTERVALS,
    }


@router.get("/", response_class=HTMLResponse)
def overview(request: Request):
    today_trade = queries.get_today_trade()
    stats = queries.get_live_stats()
    equity_curve = queries.get_equity_curve()
    heartbeat = queries.get_heartbeat()
    recent = queries.get_live_trades(limit=8)
    live_status = live_control.get_status()
    pipeline = queries.get_pipeline_stage(today_trade)

    ctx = {
        "request": request,
        **_nav_ctx("overview"),
        "today_trade": today_trade,
        "stats": stats,
        "equity_curve": equity_curve,
        "heartbeat": heartbeat,
        "recent_trades": recent,
        "live_status": live_status,
        "pipeline": pipeline,
    }
    return templates.TemplateResponse("overview.html", ctx)


@router.post("/api/live/poll")
def trigger_poll():
    result = poll_once()
    return result


@router.post("/api/live/start")
def start_live_monitoring():
    """Arms today's session and immediately runs one poll for instant
    feedback; the scheduler's interval job takes over from here for the
    rest of the 09:15-15:30 IST session."""
    live_control.start()
    result = poll_once()
    return {"enabled": True, **result}


@router.post("/api/live/stop")
def stop_live_monitoring():
    live_control.stop()
    return {"enabled": False}


@router.post("/api/live/structure-interval")
def set_live_structure_interval(interval: str = Form(...)):
    """Changes the live monitor's structure/entry timeframe (1m/2m/3m/5m).
    Takes effect on the next poll - no restart needed."""
    live_control.set_structure_interval(interval)
    return {"structure_interval": interval}


@router.get("/trades", response_class=HTMLResponse)
def trade_history(request: Request):
    trades = queries.get_live_trades(limit=200)
    ctx = {
        "request": request,
        **_nav_ctx("trades"),
        "trades": trades,
        "directions": [d.value for d in Direction],
        "entry_types": [e.value for e in EntryType],
        "statuses": [s.value for s in TradeStatus],
        "filters": {},
    }
    return templates.TemplateResponse("trades.html", ctx)


@router.delete("/trades/{trade_id}", response_class=HTMLResponse)
def delete_trade(trade_id: int):
    queries.delete_trade(trade_id)
    return HTMLResponse("")  # htmx removes the row via hx-swap="outerHTML" on an empty response


@router.post("/trades/delete-all", response_class=HTMLResponse)
def delete_all_trades(request: Request):
    queries.delete_all_live_trades()
    ctx = {"request": request, "trades": []}
    return templates.TemplateResponse("_trades_table.html", ctx)


@router.get("/trades/table", response_class=HTMLResponse)
def trade_history_table(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    direction: str | None = None,
    entry_type: str | None = None,
    status: str | None = None,
):
    start_d = dt.date.fromisoformat(start) if start else None
    end_d = dt.date.fromisoformat(end) if end else None
    trades = queries.get_live_trades(
        start=start_d,
        end=end_d,
        direction=direction or None,
        entry_type=entry_type or None,
        status=status or None,
        limit=500,
    )
    return templates.TemplateResponse("_trades_table.html", {"request": request, "trades": trades})


@router.get("/monthly", response_class=HTMLResponse)
def monthly_performance(request: Request):
    stats = queries.get_live_stats()
    ctx = {"request": request, **_nav_ctx("monthly"), "stats": stats}
    return templates.TemplateResponse("monthly.html", ctx)


@router.get("/backtest", response_class=HTMLResponse)
def backtest_page(request: Request):
    runs = queries.get_backtest_runs()
    ctx = {
        "request": request,
        **_nav_ctx("backtest"),
        "runs": runs,
        "default_start": (now_ist().date() - dt.timedelta(days=settings.backtest_lookback_days)).isoformat(),
        "default_end": now_ist().date().isoformat(),
        "default_structure_interval": settings.structure_interval,
    }
    return templates.TemplateResponse("backtest.html", ctx)


def _progress_cb(run_id: int):
    def _cb(current: int, total: int, day: dt.date):
        with _backtest_lock:
            _backtest_progress[run_id] = {"current": current, "total": total, "day": day.isoformat()}

    return _cb


@router.post("/backtest/run", response_class=HTMLResponse)
def start_backtest(
    request: Request,
    start_date: str = Form(...),
    end_date: str = Form(...),
    structure_interval: str = Form(...),
):
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    if structure_interval not in live_control.VALID_INTERVALS:
        structure_interval = settings.structure_interval

    with get_session() as session:
        run = BacktestRun(start_date=start, end_date=end, status="RUNNING", structure_interval=structure_interval)
        session.add(run)
        session.flush()
        run_id = run.id

    def _job():
        try:
            run_backtest(
                start, end,
                structure_interval=structure_interval,
                progress_cb=_progress_cb(run_id),
                existing_run_id=run_id,
            )
        except Exception:
            pass

    thread = threading.Thread(target=_job, daemon=True)
    thread.start()

    return templates.TemplateResponse(
        "_backtest_status.html", {"request": request, "run_id": run_id, "status": "RUNNING"}
    )


@router.get("/backtest/status/{run_id}", response_class=HTMLResponse)
def backtest_status(request: Request, run_id: int):
    run = queries.get_backtest_run(run_id)
    progress = _backtest_progress.get(run_id)
    return templates.TemplateResponse(
        "_backtest_status.html",
        {"request": request, "run_id": run_id, "status": run["status"] if run else "UNKNOWN", "run": run, "progress": progress},
    )


@router.post("/backtest/{run_id}/delete")
def delete_backtest_run(run_id: int):
    queries.delete_backtest_run(run_id)
    return RedirectResponse(url="/backtest", status_code=303)


@router.get("/backtest/{run_id}", response_class=HTMLResponse)
def backtest_detail(request: Request, run_id: int):
    run = queries.get_backtest_run(run_id)
    trades = queries.get_backtest_trades(run_id)
    monthly = queries.get_backtest_monthly(run_id)
    ctx = {"request": request, **_nav_ctx("backtest"), "run": run, "trades": trades, "monthly": monthly}
    return templates.TemplateResponse("backtest_detail.html", ctx)


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    errors = queries.get_recent_errors()
    heartbeat = queries.get_heartbeat()
    scheduler = get_scheduler()
    jobs = []
    if scheduler:
        for job in scheduler.get_jobs():
            jobs.append({"id": job.id, "next_run": job.next_run_time})
    ctx = {"request": request, **_nav_ctx("logs"), "errors": errors, "heartbeat": heartbeat, "jobs": jobs}
    return templates.TemplateResponse("logs.html", ctx)


@router.get("/download/report/{filename}")
def download_report(filename: str):
    path = settings.reports_dir / os.path.basename(filename)
    return FileResponse(path, filename=os.path.basename(filename))


@router.get("/snapshots/{filename}")
def get_snapshot(filename: str):
    """Serves trade-setup chart PNGs. Routed through the authenticated
    router (rather than a public StaticFiles mount) so snapshots aren't
    reachable by anyone who guesses/finds a filename without logging in."""
    path = settings.snapshots_dir / os.path.basename(filename)
    return FileResponse(path, media_type="image/png")
