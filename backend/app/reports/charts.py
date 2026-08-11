"""Generates a small PNG snapshot of a trade's setup (candles at whatever
resolution the strategy actually ran on - settings.structure_interval,
e.g. 5m - around the entry, with entry/SL/TP marked, saves a PNG, returns
its path) for the Excel "Screenshot or chart reference" column and the
dashboard's Trade Log."""
from __future__ import annotations

import datetime as dt
import io

import matplotlib

matplotlib.use("Agg")  # headless - this runs inside a scheduler/web server, never a GUI session
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

from app.config import settings
from app.strategy.types import Direction, TradeResult

_STRUCTURE_COLORS = {"MSS": "#a855f7", "CHOCH": "#f59e0b", "BOS": "#06b6d4"}
_ENTRY_COLORS = {
    "CISD": "#ec4899",
    "ORDER_BLOCK": "#3b82f6",
    "BREAKER_BLOCK": "#f97316",
    "GOLDEN_RATIO": "#eab308",
    "FVG": "#14b8a6",
}


def _nearest_pos(index: pd.DatetimeIndex, ts: dt.datetime) -> int | None:
    """Position of the candle nearest `ts` within `index`, or None if the
    window has no candles at all."""
    if ts is None or len(index) == 0:
        return None
    pos = index.get_indexer([ts], method="nearest")[0]
    return int(pos) if pos != -1 else None


