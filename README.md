# NIFTY 50 ICT/SMC Intraday Strategy System

A local Python + web application implementing an **RSI bias + Market Structure Shift** ICT/SMC strategy
on NIFTY 50, with BANKNIFTY used for SMT divergence confirmation. It runs a live signal
monitor during market hours, a historical backtester, and a dashboard - all on your own machine.

**Signal generation never places real broker orders on its own.** Connecting a broker account (Broker
page) is opt-in and only ever places an order when you submit one yourself.

## Strategy Recap

1. **Bias** from RSI on the 1-minute chart, from 09:30 onward (`bias_source="RSI"`). Two readings of
   "RSI breaking the top/bottom line" are implemented, because they are genuinely different strategies:
   - `rsi_bias_mode="reversal"` (default) - RSI crossing back **up** through the oversold line is buying
     pressure (exhaustion), and back **down** through overbought is selling pressure.
   - `rsi_bias_mode="momentum"` - RSI pushing **up** through overbought is buying pressure (strength).

   These produce opposite trades on the same chart and are never blended. Backtest both over the same
   window to settle which one you mean. The original first-candle liquidity-sweep model is still
   available as `bias_source="FIRST_CANDLE"`.
2. Switch to 1-minute market structure and wait for a **market structure shift** (CHOCH) in the bias
   direction, with an SMT divergence check against BANKNIFTY. A plain BOS is **not** a trade: by default
   (`require_choch_only=True`) the engine steps over it and keeps scanning the same session for a real
   MSS, rather than ending the day.
