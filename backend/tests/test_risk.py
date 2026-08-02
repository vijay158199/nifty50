import math

import pytest


def test_buy_risk_plan_levels_and_sizing(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "stop_loss_points", 15.0)
    monkeypatch.setattr(settings, "take_profit_points", 30.0)
    monkeypatch.setattr(settings, "account_capital", 100_000.0)
    monkeypatch.setattr(settings, "risk_pct_per_trade", 1.0)
    monkeypatch.setattr(settings, "lot_size", 75)

    plan = build_risk_plan(entry_price=24000.0, direction=Direction.BUY)

    assert plan.stop_loss == 23985.0
    assert plan.take_profit == 24030.0
    # risk_amount = 100000 * 1% = 1000; points_at_risk_per_lot = 15*75=1125 -> floor(1000/1125)=0 -> clamped to 1
    assert plan.risk_amount == 1000.0
    assert plan.position_size_lots == 1


def test_sell_risk_plan_levels_are_mirrored(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "stop_loss_points", 15.0)
    monkeypatch.setattr(settings, "take_profit_points", 30.0)

    plan = build_risk_plan(entry_price=24000.0, direction=Direction.SELL)

    assert plan.stop_loss == 24015.0
    assert plan.take_profit == 23970.0


def test_position_sizing_scales_with_capital(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "stop_loss_points", 15.0)
    monkeypatch.setattr(settings, "account_capital", 500_000.0)
    monkeypatch.setattr(settings, "risk_pct_per_trade", 2.0)
    monkeypatch.setattr(settings, "lot_size", 75)

    plan = build_risk_plan(entry_price=24000.0, direction=Direction.BUY)

    # risk_amount = 500000*2% = 10000; points_at_risk_per_lot=15*75=1125 -> floor(10000/1125)=8
    assert plan.risk_amount == 10_000.0
    assert plan.position_size_lots == 8
