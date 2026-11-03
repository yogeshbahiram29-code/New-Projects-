"""Live / paper trading engine and daily scheduler.

Daily cycle (exchange time):
  08:45 pre_market  - broker login, reconcile with broker, make sure EVERY
                      position has an active broker-side stop
  09:17 gap_guard   - execute exits queued last night; dump anything that
                      opened below its stop
  09:30 entries     - place tonight's approved trade plans (stop-limit BUY)
  every 2 min       - monitor: fills -> immediate OCO stop+target; exits;
                      kill switch; intraday daily-loss breaker
  15:20 cancel      - cancel unfilled entries (protect any partial fill)
  16:00 eod         - fresh data, regime, manage stops, scan, size, plan
                      tomorrow, WhatsApp daily report

Safety rules enforced here:
  * never holds a position without a stop (OCO -> day stop -> market exit)
  * never touches shares the bot didn't buy (your own investments are safe)
  * never commits more than live_capital_limit + its own realised profit
  * at-most-once order placement (intent recorded before every entry)
  * kill switch file (state/KILL) blocks every new order instantly
"""
from __future__ import annotations

import logging
import time as _time
import traceback
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .brokers.base import CANCELLED, FILLED, REJECTED, Broker, BrokerError, NotSupported
from .config import Settings
from .costs import CostModel
from .data import events_calendar, load_full_universe
from .exits import partial_price, partial_quantity
from .models import ClosedTrade, Position, Regime, TradePlan
from .notify import Notifier
from .risk import AccountState, RiskEngine, check_circuit_breakers
from .scanner import correlations_with, returns_panel
from .store import Store
from .strategies import get_strategy

