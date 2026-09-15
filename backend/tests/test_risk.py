import math

import pytest


def test_buy_risk_plan_levels_and_sizing(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "dynamic_risk_from_displacement", False)
    monkeypatch.setattr(settings, "stop_loss_points", 15.0)
    monkeypatch.setattr(settings, "take_profit_rr_multiple", 2.0)
    monkeypatch.setattr(settings, "account_capital", 100_000.0)
    monkeypatch.setattr(settings, "risk_pct_per_trade", 1.0)
    monkeypatch.setattr(settings, "lot_size", 75)

    plan = build_risk_plan(entry_price=24000.0, direction=Direction.BUY)

    assert plan.stop_loss == 23985.0
    assert plan.take_profit == 24030.0  # 2x the 15pt SL distance
    # risk_amount = 100000 * 1% = 1000; points_at_risk_per_lot = 15*75=1125 -> floor(1000/1125)=0 -> clamped to 1
    assert plan.risk_amount == 1000.0
    assert plan.position_size_lots == 1


def test_sell_risk_plan_levels_are_mirrored(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "dynamic_risk_from_displacement", False)
    monkeypatch.setattr(settings, "stop_loss_points", 15.0)
    monkeypatch.setattr(settings, "take_profit_rr_multiple", 2.0)

    plan = build_risk_plan(entry_price=24000.0, direction=Direction.SELL)

    assert plan.stop_loss == 24015.0
    assert plan.take_profit == 23970.0


def test_position_sizing_scales_with_capital(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "dynamic_risk_from_displacement", False)
    monkeypatch.setattr(settings, "stop_loss_points", 15.0)
    monkeypatch.setattr(settings, "account_capital", 500_000.0)
    monkeypatch.setattr(settings, "risk_pct_per_trade", 2.0)
    monkeypatch.setattr(settings, "lot_size", 75)

    plan = build_risk_plan(entry_price=24000.0, direction=Direction.BUY)

    # risk_amount = 500000*2% = 10000; points_at_risk_per_lot=15*75=1125 -> floor(10000/1125)=8
    assert plan.risk_amount == 10_000.0
    assert plan.position_size_lots == 8


def test_dynamic_risk_uses_leg_origin_for_sl_buy(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "dynamic_risk_from_displacement", True)
    monkeypatch.setattr(settings, "take_profit_rr_multiple", 2.0)
    monkeypatch.setattr(settings, "account_capital", 100_000.0)
    monkeypatch.setattr(settings, "risk_pct_per_trade", 1.0)
    monkeypatch.setattr(settings, "lot_size", 75)

    # leg ran from 23960 (origin low) up to 24010 (leg high); entry at 23990
    # (a retracement within the leg) -> SL at the origin; TP is 2x that SL
    # distance from entry, not tied to the leg's own high at all.
    plan = build_risk_plan(entry_price=23990.0, direction=Direction.BUY, leg_high=24010.0, leg_low=23960.0)

    assert plan.stop_loss == 23960.0
    # sl_points = 23990-23960 = 30 -> TP = 23990 + 2*30 = 24050
    assert plan.take_profit == 24050.0
    # risk_amount=1000; points_at_risk_per_lot=30*75=2250 -> floor(1000/2250)=0 -> clamped to 1
    assert plan.position_size_lots == 1


def test_dynamic_risk_uses_leg_origin_for_sl_sell(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "dynamic_risk_from_displacement", True)
    monkeypatch.setattr(settings, "take_profit_rr_multiple", 2.0)

    # leg ran from 24040 (origin high) down to 23990 (leg low); entry at
    # 24010 -> SL at the origin high; TP is 2x that SL distance from entry.
    plan = build_risk_plan(entry_price=24010.0, direction=Direction.SELL, leg_high=24040.0, leg_low=23990.0)

    assert plan.stop_loss == 24040.0
    # sl_points = |24010-24040| = 30 -> TP = 24010 - 2*30 = 23950
    assert plan.take_profit == 23950.0


def test_dynamic_risk_tp_uses_configured_rr_multiple_for_buy(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "dynamic_risk_from_displacement", True)
    monkeypatch.setattr(settings, "take_profit_rr_multiple", 1.5)

    # SL = leg_low = 23960; sl_points = 23990-23960 = 30 -> TP = 23990 + 1.5*30 = 24035
    plan = build_risk_plan(entry_price=23990.0, direction=Direction.BUY, leg_high=24010.0, leg_low=23960.0)

    assert plan.stop_loss == 23960.0
    assert plan.take_profit == 24035.0


def test_dynamic_risk_tp_uses_configured_rr_multiple_for_sell(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "dynamic_risk_from_displacement", True)
    monkeypatch.setattr(settings, "take_profit_rr_multiple", 1.5)

    # SL = leg_high = 24040; sl_points = |24010-24040| = 30 -> TP = 24010 - 1.5*30 = 23965
    plan = build_risk_plan(entry_price=24010.0, direction=Direction.SELL, leg_high=24040.0, leg_low=23990.0)

    assert plan.stop_loss == 24040.0
    assert plan.take_profit == 23965.0


def test_dynamic_risk_falls_back_to_fixed_points_without_leg_bounds(monkeypatch):
    from app.config import settings
    from app.strategy.risk import build_risk_plan
    from app.strategy.types import Direction

    monkeypatch.setattr(settings, "dynamic_risk_from_displacement", True)
    monkeypatch.setattr(settings, "stop_loss_points", 15.0)
    monkeypatch.setattr(settings, "take_profit_rr_multiple", 2.0)

    plan = build_risk_plan(entry_price=24000.0, direction=Direction.BUY)

    assert plan.stop_loss == 23985.0
    assert plan.take_profit == 24030.0
