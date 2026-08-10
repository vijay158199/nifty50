"""Central configuration for the strategy system.

All tunables live here so the engine, backtester, live monitor and
dashboard share a single source of truth. Values can be overridden via
environment variables (or a .env file) using the ``NIFTY_`` prefix, e.g.
``NIFTY_CAPITAL=200000``.
"""
import os
import secrets
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = DATA_DIR / "reports"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
LOGS_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "nifty_strategy.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NIFTY_", env_file=".env", extra="ignore")

    # --- Instruments -------------------------------------------------
    primary_symbol: str = "^NSEI"          # NIFTY 50 index (yfinance ticker)
    primary_label: str = "NIFTY 50"
    confirm_symbol: str = "^NSEBANK"       # BANKNIFTY, used only for SMT divergence
    confirm_label: str = "BANKNIFTY"

    # --- Session (IST) -------------------------------------------------
    session_start: str = "09:15"
    session_end: str = "15:30"
    # Size of the "first candle" whose high/low defines the day's liquidity
    # levels. Changed from 30 to 60 (2026-08-02) after a local A/B backtest
    # on the same ~2-month window: 30min gave 26.3% win/-60pts, 60min gave
    # 47.1% win/+105pts AND halved max drawdown (90->45pts) - the only
    # tested lever (vs. R:R ratio, entry-type isolation) that improved
    # multiple metrics together rather than trading one off against another.
    # Still a small sample (17 trades) - worth confirming against live
    # results over time, not just this one backtest window.
    first_candle_minutes: int = 60
    timezone: str = "Asia/Kolkata"

    # --- Strategy parameters -------------------------------------------
    # Structure/entry/exit granularity below the 60m liquidity levels.
    # Fixed to 1m per explicit user spec (2026-08-10) - 3m/5m are no longer
    # offered (see live.control.VALID_INTERVALS). NOTE: 1m candles are only
    # available from Yahoo for the trailing ~30 days (settings.
    # yfinance_1m_lookback_days), vs 5m's ~60 - backtests further back than
    # that automatically fall back to 5m for those days (flagged
    # reduced_resolution=True), same as before.
    structure_interval: str = "1m"
    swing_fractal_window: int = 3          # bars either side for a fractal swing point
    require_smt_alignment: bool = False    # if True, SMT divergence is mandatory, not just supportive
    # If True, BOS-classified setups (continuation) are rejected as
    # NO_SETUP - only CHOCH (reversal) setups are ever traded.
    require_choch_only: bool = False
    # If True, a BOS/CHOCH break only counts once the breaking candle itself
    # shows strong displacement (a "long body"), not just any close beyond
    # the swing point - see structure._is_displacement_candle. Explicit user
    # spec (2026-08-10): "wait for a CHOCH, MSS, or BOS confirmation candle
    # with a long body, indicating strong displacement" - and skip waiting
    # for a sweep/breakout close of the liquidity level itself (the trigger
    # already fires on first touch - see breakout_sweep.find_trigger).
    require_displacement_candle: bool = True
    displacement_lookback_bars: int = 20       # prior bars used for the average-body baseline
    displacement_body_multiplier: float = 1.5  # confirmation candle's body must be >= this x that average
    # Entry model (2026-08-10, explicit user spec): Fair Value Gap only -
    # enter on retracement to the 50% (CE) level of the FVG or deeper.
    # Order Block/Breaker Block/CISD/Golden Ratio were tried alongside FVG
    # too (2026-08-10) - a 43-day backtest showed they crowd out FVG (their
    # shallower zones almost always touch first) and drag win rate from
    # 44.4% down to 38.5%, so FVG-only was kept. Zones for the others are
    # still built in entries.py (harmless/unused) so they can be added back
    # by editing this tuple if that ever changes.
    entry_priority: tuple[str, ...] = ("FVG",)
    golden_ratio_low: float = 0.618
    golden_ratio_high: float = 0.705
    # Expressed in minutes (not bars) so it means the same thing regardless
    # of structure_interval - a bar count would silently mean 5x more real
    # time at 5m than at 1m.
    entry_search_minutes: int = 180        # stop looking for an entry after this many minutes from trigger

    # --- Risk management -------------------------------------------------
    stop_loss_points: float = 15.0
    take_profit_points: float = 30.0
    account_capital: float = 100_000.0
    risk_pct_per_trade: float = 1.0        # percent of capital risked per trade
    lot_size: int = 75                     # NIFTY point value per lot

    # --- Data ------------------------------------------------------------
    yfinance_1m_lookback_days: int = 30    # Yahoo hard limit for 1m candles (only relevant if structure_interval="1m")
    yfinance_5m_lookback_days: int = 60    # Yahoo hard limit for 5m candles (30m candles are derived from these)
    candle_cache_ttl_minutes: int = 5
    backtest_lookback_days: int = 60

    # --- Live monitor / scheduler ----------------------------------------
    poll_interval_seconds: int = 60
    daily_report_time: str = "17:00"       # IST, matches the spec's 5 PM run
    max_fetch_retries: int = 3

    # --- Auth (single local user) -----------------------------------------
    # Override both via env / fly secrets (NIFTY_AUTH_USERNAME / NIFTY_AUTH_PASSWORD) -
    # this is a local single-user gate, not multi-tenant auth. MUST be
    # overridden before deploying publicly - the defaults are not secret.
    auth_username: str = "vijay"
    auth_password: str = "changeme123"

    # --- Paths -------------------------------------------------------------
    data_dir: Path = DATA_DIR
    reports_dir: Path = REPORTS_DIR
    snapshots_dir: Path = SNAPSHOTS_DIR
    logs_dir: Path = LOGS_DIR
    db_path: Path = DB_PATH

    @field_validator("risk_pct_per_trade")
    @classmethod
    def _risk_in_range(cls, v: float) -> float:
        if not (0 < v <= 100):
            raise ValueError("risk_pct_per_trade must be between 0 and 100")
        return v

    @field_validator("account_capital")
    @classmethod
    def _capital_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("account_capital must be positive")
        return v

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.reports_dir, self.snapshots_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()


def get_session_secret() -> str:
    """A signing key for the login session cookie. Persisted to a local file
    (on the persistent volume in production) so sessions survive process
    restarts, rather than regenerated (and thus invalidated) every time the
    server boots. Can also be pinned via NIFTY_SESSION_SECRET so it survives
    even a fresh volume."""
    env_secret = os.environ.get("NIFTY_SESSION_SECRET")
    if env_secret:
        return env_secret
    secret_path = DATA_DIR / ".session_secret"
    if secret_path.exists():
        return secret_path.read_text().strip()
    secret = secrets.token_hex(32)
    secret_path.write_text(secret)
    return secret
