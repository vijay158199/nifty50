"""All dashboard routes: full-page renders plus a handful of HTMX partial
endpoints (trade table filtering, backtest progress polling, manual refresh).
"""
from __future__ import annotations

import datetime as dt
import os
import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.api import queries
from app.auth import SESSION_KEY, verify_credentials
from app.backtest.runner import run_backtest
from app.broker import registry as broker_registry
from app.broker import store as broker_store
from app.config import BACKEND_DIR, settings
from app.data.calendar import is_trading_day, now_ist, trading_days
from app.data.fetcher import INTERVAL_MINUTES, get_session_data
from app.live import control as live_control
from app.live.monitor import poll_once
from app.live.scheduler import get_scheduler
from app.models.db import get_session, log_event
from app.models.schema import BacktestRun
from app.reports import charts
from app.strategy.engine import run_day
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


@router.post("/api/live/skip-no-fvg")
def set_live_skip_no_fvg(skip_no_fvg_structure: bool = Form(False)):
    """Toggles whether a BOS/CHOCH with no FVG entry ends the day (default)
    or gets skipped in favor of the next one later in the session. Tested
    2026-08-26: no clear win at 1m, kept as a user choice, not the default."""
    live_control.set_skip_no_fvg_structure(skip_no_fvg_structure)
    return {"skip_no_fvg_structure": skip_no_fvg_structure}


@router.get("/api/live/chart.png")
def live_chart_png():
    """Renders today's session chart fresh on every request (same pipeline
    poll_once runs, just not persisted) - backs the overview page's "Today's
    Live Chart" <img>, which polls this on a timer. Returns 204 rather than
    a broken image when there's nothing to show yet (no data, or the fetch
    itself fails - e.g. off-hours/yfinance hiccup)."""
    trade_date = now_ist().date()
    try:
        live_interval = live_control.get_structure_interval()
        skip_no_fvg = live_control.get_skip_no_fvg_structure()
        sd_primary = get_session_data(settings.primary_symbol, trade_date, structure_interval=live_interval)
        sd_confirm = get_session_data(settings.confirm_symbol, trade_date, structure_interval=live_interval)
        if sd_primary.fine.empty:
            return Response(status_code=204)

        result = run_day(
            trade_date,
            sd_primary.candles_30m,
            sd_primary.fine,
            sd_confirm.fine,
            reduced_resolution=sd_primary.reduced_resolution,
            candle_interval_minutes=INTERVAL_MINUTES.get(sd_primary.resolution, 1),
            skip_no_fvg_structure=skip_no_fvg,
        )
        png_bytes = charts.render_live_chart(result, sd_primary.fine)
        if png_bytes is None:
            return Response(status_code=204)
        return Response(content=png_bytes, media_type="image/png")
    except Exception as exc:  # noqa: BLE001 - this backs an <img> tag, never 500 it
        log_event("WARNING", "api.live_chart", f"live_chart_png failed for {trade_date}: {exc}")
        return Response(status_code=204)


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


