"""Risk management: SL either fixed-points (15 by default) or, per
settings.dynamic_risk_from_displacement, the displacement leg's own origin
swing - the swing price the impulsive move broke away from. TP is always a
fixed reward multiple (settings.take_profit_rr_multiple, 2.0 by default) of
that ACTUAL SL distance, whichever way it was derived. Position sizing is
likewise derived from the actual stop-loss distance and configured
risk-per-trade, so a tighter dynamic stop sizes up and a wider one sizes
down for the same rupee risk."""
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
        # setup if retaken, so SL sits there. Mirrored for SELL.
        stop_loss = leg_low if direction is Direction.BUY else leg_high
    else:
        stop_loss = (
            entry_price - settings.stop_loss_points
            if direction is Direction.BUY
            else entry_price + settings.stop_loss_points
        )

    sl_points = abs(entry_price - stop_loss)
    rr = settings.take_profit_rr_multiple
    take_profit = entry_price + rr * sl_points if direction is Direction.BUY else entry_price - rr * sl_points

    risk_amount = settings.account_capital * (settings.risk_pct_per_trade / 100.0)
    points_at_risk_per_lot = sl_points * settings.lot_size
    position_size_lots = max(1, math.floor(risk_amount / points_at_risk_per_lot)) if points_at_risk_per_lot > 0 else 1

    return RiskPlan(
        stop_loss=stop_loss,
        take_profit=take_profit,
        position_size_lots=position_size_lots,
        risk_amount=risk_amount,
    )
