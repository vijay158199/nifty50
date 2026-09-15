"""Which brokers this app can actually connect to today vs. which are
listed but not wired up yet - explicit user spec (2026-09-14): "intigrate
with broaker platform like grow, angle one, upstock, dhan and more", with
Groww built out first. Adding a new broker later means writing a
BrokerAdapter (see base.py) and adding one line to SUPPORTED - the store/
routes/template are all broker-agnostic already."""
from __future__ import annotations

from app.broker.base import BrokerAdapter
from app.broker.groww import GrowwAdapter

SUPPORTED: dict[str, BrokerAdapter] = {
    "groww": GrowwAdapter(),
}

# label only - shown as a disabled "coming soon" card on the Broker page
COMING_SOON: dict[str, str] = {
    "angel_one": "Angel One",
    "upstox": "Upstox",
    "dhan": "Dhan",
}


def get_adapter(broker_id: str) -> BrokerAdapter | None:
    return SUPPORTED.get(broker_id)
