"""Groww broker adapter, via the official `growwapi` Python SDK.

Auth: Groww supports two ways to mint an access token (see
https://groww.in/trade-api/docs/python-sdk) - API Key + Secret, or an API
Key paired with a TOTP code generated from a TOTP secret (via `pyotp`).
Either is accepted here; the user picks one when connecting.

Method names for funds/holdings beyond `place_order` aren't nailed down to
one exact signature across SDK versions in the public docs, so those two
calls probe a short list of likely method names on the installed SDK and
use whichever responds - if none match, the adapter raises a clear error
rather than silently returning nothing."""
from __future__ import annotations

from app.broker.base import BrokerAdapter, BrokerFunds, BrokerHolding, ConnectResult, OrderResult

_FUNDS_METHOD_CANDIDATES = ("get_available_margin_details", "get_margin_details", "get_available_margin", "margin")
_HOLDINGS_METHOD_CANDIDATES = ("get_holdings_for_user", "get_holdings", "holdings")


class GrowwAdapter(BrokerAdapter):
    broker_id = "groww"
    display_name = "Groww"
    auth_fields = [
        ("api_key", "API Key", False),
        ("api_secret", "API Secret (Method A)", True),
        ("totp_secret", "TOTP Secret (Method B)", True),
    ]

    def _client(self, access_token: str):
        from growwapi import GrowwAPI

        return GrowwAPI(access_token)

    def connect(self, credentials: dict) -> ConnectResult:
        try:
            from growwapi import GrowwAPI
        except ImportError:
            return ConnectResult(ok=False, error="The 'growwapi' package isn't installed on the server.")

        api_key = (credentials.get("api_key") or "").strip()
        api_secret = (credentials.get("api_secret") or "").strip()
        totp_secret = (credentials.get("totp_secret") or "").strip()
        if not api_key:
            return ConnectResult(ok=False, error="API key is required.")
        if not api_secret and not totp_secret:
            return ConnectResult(ok=False, error="Provide either an API secret (Method A) or a TOTP secret (Method B).")

        try:
            if totp_secret:
                import pyotp

                totp = pyotp.TOTP(totp_secret).now()
                access_token = GrowwAPI.get_access_token(api_key=api_key, totp=totp)
            else:
                access_token = GrowwAPI.get_access_token(api_key=api_key, secret=api_secret)
        except ImportError:
            return ConnectResult(ok=False, error="The 'pyotp' package isn't installed on the server.")
        except Exception as exc:  # noqa: BLE001 - surface whatever the SDK/broker says, verbatim
            return ConnectResult(ok=False, error=f"Groww rejected the connection: {exc}")

        if not access_token:
            return ConnectResult(ok=False, error="Groww did not return an access token.")
        return ConnectResult(ok=True, access_token=access_token)

    def get_funds(self, access_token: str) -> BrokerFunds:
        client = self._client(access_token)
        for method_name in _FUNDS_METHOD_CANDIDATES:
            method = getattr(client, method_name, None)
            if method is None:
                continue
            raw = method()
            raw = raw if isinstance(raw, dict) else {"value": raw}
            return BrokerFunds(
                available_margin=_first_numeric(raw, "available_margin", "net_available_margin", "clear_cash"),
                used_margin=_first_numeric(raw, "used_margin", "utilised_margin"),
                raw=raw,
            )
        raise RuntimeError("This version of growwapi doesn't expose a funds/margin method this adapter recognizes.")

    def get_holdings(self, access_token: str) -> list[BrokerHolding]:
        client = self._client(access_token)
        for method_name in _HOLDINGS_METHOD_CANDIDATES:
            method = getattr(client, method_name, None)
            if method is None:
                continue
            raw = method()
            items = raw.get("holdings", raw) if isinstance(raw, dict) else raw
            holdings = []
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                holdings.append(BrokerHolding(
                    symbol=str(item.get("trading_symbol") or item.get("symbol") or "?"),
                    quantity=float(item.get("quantity", 0) or 0),
                    avg_price=_as_float(item.get("average_price")),
                    ltp=_as_float(item.get("ltp") or item.get("last_price")),
                ))
            return holdings
        raise RuntimeError("This version of growwapi doesn't expose a holdings method this adapter recognizes.")

    def place_order(self, access_token: str, order: dict) -> OrderResult:
        client = self._client(access_token)
        try:
            resp = client.place_order(
                trading_symbol=order["symbol"],
                quantity=int(order["quantity"]),
                exchange=_const(client, "EXCHANGE", order.get("exchange", "NSE")),
                segment=_const(client, "SEGMENT", order.get("segment", "CASH")),
                product=_const(client, "PRODUCT", order.get("product", "CNC")),
                order_type=_const(client, "ORDER_TYPE", order.get("order_type", "MARKET")),
                transaction_type=_const(client, "TRANSACTION_TYPE", order["transaction_type"]),
                price=order.get("price") or 0,
            )
        except Exception as exc:  # noqa: BLE001 - surface whatever the SDK/broker says, verbatim
            return OrderResult(ok=False, error=str(exc))

        raw = resp if isinstance(resp, dict) else {}
        order_id = raw.get("order_id") or raw.get("groww_order_id")
        return OrderResult(ok=True, order_id=str(order_id) if order_id else None, raw=raw)


def _const(client, prefix: str, value: str) -> str:
    """Resolves e.g. ("EXCHANGE", "NSE") to client.EXCHANGE_NSE if the
    installed SDK defines that constant, else falls back to the plain
    string (most broker SDKs accept either)."""
    return getattr(client, f"{prefix}_{value}", value)


def _first_numeric(raw: dict, *keys: str) -> float | None:
    for key in keys:
        if key in raw and raw[key] is not None:
            try:
                return float(raw[key])
            except (TypeError, ValueError):
                continue
    return None


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
