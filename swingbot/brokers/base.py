"""Broker interface. Strategy and risk code only ever talk to this interface,
so switching between Groww, Zerodha and paper trading changes nothing else.

Order model (cash delivery / CNC only - no margin, no F&O):
* entry      : stop-limit BUY, DAY validity - fires only if price trades above
               the trigger, never pays more than the limit, expires at the close.
* protection : broker-side OCO (stop-loss + target) that survives restarts,
               crashes and overnight. Placed right after the entry fills.
* day stop   : fallback stop-loss SELL (DAY) used if OCO placement fails.
* exits      : market SELL for gap-guard / close-based exits.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass


class BrokerError(RuntimeError):
    pass


class NotSupported(BrokerError):
    pass


# normalised order states
OPEN, FILLED, PARTIAL, CANCELLED, REJECTED, UNKNOWN = "OPEN", "FILLED", "PARTIAL", "CANCELLED", "REJECTED", "UNKNOWN"


@dataclass
class OrderStatus:
    order_id: str
    status: str  # one of the normalised states above
    filled_qty: int = 0
    avg_price: float = 0.0
    message: str = ""
    raw: dict | None = None


@dataclass
class Holding:
    symbol: str
    quantity: int
    avg_price: float
    last_price: float | None = None


class Broker(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def available_cash(self) -> float: ...

    @abc.abstractmethod
    def holdings(self) -> dict[str, Holding]:
        """Delivery holdings INCLUDING shares bought today (T-day positions)."""

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        raise NotSupported(f"{self.name}: live quotes not available")

    @abc.abstractmethod
    def place_entry(self, symbol: str, qty: int, trigger: float, limit: float, ref: str) -> str:
        """Stop-limit BUY (DAY). Returns order id."""

    @abc.abstractmethod
    def order_status(self, order_id: str) -> OrderStatus: ...

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> None: ...

    @abc.abstractmethod
    def place_protection(
        self, symbol: str, qty: int, stop: float, stop_limit: float, target: float, last_price: float, ref: str
    ) -> str:
        """Broker-side OCO: stop-loss + target SELL. Returns protection id."""

    @abc.abstractmethod
    def modify_protection(
        self, prot_id: str, symbol: str, qty: int, stop: float, stop_limit: float, target: float, last_price: float
    ) -> str:
        """Returns the (possibly new) protection id."""

    @abc.abstractmethod
    def cancel_protection(self, prot_id: str) -> None: ...

    @abc.abstractmethod
    def protection_active(self, prot_id: str) -> bool: ...

    @abc.abstractmethod
    def place_day_stop(self, symbol: str, qty: int, stop: float, stop_limit: float, ref: str) -> str:
        """Fallback stop-loss SELL valid for today only."""

    @abc.abstractmethod
    def sell_market(self, symbol: str, qty: int, ref: str) -> str: ...

    def protection_fill(self, prot_id: str) -> tuple[float, str] | None:
        """(fill price, 'stop'|'target') once an OCO has executed, if the broker exposes it."""
        return None

    def daily_login_required(self) -> bool:
        """True if a human must log in each morning (e.g. Kite Connect)."""
        return False


def tick_round(price: float, tick: float = 0.05) -> float:
    """NSE equity tick size is ₹0.05 for most stocks."""
    return round(round(price / tick) * tick, 2)
