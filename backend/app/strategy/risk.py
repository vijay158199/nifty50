"""Fixed-points risk management: 15-point stop, 30-point target, position
sizing derived from the stop-loss distance and configured risk-per-trade."""
from __future__ import annotations

import math

from app.config import settings
from app.strategy.types import Direction, RiskPlan


def build_risk_plan(entry_price: float, direction: Direction) -> RiskPlan:
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