@router.get("/performance", response_class=HTMLResponse)
def performance_page(request: Request, baseline: str | None = None):
    """Track Record: the chosen backtest run's history (if any) followed by
    every live-monitored day since, as one continuous record - combined win
    rate, combined equity curve, and a full day-by-day trade log. Grows on
    its own as each trading day's live poll/report finalizes (no separate
    "append" step needed - it just re-queries the Trade table fresh on
    every view)."""
    done_runs = queries.get_done_backtest_runs()
    if baseline == "none":
        selected_id = None
    elif baseline and baseline.isdigit():
        selected_id = int(baseline)
    else:
        selected_id = done_runs[0]["id"] if done_runs else None

    stats = queries.get_combined_stats(selected_id)
    trades = queries.get_combined_trades(selected_id)
    equity_curve, backtest_points = queries.get_combined_equity_curve(selected_id)

    ctx = {
        "request": request,
        **_nav_ctx("performance"),
        "runs": done_runs,
        "selected_run_id": selected_id,
        "stats": stats,
        "trades": list(reversed(trades)),
        "equity_curve": equity_curve,
        "backtest_points": backtest_points,
    }
    return templates.TemplateResponse("performance.html", ctx)


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
        "default_skip_no_fvg_structure": settings.skip_no_fvg_structure,
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
    skip_no_fvg_structure: bool = Form(False),
):
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    if structure_interval not in live_control.VALID_INTERVALS:
        structure_interval = settings.structure_interval

    with get_session() as session:
        run = BacktestRun(
            start_date=start, end_date=end, status="RUNNING",
            structure_interval=structure_interval, skip_no_fvg_structure=skip_no_fvg_structure,
        )
        session.add(run)
        session.flush()
        run_id = run.id

    def _job():
        try:
            run_backtest(
                start, end,
                structure_interval=structure_interval,
                skip_no_fvg_structure=skip_no_fvg_structure,
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


@router.get("/journal", response_class=HTMLResponse)
def journal_page(request: Request):
    trades = queries.get_journal_trades()
    analytics = queries.get_journal_analytics()
    ctx = {
        "request": request,
        **_nav_ctx("journal"),
        "trades": trades,
        "analytics": analytics,
    }
    return templates.TemplateResponse("journal.html", ctx)


@router.post("/journal/{trade_id}", response_class=HTMLResponse)
def journal_update(
    request: Request,
    trade_id: int,
    notes: str = Form(""),
    rating: str = Form(""),
    tags: str = Form(""),
):
    rating_val = int(rating) if rating.strip().isdigit() and 1 <= int(rating) <= 5 else None
    clean_tags = ",".join(t.strip() for t in tags.split(",") if t.strip())
    trade = queries.update_trade_journal(trade_id, notes.strip() or None, rating_val, clean_tags or None)
    return templates.TemplateResponse("_journal_card.html", {"request": request, "t": trade})


@router.get("/broker", response_class=HTMLResponse)
def broker_page(request: Request):
    """Fully opt-in broker-account connection page - nothing here affects
    signal generation or the Yahoo Finance data feed either way. Connecting
    an account only unlocks viewing funds/holdings and submitting orders
    YOU fill in yourself below; nothing here is wired to the strategy's
    own signals."""
    accounts = broker_store.get_all_accounts()
    brokers = []
    for broker_id, adapter in broker_registry.SUPPORTED.items():
        account = accounts.get(broker_id)
        funds = holdings = None
        fetch_error = None
        if account and account["status"] == "CONNECTED":
            token = broker_store.get_access_token(broker_id)
            if token:
                try:
                    funds = adapter.get_funds(token)
                except Exception as exc:  # noqa: BLE001
                    fetch_error = str(exc)
                try:
                    holdings = adapter.get_holdings(token)
                except Exception:  # noqa: BLE001 - funds error (if any) is enough to surface
                    pass
        brokers.append({
            "id": broker_id, "label": adapter.display_name, "available": True,
            "account": account, "funds": funds, "holdings": holdings, "fetch_error": fetch_error,
        })
    for broker_id, label in broker_registry.COMING_SOON.items():
        brokers.append({"id": broker_id, "label": label, "available": False, "account": None})

    ctx = {"request": request, **_nav_ctx("broker"), "brokers": brokers}
    return templates.TemplateResponse("broker.html", ctx)


@router.post("/broker/{broker_id}/connect")
def broker_connect(
    broker_id: str,
    label: str = Form(""),
    api_key: str = Form(""),
    api_secret: str = Form(""),
    totp_secret: str = Form(""),
):
    adapter = broker_registry.get_adapter(broker_id)
    if adapter is None:
        return RedirectResponse(url="/broker", status_code=303)

    credentials = {"api_key": api_key.strip(), "api_secret": api_secret.strip(), "totp_secret": totp_secret.strip()}
    auth_method = "totp" if credentials["totp_secret"] else "api_key_secret"
    result = adapter.connect(credentials)
    if result.ok and result.access_token:
        broker_store.save_connection(broker_id, label.strip() or None, auth_method, credentials, result.access_token)
        log_event("INFO", "broker.connect", f"{broker_id} account connected.")
    else:
        broker_store.mark_error(broker_id, auth_method, credentials, result.error or "Connection failed.")
        log_event("WARNING", "broker.connect", f"{broker_id} connect failed: {result.error}")
    return RedirectResponse(url="/broker", status_code=303)


@router.post("/broker/{broker_id}/disconnect")
def broker_disconnect(broker_id: str):
    broker_store.disconnect(broker_id)
    log_event("INFO", "broker.connect", f"{broker_id} account disconnected.")
    return RedirectResponse(url="/broker", status_code=303)


@router.post("/broker/{broker_id}/order", response_class=HTMLResponse)
def broker_place_order(
    request: Request,
    broker_id: str,
    symbol: str = Form(...),
    transaction_type: str = Form(...),
    quantity: str = Form(...),
    exchange: str = Form("NSE"),
    segment: str = Form("CASH"),
    product: str = Form("CNC"),
    order_type: str = Form("MARKET"),
    price: str = Form(""),
):
    """Places a real order via the connected broker - ONLY when the user
    fills in this form and submits it themselves (the Broker page's confirm
    dialog makes that explicit). Never triggered automatically by a
    strategy signal."""
    adapter = broker_registry.get_adapter(broker_id)
    token = broker_store.get_access_token(broker_id) if adapter else None
    if adapter is None or token is None:
        result_ctx = {"request": request, "ok": False, "error": "This broker isn't connected."}
        return templates.TemplateResponse("_broker_order_result.html", result_ctx)

    order = {
        "symbol": symbol.strip().upper(),
        "transaction_type": transaction_type,
        "quantity": quantity.strip(),
        "exchange": exchange,
        "segment": segment,
        "product": product,
        "order_type": order_type,
        "price": float(price) if price.strip() else None,
    }
    result = adapter.place_order(token, order)
    log_event(
        "INFO" if result.ok else "ERROR", "broker.order",
        f"{broker_id} {order['transaction_type']} {order['quantity']} {order['symbol']}: "
        + (f"placed, order_id={result.order_id}" if result.ok else f"failed - {result.error}"),
    )
    result_ctx = {"request": request, "ok": result.ok, "order_id": result.order_id, "error": result.error, "order": order}
    return templates.TemplateResponse("_broker_order_result.html", result_ctx)


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
