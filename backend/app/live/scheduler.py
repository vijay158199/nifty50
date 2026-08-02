"""APScheduler wiring: a self-gating poll job that only does work during
market hours on trading days (robust to process restarts - it just resumes
polling on the next tick, since all state lives in the DB), plus fixed-time
jobs for session close and the 17:00 IST daily report finalization required
by the spec ("runs every trading day at 5:00 PM IST")."""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.data import calendar
from app.live import control, monitor
from app.models.db import log_event

_scheduler: BackgroundScheduler | None = None


def _safe(fn_name: str, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a job failure must never kill the scheduler
        log_event("ERROR", "live.scheduler", f"Job '{fn_name}' raised: {exc}")


def _poll_job() -> None:
    now = calendar.now_ist()
    if not calendar.is_within_session(now):
        return
    if not control.is_enabled_today():
        return
    _safe("live_poll", monitor.poll_once)


def _session_stop_job() -> None:
    if not calendar.is_trading_day(calendar.now_ist().date()):
        return
    _safe("session_stop", monitor.stop_session)
    _safe("live_control_reset", control.stop)


def _daily_report_job() -> None:
    if not calendar.is_trading_day(calendar.now_ist().date()):
        return
    _safe("daily_report", monitor.finalize_daily_report)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    sched = BackgroundScheduler(timezone=calendar.IST)

    sched.add_job(
        _poll_job,
        trigger="interval",
        seconds=settings.poll_interval_seconds,
        id="live_poll",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=settings.poll_interval_seconds,
    )

    end_h, end_m = (int(x) for x in settings.session_end.split(":"))
    sched.add_job(
        _session_stop_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=end_h, minute=end_m, timezone=calendar.IST),
        id="session_stop",
        max_instances=1,
    )

    report_h, report_m = (int(x) for x in settings.daily_report_time.split(":"))
    sched.add_job(
        _daily_report_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=report_h, minute=report_m, timezone=calendar.IST),
        id="daily_report",
        max_instances=1,
    )

    sched.start()
    log_event("INFO", "live.scheduler", "Scheduler started: live_poll (interval), session_stop, daily_report jobs registered.")
    _scheduler = sched
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler
