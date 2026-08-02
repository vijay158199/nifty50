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
    BREAKOUT = "BREAKOUT"
    SWEEP = "SWEEP"


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
    """A liquidity interaction with one of the first 30-min candle's levels.
    Deliberately carries NO trade direction - that's only knowable once 1m
    structure shows whether price continues (BOS) or reverses (CHOCH), see
    `structure.detect_bos_choch`."""
    liquidity_side: LiquiditySide
    trigger_type: TriggerType  # BREAKOUT | SWEEP - how that single interaction bar behaved, informational only
    trigger_time: dt.datetime
    first_candle_high: float
    first_candle_low: float
    trigger_candle_close: float


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