def render_trade_snapshot(result: TradeResult, candles_fine: pd.DataFrame) -> str | None:
    """Renders candles from the trigger time through the exit (padded a bit
    either side) with entry/SL/TP marked, saves a PNG, returns its path.

    Also annotates the setup itself so the snapshot is self-explanatory
    without cross-referencing the trade log row:
      - a shaded band at the first 30-min candle's high/low (the liquidity
        that was broken/swept to trigger the setup)
      - a vertical marker at the MSS/CHOCH/BOS structure break
      - a shaded zone at the entry concept (CISD/Order Block/Breaker
        Block/Golden Ratio) that timed the entry
    """
    if result.entry is None or result.risk is None or candles_fine.empty:
        return None

    # Pad by ~8 candles' worth of time either side, not a fixed 15 minutes -
    # at 5m that's 40 minutes (still 8 candles of context); a flat 15min
    # would only show 3 candles of padding and look too cropped.
    interval_minutes = 1
    if len(candles_fine) > 1:
        interval_minutes = max(1, int((candles_fine.index[1] - candles_fine.index[0]).total_seconds() // 60))
    pad = dt.timedelta(minutes=max(15, interval_minutes * 8))

    window_start = (result.trigger.trigger_time if result.trigger else result.entry.entry_time) - pad
    window_end = (result.exit_time or result.entry.entry_time) + pad
    plot_candles = candles_fine[(candles_fine.index >= window_start) & (candles_fine.index <= window_end)]
    if plot_candles.empty:
        plot_candles = candles_fine

    hlines = dict(
        hlines=[result.entry.entry_price, result.risk.stop_loss, result.risk.take_profit],
        colors=["#3b82f6", "#ef4444", "#22c55e"],
        linestyle="--",
        linewidths=1,
    )

    filename = f"{result.trade_date.isoformat()}_{result.symbol.strip('^')}_{result.entry.entry_time.strftime('%H%M')}.png"
    out_path = settings.snapshots_dir / filename

    fig, axlist = mpf.plot(
        plot_candles,
        type="candle",
        style="yahoo",
        hlines=hlines,
        title=f"{result.trade_date.strftime('%d %b %Y')} · {result.symbol_label} {result.direction.value} - {result.entry.entry_type.value}",
        returnfig=True,
        figsize=(7.4, 3.8),
    )
    ax = axlist[0]
    x_last = len(plot_candles) - 1
    y_top = max(plot_candles["High"].max(), result.risk.take_profit)
    y_bottom = min(plot_candles["Low"].min(), result.risk.stop_loss)
    y_span = max(y_top - y_bottom, 1.0)
    ax.set_ylim(y_bottom - y_span * 0.12, y_top + y_span * 0.14)

    # 0) Label the entry / SL / TP hlines at the right edge (colors match hlines above)
    for price, label, color in (
        (result.entry.entry_price, "Entry", "#3b82f6"),
        (result.risk.stop_loss, "SL", "#ef4444"),
        (result.risk.take_profit, "TP", "#22c55e"),
    ):
        ax.text(x_last, price, f" {label}", fontsize=7, color=color, fontweight="bold", va="center", ha="left")

    # 1) First 30-min candle liquidity band (the level that was broken/swept)
    if result.trigger is not None:
        ax.axhspan(
            result.trigger.first_candle_low,
            result.trigger.first_candle_high,
            color="#94a3b8",
            alpha=0.16,
            zorder=0,
        )
        ax.text(
            0, result.trigger.first_candle_high, f" {settings.first_candle_minutes}m liquidity ",
            fontsize=7, color="#64748b", va="bottom", ha="left",
        )

    # 2) MSS / CHOCH / BOS structure-break marker (labelled near the top of the chart
    #    so it never collides with the candles or the entry/SL/TP lines below it)
    if result.structure is not None:
        pos = _nearest_pos(plot_candles.index, result.structure.ts)
        if pos is not None:
            color = _STRUCTURE_COLORS.get(result.structure.structure_type.value, "#a855f7")
            ax.axvline(x=pos, color=color, linestyle=":", linewidth=1.3, zorder=1, ymax=0.95)
            label = result.structure.signal_label
            if result.structure.smt_divergence:
                label += " +SMT"
            ax.annotate(
                label,
                xy=(pos, y_top + y_span * 0.14), xycoords="data",
                fontsize=7.5, color=color, fontweight="bold", ha="center", va="top",
            )

    # 3) Entry concept zone (CISD / Order Block / Breaker Block / Golden Ratio)
    entry_pos = _nearest_pos(plot_candles.index, result.entry.entry_time)
    entry_color = _ENTRY_COLORS.get(result.entry.entry_type.value, "#3b82f6")
    if result.entry.zone_high is not None and result.entry.zone_low is not None:
        ax.axhspan(result.entry.zone_low, result.entry.zone_high, color=entry_color, alpha=0.18, zorder=0)
    if entry_pos is not None:
        ax.annotate(
            result.entry.entry_type.value,
            xy=(entry_pos, result.entry.entry_price),
            xytext=(0, -20), textcoords="offset points",
            fontsize=7.5, color=entry_color, fontweight="bold", ha="center",
            arrowprops=dict(arrowstyle="->", color=entry_color, linewidth=1),
        )

    fig.savefig(out_path, dpi=115, bbox_inches="tight")
    plt.close(fig)

    return str(out_path)


def render_live_chart(result: TradeResult, candles_fine: pd.DataFrame) -> bytes | None:
    """Live counterpart to render_trade_snapshot for the overview page's
    "Today's Live Chart" card: renders the FULL session so far (not a
    cropped window) and doesn't require a resolved (or even found) trade -
    shows just the liquidity band on a NO_SETUP day, adds the structure
    marker once structure resolves, and the entry zone/SL/TP once an entry
    is found. Returns PNG bytes directly (not saved to disk - this is
    regenerated fresh on every request, not archived per-trade)."""
    if candles_fine.empty:
        return None

    has_risk = result.entry is not None and result.risk is not None
    hlines = None
    if has_risk:
        hlines = dict(
            hlines=[result.entry.entry_price, result.risk.stop_loss, result.risk.take_profit],
            colors=["#3b82f6", "#ef4444", "#22c55e"],
            linestyle="--",
            linewidths=1,
        )

    title = f"{result.trade_date.strftime('%d %b %Y')} · {result.symbol_label}"
    if result.direction is not None:
        title += f" {result.direction.value}"
    if result.entry is not None:
        title += f" - {result.entry.entry_type.value}"
    title += f" ({result.status.value})"

    fig, axlist = mpf.plot(
        candles_fine,
        type="candle",
        style="yahoo",
        hlines=hlines,
        title=title,
        returnfig=True,
        figsize=(9.6, 4.4),
    )
    ax = axlist[0]
    x_last = len(candles_fine) - 1
    y_top = max([candles_fine["High"].max()] + ([result.risk.take_profit] if has_risk else []))
    y_bottom = min([candles_fine["Low"].min()] + ([result.risk.stop_loss] if has_risk else []))
    y_span = max(y_top - y_bottom, 1.0)
    ax.set_ylim(y_bottom - y_span * 0.12, y_top + y_span * 0.14)

    if has_risk:
        for price, label, color in (
            (result.entry.entry_price, "Entry", "#3b82f6"),
            (result.risk.stop_loss, "SL", "#ef4444"),
            (result.risk.take_profit, "TP", "#22c55e"),
        ):
            ax.text(x_last, price, f" {label}", fontsize=7, color=color, fontweight="bold", va="center", ha="left")

    if result.trigger is not None:
        ax.axhspan(result.trigger.first_candle_low, result.trigger.first_candle_high, color="#94a3b8", alpha=0.16, zorder=0)
        ax.text(
            0, result.trigger.first_candle_high, f" {settings.first_candle_minutes}m liquidity ",
            fontsize=7, color="#64748b", va="bottom", ha="left",
        )

    if result.structure is not None:
        pos = _nearest_pos(candles_fine.index, result.structure.ts)
        if pos is not None:
            color = _STRUCTURE_COLORS.get(result.structure.structure_type.value, "#a855f7")
            ax.axvline(x=pos, color=color, linestyle=":", linewidth=1.3, zorder=1, ymax=0.95)
            label = result.structure.signal_label
            if result.structure.smt_divergence:
                label += " +SMT"
            ax.annotate(
                label,
                xy=(pos, y_top + y_span * 0.14), xycoords="data",
                fontsize=7.5, color=color, fontweight="bold", ha="center", va="top",
            )

    if result.entry is not None:
        entry_pos = _nearest_pos(candles_fine.index, result.entry.entry_time)
        entry_color = _ENTRY_COLORS.get(result.entry.entry_type.value, "#3b82f6")
        if result.entry.zone_high is not None and result.entry.zone_low is not None:
            ax.axhspan(result.entry.zone_low, result.entry.zone_high, color=entry_color, alpha=0.18, zorder=0)
        if entry_pos is not None:
            ax.annotate(
                result.entry.entry_type.value,
                xy=(entry_pos, result.entry.entry_price),
                xytext=(0, -20), textcoords="offset points",
                fontsize=7.5, color=entry_color, fontweight="bold", ha="center",
                arrowprops=dict(arrowstyle="->", color=entry_color, linewidth=1),
            )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=115, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
