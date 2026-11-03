"""Daily event-driven backtester.

It reuses the live modules (scanner, risk engine, exit rules, cost model), so a
backtest tests the same logic that will trade real money. Execution model:

* Plans are built after the close of day T and can only fill on day T+1.
* Entry fills when T+1 trades above the trigger; never above the limit price.
  If T+1 opens more than ``max_chase_pct`` above the trigger, the trade is skipped.
* Stops: gap below the stop fills at the open; otherwise at the stop. If the
  stop and target are both inside the same bar, the stop is assumed first.
* Entry-day low below the stop counts as a stop-out (conservative).
* Close-based exits (trend break, time stop) fill at the next open.
* Charges and slippage are applied to every fill.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .config import Settings
from .costs import CostModel
from .exits import partial_price, partial_quantity
from .models import ClosedTrade, Position, Regime
from .risk import AccountState, RiskEngine, check_circuit_breakers
from .scanner import Candidate, correlations_with, returns_panel
from .strategies import get_strategy


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[ClosedTrade]
    regimes: pd.Series
    rejected: dict[str, int] = field(default_factory=dict)
    signals: int = 0
    initial_capital: float = 0.0
    halts: list = field(default_factory=list)  # (date, drawdown %) where live trading would have halted
    exposure_pct: float = 0.0  # % of days with at least one open position
    strategy: str = ""

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades])


class Backtester:
    def __init__(self, settings: Settings, broker_costs: str = "paper", research: bool = False):
        """research=True: a drawdown halt is recorded but trading continues, so the
        whole period is evaluated (live trading always stops for real)."""
        self.s = settings
        self.research = research
        self.strategy = get_strategy(settings)
        self.costs = CostModel.from_settings(settings.section("costs"), broker_costs)
        self.risk = RiskEngine(settings, self.costs)
        self.exec = settings.section("execution")
        self.exits_cfg = settings.section("exits")
        self.slip = self.costs.slippage_pct / 100

    # ------------------------------------------------------------------
    def run(
        self,
        raw: dict[str, pd.DataFrame],
        index_df: pd.DataFrame,
        start: str | date | None = None,
        end: str | date | None = None,
        sectors: dict[str, str] | None = None,
        events: dict[str, list[date]] | None = None,
        progress: bool = False,
    ) -> BacktestResult:
        prep = self.strategy.prepare(raw, index_df)
        frames = prep.frames
        regimes = self.strategy.regime_series(index_df, prep)
        rets = returns_panel(frames)
        halts: list = []
        exposure_days = 0

        days = index_df.index
        if start is not None:
            days = days[days >= pd.Timestamp(start)]
        if end is not None:
            days = days[days <= pd.Timestamp(end)]

        capital = float(self.s.get("initial_capital", 10000))
        state = AccountState(equity=capital, cash=capital)
        positions: list[Position] = []
        pending: list[Candidate] = []
        trades: list[ClosedTrade] = []
        equity_curve: dict[pd.Timestamp, float] = {}
        rejected: dict[str, int] = {}
        signals = 0
        breaker_cfg = self.s.section("circuit_breakers")
        r_cfg = self.s.section("risk")

        for n, t in enumerate(days):
            regime_prev = regimes.get(days[n - 1], Regime.NEUTRAL) if n > 0 else Regime.NEUTRAL
            state.start_day(t.date(), state.equity, state.cash)

            # 1) queued close-based exits at the open -------------------------
            for pos in list(positions):
                if pos.pending_exit_reason and t in frames[pos.symbol].index:
                    bar = frames[pos.symbol].loc[t]
                    self._close(pos, float(bar["open"]) * (1 - self.slip), t, pos.pending_exit_reason, positions, trades, state, r_cfg)

            # 2) intraday stops / targets / partials for existing positions -----
            for pos in list(positions):
                df = frames[pos.symbol]
                if t not in df.index:
                    continue
                bar = df.loc[t]
                o, h, lo = float(bar["open"]), float(bar["high"]), float(bar["low"])
                stop_label = "trailing stop" if pos.stop_price >= pos.entry_price else "stop-loss"
                if o <= pos.stop_price:
                    self._close(pos, o * (1 - self.slip), t, f"{stop_label} (gap down)", positions, trades, state, r_cfg)
                    continue
                if lo <= pos.stop_price:
                    self._close(pos, pos.stop_price * (1 - self.slip), t, stop_label, positions, trades, state, r_cfg)
                    continue
                profile = self.s.profile_for(state.equity)
                if self.strategy.allows_partials and profile.partial_exit_enabled and not pos.partial_taken and pos.quantity >= 2:
                    lvl = partial_price(pos, self.exits_cfg)
                    if h >= lvl:
                        self._partial(pos, max(o, lvl), partial_quantity(pos, self.exits_cfg), state)
                if not pos.trailing_only and h >= pos.target_price:
                    self._close(pos, max(o, pos.target_price), t, "target reached", positions, trades, state, r_cfg)

            # 3) new entries from yesterday's plans ------------------------------
            if pending:
                # refresh equity at the open so today's realised stop-outs count
                # towards the daily-loss breaker before any new entry
                eq_open = state.cash
                for pos in positions:
                    df = frames[pos.symbol]
                    col = "open" if t in df.index else "close"
                    eq_open += pos.quantity * float(df.loc[:t, col].iloc[-1])
                state.mark(eq_open)
                breaker = check_circuit_breakers(state, breaker_cfg)
                if self.research and state.halted:
                    halts.append((t.date(), round(state.drawdown_pct, 1)))
                    state.halted, state.halt_reason, state.peak_equity = False, "", state.equity
                    breaker = check_circuit_breakers(state, breaker_cfg)
                for cand in pending:
                    plan = cand.plan
                    df = frames[plan.symbol]
                    if t not in df.index:
                        continue
                    held = [p.symbol for p in positions]
                    prev_day = days[n - 1]
                    corr = (lambda sym=plan.symbol, h=tuple(held), d=prev_day: correlations_with(rets, sym, list(h), d)) if held else None
                    adv = float(df.loc[days[n - 1], "vol_avg50"]) if days[n - 1] in df.index else None
                    decision = self.risk.evaluate(plan, state, positions, regime_prev, adv, corr, breaker=breaker)
                    if not decision.approved:
                        key = re.sub(r"₹?-?[\d][\d,.]*%?", "#", decision.reasons[-1].split(" (")[0])[:70]
                        rejected[key] = rejected.get(key, 0) + 1
                        continue
                    bar = df.loc[t]
                    o, h, lo = float(bar["open"]), float(bar["high"]), float(bar["low"])
                    max_chase = plan.trigger_price * (1 + float(self.exec.get("max_chase_pct", 2.0)) / 100)
                    if o > max_chase or o > plan.limit_price or h < plan.trigger_price:
                        continue
                    fill = min(max(o, plan.trigger_price) * (1 + self.slip), plan.limit_price)
                    qty = decision.quantity
                    cost = self.costs.buy_cost(qty * fill)
                    if qty * fill + cost > state.cash:
                        continue
                    state.cash -= qty * fill + cost
                    state.entries_today += 1
                    state.orders_today += 2  # entry + protective stop
                    pos = Position(
                        symbol=plan.symbol,
                        setup=plan.setup.value,
                        entry_date=t.date(),
                        entry_price=fill,
                        quantity=qty,
                        initial_stop=plan.stop_price,
                        stop_price=plan.stop_price,
                        target_price=plan.target_price,
                        sector=plan.sector,
                        entry_costs=cost,
                        equity_at_entry=state.equity,
                        trailing_only=plan.trailing_only,
                        highest_close=fill,
                    )
                    positions.append(pos)
                    if lo <= pos.stop_price:  # conservative: assume stop hit after entry
                        self._close(pos, pos.stop_price * (1 - self.slip), t, "stop-loss (entry day)", positions, trades, state, r_cfg)
                pending = []

            # 4) after the close: mark to market, manage, scan -------------------
            mtm = state.cash
            for pos in positions:
                df = frames[pos.symbol]
                px = float(df.loc[:t, "close"].iloc[-1])
                mtm += pos.quantity * px
            state.mark(mtm)
            equity_curve[t] = mtm
            exposure_days += 1 if positions else 0

            reg = regimes.get(t, Regime.NEUTRAL)
            profile = self.s.profile_for(state.equity)
            for pos in positions:
                df = frames[pos.symbol]
                if t not in df.index:
                    continue
                per_share_cost = (pos.entry_costs + self.costs.sell_cost(pos.quantity * pos.entry_price)) / max(pos.quantity, 1)
                res = self.strategy.manage(pos, df.loc[t], reg, profile.partial_exit_enabled and self.strategy.allows_partials,
                                           None, per_share_cost)
                if res.new_stop is not None:
                    pos.stop_price = res.new_stop
                if res.exit_reason:
                    pos.pending_exit_reason = res.exit_reason

            if reg.allows_entries and n + 1 < len(days):
                pending = self.strategy.scan(prep, t, sectors, events)
                signals += len(pending)
            if progress and n % 250 == 0:
                print(f"  {t.date()}  equity ₹{mtm:,.0f}  open {len(positions)}  trades {len(trades)}")

        # close anything still open at the last price (marked, not a real exit)
        last = days[-1] if len(days) else None
        for pos in list(positions):
            px = float(frames[pos.symbol].loc[:last, "close"].iloc[-1])
            self._close(pos, px, last, "end of backtest", positions, trades, state, r_cfg)
        if last is not None:
            equity_curve[last] = state.cash

        return BacktestResult(
            equity=pd.Series(equity_curve, dtype=float),
            trades=trades,
            regimes=regimes.loc[days] if len(days) else regimes,
            rejected=dict(sorted(rejected.items(), key=lambda kv: -kv[1])),
            signals=signals,
            initial_capital=capital,
            halts=halts,
            exposure_pct=round(exposure_days / max(len(days), 1) * 100, 1),
            strategy=self.strategy.name,
        )

    # ------------------------------------------------------------------
    def _partial(self, pos: Position, price: float, qty: int, state: AccountState) -> None:
        qty = min(qty, pos.quantity - 1)
        if qty < 1:
            return
        value = qty * price
        sell_cost = self.costs.sell_cost(value)
        entry_cost_share = pos.entry_costs * qty / pos.initial_quantity
        pos.realized_pnl += value - sell_cost - qty * pos.entry_price - entry_cost_share
        state.cash += value - sell_cost
        state.orders_today += 1
        pos.quantity -= qty
        pos.partial_taken = True
        pos.stop_price = max(pos.stop_price, pos.entry_price)

    def _close(self, pos, price, t, reason, positions, trades, state, r_cfg) -> None:
        value = pos.quantity * price
        sell_cost = self.costs.sell_cost(value)
        entry_cost_share = pos.entry_costs * pos.quantity / pos.initial_quantity
        pnl = pos.realized_pnl + value - sell_cost - pos.quantity * pos.entry_price - entry_cost_share
        state.cash += value - sell_cost
        state.orders_today += 1
        positions.remove(pos)
        total_costs = pos.entry_costs + sell_cost
        risk_money = pos.initial_risk_per_share * pos.initial_quantity
        exit_d = t.date() if hasattr(t, "date") else t
        trades.append(
            ClosedTrade(
                symbol=pos.symbol,
                setup=pos.setup,
                entry_date=pos.entry_date,
                exit_date=exit_d,
                entry_price=round(pos.entry_price, 2),
                exit_price=round(price, 2),
                quantity=pos.initial_quantity,
                initial_stop=pos.initial_stop,
                pnl=round(pnl, 2),
                costs=round(total_costs, 2),
                r_multiple=round(pnl / risk_money, 3) if risk_money > 0 else 0.0,
                exit_reason=reason,
                bars_held=pos.bars_held,
                sector=pos.sector,
                return_pct=round(pnl / pos.equity_at_entry * 100, 3) if pos.equity_at_entry else 0.0,
            )
        )
        if reason != "end of backtest":
            state.on_trade_closed(
                pnl,
                int(r_cfg.get("losing_streak_reduce_after", 3)),
                int(r_cfg.get("losing_streak_restore_after_wins", 2)),
            )
