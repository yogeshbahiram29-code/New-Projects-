"""Signal mode: ₹0 swing trading - no server, no broker API, no static IP.

Once a day after the close the bot runs the full analysis + risk engine and
WhatsApps you exact instructions:

  * NEW TRADES  - the order to place tomorrow (stop-limit BUY) and the GTT
                  stop-loss/target to set once it fills, with the quantity
                  already sized by the risk rules
  * POSITIONS   - raise your GTT stop to ₹X / sell at tomorrow's open (trend
                  break, time stop, results ahead) / your stop or target was
                  hit today
  * RISK STATUS - regime, equity, drawdown, circuit breakers

You place the orders in the Zerodha / Groww app (about 2 minutes a day) and
tell the bot what you did with `took` / `sold`. Everything is stored in plain
CSV/JSON files in journal/, so it runs free on your own laptop or on GitHub
Actions (see .github/workflows/).
"""
from __future__ import annotations

import json
import logging
from dataclasses import fields
from datetime import date
from pathlib import Path

import pandas as pd

from . import analysis, regime as regime_mod
from .config import Settings
from .costs import CostModel
from .data import events_calendar, load_universe
from .exits import manage_position
from .models import ClosedTrade, Position, Regime, TradePlan
from .notify import Notifier
from .risk import AccountState, RiskEngine, check_circuit_breakers
from .scanner import correlations_with, returns_panel, scan

log = logging.getLogger("swingbot.signals")

POS_FIELDS = [f.name for f in fields(Position)]
FRONT = ["symbol", "quantity", "entry_price", "stop_price", "target_price", "entry_date"]
POS_COLUMNS = FRONT + [f for f in POS_FIELDS if f not in FRONT] + ["last_bar"]
TRADE_COLUMNS = [f.name for f in fields(ClosedTrade)]


class SignalError(RuntimeError):
    pass


def _f(x: float) -> str:
    return f"₹{x:,.2f}"


