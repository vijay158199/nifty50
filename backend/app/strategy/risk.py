"""Risk management: SL/TP either fixed-points (15/30 by default) or, per
settings.dynamic_risk_from_displacement, set from the displacement leg's own
high/low - the swing origin through the running extreme the leg reached
before entry. Either way, position sizing is derived from the actual
stop-loss distance and configured risk-per-trade, so a tighter dynamic stop
sizes up and a wider one sizes down for the same rupee risk."""
from __future__ import annotations

import math

from app.config import settings
from app.strategy.types import Direction, RiskPlan


def build_risk_plan(
    entry_price: float,
    direction: Direction,
    leg_high: float | None = None,
    leg_low: float | None = None,
) -> RiskPlan:
    if settings.dynamic_risk_from_displacement and leg_high is not None and leg_low is not None:
        # BUY: the leg ran up from leg_low - that origin invalidates the
        # setup if retaken, so SL sits there. TP is the leg's own high
        # PLUS settings.tp_extension_pct of the leg's range beyond it - an
        # explicit user spec (2026-08-12): since entry is at the FVG's 50%
        # level or a deeper fill, the target should likewise be the leg
        # high or a further extension past it, not just the bare high.
        # Mirrored for SELL.
        leg_range = leg_high - leg_low
        extension = settings.tp_extension_pct * leg_range
        if direction is Direction.BUY:
            stop_loss, take_profit = leg_low, leg_high + extension
        else:
            stop_loss, take_profit = leg_high, leg_low - extension
        sl_points = abs(entry_price - stop_loss)
    else:
        sl_points = settings.stop_loss_points
        tp_points = settings.take_profit_points
        if direction is Direction.BUY:
            stop_loss = entry_price - sl_points
            take_profit = entry_price + tp_points
        else:
            stop_loss = entry_price + sl_points
            take_profit = entry_price - tp_points

    risk_amount = settings.account_capital * (settings.risk_pct_per_trade / 100.0)
    points_at_risk_per_lot = sl_points * settings.lot_size
    position_size_lots = max(1, math.floor(risk_amount / points_at_risk_per_lot)) if points_at_risk_per_lot > 0 else 1

    return RiskPlan(
        stop_loss=stop_loss,
        take_profit=take_profit,
        position_size_lots=position_size_lots,
        risk_amount=risk_amount,
    )
