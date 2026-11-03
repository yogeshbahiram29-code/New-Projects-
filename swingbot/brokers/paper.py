"""Paper broker: simulated orders, real market data.

Fills are simulated once a day from the completed daily candle, with exactly
the same rules as the backtester (gap handling, limit prices, slippage and
charges). Nothing is ever sent to a real exchange.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..costs import CostModel
from .base import CANCELLED, FILLED, OPEN, Broker, BrokerError, Holding, OrderStatus


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, state_path: str | Path, initial_cash: float, costs: CostModel):
        self.path = Path(state_path)
        self.costs = costs
        self.slip = costs.slippage_pct / 100
        self.initial_cash = initial_cash
        self.s: dict = {}

    # -- persistence ---------------------------------------------------------
    def connect(self) -> None:
        if self.path.exists():
            self.s = json.loads(self.path.read_text())
        else:
            self.s = {"cash": self.initial_cash, "holdings": {}, "orders": {}, "protections": {}, "seq": 0, "last_prices": {}}
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=2, default=str))
        tmp.replace(self.path)

    def _next_id(self, prefix: str) -> str:
        self.s["seq"] += 1
        return f"{prefix}{self.s['seq']:06d}"

    # -- account -------------------------------------------------------------
    def available_cash(self) -> float:
        return float(self.s["cash"])

    def holdings(self) -> dict[str, Holding]:
        lp = self.s.get("last_prices", {})
        return {
            sym: Holding(sym, int(h["qty"]), float(h["avg"]), lp.get(sym))
            for sym, h in self.s["holdings"].items()
            if int(h["qty"]) > 0
        }

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        lp = self.s.get("last_prices", {})
        return {s: lp[s] for s in symbols if s in lp}

    # -- orders --------------------------------------------------------------
    def place_entry(self, symbol, qty, trigger, limit, ref) -> str:
        oid = self._next_id("PE")
        self.s["orders"][oid] = {"kind": "entry", "symbol": symbol, "qty": qty, "trigger": trigger, "limit": limit,
                                 "status": OPEN, "filled": 0, "avg": 0.0, "ref": ref}
        self._save()
        return oid

    def order_status(self, order_id) -> OrderStatus:
        o = self.s["orders"].get(order_id)
        if o is None:
            raise BrokerError(f"unknown order {order_id}")
        return OrderStatus(order_id, o["status"], int(o["filled"]), float(o["avg"]), o.get("msg", ""), dict(o))

    def cancel_order(self, order_id) -> None:
        o = self.s["orders"].get(order_id)
        if o and o["status"] == OPEN:
            o["status"] = CANCELLED
            self._save()

    def place_protection(self, symbol, qty, stop, stop_limit, target, last_price, ref) -> str:
        self._require_holding(symbol, qty)
        pid = self._next_id("OCO")
        self.s["protections"][pid] = {"symbol": symbol, "qty": qty, "stop": stop, "stop_limit": stop_limit,
                                      "target": target, "status": "ACTIVE"}
        self._save()
        return pid

    def modify_protection(self, prot_id, symbol, qty, stop, stop_limit, target, last_price) -> str:
        p = self.s["protections"].get(prot_id)
        if not p or p["status"] != "ACTIVE":
            raise BrokerError(f"protection {prot_id} not active")
        p.update({"qty": qty, "stop": stop, "stop_limit": stop_limit, "target": target})
        self._save()
        return prot_id

    def cancel_protection(self, prot_id) -> None:
        p = self.s["protections"].get(prot_id)
        if p and p["status"] == "ACTIVE":
            p["status"] = "CANCELLED"
            self._save()

    def protection_active(self, prot_id) -> bool:
        p = self.s["protections"].get(prot_id or "")
        return bool(p and p["status"] == "ACTIVE")

    def protection_fill(self, prot_id):
        p = self.s["protections"].get(prot_id or "")
        if p and p["status"] == "TRIGGERED":
            return float(p["fill_price"]), p["reason"]
        return None

    def place_day_stop(self, symbol, qty, stop, stop_limit, ref) -> str:
        self._require_holding(symbol, qty)
        oid = self._next_id("DS")
        self.s["orders"][oid] = {"kind": "day_stop", "symbol": symbol, "qty": qty, "trigger": stop, "limit": stop_limit,
                                 "status": OPEN, "filled": 0, "avg": 0.0, "ref": ref}
        self._save()
        return oid

    def sell_market(self, symbol, qty, ref) -> str:
        self._require_holding(symbol, qty)
        oid = self._next_id("MS")
        self.s["orders"][oid] = {"kind": "market_sell", "symbol": symbol, "qty": qty, "status": OPEN,
                                 "filled": 0, "avg": 0.0, "ref": ref}
        self._save()
        return oid

    def _require_holding(self, symbol, qty) -> None:
        h = self.s["holdings"].get(symbol)
        if not h or int(h["qty"]) < qty:
            raise BrokerError(f"paper: not enough {symbol} shares to sell {qty}")

    # -- simulation ----------------------------------------------------------
    def _buy(self, symbol, qty, price) -> None:
        cost = self.costs.buy_cost(qty * price)
        self.s["cash"] -= qty * price + cost
        h = self.s["holdings"].setdefault(symbol, {"qty": 0, "avg": 0.0})
        total = h["qty"] * h["avg"] + qty * price
        h["qty"] += qty
        h["avg"] = total / h["qty"]

    def _sell(self, symbol, qty, price) -> None:
        cost = self.costs.sell_cost(qty * price)
        self.s["cash"] += qty * price - cost
        h = self.s["holdings"][symbol]
        h["qty"] -= qty
        if h["qty"] <= 0:
            del self.s["holdings"][symbol]

    def simulate_day(self, bars: dict[str, dict], day: date) -> list[str]:
        """Apply one completed daily candle per symbol to all working orders.

        bars: symbol -> {"open","high","low","close"}. Returns a log of fills.
        """
        log: list[str] = []
        orders, prots = self.s["orders"], self.s["protections"]

        # 1. market sells fill at the open
        for oid, o in orders.items():
            if o["status"] == OPEN and o["kind"] == "market_sell" and o["symbol"] in bars:
                px = bars[o["symbol"]]["open"] * (1 - self.slip)
                self._sell(o["symbol"], o["qty"], px)
                o.update(status=FILLED, filled=o["qty"], avg=px)
                log.append(f"SELL {o['qty']} {o['symbol']} @ {px:.2f} (market)")
                for p in prots.values():  # selling the shares retires their OCO
                    if p["symbol"] == o["symbol"] and p["status"] == "ACTIVE" and o["symbol"] not in self.s["holdings"]:
                        p["status"] = "CANCELLED"

        # 2. protective OCOs and day stops
        for pid, p in prots.items():
            if p["status"] != "ACTIVE" or p["symbol"] not in bars:
                continue
            b = bars[p["symbol"]]
            px = None
            if b["open"] <= p["stop"]:
                px, why = b["open"] * (1 - self.slip), "stop (gap down)"
            elif b["low"] <= p["stop"]:
                px, why = p["stop"] * (1 - self.slip), "stop"
            elif b["high"] >= p["target"]:
                px, why = max(b["open"], p["target"]), "target"
            if px is not None:
                qty = min(p["qty"], self.s["holdings"].get(p["symbol"], {}).get("qty", 0))
                if qty > 0:
                    self._sell(p["symbol"], qty, px)
                    log.append(f"SELL {qty} {p['symbol']} @ {px:.2f} ({why})")
                p.update(status="TRIGGERED", fill_price=px, reason=why)
        for oid, o in orders.items():
            if o["status"] != OPEN or o["kind"] != "day_stop" or o["symbol"] not in bars:
                continue
            b = bars[o["symbol"]]
            if b["low"] <= o["trigger"]:
                px = min(b["open"], o["trigger"]) * (1 - self.slip)
                self._sell(o["symbol"], o["qty"], px)
                o.update(status=FILLED, filled=o["qty"], avg=px)
                log.append(f"SELL {o['qty']} {o['symbol']} @ {px:.2f} (day stop)")
            else:
                o["status"] = CANCELLED  # DAY validity

        # 3. entries (stop-limit BUY, DAY validity)
        for oid, o in orders.items():
            if o["status"] != OPEN or o["kind"] != "entry":
                continue
            b = bars.get(o["symbol"])
            if b and b["high"] >= o["trigger"] and b["open"] <= o["limit"]:
                px = min(max(b["open"], o["trigger"]) * (1 + self.slip), o["limit"])
                if self.s["cash"] >= o["qty"] * px * 1.003:
                    self._buy(o["symbol"], o["qty"], px)
                    o.update(status=FILLED, filled=o["qty"], avg=px)
                    log.append(f"BUY {o['qty']} {o['symbol']} @ {px:.2f}")
                    continue
                o["msg"] = "insufficient funds"
            o["status"] = CANCELLED if o["status"] == OPEN else o["status"]

        for sym, b in bars.items():
            self.s["last_prices"][sym] = b["close"]
        self.s["last_sim_day"] = day.isoformat()
        self._save()
        return log