class SignalDesk:
    def __init__(self, settings: Settings, loader, notifier: Notifier | None = None, journal_dir: str | Path | None = None):
        """loader: object with .load(symbols, history_days, end) -> {symbol: OHLCV frame} (e.g. CachedData)."""
        self.s = settings
        self.loader = loader
        self.n = notifier
        self.dir = Path(journal_dir or settings.get("signals.dir", "journal"))
        self.dir.mkdir(parents=True, exist_ok=True)
        broker_costs = settings.get("signals.broker_costs", "zerodha")
        self.costs = CostModel.from_settings(settings.section("costs"), broker_costs)
        self.risk = RiskEngine(settings, self.costs)
        self.exits_cfg = settings.section("exits")
        self.exec = settings.section("execution")
        self.capital = float(settings.get("initial_capital", 10000))
        self.sent_ok: bool | None = None  # result of the last WhatsApp send

    # ------------------------------------------------------------------ files
    @property
    def pos_path(self) -> Path:
        return self.dir / "my_positions.csv"

    @property
    def trades_path(self) -> Path:
        return self.dir / "closed_trades.csv"

    @property
    def plans_path(self) -> Path:
        return self.dir / "plans.json"

    @property
    def account_path(self) -> Path:
        return self.dir / "account.json"

    def positions(self) -> tuple[list[Position], dict[str, str]]:
        if not self.pos_path.exists() or self.pos_path.stat().st_size == 0:
            return [], {}
        df = pd.read_csv(self.pos_path, dtype=str).fillna("")
        out, last_bar = [], {}
        for _, r in df.iterrows():
            if not r.get("symbol"):
                continue
            d = {}
            for f in fields(Position):
                v = r.get(f.name, "")
                if v == "":
                    continue
                if f.name in ("quantity", "bars_held", "initial_quantity"):
                    d[f.name] = int(float(v))
                elif f.name in ("partial_taken", "reached_1r"):
                    d[f.name] = str(v).strip().lower() in ("true", "1", "yes")
                elif f.name in ("symbol", "setup", "sector", "protection_id", "pending_exit_reason", "entry_date"):
                    d[f.name] = str(v)
                else:
                    d[f.name] = float(v)
            d.setdefault("setup", "manual")
            d.setdefault("entry_date", date.today().isoformat())
            d.setdefault("initial_stop", d.get("stop_price"))
            missing = [k for k in ("quantity", "entry_price", "stop_price", "target_price") if k not in d]
            if missing:
                raise SignalError(f"{d['symbol']}: my_positions.csv is missing {', '.join(missing)}")
            pos = Position.from_dict(d)
            out.append(pos)
            last_bar[pos.symbol] = r.get("last_bar", "") or ""
        return out, last_bar

    def save_positions(self, positions: list[Position], last_bar: dict[str, str]) -> None:
        rows = []
        for p in positions:
            d = p.to_dict()
            d["last_bar"] = last_bar.get(p.symbol, "")
            rows.append(d)
        pd.DataFrame(rows, columns=POS_COLUMNS).to_csv(self.pos_path, index=False)

    def closed_trades(self) -> pd.DataFrame:
        if not self.trades_path.exists() or self.trades_path.stat().st_size == 0:
            return pd.DataFrame(columns=TRADE_COLUMNS)
        return pd.read_csv(self.trades_path)

    def _append_trade(self, t: ClosedTrade) -> None:
        df = pd.concat([self.closed_trades(), pd.DataFrame([t.to_dict()])], ignore_index=True)
        df.to_csv(self.trades_path, index=False)

    def account(self) -> AccountState:
        if self.account_path.exists():
            return AccountState.from_dict(json.loads(self.account_path.read_text())["account"])
        return AccountState(equity=self.capital, cash=self.capital)

    def _meta(self) -> dict:
        if self.account_path.exists():
            return json.loads(self.account_path.read_text()).get("meta", {})
        return {}

    def save_account(self, a: AccountState, meta: dict | None = None) -> None:
        payload = {"account": a.to_dict(), "meta": meta if meta is not None else self._meta()}
        self.account_path.write_text(json.dumps(payload, indent=2, default=str))

    def plans(self) -> dict[str, TradePlan]:
        if not self.plans_path.exists():
            return {}
        d = json.loads(self.plans_path.read_text())
        return {p["symbol"]: TradePlan.from_dict(p) for p in d.get("plans", [])}

    # ------------------------------------------------------------ bookkeeping
    def realized_pnl(self) -> float:
        t = self.closed_trades()
        return float(t["pnl"].sum()) if len(t) else 0.0

    def cash(self, positions: list[Position]) -> float:
        """Capital + closed P&L, minus what is still tied up in open positions."""
        tied_up = sum(
            p.quantity * p.entry_price + p.entry_costs * p.quantity / max(p.initial_quantity, 1) - p.realized_pnl
            for p in positions
        )
        return self.capital + self.realized_pnl() - tied_up

    def equity(self, positions: list[Position], prices: dict[str, float]) -> float:
        return self.cash(positions) + sum(p.quantity * prices.get(p.symbol, p.entry_price) for p in positions)

    def took(self, symbol: str, qty: int, price: float, stop: float | None = None, target: float | None = None,
             when: date | None = None) -> Position:
        symbol = symbol.upper()
        positions, last_bar = self.positions()
        if any(p.symbol == symbol for p in positions):
            raise SignalError(f"{symbol} is already in your journal - use `sold` first or edit {self.pos_path}")
        plan = self.plans().get(symbol)
        if stop is None or target is None:
            if plan is None:
                raise SignalError(f"No plan for {symbol} in {self.plans_path}. Give --stop and --target yourself.")
            stop = plan.stop_price if stop is None else stop
            target = plan.target_price if target is None else target
        if not stop < price < target:
            raise SignalError(f"need stop < price < target (got {stop} / {price} / {target})")
        positions_now = list(positions)
        eq = self.equity(positions_now, {})
        pos = Position(
            symbol=symbol, setup=plan.setup.value if plan else "manual", entry_date=when or date.today(),
            entry_price=float(price), quantity=int(qty), initial_stop=float(stop), stop_price=float(stop),
            target_price=float(target), sector=plan.sector if plan else "UNKNOWN",
            entry_costs=round(self.costs.buy_cost(qty * price), 2), equity_at_entry=eq,
        )
        last_bar[symbol] = pos.entry_date.isoformat()  # management starts with the next candle
        self.save_positions(positions + [pos], last_bar)
        return pos

    def sold(self, symbol: str, price: float, qty: int | None = None, reason: str = "manual sell",
             when: date | None = None) -> ClosedTrade | None:
        symbol = symbol.upper()
        positions, last_bar = self.positions()
        pos = next((p for p in positions if p.symbol == symbol), None)
        if pos is None:
            raise SignalError(f"{symbol} is not in your journal")
        t = self._close(pos, float(price), reason, when or date.today(), qty)
        if t is not None or pos.quantity <= 0:
            positions = [p for p in positions if p.symbol != symbol]
            last_bar.pop(symbol, None)
        self.save_positions(positions, last_bar)
        return t

    def _close(self, pos: Position, price: float, reason: str, when: date, qty: int | None = None) -> ClosedTrade | None:
        qty = pos.quantity if qty is None else min(int(qty), pos.quantity)
        value = qty * price
        sell_cost = self.costs.sell_cost(value)
        entry_share = pos.entry_costs * qty / max(pos.initial_quantity, 1)
        pnl = value - sell_cost - qty * pos.entry_price - entry_share
        if qty < pos.quantity:
            pos.realized_pnl += pnl
            pos.quantity -= qty
            pos.partial_taken = True
            return None
        total = pos.realized_pnl + pnl
        risk_money = pos.initial_risk_per_share * pos.initial_quantity
        t = ClosedTrade(
            symbol=pos.symbol, setup=pos.setup, entry_date=pos.entry_date, exit_date=when,
            entry_price=round(pos.entry_price, 2), exit_price=round(price, 2), quantity=pos.initial_quantity,
            initial_stop=pos.initial_stop, pnl=round(total, 2), costs=round(pos.entry_costs + sell_cost, 2),
            r_multiple=round(total / risk_money, 2) if risk_money else 0.0, exit_reason=reason,
            bars_held=pos.bars_held, sector=pos.sector,
            return_pct=round(total / pos.equity_at_entry * 100, 3) if pos.equity_at_entry else 0.0,
        )
        self._append_trade(t)
        acct = self.account()
        r = self.s.section("risk")
        acct.on_trade_closed(total, int(r.get("losing_streak_reduce_after", 3)), int(r.get("losing_streak_restore_after_wins", 2)))
        self.save_account(acct)
        pos.quantity = 0
        return t

    # ------------------------------------------------------------- daily run
    def run(self, today: date | None = None, send: bool = True) -> str:
        today = today or date.today()
        if self.loader is None:
            raise SignalError("no market data loader configured")
        uni = load_universe(self.s.get("universe.file", "config/universe.csv"))
        positions, last_bar = self.positions()
        bench = self.s.get("regime.benchmark", "NIFTY50")
        symbols = sorted(set(uni) | {p.symbol for p in positions})
        try:
            raw = self.loader.load(symbols + [bench], int(self.s.get("data.history_days", 600)), today)
        except Exception as e:
            raise SignalError(f"market data download failed ({type(e).__name__}: {e}) - check internet") from e
        index_df = raw.pop(bench, None)
        if index_df is None or index_df.empty:
            raise SignalError("could not download index data - check internet / data provider")
        bar_day = index_df.index[-1]
        meta = self._meta()
        if meta.get("last_bar") == bar_day.date().isoformat():
            msg = f"No new market data since {bar_day:%d %b} (weekend/holiday?). Nothing to do."
            log.info(msg)
            return msg
        stale = (pd.Timestamp(today) - bar_day).days > 4
        fresh = {s: df for s, df in raw.items() if not df.empty and df.index[-1] >= bar_day}
        frames, rs_panel = analysis.prepare_universe(fresh, index_df)
        breadth = regime_mod.breadth_series(frames)
        b_today = float(breadth.iloc[-1]) if len(breadth) and not pd.isna(breadth.iloc[-1]) else None
        regime = regime_mod.current_regime(index_df, b_today, self.s.section("regime"))
        events, events_ok = events_calendar(self.s)

        acct = self.account()
        prev_prices = {}
        for p in positions:  # closes as of the previous run -> start-of-day equity
            hist = raw.get(p.symbol)
            if hist is not None:
                upto = hist["close"].loc[: pd.Timestamp(last_bar.get(p.symbol) or p.entry_date.isoformat())]
                if len(upto):
                    prev_prices[p.symbol] = float(upto.iloc[-1])
        acct.start_day(bar_day.date(), self.equity(positions, prev_prices), self.cash(positions))

        # ---- manage your open positions, candle by candle since the last run
        profile = self.s.profile_for(acct.equity)
        pos_lines, closed_lines = [], []
        keep: list[Position] = []
        for pos in positions:
            df = frames.get(pos.symbol)
            if df is None:
                pos_lines.append(f"- {pos.symbol}: no fresh data today - keep your GTT stop at {_f(pos.stop_price)}")
                keep.append(pos)
                continue
            start = pd.Timestamp(last_bar.get(pos.symbol) or pos.entry_date.isoformat())
            new_bars = df[df.index > start]
            closed = False
            actions: list[str] = []
            for ts, row in new_bars.iterrows():
                o, h, lo = float(row["open"]), float(row["high"]), float(row["low"])
                if pos.pending_exit_reason:  # you were told to sell at this open
                    t = self._close(pos, o, pos.pending_exit_reason + " (assumed sold at open)", ts.date())
                    closed_lines.append(self._trade_line(t, "Assumed you sold at the open as advised"))
                    closed = True
                    break
                stop_label = "trailing stop" if pos.stop_price >= pos.entry_price else "stop-loss"
                exit_px, why = None, None
                if o <= pos.stop_price:
                    exit_px, why = o, f"{stop_label} (gap down)"
                elif lo <= pos.stop_price:
                    exit_px, why = pos.stop_price, stop_label
                elif h >= pos.target_price:
                    exit_px, why = max(o, pos.target_price), "target reached"
                if exit_px is not None:
                    t = self._close(pos, exit_px, why + " (GTT)", ts.date())
                    closed_lines.append(self._trade_line(t, "Your GTT should have executed - check the app"))
                    closed = True
                    break
                per_share = (pos.entry_costs + self.costs.sell_cost(pos.quantity * pos.entry_price)) / max(pos.quantity, 1)
                ev_days = None
                for d in events.get(pos.symbol, []):
                    if d >= ts.date():
                        n = len(pd.bdate_range(ts.date(), d)) - 1
                        ev_days = n if ev_days is None else min(ev_days, n)
                res = manage_position(pos, row, regime, self.exits_cfg, False, ev_days, per_share)
                if res.new_stop is not None:
                    pos.stop_price = res.new_stop
                    actions = [a for a in actions if not a.startswith("MODIFY")]
                    actions.append(f"MODIFY your GTT: stop trigger {_f(pos.stop_price)}, "
                                   f"stop limit {_f(self._stop_limit(pos.stop_price))}, target {_f(pos.target_price)}")
                if res.exit_reason:
                    pos.pending_exit_reason = res.exit_reason
                last_bar[pos.symbol] = ts.date().isoformat()
            if closed:
                continue
            keep.append(pos)
            px = float(df["close"].iloc[-1])
            head = (f"- {pos.symbol} {pos.quantity} sh @ {_f(pos.entry_price)}, now {_f(px)} "
                    f"({pos.r_multiple(px):+.2f}R), stop {_f(pos.stop_price)}")
            if pos.pending_exit_reason:
                actions = [f"SELL all {pos.quantity} at tomorrow's open (market order) - {pos.pending_exit_reason}. "
                           f"Cancel the GTT first. Then record: sold {pos.symbol} <price>"]
            pos_lines.append(head + ("".join(f"\n   >> {a}" for a in actions) if actions else "\n   >> no change, keep your GTT"))
        positions = keep

        prices = {s: float(df["close"].iloc[-1]) for s, df in fresh.items()}
        acct.mark(self.equity(positions, prices), self.cash(positions))
        breaker = check_circuit_breakers(acct, self.s.section("circuit_breakers"), False, events_ok)

        # ---- new trades for tomorrow
        plan_lines: list[str] = []
        new_plans: list[TradePlan] = []
        near: list[str] = []
        if stale:
            plan_lines.append(f"Data is old (last candle {bar_day:%d %b}) - no new trades.")
        elif not regime.allows_entries:
            plan_lines.append(f"Market regime {regime.value.upper()} - no new trades (cash is a position).")
        elif not breaker.allow_entries:
            plan_lines.append("No new trades: " + "; ".join(breaker.reasons))
        else:
            cands = scan(frames, bar_day, self.s, rs_panel, {s: uni.get(s, "UNKNOWN") for s in frames}, events)
            rets = returns_panel(frames)
            held = [p.symbol for p in positions]
            pseudo = list(positions)
            limit = int(self.s.get("risk.max_new_entries_per_day", 2))
            for c in cands:
                corr = correlations_with(rets, c.plan.symbol, held, bar_day)
                adv = float(frames[c.plan.symbol]["vol_avg50"].iloc[-1])
                d = self.risk.evaluate(c.plan, acct, pseudo, regime, adv, corr, breaker=breaker)
                if not d.approved:
                    if len(near) < 3:
                        near.append(f"{c.plan.symbol} ({c.card.total:g}): {d.reasons[-1]}")
                    continue
                c.plan.quantity = d.quantity
                acct.cash -= d.position_value + d.est_costs  # don't promise the same cash twice
                new_plans.append(c.plan)
                pseudo.append(Position(c.plan.symbol, c.plan.setup.value, bar_day.date(), c.plan.trigger_price,
                                       d.quantity, c.plan.stop_price, c.plan.stop_price, c.plan.target_price, c.plan.sector))
                acct.entries_today += 1
                plan_lines.append(self._plan_text(len(new_plans), c, d))
                if len(new_plans) >= limit:
                    break
            if not new_plans:
                plan_lines.append("No setup passed every rule today. Waiting is part of the strategy.")
        acct.entries_today = 0
        acct.cash = self.cash(positions)

        # ---- save + report
        self.save_positions(positions, last_bar)
        self.plans_path.write_text(json.dumps({"for_day_after": bar_day.date().isoformat(),
                                               "plans": [p.to_dict() for p in new_plans]}, indent=2))
        meta["last_bar"] = bar_day.date().isoformat()
        self.save_account(acct, meta)

        lines = [
            f"SWING SIGNALS - data to {bar_day:%a %d %b %Y}",
            f"Market: {regime.value.upper()}" + (f" | breadth {b_today:.0f}%" if b_today is not None else ""),
            f"Equity {_f(acct.equity)} (from peak -{acct.drawdown_pct:.1f}%) | risk profile {profile.name}",
        ]
        if breaker.reasons:
            lines.append("Risk controls: " + "; ".join(breaker.reasons))
        if closed_lines:
            lines += ["", "CLOSED"] + closed_lines
        lines += ["", f"YOUR POSITIONS ({len(positions)})"] + (pos_lines or ["- none"])
        lines += ["", "NEW TRADES FOR NEXT SESSION"] + plan_lines
        if near:
            lines += ["", "Near misses (not traded):"] + [f"- {x}" for x in near]
        lines += ["", "Rules: always set the GTT stop right after you buy. Never move a stop down."]
        msg = "\n".join(lines)
        if send and self.n:
            self.sent_ok = self.n.send(msg)
        return msg

    # ------------------------------------------------------------- formatting
    def _stop_limit(self, stop: float) -> float:
        return round(stop * (1 - float(self.exec.get("stop_limit_buffer_pct", 5.0)) / 100), 2)

    def _plan_text(self, i, cand, decision) -> str:
        p = cand.plan
        return (
            f"{i}) {p.symbol} - {p.setup.value}, score {cand.card.total:g}/100 ({p.notes})\n"
            f"   STEP 1 (after 9:30 AM): Stop-Limit BUY, Delivery/CNC, qty {p.quantity}, "
            f"trigger {_f(p.trigger_price)}, limit {_f(p.limit_price)}. Day order - expires by itself if not triggered.\n"
            f"   STEP 2 (as soon as it fills): GTT OCO SELL {p.quantity} - stop trigger {_f(p.stop_price)} "
            f"(limit {_f(self._stop_limit(p.stop_price))}), target {_f(p.target_price)}.\n"
            f"   STEP 3: record it -> took {p.symbol} {p.quantity} <fill price>\n"
            f"   Risk {_f(decision.risk_amount)}, reward:risk {p.reward_risk:.1f}, est. charges {_f(decision.est_costs)}"
        )

    @staticmethod
    def _trade_line(t: ClosedTrade, note: str) -> str:
        return (f"- {t.symbol}: {t.exit_reason} @ {_f(t.exit_price)} -> P&L {_f(t.pnl)} ({t.r_multiple:+.2f}R). {note}; "
                f"if the real price differed, fix it in closed_trades.csv")


def journal_summary(desk: SignalDesk) -> str:
    positions, _ = desk.positions()
    t = desk.closed_trades()
    acct = desk.account()
    lines = [f"Capital {_f(desk.capital)} | realised P&L {_f(desk.realized_pnl())} | peak {_f(acct.peak_equity)} | "
             f"drawdown {acct.drawdown_pct:.1f}% | losing streak {acct.consecutive_losses}"]
    for p in positions:
        lines.append(f"  OPEN {p.symbol} x{p.quantity} @ {p.entry_price:.2f} stop {p.stop_price:.2f} target {p.target_price:.2f}"
                     + (f"  -> SELL at open: {p.pending_exit_reason}" if p.pending_exit_reason else ""))
    if len(t):
        lines.append(f"Closed trades {len(t)} | win rate {(t.pnl > 0).mean() * 100:.0f}% | avg {t.r_multiple.mean():+.2f}R | "
                     f"total {_f(t.pnl.sum())}")
    return "\n".join(lines)


__all__ = ["SignalDesk", "SignalError", "journal_summary", "Regime"]
