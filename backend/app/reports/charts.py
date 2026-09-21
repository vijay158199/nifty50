"""Generates a small PNG snapshot of a trade's setup (candles at whatever
resolution the strategy actually ran on - settings.structure_interval,
e.g. 1m - around the entry, with entry/SL/TP marked, saves a PNG, returns
its path) for the Excel "Screenshot or chart reference" column and the
dashboard's Trade Log.

Styled as a clean, dark "prop-desk" chart (same palette as the dashboard's
own dark theme - see frontend/static/css/app.css) rather than a default
matplotlib look, since these PNGs are also what gets shared/posted (the
Overview page's live chart and every Trade Log thumbnail)."""
from __future__ import annotations

import datetime as dt
import io

import matplotlib

matplotlib.use("Agg")  # headless - this runs inside a scheduler/web server, never a GUI session
import matplotlib.pyplot as plt
import matplotlib.transforms as mtransforms
import mplfinance as mpf
import pandas as pd

from app.config import settings
from app.strategy.types import Direction, TradeResult

# --- Palette - mirrors frontend/static/css/app.css's dark theme tokens so
# every exported chart reads as the same brand as the dashboard itself. -----
_BG = "#0a0f1a"          # --bg (dark)
_PANEL = "#0d1422"
_GRID = "#1c2740"
_TEXT = "#eef1f6"        # --text-primary (dark)
_TEXT_MUTED = "#7c88a3"  # close to --text-muted (dark)
_ACCENT = "#d9a548"      # --accent (dark) - brand gold
_UP = "#1ecb75"          # bullish candle body
_DOWN = "#f0454f"        # bearish candle body
_ENTRY = "#4da3ff"       # entry line
_SL = "#f0454f"          # stop-loss line
_TP = "#1ecb75"          # take-profit line
_SWING = "#8a93a3"       # broken-swing level (dotted)
_LIQUIDITY = "#6b7690"   # first-candle liquidity band

_STRUCTURE_COLORS = {"MSS": "#c77dff", "CHOCH": "#ffb454", "BOS": "#22d3ee"}
_ENTRY_COLORS = {
    "CISD": "#ec4899",
    "ORDER_BLOCK": "#3b82f6",
    "BREAKER_BLOCK": "#f97316",
    "GOLDEN_RATIO": "#eab308",
    "FVG": "#14b8a6",
}

_BADGE_BY_STATUS = {
    "TARGET_HIT": ("WIN", _UP),
    "STOP_HIT": ("LOSS", _DOWN),
    "NO_SETUP": ("NO SETUP", _TEXT_MUTED),
    "OPEN": ("OPEN", _ACCENT),
    "AWAITING_ENTRY": ("WATCHING", _ACCENT),
    "EXPIRED": ("EXPIRED", _TEXT_MUTED),
}


def _badge_for(result: TradeResult) -> tuple[str, str]:
    """(label, color) shown as a small pill top-right - lets a viewer read
    win/loss at a glance without parsing numbers, e.g. on a social post."""
    status = result.status.value
    if status == "MANUAL_EXIT":
        pnl = result.pnl_points
        if pnl is None:
            return "CLOSED", _TEXT_MUTED
        if pnl > 0:
            return "WIN", _UP
        if pnl < 0:
            return "LOSS", _DOWN
        return "FLAT", _TEXT_MUTED
    return _BADGE_BY_STATUS.get(status, (status.replace("_", " "), _TEXT_MUTED))


def _chart_style():
    mc = mpf.make_marketcolors(
        up=_UP, down=_DOWN,
        edge={"up": _UP, "down": _DOWN},
        wick={"up": _UP, "down": _DOWN},
        volume="in",
    )
    return mpf.make_mpf_style(
        marketcolors=mc,
        facecolor=_PANEL,
        figcolor=_BG,
        edgecolor=_GRID,
        gridcolor=_GRID,
        gridstyle=":",
        rc={
            "font.family": "sans-serif",
            "font.sans-serif": ["Segoe UI", "Arial", "DejaVu Sans"],
            "axes.edgecolor": _GRID,
            "axes.labelcolor": _TEXT_MUTED,
            "xtick.color": _TEXT_MUTED,
            "ytick.color": _TEXT_MUTED,
            "text.color": _TEXT,
            "axes.grid": True,
            "grid.linewidth": 0.5,
            "grid.alpha": 0.5,
        },
    )


