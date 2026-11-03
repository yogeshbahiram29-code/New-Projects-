"""Plain data objects shared across the bot."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any


class Regime(str, Enum):
    BULL = "bull"
    NEUTRAL = "neutral"
    BEAR = "bear"
    SHOCK = "shock"

    @property
    def allows_entries(self) -> bool:
        return self in (Regime.BULL, Regime.NEUTRAL)

    @property
    def risk_key(self) -> str:
        # SHOCK uses the bear limits (zero new risk)
        return "bear" if self is Regime.SHOCK else self.value


class SetupType(str, Enum):
    PULLBACK = "pullback"
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"


@dataclass
class ScoreCard:
    symbol: str
    total: float
    parts: dict[str, float]
    passed_hard_filters: bool
    reject_reasons: list[str] = field(default_factory=list)


@dataclass
class TradePlan:
    """A proposed trade. Strategy code creates it; the risk engine sizes or rejects it."""

    symbol: str
    setup: SetupType
    score: float
    trigger_price: float  # buy when price trades above this
    limit_price: float  # never pay more than this
    stop_price: float
    target_price: float
    atr: float
    signal_date: date
    sector: str = "UNKNOWN"
    notes: str = ""
    quantity: int = 0  # filled in by the risk engine

    @property
    def risk_per_share(self) -> float:
        return self.trigger_price - self.stop_price

    @property
    def reward_per_share(self) -> float:
        return self.target_price - self.trigger_price

    @property
    def reward_risk(self) -> float:
        rps = self.risk_per_share
        return self.reward_per_share / rps if rps > 0 else 0.0

    @property
    def stop_distance_pct(self) -> float:
        return (self.trigger_price - self.stop_price) / self.trigger_price * 100

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["setup"] = self.setup.value
        d["signal_date"] = self.signal_date.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TradePlan":
        d = dict(d)
        d["setup"] = SetupType(d["setup"])
        d["signal_date"] = date.fromisoformat(d["signal_date"])
        return cls(**d)


@dataclass
class RiskDecision:
    approved: bool
    quantity: int = 0
    risk_amount: float = 0.0
    position_value: float = 0.0
    est_costs: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class Position:
    """An open trade, tracked from fill until exit."""

    symbol: str
    setup: str
    entry_date: date
    entry_price: float
    quantity: int
    initial_stop: float
    stop_price: float
    target_price: float
    sector: str = "UNKNOWN"
    highest_high: float = 0.0
    partial_taken: bool = False
    bars_held: int = 0
    reached_1r: bool = False
    protection_id: str | None = None  # broker GTT/OCO id
    entry_costs: float = 0.0
    initial_quantity: int = 0
    realized_pnl: float = 0.0  # from partial exits, after charges
    equity_at_entry: float = 0.0
    pending_exit_reason: str | None = None  # close-based exit queued for next session

    def __post_init__(self) -> None:
        if self.initial_quantity <= 0:
            self.initial_quantity = self.quantity
        if self.highest_high <= 0:
            self.highest_high = self.entry_price

    @property
    def initial_risk_per_share(self) -> float:
        return max(self.entry_price - self.initial_stop, 1e-9)

    @property
    def open_risk(self) -> float:
        """Money lost if the current stop is hit (0 once the stop is at/above entry)."""
        return max(self.entry_price - self.stop_price, 0.0) * self.quantity

    def r_multiple(self, price: float) -> float:
        return (price - self.entry_price) / self.initial_risk_per_share

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["entry_date"] = self.entry_date.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        d = dict(d)
        d["entry_date"] = date.fromisoformat(d["entry_date"])
        return cls(**d)


@dataclass
class ClosedTrade:
    symbol: str
    setup: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    quantity: int
    initial_stop: float
    pnl: float  # after costs
    costs: float
    r_multiple: float
    exit_reason: str
    bars_held: int
    sector: str = "UNKNOWN"
    return_pct: float = 0.0  # pnl as % of equity at entry

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["entry_date"] = self.entry_date.isoformat()
        d["exit_date"] = self.exit_date.isoformat()
        return d
