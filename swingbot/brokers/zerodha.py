"""Zerodha Kite Connect adapter (`kiteconnect` >= 5.2).

Kite Connect rules you need to know:
* The free "Personal" API plan covers orders, GTT and portfolio. Live quotes
  and historical candles need the paid "Connect" plan; this bot works without
  them (it uses free end-of-day data and broker-side GTT orders).
* Access tokens expire every morning (~6 AM). A human must log in once per
  day: the bot sends you the login link on WhatsApp, you tap it and log in,
  then run `python -m swingbot kite-login <request_token>` (or use the
  callback server). Until you log in, existing GTT stop-losses keep protecting
  your positions; the bot simply won't open new trades that day.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..config import env
from .base import (
    CANCELLED, FILLED, OPEN, PARTIAL, REJECTED, UNKNOWN,
    Broker, BrokerError, Holding, NotSupported, OrderStatus, tick_round,
)


class ZerodhaBroker(Broker):
    name = "zerodha"

    def __init__(self, token_path: str | Path, tag: str = "swingbot"):
        self.kite = None
        self.token_path = Path(token_path)
        self.tag = tag[:20]

    # -- session ---------------------------------------------------------------
    def _client(self):
        try:
            from kiteconnect import KiteConnect
        except ImportError as e:  # pragma: no cover
            raise BrokerError("pip install 'kiteconnect>=5.2'") from e
        return KiteConnect(api_key=env("KITE_API_KEY"))

    def login_url(self) -> str:
        return self._client().login_url()

    def complete_login(self, request_token: str) -> None:
        kite = self._client()
        data = kite.generate_session(request_token, api_secret=env("KITE_API_SECRET"))
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps({"access_token": data["access_token"], "date": date.today().isoformat()}))

    def connect(self) -> None:
        if not self.token_path.exists():
            raise BrokerError("no Kite session - daily login needed")
        tok = json.loads(self.token_path.read_text())
        if tok.get("date") != date.today().isoformat():
            raise BrokerError("Kite session is from a previous day - daily login needed")
        self.kite = self._client()
        self.kite.set_access_token(tok["access_token"])

    def daily_login_required(self) -> bool:
        return True

    def _k(self):
        if self.kite is None:
            raise BrokerError("not connected")
        return self.kite

    # -- account ---------------------------------------------------------------
    def available_cash(self) -> float:
        m = self._k().margins("equity")
        avail = m.get("available", {})
        return float(avail.get("live_balance", m.get("net", 0.0)))

    def holdings(self) -> dict[str, Holding]:
        out: dict[str, Holding] = {}
        for h in self._k().holdings():
            qty = int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0))
            if qty > 0:
                out[h["tradingsymbol"]] = Holding(h["tradingsymbol"], qty, float(h["average_price"]), h.get("last_price"))
        # today's CNC buys/sells live in positions until settlement
        for p in self._k().positions().get("day", []):
            if p.get("product") != "CNC" or p.get("exchange") != "NSE":
                continue
            sym, q = p["tradingsymbol"], int(p.get("quantity", 0))
            if q == 0:
                continue
            if sym in out:
                out[sym].quantity = max(out[sym].quantity + q, 0)
            elif q > 0:
                out[sym] = Holding(sym, q, float(p.get("average_price", 0.0)), p.get("last_price"))
        return {s: h for s, h in out.items() if h.quantity > 0}

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        try:
            data = self._k().ltp([f"NSE:{s}" for s in symbols])
        except Exception as e:  # Personal (free) plan has no market data
            raise NotSupported(f"Kite quotes unavailable (needs paid Connect plan): {e}") from e
        return {k.split(":", 1)[1]: float(v["last_price"]) for k, v in data.items()}

    # -- orders ------------------------------------------------------------------
    def place_entry(self, symbol, qty, trigger, limit, ref) -> str:
        k = self._k()
        return str(k.place_order(
            variety=k.VARIETY_REGULAR, exchange=k.EXCHANGE_NSE, tradingsymbol=symbol,
            transaction_type=k.TRANSACTION_TYPE_BUY, quantity=int(qty), product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_SL, price=tick_round(limit), trigger_price=tick_round(trigger),
            validity=k.VALIDITY_DAY, tag=self.tag,
        ))

    def order_status(self, order_id) -> OrderStatus:
        hist = self._k().order_history(order_id)
        if not hist:
            return OrderStatus(order_id, UNKNOWN)
        last = hist[-1]
        raw = str(last.get("status", "")).upper()
        filled = int(last.get("filled_quantity") or 0)
        qty = int(last.get("quantity") or 0)
        avg = float(last.get("average_price") or 0.0)
        if raw == "COMPLETE":
            status = FILLED
        elif raw == "REJECTED":
            status = REJECTED
        elif raw == "CANCELLED":
            status = PARTIAL if filled > 0 else CANCELLED
        else:
            status = PARTIAL if 0 < filled < qty else OPEN
        return OrderStatus(order_id, status, filled, avg, last.get("status_message") or "", last)

    def cancel_order(self, order_id) -> None:
        k = self._k()
        k.cancel_order(variety=k.VARIETY_REGULAR, order_id=order_id)

    def _gtt_orders(self, symbol, qty, stop_limit, target):
        k = self._k()
        return [
            {"transaction_type": k.TRANSACTION_TYPE_SELL, "quantity": int(qty), "order_type": k.ORDER_TYPE_LIMIT,
             "product": k.PRODUCT_CNC, "price": tick_round(stop_limit)},
            {"transaction_type": k.TRANSACTION_TYPE_SELL, "quantity": int(qty), "order_type": k.ORDER_TYPE_LIMIT,
             "product": k.PRODUCT_CNC, "price": tick_round(target)},
        ]

    def place_protection(self, symbol, qty, stop, stop_limit, target, last_price, ref) -> str:
        k = self._k()
        r = k.place_gtt(
            trigger_type=k.GTT_TYPE_OCO, tradingsymbol=symbol, exchange=k.EXCHANGE_NSE,
            trigger_values=[tick_round(stop), tick_round(target)], last_price=float(last_price),
            orders=self._gtt_orders(symbol, qty, stop_limit, target),
        )
        return str(r["trigger_id"])

    def modify_protection(self, prot_id, symbol, qty, stop, stop_limit, target, last_price) -> str:
        k = self._k()
        r = k.modify_gtt(
            trigger_id=prot_id, trigger_type=k.GTT_TYPE_OCO, tradingsymbol=symbol, exchange=k.EXCHANGE_NSE,
            trigger_values=[tick_round(stop), tick_round(target)], last_price=float(last_price),
            orders=self._gtt_orders(symbol, qty, stop_limit, target),
        )
        return str(r.get("trigger_id", prot_id))

    def cancel_protection(self, prot_id) -> None:
        self._k().delete_gtt(prot_id)

    def protection_active(self, prot_id) -> bool:
        if not prot_id:
            return False
        return str(self._k().get_gtt(prot_id).get("status", "")).lower() == "active"

    def protection_fill(self, prot_id):
        """Find the executed leg of a triggered GTT and its average price."""
        try:
            g = self._k().get_gtt(prot_id)
            if str(g.get("status", "")).lower() != "triggered":
                return None
            legs = g.get("orders", [])
            for i, leg in enumerate(legs):
                oid = ((leg.get("result") or {}).get("order_result") or {}).get("order_id")
                if not oid:
                    continue
                st = self.order_status(str(oid))
                if st.status == FILLED and st.avg_price > 0:
                    return st.avg_price, "stop" if i == 0 else "target"
        except Exception:
            return None
        return None

    def place_day_stop(self, symbol, qty, stop, stop_limit, ref) -> str:
        k = self._k()
        return str(k.place_order(
            variety=k.VARIETY_REGULAR, exchange=k.EXCHANGE_NSE, tradingsymbol=symbol,
            transaction_type=k.TRANSACTION_TYPE_SELL, quantity=int(qty), product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_SL, price=tick_round(stop_limit), trigger_price=tick_round(stop),
            validity=k.VALIDITY_DAY, tag=self.tag,
        ))

    def sell_market(self, symbol, qty, ref) -> str:
        k = self._k()
        return str(k.place_order(
            variety=k.VARIETY_REGULAR, exchange=k.EXCHANGE_NSE, tradingsymbol=symbol,
            transaction_type=k.TRANSACTION_TYPE_SELL, quantity=int(qty), product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_MARKET, validity=k.VALIDITY_DAY, market_protection=-1, tag=self.tag,
        ))

    def preflight(self) -> dict:
        k = self._k()
        out = {}
        for name, fn in [("profile", k.profile), ("margins", lambda: k.margins("equity")), ("holdings", k.holdings),
                         ("positions", k.positions), ("gtts", k.get_gtts)]:
            try:
                out[name] = fn()
            except Exception as e:
                out[name] = f"ERROR: {e}"
        try:
            out["parsed_cash"] = self.available_cash()
            out["parsed_holdings"] = {s: vars(h) for s, h in self.holdings().items()}
        except Exception as e:
            out["parse_error"] = str(e)
        return out