def _trade_focus_window(result: TradeResult, candles_fine: pd.DataFrame) -> pd.DataFrame:
    """Crops to a tight window around the actual setup - from a bit before
    the liquidity trigger through a bit after the exit (or through the last
    available candle if still open) - instead of the full session. Without
    this, a chart grabbed hours after a trade resolved (e.g. for sharing)
    buries a 30-40pt setup inside a 400-500pt full-day axis, squeezing
    every level/marker into an unreadable sliver at one edge. Falls back to
    the full session when there's no entry yet to focus on."""
    if result.entry is None or candles_fine.empty:
        return candles_fine

    interval_minutes = 1
    if len(candles_fine) > 1:
        interval_minutes = max(1, int((candles_fine.index[1] - candles_fine.index[0]).total_seconds() // 60))
    pad = dt.timedelta(minutes=max(15, interval_minutes * 8))

    window_start = (result.trigger.trigger_time if result.trigger else result.entry.entry_time) - pad
    window_end = (result.exit_time or result.entry.entry_time) + pad
    windowed = candles_fine[(candles_fine.index >= window_start) & (candles_fine.index <= window_end)]
    return windowed if not windowed.empty else candles_fine


def _nearest_pos(index: pd.DatetimeIndex, ts: dt.datetime) -> int | None:
    """Position of the candle nearest `ts` within `index`, or None if the
    window has no candles at all."""
    if ts is None or len(index) == 0:
        return None
    pos = index.get_indexer([ts], method="nearest")[0]
    return int(pos) if pos != -1 else None


_HEADER_TOP = 0.80  # fraction of figure height reserved for text, above the candle axes


def _reserve_header_band(fig) -> None:
    """Carves out a dedicated text band at the top of the figure (candles
    never drawn above `_HEADER_TOP`) so header/legend text never has to
    compete with price action for space - independent of how tall the
    candle range happens to be on a given day."""
    fig.subplots_adjust(top=_HEADER_TOP)


def _annotate_header(fig, ax, result: TradeResult, subtitle: str) -> None:
    """Brand wordmark + headline + status badge + PnL callout, drawn as
    figure-level text (all within the reserved header band) so it's fully
    styleable (mplfinance's own `title=` kwarg has no such control)."""
    fig.text(0.012, 0.965, "NIFTY · ICT/SMC STRATEGY", fontsize=8.5, fontweight="bold",
              color=_ACCENT, ha="left", va="top", family="sans-serif")
    fig.text(0.012, 0.925, subtitle, fontsize=11.5, fontweight="bold",
              color=_TEXT, ha="left", va="top", family="sans-serif")

    badge_label, badge_color = _badge_for(result)
    fig.text(0.988, 0.965, badge_label, fontsize=9.5, fontweight="bold",
             color=_BG, ha="right", va="top",
             bbox=dict(boxstyle="round,pad=0.35", fc=badge_color, ec="none"), zorder=6)

    pnl = result.pnl_points
    if pnl is not None:
        pnl_color = _UP if pnl > 0 else (_DOWN if pnl < 0 else _TEXT_MUTED)
        fig.text(0.988, 0.925, f"{pnl:+.1f} pts", fontsize=12, fontweight="bold",
                 color=pnl_color, ha="right", va="top", zorder=6)

    fig.text(0.988, 0.015, "NIFTY 50 ICT/SMC · nifty-strategy dashboard", fontsize=6.5,
              color=_TEXT_MUTED, ha="right", va="bottom", alpha=0.65, family="sans-serif")


def _annotate_liquidity(ax, result: TradeResult) -> None:
    """Shades the first `settings.first_candle_minutes` candle's high/low -
    the liquidity level the whole session is measured against - across the
    full chart width, faint enough not to compete with the candles."""
    trigger = result.trigger
    if trigger is None:
        return
    # An RSI-sourced bias has no first-candle band to shade - there is no
    # liquidity range involved, so there is simply nothing to draw here.
    if trigger.first_candle_low is None or trigger.first_candle_high is None:
        return
    ax.axhspan(trigger.first_candle_low, trigger.first_candle_high, color=_LIQUIDITY, alpha=0.10, zorder=0)


def _annotate_levels(fig, ax, result: TradeResult) -> None:
    """Draws the Liquidity/Swing/TP/ENTRY/SL horizontal lines at their true
    price on the chart, plus one compact caption line in the header band
    spelling out their values - a tight points-based SL/TP routinely puts
    levels within a few pixels of each other, so price-anchored labels
    would overlap; a fixed caption in the header (outside the candle area
    entirely) is always readable regardless of how close the levels sit."""
    entry, risk = result.entry, result.risk
    structure = result.structure
    trigger = result.trigger

    caption_parts: list[str] = []
    if trigger is not None:
        if trigger.first_candle_low is not None and trigger.first_candle_high is not None:
            caption_parts.append(
                f"{settings.first_candle_minutes}m Liq "
                f"{trigger.first_candle_low:,.1f}-{trigger.first_candle_high:,.1f}"
            )
        elif trigger.rsi_value is not None:
            caption_parts.append(f"RSI({settings.rsi_period}) {trigger.rsi_value:,.1f}")

    items: list[tuple[str, float, str]] = []
    if structure is not None:
        items.append((f"Swing {structure.broken_swing.kind}", structure.broken_swing.price, _SWING))
    if entry is not None and risk is not None:
        items.extend([
            ("TP", risk.take_profit, _TP),
            ("Entry", entry.entry_price, _ENTRY),
            ("SL", risk.stop_loss, _SL),
        ])
    if not items and not caption_parts:
        return

    for _, price, color in items:
        style = ":" if color == _SWING else "--"
        alpha = 0.55 if color == _SWING else 0.85
        ax.axhline(y=price, color=color, linestyle=style, linewidth=1, alpha=alpha, zorder=2)

    if caption_parts:
        fig.text(0.012, 0.895, "    ".join(caption_parts), fontsize=8.5, fontweight="600",
                  color=_TEXT_MUTED, ha="left", va="top", family="sans-serif")

    _label_levels_on_axis(ax, items)


def _label_levels_on_axis(ax, items: list[tuple[str, float, str]]) -> None:
    """Small colored price tags right on the axes' edge, at each level's own
    price - so a viewer maps color straight to level without cross-
    referencing the header caption. This is the main thing that makes a
    shared snapshot teach anything: the SL/TP/Entry/Swing lines are
    identified right where they sit, not just spelled out in text elsewhere.
    De-collides labels that would otherwise land on top of each other (a
    dynamic stop often sits at the exact same price as the broken swing)."""
    if not items:
        return
    y_bottom, y_top = ax.get_ylim()
    y_span = max(y_top - y_bottom, 1e-6)
    min_gap = y_span * 0.06

    ordered = sorted(items, key=lambda it: -it[1])
    label_ys: list[float] = []
    for _, price, _ in ordered:
        y = price if not label_ys else min(price, label_ys[-1] - min_gap)
        label_ys.append(y)

    trans = mtransforms.blended_transform_factory(ax.transAxes, ax.transData)
    for (label, price, color), y in zip(ordered, label_ys):
        ax.annotate(
            f" {label} {price:,.1f} ",
            xy=(1.0, y), xycoords=trans, va="center", ha="left",
            fontsize=7.5, fontweight="bold", color=_BG,
            bbox=dict(boxstyle="round,pad=0.28", fc=color, ec="none", alpha=0.95),
            annotation_clip=False, zorder=6,
        )


_GLOSSARY = "CHOCH = reversal · BOS = continuation · FVG = entry zone · SL/TP = risk / target"


def _annotate_glossary(fig) -> None:
    """One small line spelling out the ICT/SMC jargon in plain terms - the
    difference between a chart only the strategy's author can read and one a
    social-media follower with no prior context can actually learn from."""
    fig.text(0.012, 0.015, _GLOSSARY, fontsize=6.3, color=_TEXT_MUTED,
              alpha=0.75, ha="left", va="bottom", family="sans-serif")


def _annotate_structure(ax, y_top: float, y_span: float, plot_candles: pd.DataFrame, result: TradeResult) -> None:
    structure = result.structure
    if structure is None:
        return
    pos = _nearest_pos(plot_candles.index, structure.ts)
    if pos is None:
        return
    color = _STRUCTURE_COLORS.get(structure.structure_type.value, _ACCENT)
    ax.axvline(x=pos, color=color, linestyle=":", linewidth=1.3, zorder=1, ymax=0.95)
    label = structure.signal_label.upper()
    if structure.smt_divergence:
        label += " +SMT"
    ax.annotate(
        label, xy=(pos, y_top + y_span * 0.075), xycoords="data",
        fontsize=7.5, color=_BG, fontweight="bold", ha="center", va="center",
        bbox=dict(boxstyle="round,pad=0.3", fc=color, ec="none"), zorder=5,
    )


def _annotate_entry_marker(ax, plot_candles: pd.DataFrame, result: TradeResult, y_span: float) -> None:
    entry = result.entry
    if entry is None:
        return
    entry_color = _ENTRY_COLORS.get(entry.entry_type.value, _ENTRY)
    if entry.zone_high is not None and entry.zone_low is not None:
        ax.axhspan(entry.zone_low, entry.zone_high, color=entry_color, alpha=0.14, zorder=0)
    entry_pos = _nearest_pos(plot_candles.index, entry.entry_time)
    if entry_pos is None:
        return

    # A single triangle marker just outside the entry candle's own range
    # (below its low for a BUY, above its high for a SELL), with the entry
    # concept's name (CISD/Order Block/Breaker Block/Golden Ratio/FVG)
    # labelled next to it - the ENTRY line/caption already gives the price.
    candle = plot_candles.iloc[entry_pos]
    margin = y_span * 0.025
    is_buy = result.direction is Direction.BUY
    marker_y = float(candle["Low"]) - margin if is_buy else float(candle["High"]) + margin
    marker = "^" if is_buy else "v"
    ax.plot(
        entry_pos, marker_y, marker=marker, markersize=9,
        markerfacecolor=entry_color, markeredgecolor=_BG, markeredgewidth=1,
        clip_on=True, zorder=5,
    )
    ax.annotate(
        entry.entry_type.value,
        xy=(entry_pos, marker_y),
        xytext=(0, -14 if is_buy else 14), textcoords="offset points",
        fontsize=7.5, color=entry_color, fontweight="bold", ha="center", zorder=5,
    )


def _finalize_axes(ax, plot_candles: pd.DataFrame, result: TradeResult) -> tuple[float, float, float]:
    x_last = len(plot_candles) - 1
    has_risk = result.entry is not None and result.risk is not None
    y_top = max([plot_candles["High"].max()] + ([result.risk.take_profit] if has_risk else []))
    y_bottom = min([plot_candles["Low"].min()] + ([result.risk.stop_loss] if has_risk else []))
    y_span = max(y_top - y_bottom, 1.0)
    ax.set_ylim(y_bottom - y_span * 0.12, y_top + y_span * 0.22)
    for spine in ax.spines.values():
        spine.set_edgecolor(_GRID)
        spine.set_linewidth(0.8)
    ax.tick_params(labelsize=8)
    return x_last, y_top, y_span


def render_trade_snapshot(result: TradeResult, candles_fine: pd.DataFrame) -> str | None:
    """Renders candles from the trigger time through the exit (padded a bit
    either side) with entry/SL/TP marked, saves a PNG, returns its path.

    Also annotates the setup itself so the snapshot is self-explanatory
    without cross-referencing the trade log row:
      - a shaded band at the first N-min candle's high/low (the liquidity
        that was broken/swept to trigger the setup)
      - a vertical marker at the MSS/CHOCH/BOS structure break
      - a shaded zone at the entry concept (CISD/Order Block/Breaker
        Block/Golden Ratio/FVG) that timed the entry
    """
    if result.entry is None or result.risk is None or candles_fine.empty:
        return None

    plot_candles = _trade_focus_window(result, candles_fine)

    filename = f"{result.trade_date.isoformat()}_{result.symbol.strip('^')}_{result.entry.entry_time.strftime('%H%M')}.png"
    out_path = settings.snapshots_dir / filename

    fig, axlist = mpf.plot(
        plot_candles, type="candle", style=_chart_style(), returnfig=True, figsize=(8.2, 4.8),
    )
    ax = axlist[0]
    _reserve_header_band(fig)
    x_last, y_top, y_span = _finalize_axes(ax, plot_candles, result)

    subtitle = f"{result.symbol_label} · {result.trade_date.strftime('%d %b %Y')} · {result.direction.value if result.direction else '-'} · {result.entry.entry_type.value}"
    _annotate_header(fig, ax, result, subtitle)
    _annotate_liquidity(ax, result)
    _annotate_levels(fig, ax, result)
    _annotate_structure(ax, y_top, y_span, plot_candles, result)
    _annotate_entry_marker(ax, plot_candles, result, y_span)
    _annotate_glossary(fig)

    fig.savefig(out_path, dpi=160, facecolor=_BG, bbox_inches="tight")
    plt.close(fig)

    return str(out_path)


def render_live_chart(result: TradeResult, candles_fine: pd.DataFrame) -> bytes | None:
    """Live counterpart to render_trade_snapshot for the overview page's
    "Today's Live Chart" card: renders the full session so far while still
    watching for a setup (nothing meaningful to crop to yet), but switches
    to the same tight, trade-focused window as render_trade_snapshot once an
    entry exists - otherwise a chart grabbed well after a trade resolves
    (e.g. for sharing) keeps stretching to the full day's range long after
    the setup itself has become an unreadable sliver at one edge. Returns
    PNG bytes directly (not saved to disk - this is regenerated fresh on
    every request, not archived per-trade)."""
    if candles_fine.empty:
        return None

    plot_candles = _trade_focus_window(result, candles_fine)

    fig, axlist = mpf.plot(
        plot_candles, type="candle", style=_chart_style(), returnfig=True, figsize=(10.2, 5.2),
    )
    ax = axlist[0]
    _reserve_header_band(fig)
    x_last, y_top, y_span = _finalize_axes(ax, plot_candles, result)

    subtitle = f"{result.symbol_label} · {result.trade_date.strftime('%d %b %Y')}"
    if result.direction is not None:
        subtitle += f" · {result.direction.value}"
    if result.entry is not None:
        subtitle += f" · {result.entry.entry_type.value}"
    _annotate_header(fig, ax, result, subtitle)
    _annotate_liquidity(ax, result)
    _annotate_levels(fig, ax, result)
    _annotate_structure(ax, y_top, y_span, plot_candles, result)
    _annotate_entry_marker(ax, plot_candles, result, y_span)
    _annotate_glossary(fig)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
