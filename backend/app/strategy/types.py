"""Shared dataclasses passed between strategy engine stages. Kept independent
of the DB schema so the engine has no persistence dependency (live monitor
and backtester each map these to `Trade` rows themselves)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Direction":
        return Direction.SELL if self is Direction.BUY else Direction.BUY


class TriggerType(str, Enum):
    """How the day's bias was established. BREAKOUT/SWEEP come from the
    first-candle liquidity model (breakout_sweep.py); the RSI_* members come
    from the RSI model (rsi.py), selected by settings.bias_source. Values are
    kept <= 16 chars to fit Trade.trigger_type's column."""
    BREAKOUT = "BREAKOUT"
    SWEEP = "SWEEP"
    # "reversal" reading: RSI leaving an extreme (exhaustion).
    RSI_OS_EXIT = "RSI_OS_EXIT"    # crossed back up through oversold -> bullish
    RSI_OB_EXIT = "RSI_OB_EXIT"    # crossed back down through overbought -> bearish
    # "momentum" reading: RSI driving into an extreme (strength).
    RSI_OB_PUSH = "RSI_OB_PUSH"    # pushed up through overbought -> bullish
    RSI_OS_PUSH = "RSI_OS_PUSH"    # pushed down through oversold -> bearish


class LiquiditySide(str, Enum):
    """Which of the first 30-min candle's two liquidity levels price
    interacted with first. Trade direction is NOT decided here - it's
    derived afterward from whether 1m structure continues (BOS) or
    reverses (CHOCH) relative to this side."""
    HIGH = "HIGH"
    LOW = "LOW"


class StructureType(str, Enum):
    BOS = "BOS"
    CHOCH = "CHOCH"
    MSS = "MSS"


class EntryType(str, Enum):
    CISD = "CISD"
    ORDER_BLOCK = "ORDER_BLOCK"
    BREAKER_BLOCK = "BREAKER_BLOCK"
    GOLDEN_RATIO = "GOLDEN_RATIO"
    FAIR_VALUE_GAP = "FVG"


class TradeStatus(str, Enum):
    NO_SETUP = "NO_SETUP"
    AWAITING_ENTRY = "AWAITING_ENTRY"
    OPEN = "OPEN"
    TARGET_HIT = "TARGET_HIT"
    STOP_HIT = "STOP_HIT"
    MANUAL_EXIT = "MANUAL_EXIT"
    EXPIRED = "EXPIRED"  # session ended before entry or exit


@dataclass
class SwingPoint:
    ts: dt.datetime
    price: float
    kind: str  # "high" | "low"
    index: int  # positional index into the source candle series


@dataclass
class TriggerEvent:
    """Whatever established the day's bias - either a liquidity interaction
    with the first candle's levels, or an RSI line break.

    In the first-candle model this deliberately carries NO trade direction:
    that's only knowable once 1m structure shows whether price continues
    (BOS) or reverses (CHOCH), see `structure.detect_bos_choch`. The RSI
    model does know a direction up front (`bias_direction`), but it still
    only expresses it as the `liquidity_side` whose CHOCH resolves that way
    - the structure stage remains the thing that confirms a trade.
    """
    liquidity_side: LiquiditySide
    trigger_type: TriggerType
    trigger_time: dt.datetime
    trigger_candle_close: float
    # First-candle model only - None when bias came from RSI.
    first_candle_high: float | None = None
    first_candle_low: float | None = None
    # RSI model only - None when bias came from the first candle.
    rsi_value: float | None = None
    rsi_previous: float | None = None
    bias_direction: Direction | None = None


@dataclass
class StructureEvent:
    structure_type: StructureType
    direction: Direction
    liquidity_side: LiquiditySide
    ts: dt.datetime
    broken_swing: SwingPoint
    smt_divergence: bool = False
    smt_detail: str | None = None

    @property
    def signal_label(self) -> str:
        """e.g. "BOS High" / "CHOCH Low" - matches the spec's named outputs."""
        side = "High" if self.liquidity_side is LiquiditySide.HIGH else "Low"
        return f"{self.structure_type.value} {side}"


@dataclass
class EntrySignal:
    entry_type: EntryType
    direction: Direction
    entry_time: dt.datetime
    entry_price: float
    reason: str
    zone_high: float | None = None
    zone_low: float | None = None


@dataclass
class RiskPlan:
    stop_loss: float
    take_profit: float
    position_size_lots: int
    risk_amount: float


@dataclass
class TradeResult:
    trade_date: dt.date
    symbol: str
    symbol_label: str
    direction: Direction | None = None

    trigger: TriggerEvent | None = None
    structure: StructureEvent | None = None
    entry: EntrySignal | None = None
    risk: RiskPlan | None = None
    # Candles making up the displacement/"explosive move" leg itself - set
    # alongside entry (both come from the same build_entry_zones call).
    leg_candle_count: int | None = None

    exit_time: dt.datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None

    status: TradeStatus = TradeStatus.NO_SETUP
    reduced_resolution: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def pnl_points(self) -> float | None:
        if self.entry is None or self.exit_price is None:
            return None
        sign = 1 if self.direction is Direction.BUY else -1
        return sign * (self.exit_price - self.entry.entry_price)

    @property
    def pnl_amount(self) -> float | None:
        if self.pnl_points is None or self.risk is None:
            return None
        from app.config import settings

        return self.pnl_points * settings.lot_size * self.risk.position_size_lots

    @property
    def rr_achieved(self) -> float | None:
        pts = self.pnl_points
        if pts is None or self.risk is None or self.risk.stop_loss == 0:
            return None
        risk_distance = abs(self.entry.entry_price - self.risk.stop_loss)
        if risk_distance == 0:
            return None
        return pts / risk_distance