log = logging.getLogger("swingbot.engine")


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class LiveEngine:
    def __init__(self, settings: Settings, broker: Broker, notifier: Notifier, store: Store, data=None, now_fn=None):
        self.s = settings
        self.b = broker
        self.n = notifier
        self.db = store
        self.data = data  # CachedData
        self.tz = ZoneInfo(settings.get("timezone", "Asia/Kolkata"))
        self._now = now_fn or (lambda: datetime.now(self.tz))
        self.costs = CostModel.from_settings(settings.section("costs"), settings.get("broker", "paper"))
        self.risk = RiskEngine(settings, self.costs)
        self.exec = settings.section("execution")
        self.exits_cfg = settings.section("exits")
        self.sched = settings.section("schedule")
        self.state_dir = Path(settings.get("paths.state_dir", "state"))
        self.connected = False
        self.failures = 0

    # ------------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self._now()

    def today(self) -> date:
        return self.now().date()

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d.isoformat() not in set(map(str, self.sched.get("holidays", []) or []))

    def next_trading_day(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    @property
    def kill_file(self) -> Path:
        return self.state_dir / "KILL"

    def kill_active(self) -> bool:
        return self.kill_file.exists()

    def capital(self) -> float:
        key = "live_capital_limit" if self.s.mode == "live" else "initial_capital"
        return float(self.s.get(key, self.s.get("initial_capital", 10000)))

    def account(self) -> AccountState:
        a = self.db.account()
        if a is None:
            cap = self.capital()
            a = AccountState(equity=cap, cash=cap)
            self.db.put("bot_cash", cap)
            self.db.save_account(a)
        return a

    def bot_cash(self) -> float:
        return float(self.db.get("bot_cash", self.capital()))

    def last_prices(self) -> dict[str, float]:
        return self.db.get("last_prices", {}) or {}

    def bot_equity(self, prices: dict[str, float] | None = None) -> float:
        prices = prices or self.last_prices()
        eq = self.bot_cash()
        for p in self.db.positions():
            eq += p.quantity * prices.get(p.symbol, p.entry_price)
        return eq

    def ensure_connected(self) -> bool:
        if self.connected:
            return True
        try:
            self.b.connect()
            self.connected = True
            return True
        except BrokerError as e:
            if self.b.daily_login_required():
                if self.db.get("login_prompt_day") != self.today().isoformat():
                    url = self.b.login_url() if hasattr(self.b, "login_url") else "(see README)"
                    self.n.send(f"Daily Zerodha login needed. Tap: {url}\nThen run: python -m swingbot kite-login <request_token>")
                    self.db.put("login_prompt_day", self.today().isoformat())
            else:
                self.n.send(f"Broker connection failed: {e}", urgent=True)
            return False

    def _ref(self) -> str:
        seq = int(self.db.get("ref_seq", 0)) + 1
        self.db.put("ref_seq", seq)
        return f"{self.today():%y%m%d}{seq % 10000:04d}"

    def _stop_limit(self, stop: float) -> float:
        return stop * (1 - float(self.exec.get("stop_limit_buffer_pct", 5.0)) / 100)

    # ------------------------------------------------------------- protection
    def protect(self, pos: Position, last_price: float) -> bool:
        """Put a broker-side stop on ``pos``. Never leaves a position naked."""
        for attempt in range(3):
            try:
                pos.protection_id = self.b.place_protection(
                    pos.symbol, pos.quantity, pos.stop_price, self._stop_limit(pos.stop_price),
                    pos.target_price, last_price, self._ref(),
                )
                self.db.save_position(pos)
                return True
            except BrokerError as e:
                log.warning("OCO for %s failed (attempt %d): %s", pos.symbol, attempt + 1, e)
                _time.sleep(2 * (attempt + 1))
        try:  # fallback: plain stop-loss order for today; OCO retried at next pre-market
            oid = self.b.place_day_stop(pos.symbol, pos.quantity, pos.stop_price, self._stop_limit(pos.stop_price), self._ref())
            pos.protection_id = None
            self.db.save_position(pos)
            self.db.put(f"day_stop:{pos.symbol}", oid)
            self.n.send(f"{pos.symbol}: OCO rejected, placed a DAY stop-loss at ₹{pos.stop_price:.2f} instead. Will retry OCO tomorrow.", urgent=True)
            return True
        except BrokerError as e:
            self.n.send(f"{pos.symbol}: could not place ANY stop-loss ({e}). Exiting at market to stay safe.", urgent=True)
            try:
                oid = self.b.sell_market(pos.symbol, pos.quantity, self._ref())
                self._queue_exit_order(oid, pos.symbol, "no stop possible - safety exit")
            except BrokerError as e2:
                self.n.send(f"{pos.symbol}: SAFETY EXIT FAILED ({e2}). Close it manually in the app NOW.", urgent=True)
            return False

    def _queue_exit_order(self, oid: str, symbol: str, reason: str) -> None:
        q = self.db.get("exit_orders", {}) or {}
        q[oid] = {"symbol": symbol, "reason": reason}
        self.db.put("exit_orders", q)

    def ensure_protection(self) -> None:
        prices = self.last_prices()
        for pos in self.db.positions():
            try:
                if pos.protection_id and self.b.protection_active(pos.protection_id):
                    continue
            except BrokerError as e:
                log.warning("protection check %s: %s", pos.symbol, e)
            held = self.b.holdings().get(pos.symbol)
            if held is None or held.quantity <= 0:
                continue  # closed - reconcile handles it
            self.protect(pos, prices.get(pos.symbol, pos.entry_price))

    # ------------------------------------------------------------- bookkeeping
    def finalize(self, pos: Position, exit_price: float, reason: str, qty: int | None = None) -> None:
        qty = pos.quantity if qty is None else qty
        value = qty * exit_price
        sell_cost = self.costs.sell_cost(value)
        entry_share = pos.entry_costs * qty / max(pos.initial_quantity, 1)
        pnl = value - sell_cost - qty * pos.entry_price - entry_share
        self.db.put("bot_cash", self.bot_cash() + value - sell_cost)
        if qty < pos.quantity:  # partial exit
            pos.realized_pnl += pnl
            pos.quantity -= qty
            pos.partial_taken = True
            self.db.save_position(pos)
            self.n.send(f"PARTIAL SELL {qty} {pos.symbol} @ ₹{exit_price:.2f} ({reason}), P&L ₹{pnl:,.0f}")
            return
        total = pos.realized_pnl + pnl
        risk_money = pos.initial_risk_per_share * pos.initial_quantity
        t = ClosedTrade(
            symbol=pos.symbol, setup=pos.setup, entry_date=pos.entry_date, exit_date=self.today(),
            entry_price=round(pos.entry_price, 2), exit_price=round(exit_price, 2), quantity=pos.initial_quantity,
            initial_stop=pos.initial_stop, pnl=round(total, 2), costs=round(pos.entry_costs + sell_cost, 2),
            r_multiple=round(total / risk_money, 2) if risk_money else 0.0, exit_reason=reason,
            bars_held=pos.bars_held, sector=pos.sector,
            return_pct=round(total / pos.equity_at_entry * 100, 3) if pos.equity_at_entry else 0.0,
        )
        self.db.add_trade(t)
        self.db.delete_position(pos.symbol)
        acct = self.account()
        acct.mark(self.bot_equity())
        r = self.s.section("risk")
        acct.on_trade_closed(total, int(r.get("losing_streak_reduce_after", 3)), int(r.get("losing_streak_restore_after_wins", 2)))
        self.db.save_account(acct)
        self.db.log("exit", f"{pos.symbol} {reason} pnl {total:.0f}")
        self.n.send(f"SOLD {pos.symbol} @ ₹{exit_price:.2f} - {reason}. P&L ₹{total:,.0f} ({t.r_multiple:+.2f}R). Equity ₹{acct.equity:,.0f}")

    def reconcile(self) -> None:
        """Compare the bot's positions with the broker; the broker wins."""
        holdings = self.b.holdings()
        prices = self.last_prices()
        for pos in self.db.positions():
            held = holdings.get(pos.symbol)
            qty = held.quantity if held else 0
            if (pos.pending_exit_reason or "").startswith("SENT"):
                continue  # an exit order is in flight; the monitor finalises it with the real fill price
            if qty <= 0:
                try:
                    still_protected = bool(pos.protection_id) and self.b.protection_active(pos.protection_id)
                except BrokerError:
                    still_protected = True  # unsure -> don't guess
                if still_protected:
                    self.n.send(f"{pos.symbol}: broker shows 0 shares but its stop order is still active. "
                                "Not closing it in the journal - please check the app.", urgent=True)
                    continue
                fill = self.b.protection_fill(pos.protection_id) if pos.protection_id else None
                if fill:
                    px, leg = fill
                    reason = ("trailing stop" if pos.stop_price >= pos.entry_price else "stop-loss") if leg.startswith("stop") else "target reached"
                else:
                    last = held.last_price if held and held.last_price else prices.get(pos.symbol)
                    near_stop = last is None or abs(last - pos.stop_price) <= abs(last - pos.target_price)
                    px = pos.stop_price if near_stop else pos.target_price
                    reason = ("stop-loss" if near_stop else "target reached") + " (price estimated - check contract note)"
                self.finalize(pos, px, reason)
            elif qty < pos.quantity:
                self.n.send(f"{pos.symbol}: broker shows {qty} shares, bot expected {pos.quantity}. Adjusting.", urgent=True)
                pos.quantity = qty
                self.db.save_position(pos)

    # ------------------------------------------------------------------ jobs
    def job_pre_market(self) -> None:
        if self.b.name != "paper":
            self.connected = False  # broker sessions expire overnight: always log in fresh
        if not self.ensure_connected():
            return
        self.reconcile()
        self.ensure_protection()
        acct = self._start_day()
        day, plans = self.db.plans()
        n_plans = len(plans) if day == self.today().isoformat() else 0
        pos = self.db.positions()
        self.n.send(
            f"Good morning. Equity ₹{acct.equity:,.0f}, {len(pos)} open position(s), {n_plans} trade plan(s) for today."
            + (" KILL SWITCH ON." if self.kill_active() else "")
            + (" HALTED - needs review." if acct.halted else "")
        )

    def job_gap_guard(self) -> None:
        if not self.ensure_connected():
            return
        try:
            quotes = self.b.ltp([p.symbol for p in self.db.positions()]) if self.db.positions() else {}
        except (NotSupported, BrokerError):  # e.g. free API plans without live quotes
            quotes = {}
        for pos in self.db.positions():
            reason = pos.pending_exit_reason
            ltp = quotes.get(pos.symbol)
            if not reason and ltp is not None and ltp < pos.stop_price:
                reason = "opened below stop (gap down)"
            if not reason:
                continue
            try:
                if pos.protection_id:
                    self.b.cancel_protection(pos.protection_id)
                oid = self.b.sell_market(pos.symbol, pos.quantity, self._ref())
                self._queue_exit_order(oid, pos.symbol, reason)
                pos.pending_exit_reason = f"SENT: {reason}"
                self.db.save_position(pos)
                self.db.log("exit_order", f"{pos.symbol} {reason}")
            except BrokerError as e:
                self.n.send(f"{pos.symbol}: exit order failed ({e}). Stop-loss still in place.", urgent=True)

    def _pending_as_positions(self) -> list[Position]:
        out = []
        for _, plan in self.db.pending():
            out.append(Position(plan.symbol, plan.setup.value, self.today(), plan.trigger_price, plan.quantity,
                                plan.stop_price, plan.stop_price, plan.target_price, plan.sector))
        return out

    def _start_day(self) -> AccountState:
        acct = self.account()
        acct.start_day(self.today(), self.bot_equity(), self.bot_cash())
        self.db.save_account(acct)
        return acct

    def job_entries(self) -> None:
        self._start_day()
        if self.kill_active():
            self.n.send("Kill switch is ON - no new entries today.")
            return
        if not self.ensure_connected():
            return
        now = self.now()
        cutoff = datetime.combine(now.date(), _t(self.sched.get("market_close", "15:30")), self.tz) - timedelta(
            minutes=int(self.exec.get("no_entry_last_minutes", 10)) + 30)
        if now > cutoff:
            self.n.send("Entry window missed today (bot started late) - no entries placed.")
            return
        day, plans = self.db.plans()
        if day != self.today().isoformat() or not plans:
            return
        regime = Regime(self.db.get("regime", "neutral"))
        acct = self.account()
        acct.mark(self.bot_equity(), min(self.bot_cash(), self.b.available_cash()))
        breaker = check_circuit_breakers(acct, self.s.section("circuit_breakers"), self.kill_active(),
                                         bool(self.db.get("events_ok", False)))
        holdings = self.b.holdings()
        bot_syms = {p.symbol for p in self.db.positions()}
        try:
            quotes = self.b.ltp([p.symbol for p in plans])
        except (NotSupported, BrokerError):  # e.g. free API plans without live quotes
            quotes = {}
        corr_map = self.db.get("plan_corr", {}) or {}
        adv_map = self.db.get("plan_adv", {}) or {}
        placed = []
        for plan in plans:
            if plan.symbol in holdings and plan.symbol not in bot_syms:
                self.db.log("skip", f"{plan.symbol}: you already hold it outside the bot")
                continue
            ltp = quotes.get(plan.symbol)
            if ltp is not None and ltp > plan.trigger_price * (1 + float(self.exec.get("max_chase_pct", 2)) / 100):
                self.db.log("skip", f"{plan.symbol}: already {ltp:.2f}, too far above trigger (no chasing)")
                continue
            intent_key = f"intent:{self.today()}:{plan.symbol}"
            if self.db.get(intent_key):
                self.n.send(f"{plan.symbol}: entry was already attempted today - not repeating (check app if unsure).")
                continue
            decision = self.risk.evaluate(plan, acct, self.db.positions() + self._pending_as_positions(), regime,
                                          adv_map.get(plan.symbol), corr_map.get(plan.symbol), breaker=breaker)
            if not decision.approved:
                self.db.log("reject", f"{plan.symbol}: {decision.reasons[-1]}")
                continue
            plan.quantity = decision.quantity
            self.db.put(intent_key, True)  # at-most-once: recorded BEFORE the order is sent
            try:
                oid = self.b.place_entry(plan.symbol, plan.quantity, plan.trigger_price, plan.limit_price, self._ref())
            except BrokerError as e:
                self.n.send(f"{plan.symbol}: entry order rejected ({e})", urgent=True)
                continue
            self.db.add_pending(oid, plan)
            acct.entries_today += 1
            acct.orders_today += 1
            placed.append(f"{plan.symbol} x{plan.quantity} above ₹{plan.trigger_price:.2f} (stop ₹{plan.stop_price:.2f}, target ₹{plan.target_price:.2f})")
        self.db.save_account(acct)
        if placed:
            self.n.send("Entry orders placed (fill only if price rises above trigger):\n- " + "\n- ".join(placed))

    def _on_entry_fill(self, oid: str, plan: TradePlan, qty: int, avg: float) -> None:
        cost = self.costs.buy_cost(qty * avg)
        acct = self.account()
        pos = Position(
            symbol=plan.symbol, setup=plan.setup.value, entry_date=self.today(), entry_price=avg, quantity=qty,
            initial_stop=plan.stop_price, stop_price=plan.stop_price, target_price=plan.target_price,
            sector=plan.sector, entry_costs=cost, equity_at_entry=acct.equity,
            trailing_only=plan.trailing_only, highest_close=avg,
        )
        self.db.put("bot_cash", self.bot_cash() - qty * avg - cost)
        self.db.save_position(pos)
        self.db.remove_pending(oid)
        self.n.send(f"BOUGHT {qty} {plan.symbol} @ ₹{avg:.2f} ({plan.setup.value}, score {plan.score:g}). "
                    f"Stop ₹{plan.stop_price:.2f}, target ₹{plan.target_price:.2f}. Placing broker stop now.")
        self.protect(pos, avg)

    def job_monitor(self) -> None:
        if not self.ensure_connected():
            return
        if self.kill_active() and self.db.pending():
            for oid, _ in self.db.pending():
                try:
                    self.b.cancel_order(oid)
                except BrokerError:
                    pass
                self.db.remove_pending(oid)
            self.n.send("Kill switch: all pending entry orders cancelled. Stops stay in place.", urgent=True)

        for oid, plan in self.db.pending():
            st = self.b.order_status(oid)
            if st.status == FILLED:
                self._on_entry_fill(oid, plan, st.filled_qty or plan.quantity, st.avg_price or plan.trigger_price)
            elif st.status in (CANCELLED, REJECTED):
                self.db.remove_pending(oid)
                if st.status == REJECTED:
                    self.n.send(f"{plan.symbol}: entry rejected by broker - {st.message}")

        exits = self.db.get("exit_orders", {}) or {}
        for oid, info in list(exits.items()):
            st = self.b.order_status(oid)
            if st.status in (FILLED, CANCELLED, REJECTED):
                exits.pop(oid)
                pos = next((p for p in self.db.positions() if p.symbol == info["symbol"]), None)
                if pos and st.status == FILLED:
                    self.finalize(pos, st.avg_price, info["reason"], qty=min(st.filled_qty, pos.quantity) or None)
                elif pos:
                    self.n.send(f"{pos.symbol}: exit order {st.status} ({st.message}). Re-protecting.", urgent=True)
                    pos.pending_exit_reason = None
                    self.protect(pos, self.last_prices().get(pos.symbol, pos.entry_price))
        self.db.put("exit_orders", exits)

        positions = self.db.positions()
        if positions:
            self.reconcile()
            try:
                quotes = self.b.ltp([p.symbol for p in self.db.positions()])
            except (NotSupported, BrokerError):  # e.g. free API plans without live quotes
                quotes = {}
            if quotes:
                lp = self.last_prices()
                lp.update(quotes)
                self.db.put("last_prices", lp)
            self._check_lost_protection()
            self._maybe_partials(quotes)
            acct = self.account()
            acct.mark(self.bot_equity())
            day_loss = -acct.pct_change(acct.day_start_equity)
            if day_loss >= float(self.s.get("circuit_breakers.daily_loss_pct", 2)) and self.db.pending():
                for oid, _ in self.db.pending():
                    try:
                        self.b.cancel_order(oid)
                    except BrokerError:
                        pass
                    self.db.remove_pending(oid)
                self.n.send(f"Daily loss {day_loss:.1f}% - pending entries cancelled for today.", urgent=True)
            self.db.save_account(acct)

    def _check_lost_protection(self) -> None:
        """Stop order fired but shares are still held (e.g. gap below the stop's limit price).

        Acts only if the condition is seen on two consecutive monitor runs, so a
        normal stop-out that is still settling is not mistaken for a problem.
        """
        for pos in self.db.positions():
            if (pos.pending_exit_reason or "").startswith("SENT") or not pos.protection_id:
                continue
            try:
                active = self.b.protection_active(pos.protection_id)
            except BrokerError:
                continue
            seen_key = f"lost_seen:{pos.symbol}"
            if active:
                self.db.put(seen_key, None)
                continue
            if not self.db.get(seen_key):
                self.db.put(seen_key, self.now().isoformat())
                continue
            self.db.put(seen_key, None)
            self.n.send(f"{pos.symbol}: stop order fired but shares are still held (price gapped through the stop's "
                        "limit?). Selling at market.", urgent=True)
            try:
                oid = self.b.sell_market(pos.symbol, pos.quantity, self._ref())
                self._queue_exit_order(oid, pos.symbol, "stop triggered - market exit")
                pos.pending_exit_reason = "SENT: stop triggered - market exit"
                self.db.save_position(pos)
            except BrokerError as e:
                self.n.send(f"{pos.symbol}: market exit FAILED ({e}). Close it manually in the app NOW.", urgent=True)

    def _maybe_partials(self, quotes: dict[str, float]) -> None:
        profile = self.s.profile_for(self.account().equity)
        if not profile.partial_exit_enabled or not quotes or not get_strategy(self.s).allows_partials:
            return
        for pos in self.db.positions():
            ltp = quotes.get(pos.symbol)
            if pos.partial_taken or pos.quantity < 2 or ltp is None or ltp < partial_price(pos, self.exits_cfg):
                continue
            q = partial_quantity(pos, self.exits_cfg)
            try:
                oid = self.b.sell_market(pos.symbol, q, self._ref())
                self._queue_exit_order(oid, pos.symbol, f"partial profit at +{self.exits_cfg.get('partial_at_r', 2)}R")
                pos.partial_taken = True
                pos.stop_price = max(pos.stop_price, pos.entry_price)
                if pos.protection_id:
                    pos.protection_id = self.b.modify_protection(pos.protection_id, pos.symbol, pos.quantity - q, pos.stop_price,
                                                                 self._stop_limit(pos.stop_price), pos.target_price, ltp)
                self.db.save_position(pos)
            except BrokerError as e:
                self.n.send(f"{pos.symbol}: partial exit failed ({e})")

    def job_cancel_unfilled(self) -> None:
        if self.b.name == "paper":
            return  # paper fills are simulated at EOD; the paper broker expires DAY orders itself
        if not self.ensure_connected():
            return
        for oid, plan in self.db.pending():
            st = self.b.order_status(oid)
            if st.status == FILLED:
                self._on_entry_fill(oid, plan, st.filled_qty, st.avg_price)
                continue
            try:
                self.b.cancel_order(oid)
            except BrokerError as e:
                log.warning("cancel %s: %s", oid, e)
            if st.filled_qty > 0:
                self._on_entry_fill(oid, plan, st.filled_qty, st.avg_price)
            else:
                self.db.remove_pending(oid)

    # ------------------------------------------------------------------- EOD
    def job_eod(self) -> None:
        self._start_day()
        today = self.today()
        uni = load_full_universe(self.s)
        symbols = sorted(set(uni) | {p.symbol for p in self.db.positions()})
        bench = self.s.get("regime.benchmark", "NIFTY50")
        raw = self.data.load(symbols + [bench], int(self.s.get("data.history_days", 600)), today)
        index_df = raw.pop(bench, None)
        if index_df is None or index_df.index[-1].date() < today:
            self.n.send("EOD: today's index data not available yet (stale data). No new plans; stops unchanged.", urgent=True)
            return
        fresh = {s: df for s, df in raw.items() if df.index[-1].date() >= today}
        stale = [p.symbol for p in self.db.positions() if p.symbol not in fresh]

        # paper trading: simulate today's fills from today's candles
        if self.b.name == "paper":
            self.b.connect()
            bars = {s: df.iloc[-1][["open", "high", "low", "close"]].astype(float).to_dict() for s, df in fresh.items()}
            for line in self.b.simulate_day(bars, today):
                self.db.log("paper_fill", line)
            self.connected = True
            self.job_monitor()

        lp = self.last_prices()
        lp.update({s: float(df["close"].iloc[-1]) for s, df in fresh.items()})
        self.db.put("last_prices", lp)

        strat = get_strategy(self.s)
        prep = strat.prepare(fresh, index_df)
        frames = prep.frames
        regime = strat.current_regime(index_df, prep)
        self.db.put("regime", regime.value)
        events, events_ok = events_calendar(self.s)
        self.db.put("events_ok", events_ok)

        # manage open positions
        acct = self.account()
        profile = self.s.profile_for(acct.equity)
        notes = []
        for pos in self.db.positions():
            if pos.symbol not in frames:
                continue
            row = frames[pos.symbol].iloc[-1]
            ev = None
            for d in events.get(pos.symbol, []):
                if d >= today:
                    n_days = len(pd.bdate_range(today, d)) - 1
                    ev = n_days if ev is None else min(ev, n_days)
            per_share = (pos.entry_costs + self.costs.sell_cost(pos.quantity * pos.entry_price)) / max(pos.quantity, 1)
            res = strat.manage(pos, row, regime, profile.partial_exit_enabled and strat.allows_partials, ev, per_share)
            if res.new_stop is not None:
                try:
                    if pos.protection_id and self.ensure_connected():
                        pos.protection_id = self.b.modify_protection(pos.protection_id, pos.symbol, pos.quantity, res.new_stop,
                                                                     self._stop_limit(res.new_stop), pos.target_price, float(row["close"]))
                    pos.stop_price = res.new_stop
                    notes.append(f"{pos.symbol}: stop raised to ₹{res.new_stop:.2f}")
                except BrokerError as e:
                    notes.append(f"{pos.symbol}: stop update FAILED ({e}) - old stop still active")
            if res.exit_reason and not (pos.pending_exit_reason or "").startswith("SENT"):
                pos.pending_exit_reason = res.exit_reason
                notes.append(f"{pos.symbol}: will exit at tomorrow's open - {res.exit_reason}")
            notes.extend(f"{pos.symbol}: {n}" for n in (res.notes or []))
            self.db.save_position(pos)

        equity = self.bot_equity()
        acct.mark(equity)
        breaker = check_circuit_breakers(acct, self.s.section("circuit_breakers"), self.kill_active(), events_ok)
        self.db.save_account(acct)

        # scan + size tomorrow's plans
        plans: list[TradePlan] = []
        reject_notes: list[str] = []
        when = index_df.index[-1]
        if regime.allows_entries and breaker.allow_entries and not self.kill_active():
            sectors = {s: uni.get(s, "UNKNOWN") for s in frames}
            cands = strat.scan(prep, when, sectors, events)
            rets = returns_panel(frames)
            held = [p.symbol for p in self.db.positions()]
            corr_map, adv_map = {}, {}
            max_plans = 2 * int(self.s.get("risk.max_new_entries_per_day", 2))
            for c in cands:
                corr = correlations_with(rets, c.plan.symbol, held, when)
                adv = float(frames[c.plan.symbol]["vol_avg50"].iloc[-1])
                d = self.risk.evaluate(c.plan, acct, self.db.positions(), regime, adv, corr, breaker=breaker)
                if d.approved:
                    c.plan.quantity = d.quantity
                    plans.append(c.plan)
                    corr_map[c.plan.symbol], adv_map[c.plan.symbol] = corr, adv
                    if len(plans) >= max_plans:
                        break
                elif len(reject_notes) < 5:
                    reject_notes.append(f"{c.plan.symbol} ({c.card.total:g}): {d.reasons[-1]}")
            self.db.put("plan_corr", corr_map)
            self.db.put("plan_adv", adv_map)
        self.db.save_plans(plans, self.next_trading_day(today).isoformat())
        self._daily_report(acct, regime, breaker, plans, notes, reject_notes, stale)

    def _daily_report(self, acct, regime, breaker, plans, notes, reject_notes, stale) -> None:
        lp = self.last_prices()
        lines = [
            f"Daily report {self.today():%d %b %Y}",
            f"Equity ₹{acct.equity:,.0f} (day {acct.pct_change(acct.day_start_equity):+.2f}%, from peak -{acct.drawdown_pct:.1f}%)",
            f"Market regime: {regime.value.upper()}",
        ]
        if breaker.reasons:
            lines.append("Risk controls: " + "; ".join(breaker.reasons))
        pos = self.db.positions()
        lines.append(f"Open positions ({len(pos)}):")
        for p in pos:
            px = lp.get(p.symbol, p.entry_price)
            lines.append(f"- {p.symbol} x{p.quantity} @ ₹{p.entry_price:.2f} now ₹{px:.2f} ({p.r_multiple(px):+.2f}R), stop ₹{p.stop_price:.2f}")
        if notes:
            lines.append("Changes:\n- " + "\n- ".join(notes))
        if plans:
            lines.append("Tomorrow's plans:")
            for p in plans:
                lines.append(f"- {p.symbol} {p.setup.value} score {p.score:g}: buy {p.quantity} above ₹{p.trigger_price:.2f}, stop ₹{p.stop_price:.2f}, target ₹{p.target_price:.2f} (R:R {p.reward_risk:.1f})")
        else:
            lines.append("Tomorrow's plans: none (no setup passed every rule)")
        if reject_notes:
            lines.append("Near misses:\n- " + "\n- ".join(reject_notes))
        if stale:
            lines.append("WARNING stale data for: " + ", ".join(stale))
        if self.s.get("notifications.daily_report", True):
            self.n.send("\n".join(lines))

    # ------------------------------------------------------------- scheduler
    JOBS = [("pre_market", "pre_market"), ("gap_guard", "gap_guard"), ("entries", "entries_open"),
            ("cancel_unfilled", "cancel_unfilled"), ("eod", "eod_scan")]

    def run_job(self, name: str) -> None:
        fn = getattr(self, f"job_{name}")
        try:
            fn()
            self.failures = 0
        except Exception as e:
            self.failures += 1
            log.error("job %s failed: %s\n%s", name, e, traceback.format_exc())
            self.db.log("error", f"{name}: {e}")
            self.n.send(f"Job '{name}' failed: {type(e).__name__}: {e}", urgent=True)
            if self.failures >= 5 and not self.kill_active():
                self.kill_file.parent.mkdir(parents=True, exist_ok=True)
                self.kill_file.write_text(f"auto: {self.failures} consecutive failures at {self.now()}")
                self.n.send("5 failures in a row - kill switch turned ON automatically. Stops stay active. "
                            "Fix the issue, then run: python -m swingbot resume", urgent=True)

    def tick(self) -> None:
        now = self.now()
        d = now.date()
        if not self.is_trading_day(d):
            return
        for name, key in self.JOBS:
            at = _t(self.sched.get(key, "23:59"))
            ran_key = f"ran:{name}"
            if now.time() >= at and self.db.get(ran_key) != d.isoformat():
                self.db.put(ran_key, d.isoformat())  # mark first: never run a job twice in a day
                self.run_job(name)
        open_t, close_t = _t(self.sched.get("market_open", "09:15")), _t(self.sched.get("market_close", "15:30"))
        if open_t <= now.time() <= close_t:
            last = self.db.get("last_monitor")
            interval = int(self.exec.get("monitor_interval_seconds", 120))
            if not last or (now - datetime.fromisoformat(last)).total_seconds() >= interval:
                self.db.put("last_monitor", now.isoformat())
                self.run_job("monitor")

    def run_forever(self, poll_seconds: int = 20) -> None:
        self.n.send(f"Bot started in {self.s.mode.upper()} mode with broker {self.b.name}. Capital cap ₹{self.capital():,.0f}.")
        while True:
            self.tick()
            _time.sleep(poll_seconds)
