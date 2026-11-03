"""Risk engine - has veto power over every trade.

Strategy code only *proposes* trades (TradePlan). This module sizes them and
rejects anything that breaks a rule. Rules are deliberately simple and
explicit so every rejection can be explained in one line.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Callable

from .config import HARD_MAX_ORDERS_PER_DAY, Settings
from .costs import CostModel
from .models import Position, Regime, RiskDecision, TradePlan


# ---------------------------------------------------------------------------
# Account state used by the circuit breakers
# ---------------------------------------------------------------------------
@dataclass
class AccountState:
    equity: float
    cash: float
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    month_start_equity: float = 0.0
    current_day: str = ""
    current_week: str = ""
    current_month: str = ""
    consecutive_losses: int = 0
    wins_since_streak: int = 0
    streak_reduction: bool = False
    halted: bool = False
    halt_reason: str = ""
    entries_today: int = 0
    orders_today: int = 0
    trade_equity_curve: list[float] = field(default_factory=list)  # equity after each closed trade

    def __post_init__(self) -> None:
        if self.peak_equity <= 0:
            self.peak_equity = self.equity
        for attr in ("day_start_equity", "week_start_equity", "month_start_equity"):
            if getattr(self, attr) <= 0:
                setattr(self, attr, self.equity)

    # -- bookkeeping -------------------------------------------------------
    def start_day(self, d: date, equity: float, cash: float | None = None) -> None:
        self.mark(equity, cash)
        day, week, month = d.isoformat(), f"{d.isocalendar()[0]}-W{d.isocalendar()[1]}", d.strftime("%Y-%m")
        if day != self.current_day:
            self.current_day, self.day_start_equity = day, equity
            self.entries_today = self.orders_today = 0
        if week != self.current_week:
            self.current_week, self.week_start_equity = week, equity
        if month != self.current_month:
            self.current_month, self.month_start_equity = month, equity

    def mark(self, equity: float, cash: float | None = None) -> None:
        self.equity = equity
        if cash is not None:
            self.cash = cash
        self.peak_equity = max(self.peak_equity, equity)

    def on_trade_closed(self, pnl: float, streak_after: int, restore_after: int) -> None:
        if pnl < 0:
            self.consecutive_losses += 1
            self.wins_since_streak = 0
            if self.consecutive_losses >= streak_after:
                self.streak_reduction = True
        else:
            self.consecutive_losses = 0
            if self.streak_reduction:
                self.wins_since_streak += 1
                if self.wins_since_streak >= restore_after:
                    self.streak_reduction = False
                    self.wins_since_streak = 0
        self.trade_equity_curve.append(self.equity)
        self.trade_equity_curve = self.trade_equity_curve[-200:]

    # -- derived -----------------------------------------------------------
    def pct_change(self, base: float) -> float:
        return (self.equity - base) / base * 100 if base > 0 else 0.0

    @property
    def drawdown_pct(self) -> float:
        return (self.peak_equity - self.equity) / self.peak_equity * 100 if self.peak_equity > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AccountState":
        return cls(**d)


@dataclass
class BreakerStatus:
    allow_entries: bool
    risk_multiplier: float
    reasons: list[str]


def check_circuit_breakers(
    state: AccountState, cfg: dict, kill_switch: bool = False, event_data_ok: bool = True
) -> BreakerStatus:
    """Decide whether new entries are allowed and at what size multiplier."""
    reasons: list[str] = []
    if kill_switch:
        return BreakerStatus(False, 0.0, ["KILL SWITCH active"])

    halt_dd = float(cfg.get("peak_drawdown_halt_pct", 15))
    if state.drawdown_pct >= halt_dd and not state.halted:
        state.halted = True
        state.halt_reason = f"drawdown {state.drawdown_pct:.1f}% from peak >= {halt_dd}%"
    if state.halted:
        return BreakerStatus(False, 0.0, [f"HALTED: {state.halt_reason} - manual review + `swingbot resume` needed"])

    allow = True
    day_loss = -state.pct_change(state.day_start_equity)
    if day_loss >= float(cfg.get("daily_loss_pct", 2)):
        allow = False
        reasons.append(f"daily loss {day_loss:.1f}% - no new entries today")
    week_loss = -state.pct_change(state.week_start_equity)
    if week_loss >= float(cfg.get("weekly_loss_pct", 4)):
        allow = False
        reasons.append(f"weekly loss {week_loss:.1f}% - no new entries this week")

    mult = 1.0
    month_dd = -state.pct_change(state.month_start_equity)
    if month_dd >= float(cfg.get("monthly_drawdown_pct", 8)):
        mult = min(mult, 0.5)
        reasons.append(f"monthly drawdown {month_dd:.1f}% - half size")
    if state.streak_reduction:
        mult = min(mult, 0.5)
        reasons.append(f"losing streak ({state.consecutive_losses}) - half size")
    curve = state.trade_equity_curve
    if len(curve) >= 20 and state.equity < sum(curve[-20:]) / 20:
        mult = min(mult, 0.5)
        reasons.append("equity below its 20-trade average - half size")
    if not event_data_ok:
        mult = min(mult, 0.5)
        reasons.append("earnings calendar unavailable - half size")
    return BreakerStatus(allow, mult, reasons)


# ---------------------------------------------------------------------------
# Trade-level risk engine
# ---------------------------------------------------------------------------
class RiskEngine:
    def __init__(self, settings: Settings, costs: CostModel):
        self.s = settings
        self.costs = costs
        self.risk = settings.section("risk")
        self.breakers = settings.section("circuit_breakers")
        self.analysis = settings.section("analysis")

    def min_score(self, regime: Regime) -> float:
        if regime is Regime.BULL:
            return float(self.analysis.get("min_score_bull", 70))
        return float(self.analysis.get("min_score_neutral", 80))

    def evaluate(
        self,
        plan: TradePlan,
        state: AccountState,
        positions: list[Position],
        regime: Regime,
        avg_daily_volume: float | None = None,
        correlations: dict[str, float] | Callable[[], dict[str, float]] | None = None,
        pending_symbols: set[str] | None = None,
        breaker: BreakerStatus | None = None,
    ) -> RiskDecision:
        reasons: list[str] = []
        r = self.risk
        profile = self.s.profile_for(state.equity)

        def reject(msg: str) -> RiskDecision:
            reasons.append(msg)
            return RiskDecision(False, reasons=reasons)

        # 1. account-level gates ------------------------------------------------
        breaker = breaker or check_circuit_breakers(state, self.breakers)
        if not breaker.allow_entries:
            return reject("; ".join(breaker.reasons) or "circuit breaker")
        if not regime.allows_entries:
            return reject(f"market regime {regime.value}: no new long entries")
        max_pos = profile.max_positions.get(regime.risk_key, 0)
        if len(positions) + len(pending_symbols or ()) >= max_pos:
            return reject(f"max open positions reached ({max_pos} in {regime.value} regime, {profile.name} account)")
        if state.entries_today >= int(r.get("max_new_entries_per_day", 2)):
            return reject("max new entries for today reached")
        if state.orders_today >= min(int(r.get("max_orders_per_day", 12)), HARD_MAX_ORDERS_PER_DAY):
            return reject("max orders for today reached (runaway guard)")

        # 2. plan sanity ---------------------------------------------------------
        held = {p.symbol for p in positions} | set(pending_symbols or ())
        if plan.symbol in held:
            return reject("already holding / order pending for this stock")
        if plan.score < self.min_score(regime):
            return reject(f"score {plan.score:g} below {self.min_score(regime):g} for {regime.value} regime")
        if not (plan.stop_price < plan.trigger_price < plan.target_price):
            return reject("invalid levels (need stop < entry < target)")
        dist = plan.stop_distance_pct
        if dist > float(r.get("max_stop_distance_pct", 8)):
            return reject(f"stop {dist:.1f}% away > max {r.get('max_stop_distance_pct', 8)}%")
        if dist < float(r.get("min_stop_distance_pct", 1.5)) - 1e-9:
            return reject(f"stop {dist:.1f}% away is inside normal noise")

        # 3. position sizing ------------------------------------------------------
        risk_pct = profile.risk_pct.get(regime.risk_key, 0.0) * breaker.risk_multiplier
        risk_amount = state.equity * risk_pct / 100
        rps = plan.risk_per_share
        qty_risk = math.floor(risk_amount / rps) if rps > 0 else 0
        qty_cap_pos = math.floor(state.equity * profile.max_position_pct / 100 / plan.limit_price)
        # keep a small buffer for charges so we never need margin
        qty_cap_cash = math.floor(state.cash * 0.995 / plan.limit_price)
        caps = {"risk": qty_risk, "position size cap": qty_cap_pos, "available cash": qty_cap_cash}
        if avg_daily_volume:
            caps["liquidity (% of ADV)"] = math.floor(avg_daily_volume * float(r.get("max_adv_participation_pct", 1)) / 100)
        gap_pct = float(r.get("assumed_gap_pct", 10))
        max_gap_loss = state.equity * float(r.get("max_gap_loss_pct", 5)) / 100
        caps["gap-risk cap"] = math.floor(max_gap_loss / (plan.trigger_price * gap_pct / 100))
        qty = min(caps.values())
        binding = min(caps, key=caps.get)
        if qty < 1:
            return reject(f"position size is 0 shares (limited by {binding}); price ₹{plan.trigger_price:,.0f} too high for this account")
        if binding != "risk":
            reasons.append(f"size limited by {binding}")

        # 4. charges and reward-to-risk after costs -------------------------------
        entry_val = qty * plan.trigger_price
        cost_win = self.costs.round_trip(entry_val, qty * plan.target_price)
        cost_loss = self.costs.round_trip(entry_val, qty * plan.stop_price)
        gross_reward = qty * plan.reward_per_share
        gross_risk = qty * rps
        net_rr = (gross_reward - cost_win) / (gross_risk + cost_loss)
        if net_rr < float(r.get("min_reward_risk", 2.0)):
            return reject(f"reward:risk after charges {net_rr:.2f} < {r.get('min_reward_risk', 2.0)}")
        if cost_win > float(r.get("max_cost_share_of_reward", 0.10)) * gross_reward:
            return reject(f"charges ₹{cost_win:.0f} eat >{float(r.get('max_cost_share_of_reward', 0.1)) * 100:.0f}% of the expected reward")

        # 5. portfolio-level limits -------------------------------------------------
        new_risk = gross_risk + cost_loss
        heat = sum(p.open_risk for p in positions) + new_risk
        max_heat = state.equity * profile.max_portfolio_heat_pct / 100
        if heat > max_heat + 1e-6:
            return reject(f"portfolio heat {heat / state.equity * 100:.1f}% > {profile.max_portfolio_heat_pct}%")
        if plan.sector and plan.sector != "UNKNOWN":
            same = sum(1 for p in positions if p.sector == plan.sector)
            if same >= int(r.get("max_per_sector", 2)):
                return reject(f"sector limit: already {same} position(s) in {plan.sector}")
        if callable(correlations):
            correlations = correlations()  # computed lazily - only for trades that got this far
        if correlations:
            worst_sym, worst = max(correlations.items(), key=lambda kv: kv[1])
            if worst > float(r.get("max_correlation", 0.8)):
                return reject(f"too correlated with {worst_sym} ({worst:.2f})")

        reasons.append(
            f"risk ₹{gross_risk:,.0f} ({gross_risk / state.equity * 100:.2f}% of equity), "
            f"R:R after charges {net_rr:.2f}, est. charges ₹{cost_win:.0f}"
        )
        return RiskDecision(
            True,
            quantity=qty,
            risk_amount=round(gross_risk, 2),
            position_value=round(entry_val, 2),
            est_costs=round(cost_win, 2),
            reasons=reasons,
        )