3. Time the entry with a Fair Value Gap retracement to its 50% (CE) level or deeper (other concepts -
   CISD / Order Block / Breaker Block / Golden Ratio - are still implemented and can be re-enabled via
   `entry_priority`, but aren't used by default - they underperformed in testing).
4. Stop at the displacement leg's own origin swing, target at 2x that distance (1:2 R:R), with position
   size derived from your configured capital and risk-per-trade percentage.

See `docs` inline in `backend/app/strategy/*.py` for how each concept is implemented - every module has a
short docstring explaining the exact rule it applies.

## Project Layout

```
backend/app/
  config.py        # all tunables (capital, risk %, SL/TP points, lot size, priorities, etc.)
  data/            # Upstox fetch + local SQLite candle cache + NSE trading calendar + 30m resampling
  strategy/        # the strategy engine (swings, structure, SMT, entries, risk, orchestration)
  models/          # SQLAlchemy schema + DB session helper
  backtest/        # replays the engine over a date range, computes stats, writes the Excel workbook
  live/            # market-hours polling monitor + APScheduler jobs (session, daily 17:00 report)
  reports/         # Excel report generation + per-trade chart snapshot rendering (dark, ICT-style PNGs)
  broker/          # optional broker-account integration (adapter interface, Groww adapter, encrypted storage)
  api/              # FastAPI routes + read-side query helpers
backend/tests/      # pytest unit tests for every strategy module (synthetic OHLC fixtures)
frontend/           # Jinja2 templates + CSS + vendored htmx/Alpine/Chart.js (no Node/npm needed)
data/                # created at runtime: sqlite DB, generated Excel reports, trade chart snapshots, logs
```

## Dashboard Pages

- **Overview** / **Trade History** / **Monthly Performance** / **Backtest** - as described above.
- **Track Record** (`/performance`) - a chosen backtest run's history followed by every live-monitored day
  since, as one continuous record: combined win rate, a combined equity curve (backtest portion and live
  portion drawn in different colors), and a full day-by-day log.
- **Trader's Journal** (`/journal`) - one card per taken live trade: a 1-5 self-graded execution rating,
  free-text tags, and notes, plus aggregate analytics (win rate by weekday, tag frequency, current streak,
  best/worst trade). Backtests aren't journaled - this page is about actual trading behavior.
- **Broker** (`/broker`) - optional broker-account connection; see below.
- **Logs & Health** - scheduler job status and the recent error log.

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

71 tests cover RSI calculation and bias detection, the Upstox client's parsing/paging, swing/fractal
detection, breakout & liquidity-sweep detection, MSS/CHOCH/BOS structure
logic, SMT divergence, all four entry-timing concepts (with hand-verified synthetic fixtures), position
sizing/risk math, and the backtest statistics engine.

## Data Source

Candles come from the **Upstox API v3** (`data_provider="upstox"`), which serves 1-minute data from
**January 2022** - so a 1-minute strategy can be backtested over years rather than the trailing few
weeks. Requests are paged a month at a time (the endpoint's cap), and today's session comes from the
intraday endpoint since the historical one excludes it.

**Set up the token before first run.** Generate an **Analytics Token** from the Upstox developer console
and set it as `NIFTY_UPSTOX_ACCESS_TOKEN`. Use that, not a standard access token: a standard one expires
at **3:30 AM IST every day** with no refresh mechanism, which would mean a manual browser login before
every session. The Analytics Token is valid for a year, needs no OAuth redirect, and is read-only -
which is all this system needs, since it never places an order through Upstox.

Instrument keys are mapped from the old Yahoo tickers in `settings.upstox_instrument_keys`
(`^NSEI` -> `NSE_INDEX|Nifty 50`), so cached candles and symbol ids elsewhere in the app are unchanged.
If a fetch 404s, re-check those keys against Upstox's daily instrument master - the exact spelling of
index keys has changed before.

### The old Yahoo path (`data_provider="yfinance"`)

Kept selectable so runs cached under it stay reproducible. Its limits are why it is no longer the
default:

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
| `stop_loss_points` | 15 | Fallback fixed SL distance (only used if `dynamic_risk_from_displacement=False`) |
| `take_profit_rr_multiple` | 2.0 | TP = entry +/- this multiple of the actual SL distance (dynamic or fixed) |
| `entry_priority` | FVG | Order entries are checked in; enters at the gap's 50% level or a deeper fill |
| `require_displacement_candle` | True | Requires the BOS/CHOCH confirmation candle to have a long body (strong displacement) |
| `require_smt_alignment` | False | If True, a setup is rejected without confirmed SMT divergence |
| `poll_interval_seconds` | 60 | Live monitor polling cadence |
| `daily_report_time` | 17:00 | IST time the daily Excel report job fires |

## Notes on What Was Validated

The engine was smoke-tested against real recent NIFTY/BANKNIFTY data (not just synthetic fixtures): a
34-trade, 2-month backtest produced a realistic 64.7% win rate with genuine losing streaks (max 5) and a
75-point max drawdown - not a suspiciously perfect record - which is what you'd expect from a real
fixed-R:R momentum strategy rather than a look-ahead bug.

## Broker Integration (Optional)

The Broker page (`/broker`) lets you connect a broker account. It's entirely opt-in and doesn't change how
signals are generated or where market data comes from - both stay on Yahoo Finance either way. Connecting
an account unlocks:

- Viewing available/used margin and current holdings.
- Placing a manual order (symbol, side, quantity, exchange/segment/product, market/limit price) that you
  fill in and submit yourself, with a confirmation prompt first.

**Groww** is the only broker wired up so far (via the official `growwapi` SDK). Angel One, Upstox, and Dhan
are listed on the page as "coming soon" - adding one means writing a `BrokerAdapter`
(`backend/app/broker/base.py`) and registering it in `backend/app/broker/registry.py`; the storage layer,
routes, and template are already broker-agnostic.

**Getting a Groww API key**: generate one from the
[Groww Cloud API Keys page](https://groww.in/trade-api/api-keys) - either an API Key + Secret pair, or an
API Key (TOTP token) + TOTP Secret. Either works from the Connect form.

**Security**: credentials and the resulting access token are encrypted (Fernet/`cryptography`) before being
stored in the DB, using a key persisted to `data/.broker_secret` (or pinned via `NIFTY_BROKER_ENC_KEY`) -
a separate secret from the login session key. Nothing here is ever sent anywhere except directly to the
broker's own API.

## Future Enhancements (not built, by design - out of scope for this version)

- Automated order execution tied directly to a strategy signal (today, connecting a broker only enables
  *manual* orders you submit yourself - see Broker Integration above).
- Angel One / Upstox / Dhan adapters (framework is in place; Groww is the only one wired up so far).
- Multi-instrument support beyond NIFTY/BANKNIFTY.
- User accounts/auth (currently single-user, localhost-only, no auth by design).
