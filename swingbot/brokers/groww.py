"""Groww Trade API adapter (official `growwapi` SDK, >= 1.4.0 for GTT/OCO).

Login is fully automatic with Groww's TOTP API key: the bot generates the
6-digit TOTP from your secret, so no daily manual step is needed.

Method names and request parameters are taken from the growwapi 1.5.0 source.
Groww does not publish response schemas inside the SDK, so responses are read
defensively (several candidate field names). Run `python -m swingbot preflight`
before going live - it prints the raw responses from YOUR account so any field
mismatch shows up before real money is involved.
"""
from __future__ import annotations

import time
from typing import Any

from ..config import env
from .base import (
    CANCELLED, FILLED, OPEN, PARTIAL, REJECTED, UNKNOWN,
    Broker, BrokerError, Holding, OrderStatus, tick_round,
)

_FILLED = {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED", "COMPLETE"}
_REJECTED = {"REJECTED", "FAILED"}
_CANCELLED = {"CANCELLED", "EXPIRED"}


def _pick(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


class GrowwBroker(Broker):
    name = "groww"

    def __init__(self, oco_stop_order_type: str = "MARKET", call_gap_seconds: float = 0.25):
        self.api = None
        self.oco_stop_order_type = oco_stop_order_type
        self.gap = call_gap_seconds

    # -- session ---------------------------------------------------------------
    def connect(self) -> None:
        try:
            from growwapi import GrowwAPI
        except ImportError as e:  # pragma: no cover
            raise BrokerError("pip install 'growwapi>=1.4.0'") from e
        api_key = env("GROWW_API_KEY")
        totp_secret = env("GROWW_TOTP_SECRET", required=False)
        api_secret = env("GROWW_API_SECRET", required=False)
        if totp_secret:
            import pyotp

            token = GrowwAPI.get_access_token(api_key=api_key, totp=pyotp.TOTP(totp_secret).now())
        elif api_secret:
            token = GrowwAPI.get_access_token(api_key=api_key, secret=api_secret)
        else:
            raise BrokerError("set GROWW_TOTP_SECRET (recommended) or GROWW_API_SECRET in .env")
        self.api = GrowwAPI(token)
        self.G = GrowwAPI

    def _call(self, fn, *args, **kwargs):
        if self.api is None:
            raise BrokerError("not connected")
        for attempt in range(3):
            try:
                out = fn(*args, timeout=15, **kwargs)
                time.sleep(self.gap)
                return out
            except Exception as e:  # network blips: retry with backoff
                name = type(e).__name__
                if attempt == 2 or "Timeout" not in name and "Connection" not in name:
                    raise BrokerError(f"groww {getattr(fn, '__name__', fn)} failed: {name}: {e}") from e
                time.sleep(2 ** attempt)

    # -- account ---------------------------------------------------------------
    def available_cash(self) -> float:
        m = self._call(self.api.get_available_margin_details)
        eq = _pick(m, "equity_margin_details", default={}) or {}
        val = _pick(eq, "cnc_balance_available", "cnc_margin_available")
        if val is None:
            val = _pick(m, "clear_cash", "available_cash", "net_available")
        if val is None:
            raise BrokerError(f"could not read cash from margin response keys {list(m)} - run preflight")
        return float(val)

    def holdings(self) -> dict[str, Holding]:
        out: dict[str, Holding] = {}
        h = self._call(self.api.get_holdings_for_user)
        for item in _pick(h, "holdings", default=[]) or []:
            sym = _pick(item, "trading_symbol", "tradingsymbol", "symbol")
            qty = int(float(_pick(item, "quantity", default=0)))
            if sym and qty > 0:
                out[sym] = Holding(sym, qty, float(_pick(item, "average_price", "avg_price", default=0.0)))
        # shares bought today show up as CNC positions until settlement
        p = self._call(self.api.get_positions_for_user, segment=self.G.SEGMENT_CASH)
        for item in _pick(p, "positions", default=[]) or []:
            if str(_pick(item, "product", default="")).upper() != "CNC":
                continue
            sym = _pick(item, "trading_symbol", "symbol")
            qty = int(float(_pick(item, "quantity", default=0)))
            if not sym or qty == 0:
                continue
            prev = out.get(sym)
            if prev is None:
                out[sym] = Holding(sym, qty, float(_pick(item, "net_price", "average_price", default=0.0)))
            elif qty < 0:  # sold part of an older holding today
                prev.quantity = max(prev.quantity + qty, 0)
        return {s: h for s, h in out.items() if h.quantity > 0}

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for i in range(0, len(symbols), 50):
            chunk = tuple(f"NSE_{s}" for s in symbols[i : i + 50])
            r = self._call(self.api.get_ltp, exchange_trading_symbols=chunk, segment=self.G.SEGMENT_CASH)
            for k, v in r.items():
                px = v if isinstance(v, (int, float)) else _pick(v, "ltp", "last_price")
                if px is not None:
                    out[k.split("_", 1)[1] if "_" in k else k] = float(px)
        return out

    # -- orders ------------------------------------------------------------------
    def place_entry(self, symbol, qty, trigger, limit, ref) -> str:
        r = self._call(
            self.api.place_order,
            validity=self.G.VALIDITY_DAY,
            exchange=self.G.EXCHANGE_NSE,
            order_type=self.G.ORDER_TYPE_STOP_LOSS,
            product=self.G.PRODUCT_CNC,
            quantity=int(qty),
            segment=self.G.SEGMENT_CASH,
            trading_symbol=symbol,
            transaction_type=self.G.TRANSACTION_TYPE_BUY,
            order_reference_id=ref,
            price=tick_round(limit),
            trigger_price=tick_round(trigger),
        )
        return self._order_id(r)

    def _order_id(self, r: dict) -> str:
        oid = _pick(r, "groww_order_id", "order_id")
        if not oid:
            raise BrokerError(f"no order id in response {r}")
        return str(oid)

    def order_status(self, order_id) -> OrderStatus:
        r = self._call(self.api.get_order_status, segment=self.G.SEGMENT_CASH, groww_order_id=order_id)
        raw = str(_pick(r, "order_status", "status", default="")).upper()
        filled = int(float(_pick(r, "filled_quantity", "filled_qty", default=0) or 0))
        avg = 0.0
        if filled > 0:
            try:
                d = self._call(self.api.get_order_detail, segment=self.G.SEGMENT_CASH, groww_order_id=order_id)
                avg = float(_pick(d, "average_fill_price", "avg_fill_price", "average_price", default=0) or 0)
                qty = int(float(_pick(d, "quantity", default=filled)))
            except BrokerError:
                qty = filled
        else:
            qty = 0
        if raw in _FILLED:
            status = FILLED if filled >= qty else PARTIAL
        elif raw in _REJECTED:
            status = REJECTED
        elif raw in _CANCELLED:
            status = PARTIAL if filled > 0 else CANCELLED
        elif raw:
            status = PARTIAL if filled > 0 else OPEN
        else:
            status = UNKNOWN
        return OrderStatus(order_id, status, filled, avg, str(_pick(r, "remark", "message", default="")), r)

    def cancel_order(self, order_id) -> None:
        self._call(self.api.cancel_order, groww_order_id=order_id, segment=self.G.SEGMENT_CASH)

    def _oco_legs(self, stop, stop_limit, target, order_type):
        stop_leg = {"trigger_price": f"{tick_round(stop):.2f}", "order_type": order_type}
        if order_type == "LIMIT":
            stop_leg["price"] = f"{tick_round(stop_limit):.2f}"
        target_leg = {"trigger_price": f"{tick_round(target):.2f}", "order_type": "LIMIT", "price": f"{tick_round(target):.2f}"}
        return stop_leg, target_leg

    def place_protection(self, symbol, qty, stop, stop_limit, target, last_price, ref) -> str:
        last_err = None
        for otype in dict.fromkeys([self.oco_stop_order_type, "LIMIT"]):
            stop_leg, target_leg = self._oco_legs(stop, stop_limit, target, otype)
            try:
                r = self._call(
                    self.api.create_smart_order,
                    smart_order_type=self.G.SMART_ORDER_TYPE_OCO,
                    segment=self.G.SEGMENT_CASH,
                    trading_symbol=symbol,
                    quantity=int(qty),
                    product_type=self.G.PRODUCT_CNC,
                    exchange=self.G.EXCHANGE_NSE,
                    duration=self.G.VALIDITY_GTC,
                    reference_id=ref,
                    net_position_quantity=int(qty),
                    target=target_leg,
                    stop_loss=stop_leg,
                    transaction_type=self.G.TRANSACTION_TYPE_SELL,
                )
                pid = _pick(r, "smart_order_id", "id")
                if not pid:
                    raise BrokerError(f"no smart_order_id in response {r}")
                return str(pid)
            except BrokerError as e:
                last_err = e
        raise BrokerError(f"OCO placement failed: {last_err}")

    def modify_protection(self, prot_id, symbol, qty, stop, stop_limit, target, last_price) -> str:
        stop_leg, target_leg = self._oco_legs(stop, stop_limit, target, self.oco_stop_order_type)
        self._call(
            self.api.modify_smart_order,
            smart_order_id=prot_id,
            smart_order_type=self.G.SMART_ORDER_TYPE_OCO,
            segment=self.G.SEGMENT_CASH,
            quantity=int(qty),
            target=target_leg,
            stop_loss=stop_leg,
        )
        return prot_id

    def cancel_protection(self, prot_id) -> None:
        self._call(self.api.cancel_smart_order, segment=self.G.SEGMENT_CASH,
                   smart_order_type=self.G.SMART_ORDER_TYPE_OCO, smart_order_id=prot_id)

    def protection_active(self, prot_id) -> bool:
        if not prot_id:
            return False
        r = self._call(self.api.get_smart_order, segment=self.G.SEGMENT_CASH,
                       smart_order_type=self.G.SMART_ORDER_TYPE_OCO, smart_order_id=prot_id)
        return str(_pick(r, "status", "smart_order_status", default="")).upper() == self.G.SMART_ORDER_STATUS_ACTIVE

    def place_day_stop(self, symbol, qty, stop, stop_limit, ref) -> str:
        r = self._call(
            self.api.place_order,
            validity=self.G.VALIDITY_DAY,
            exchange=self.G.EXCHANGE_NSE,
            order_type=self.G.ORDER_TYPE_STOP_LOSS_MARKET,
            product=self.G.PRODUCT_CNC,
            quantity=int(qty),
            segment=self.G.SEGMENT_CASH,
            trading_symbol=symbol,
            transaction_type=self.G.TRANSACTION_TYPE_SELL,
            order_reference_id=ref,
            trigger_price=tick_round(stop),
        )
        return self._order_id(r)

    def sell_market(self, symbol, qty, ref) -> str:
        r = self._call(
            self.api.place_order,
            validity=self.G.VALIDITY_DAY,
            exchange=self.G.EXCHANGE_NSE,
            order_type=self.G.ORDER_TYPE_MARKET,
            product=self.G.PRODUCT_CNC,
            quantity=int(qty),
            segment=self.G.SEGMENT_CASH,
            trading_symbol=symbol,
            transaction_type=self.G.TRANSACTION_TYPE_SELL,
            order_reference_id=ref,
        )
        return self._order_id(r)

    # -- read-only diagnostics ---------------------------------------------------
    def preflight(self) -> dict[str, Any]:
        """Raw responses for a human to eyeball before going live. Places NO orders."""
        out: dict[str, Any] = {}
        for name, fn, kw in [
            ("profile", self.api.get_user_profile, {}),
            ("margin", self.api.get_available_margin_details, {}),
            ("holdings", self.api.get_holdings_for_user, {}),
            ("positions", self.api.get_positions_for_user, {"segment": self.G.SEGMENT_CASH}),
            ("ltp", self.api.get_ltp, {"exchange_trading_symbols": ("NSE_RELIANCE",), "segment": self.G.SEGMENT_CASH}),
            ("smart_orders", self.api.get_smart_order_list, {"segment": self.G.SEGMENT_CASH, "page": 0, "page_size": 5}),
            ("orders", self.api.get_order_list, {}),
        ]:
            try:
                out[name] = self._call(fn, **kw)
            except Exception as e:
                out[name] = f"ERROR: {e}"
        try:
            out["parsed_cash"] = self.available_cash()
            out["parsed_holdings"] = {s: vars(h) for s, h in self.holdings().items()}
        except Exception as e:
            out["parse_error"] = str(e)
        return out
