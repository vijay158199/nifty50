"""Common interface every broker integration implements, plus the plain
result dataclasses routes/templates deal with (never the raw SDK response
shapes, which differ per broker)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConnectResult:
    ok: bool
    access_token: str | None = None
    error: str | None = None


@dataclass
class BrokerFunds:
    available_margin: float | None = None
    used_margin: float | None = None
    raw: dict = field(default_factory=dict)  # broker's own response, shown as a fallback if the fields above are unset


@dataclass
class BrokerHolding:
    symbol: str
    quantity: float
    avg_price: float | None = None
    ltp: float | None = None


@dataclass
class OrderResult:
    ok: bool
    order_id: str | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)


class BrokerAdapter:
    """A broker not yet wired up (Angel One/Upstox/Dhan today) simply isn't
    registered in app.broker.registry.SUPPORTED - the Broker page shows it
    as "coming soon" rather than instantiating a stub that would fail
    confusingly the moment someone tried to use it."""

    broker_id: str
    display_name: str
    # (field_name, label, is_secret) - drives the connect form; is_secret
    # renders as a password input.
    auth_fields: list[tuple[str, str, bool]] = []

    def connect(self, credentials: dict) -> ConnectResult:
        raise NotImplementedError

    def get_funds(self, access_token: str) -> BrokerFunds:
        raise NotImplementedError

    def get_holdings(self, access_token: str) -> list[BrokerHolding]:
        raise NotImplementedError

    def place_order(self, access_token: str, order: dict) -> OrderResult:
        """`order` keys: symbol, transaction_type (BUY/SELL), quantity,
        exchange, segment, product, order_type, price (None for a market
        order)."""
        raise NotImplementedError
