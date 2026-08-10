# NIFTY 50 ICT/SMC Intraday Strategy System

A local Python + web application implementing the **First 30-Minute Breakout / Liquidity Sweep** ICT/SMC
strategy on NIFTY 50, with BANKNIFTY used for SMT divergence confirmation. It runs a live signal
monitor during market hours, a historical backtester, and a dashboard - all on your own machine.

**This system only generates and logs signals. It never places real broker orders.**

## Strategy Recap

1. Capture the first 30-minute candle of the session (09:15-09:45 IST), built from 1-minute (or 5-minute
   fallback) data resampled ourselves - see "Data source & its limits" below for why.
2. Find the first subsequent 30-minute candle that breaks out of, or sweeps the liquidity of, that first candle.
3. Switch to the 1-minute chart and look for a Market Structure Shift / Change of Character / Break of
   Structure in the trigger's direction, plus an SMT divergence check against BANKNIFTY.
4. Time the entry with the first of Fair Value Gap (50%/CE level or deeper), Order Block, Breaker Block,
   or CISD retracement to be touched (priority order is configurable; Golden Ratio is also implemented
   and can be re-enabled the same way).
5. Fixed risk: 15-point stop, 30-point target (1:2 R:R), with position size derived from your configured
   capital and risk-per-trade percentage.

See `docs` inline in `backend/app/strategy/*.py` for how each concept is implemented - every module has a
short docstring explaining the exact rule it applies.

## Project Layout

```
backend/app/
  config.py        # all tunables (capital, risk %, SL/TP points, lot size, priorities, etc.)
  data/            # yfinance fetch + local SQLite candle cache + NSE trading calendar + 30m resampling
  strategy/        # the strategy engine (swings, structure, SMT, entries, risk, orchestration)
  models/          # SQLAlchemy schema + DB session helper
  backtest/        # replays the engine over a date range, computes stats, writes the Excel workbook
  live/            # market-hours polling monitor + APScheduler jobs (session, daily 17:00 report)
  reports/         # Excel report generation + per-trade chart snapshot rendering
  api/              # FastAPI routes + read-side query helpers
backend/tests/      # pytest unit tests for every strategy module (synthetic OHLC fixtures)
frontend/           # Jinja2 templates + CSS + vendored htmx/Alpine/Chart.js (no Node/npm needed)
data/                # created at runtime: sqlite DB, generated Excel reports, trade chart snapshots, logs
```

## Setup

Requires Python 3.11+ (tested on 3.13) on Windows.

```powershell
cd "Nifty 50"
python -m venv venv
venv\Scripts\pip install -r backend\requirements.txt
```

## Running

```powershell
cd backend
..\venv\Scripts\python run.py
```

Then open **http://localhost:8000**. The scheduler starts automatically:

- Every `poll_interval_seconds` (default 60s) during 09:15-15:30 IST on trading days, it polls the latest
  candles and re-evaluates the day's setup.
- At 15:30 IST it marks the session stopped.
- At **17:00 IST** (matching the spec's daily run) it finalizes and writes that day's Excel monitoring
  sheet to `data/reports/`.

The dashboard itself also works with the scheduler paused/off - "Refresh Now" on the Overview page and
the Backtest page work independently of it.

## Running Tests

```powershell
cd backend
..\venv\Scripts\python -m pytest -v
```

31 tests cover swing/fractal detection, breakout & liquidity-sweep detection, MSS/CHOCH/BOS structure
logic, SMT divergence, all four entry-timing concepts (with hand-verified synthetic fixtures), position
sizing/risk math, and the backtest statistics engine.

## Data Source & Its Limits (important)

Historical/live candles come from **Yahoo Finance via `yfinance`** - no broker account or API key needed.
Two things worth knowing:

- **Yahoo only serves 1-minute candles for the trailing ~30 days**, and 5-minute candles for ~60 days.
  Once a day has been fetched once, it's cached locally in SQLite and stays available even after Yahoo's
  window rolls past it - but the *first* fetch of an old day only gets 5-minute resolution.
- Because of that, a 2-month backtest uses **1-minute candles for the most recent ~30 days** and
  automatically **falls back to 5-minute candles** for structure/entry analysis on older days (30-minute
  breakout detection is unaffected either way, since it's built from whichever finer data is available).
  Every day that used the fallback is flagged `Reduced Resolution` in both the dashboard and the Excel
  report - it is never silently mixed in.
- If Yahoo Finance access ever becomes unreliable, `jugaad-data`'s `nse` module is a solid drop-in
  alternative for NSE-native historical/intraday data - the only file that would need to change is
  `backend/app/data/fetcher.py`.

Also note: Yahoo's native `interval="30m"` candles are **not** aligned to NSE's 09:15 open (they bin from
09:30, silently dropping the real first 15 minutes). This system never uses that native 30m endpoint - it
always builds 30-minute candles itself from 1m/5m data, aligned exactly to session start
(`backend/app/data/resample.py`).

## Configuration

Everything tunable lives in `backend/app/config.py` and can be overridden via environment variables
prefixed `NIFTY_` (e.g. `NIFTY_ACCOUNT_CAPITAL=200000`, `NIFTY_RISK_PCT_PER_TRADE=0.5`) or a `.env` file
in `backend/`. Key ones:

| Setting | Default | Meaning |
|---|---|---|
| `account_capital` | 100000 | Used with `risk_pct_per_trade` to size positions |
| `risk_pct_per_trade` | 1.0 | % of capital risked per trade |
| `lot_size` | 75 | Points-to-currency multiplier per lot |
| `stop_loss_points` / `take_profit_points` | 15 / 30 | Fixed risk management |
| `entry_priority` | FVG, ORDER_BLOCK, BREAKER_BLOCK, CISD | Order entries are checked in; FVG enters at the gap's 50% level or deeper |
| `require_displacement_candle` | True | Requires the BOS/CHOCH confirmation candle to have a long body (strong displacement) |
| `require_smt_alignment` | False | If True, a setup is rejected without confirmed SMT divergence |
| `poll_interval_seconds` | 60 | Live monitor polling cadence |
| `daily_report_time` | 17:00 | IST time the daily Excel report job fires |

## Notes on What Was Validated

The engine was smoke-tested against real recent NIFTY/BANKNIFTY data (not just synthetic fixtures): a
34-trade, 2-month backtest produced a realistic 64.7% win rate with genuine losing streaks (max 5) and a
75-point max drawdown - not a suspiciously perfect record - which is what you'd expect from a real
fixed-R:R momentum strategy rather than a look-ahead bug.

## Future Enhancements (not built, by design - out of scope for a first version)

- Real broker order execution (would need explicit re-authorization given this is currently signal-only).
- Multi-instrument support beyond NIFTY/BANKNIFTY.
- User accounts/auth (currently single-user, localhost-only, no auth by design).
