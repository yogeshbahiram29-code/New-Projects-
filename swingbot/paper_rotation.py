"""Forward paper test of Option B (monthly momentum rotation).

Runs once a day after the close (GitHub Actions or a laptop). It keeps a paper
portfolio in journal/paper_rotation.json and applies EXACTLY the rules that
were backtested (shared code in portfolio.py):

* last trading day of each month, on the close: rank eligible stocks, decide
  sells/buys (or go to cash when the NIFTY regime is off)
* those paper orders execute at the NEXT trading day's open, with slippage and
  full Zerodha delivery charges, whole shares only
* every day: mark to market, compare with NIFTY since the start date, check the
  drawdown limit, and send a short Telegram/WhatsApp report

Nothing here places real orders.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .config import Settings
from .costs import CostModel
from .data import load_full_universe
from .indicators import sma
from .notify import Notifier
from .portfolio import rotation_inputs, rotation_orders


def _f(x: float) -> str:
    return f"₹{x:,.0f}"


def _is_calendar_month_end(d: date) -> bool:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.month != d.month


class RotationPaperTracker:
    def __init__(self, settings: Settings, loader, notifier: Notifier | None = None, path: str | Path | None = None):
        self.s = settings
        self.loader = loader
        self.n = notifier
        self.path = Path(path or Path(settings.get("signals.dir", "journal")) / "paper_rotation.json")
        r = settings.section("rotation")
        self.top_n = int(r.get("top_n", 10))
        self.buffer = float(r.get("buffer", 2.0))
        self.regime_sma = int(r.get("regime_sma", 200))
        self.dd_limit = float(r.get("drawdown_alert_pct", 20))
        self.review_months = int(r.get("review_after_months", 3))
        u = settings.section("universe")
        self.min_value_cr = float(u.get("min_avg_traded_value_cr", 10))
        self.min_price = float(u.get("min_price", 50))
        self.costs = CostModel.from_settings(settings.section("costs"), settings.get("signals.broker_costs", "zerodha"))
        self.slip = self.costs.slippage_pct / 100
        self.capital = float(settings.get("initial_capital", 10000))
        self.sent_ok: bool | None = None

    # ------------------------------------------------------------------ state
    def load(self) -> dict | None:
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def save(self, st: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(st, indent=1, default=str))

    # ------------------------------------------------------------- execution
    def _sell(self, st, sym, px, d, why, log):
        h = st["holdings"].pop(sym)
        value = h["qty"] * px
        fee = self.costs.sell_cost(value)
        st["cash"] += value - fee
        pnl = value - fee - h["cost_basis"]
        st["closed"].append({"symbol": sym, "entry_date": h["entry_date"], "exit_date": str(d), "qty": h["qty"],
                             "entry_price": round(h["entry_price"], 2), "exit_price": round(px, 2),
                             "pnl": round(pnl, 2), "ret_pct": round(pnl / h["cost_basis"] * 100, 2), "reason": why})
        log.append(f"SELL {h['qty']} {sym} @ {px:,.2f} ({why}) P&L {_f(pnl)}")

    def _execute(self, st, d, opens, prev_closes, log):
        p = st.get("pending") or {}
        for sym in list(p.get("sells", [])):
            o = opens.get(sym)
            if sym in st["holdings"] and o is not None and not pd.isna(o):
                self._sell(st, sym, float(o) * (1 - self.slip), d, "monthly rotation", log)
        equity = st["cash"] + sum(h["qty"] * float(prev_closes.get(s, h["entry_price"])) for s, h in st["holdings"].items())
        slot = equity / self.top_n
        for sym in p.get("buys", []):
            o = opens.get(sym)
            if sym in st["holdings"] or o is None or pd.isna(o):
                continue
            px = float(o) * (1 + self.slip)
            qty = math.floor(min(slot, st["cash"] / 1.003) / px)
            if qty < 1:
                log.append(f"SKIP {sym}: one share (₹{px:,.0f}) costs more than the ₹{slot:,.0f} slot")
                continue
            fee = self.costs.buy_cost(qty * px)
            st["cash"] -= qty * px + fee
            st["holdings"][sym] = {"qty": qty, "entry_price": px, "entry_date": str(d), "cost_basis": qty * px + fee}
            log.append(f"BUY {qty} {sym} @ {px:,.2f}")
        st["pending"] = None

    def _decide(self, st, d_ts, score, regime_on, log, when_label):
        row = score.loc[d_ts].dropna() if d_ts in score.index else pd.Series(dtype=float)
        order = list(row.sort_values(ascending=False).index)
        on = bool(regime_on.get(d_ts, False))
        sells, buys = rotation_orders(order, list(st["holdings"]), on, self.top_n, self.buffer)
        st["pending"] = {"sells": sells, "buys": buys, "decided_on": str(d_ts.date()), "regime_on": on}
        st["last_decision_month"] = d_ts.strftime("%Y-%m")
        log.append(f"MONTH-END DECISION ({when_label}, {d_ts.date()}): regime {'ON' if on else 'OFF -> cash'}; "
                   f"sell {', '.join(sells) or 'nothing'}; buy {', '.join(buys) or 'nothing'} (at next open)")

    # ------------------------------------------------------------------- run
    def run(self, today: date | None = None, send: bool = True) -> str:
        today = today or date.today()
        uni = load_full_universe(self.s)
        st = self.load()
        bench = self.s.get("regime.benchmark", "NIFTY50")
        held = list((st or {}).get("holdings", {}))
        raw = self.loader.load(sorted(set(uni) | set(held)) + [bench], int(self.s.get("data.history_days", 600)), today)
        index_df = raw.pop(bench, None)
        if index_df is None or index_df.empty:
            raise RuntimeError("could not download NIFTY data - check internet / data provider")
        days = index_df.index
        bar_day = days[-1]
        if st and st.get("last_bar") == str(bar_day.date()):
            msg = f"(repeat) {st.get('last_message', 'No new market data.')}"
            if send and self.n:
                self.sent_ok = self.n.send(msg)
            return msg

        feats, score, regime_on = rotation_inputs(raw, index_df, self.min_value_cr, self.min_price, regime_sma=self.regime_sma)
        closes = pd.DataFrame({s: f["close"] for s, f in feats.items()}).reindex(days).ffill()
        opens = pd.DataFrame({s: f["open"] for s, f in feats.items()}).reindex(days)
        log: list[str] = []

        if st is None:  # first run: start in cash, adopt the strategy's current target if the regime was on
            st = {"start_date": str(bar_day.date()), "start_capital": self.capital, "start_nifty": float(index_df["close"].iloc[-1]),
                  "cash": self.capital, "holdings": {}, "pending": None, "last_decision_month": None,
                  "peak_equity": self.capital, "max_dd_pct": 0.0, "closed": [], "history": [], "last_bar": str(bar_day.date())}
            month_ends = [d for i, d in enumerate(days[:-1]) if days[i + 1].month != d.month]
            if _is_calendar_month_end(bar_day.date()):
                month_ends.append(bar_day)  # today is itself the month's last trading day
            if month_ends:
                self._decide(st, month_ends[-1], score, regime_on, log, "start-up: latest month-end")
            log.insert(0, f"Paper portfolio started with {_f(self.capital)} on {bar_day.date()}.")
            new_days = []
        else:
            new_days = [d for d in days if d > pd.Timestamp(st["last_bar"])]

        for i, d in enumerate(new_days):
            pos = days.get_loc(d)
            prev = days[pos - 1]
            # catch-up: a month-end decision was missed (holiday / missed run) -> decide on the old month-end now
            if not st.get("pending") and d.month != prev.month and st.get("last_decision_month") != prev.strftime("%Y-%m"):
                self._decide(st, prev, score, regime_on, log, "catch-up")
            if st.get("pending"):
                self._execute(st, d.date(), opens.loc[d].to_dict(), closes.loc[prev].to_dict(), log)
            for sym in [s for s, h in st["holdings"].items() if s not in feats or feats[s].index[-1] < d]:
                self._sell(st, sym, float(closes.at[d, sym]) if sym in closes.columns else st["holdings"][sym]["entry_price"],
                           d.date(), "price data ended (delisted/merged?)", log)
            equity = st["cash"] + sum(h["qty"] * float(closes.at[d, s]) for s, h in st["holdings"].items())
            st["peak_equity"] = max(st["peak_equity"], equity)
            dd = (st["peak_equity"] - equity) / st["peak_equity"] * 100
            st["max_dd_pct"] = max(st["max_dd_pct"], dd)
            st["history"].append([str(d.date()), round(equity, 2), round(float(index_df.at[d, "close"]), 2)])
            is_me = (days[pos + 1].month != d.month) if pos + 1 < len(days) else _is_calendar_month_end(d.date())
            if is_me and st.get("last_decision_month") != d.strftime("%Y-%m"):
                self._decide(st, d, score, regime_on, log, "month-end")
        st["last_bar"] = str(bar_day.date())

        msg = self._report(st, index_df, closes, regime_on, uni, log, bar_day)
        st["last_message"] = msg
        self.save(st)
        if send and self.n:
            self.sent_ok = self.n.send(msg)
        return msg

    # ---------------------------------------------------------------- report
    def _report(self, st, index_df, closes, regime_on, sectors, log, bar_day) -> str:
        nifty = float(index_df["close"].iloc[-1])
        m = sma(index_df["close"], self.regime_sma)
        rising = bool(m.iloc[-1] > m.iloc[-21])
        equity = st["cash"] + sum(h["qty"] * float(closes.at[bar_day, s]) for s, h in st["holdings"].items())
        ret = (equity / st["start_capital"] - 1) * 100
        n_ret = (nifty / st["start_nifty"] - 1) * 100
        dd_now = (st["peak_equity"] - equity) / st["peak_equity"] * 100
        start = date.fromisoformat(st["start_date"])
        review = start + timedelta(days=30 * self.review_months)
        on = bool(regime_on.iloc[-1])
        lines = [
            f"📁 OPTION B PAPER PORTFOLIO · {bar_day:%a %d %b %Y}",
            "🧪 Paper test of the monthly momentum strategy - no real orders",
            "",
            (f"🟢 Market: UPTREND - NIFTY {nifty:,.0f} above its 200-day avg {m.iloc[-1]:,.0f}" if on else
             f"🔴 Market: DOWNTREND - NIFTY {nifty:,.0f} below its {'rising' if rising else 'falling'} "
             f"200-day avg {m.iloc[-1]:,.0f} -> stay in cash"),
            f"💼 Value {_f(equity)} ({ret:+.1f}%) · NIFTY {n_ret:+.1f}% since {start:%d %b}",
            f"📉 Drop from peak: now {dd_now:.1f}% · worst {st['max_dd_pct']:.1f}% (limit {self.dd_limit:g}%)",
            "",
        ]
        if st["holdings"]:
            lines.append(f"📦 Holdings {len(st['holdings'])}/{self.top_n} · cash {_f(st['cash'])}")
            sec_count: dict[str, int] = {}
            for sym, h in sorted(st["holdings"].items()):
                px = float(closes.at[bar_day, sym])
                chg = (px / h['entry_price'] - 1) * 100
                lines.append(f"   {'🟢' if chg >= 0 else '🔴'} {sym} ×{h['qty']}: ₹{h['entry_price']:,.2f} → ₹{px:,.2f} ({chg:+.1f}%)")
                sec = sectors.get(sym, "UNKNOWN")
                sec_count[sec] = sec_count.get(sec, 0) + 1
            heavy = [f"{k} ×{v}" for k, v in sec_count.items() if v >= 3]
            if heavy:
                lines.append("   ⚠️ Concentrated in: " + ", ".join(heavy))
        else:
            lines.append(f"📦 Holdings: none - 100% cash ({_f(st['cash'])}) 💤")
        if st.get("pending"):
            p = st["pending"]
            lines.append(f"⏭️ Next open (paper): sell {', '.join(p['sells']) or '-'} · buy {', '.join(p['buys']) or '-'}")
        if log:
            lines += ["", "📝 Today:"] + [f"   • {x}" for x in log]
        checks = [
            f"{'✅' if ret >= n_ret else '❌'} vs NIFTY: {ret - n_ret:+.1f} points",
            f"{'✅' if st['max_dd_pct'] <= self.dd_limit else '❌'} worst drop {st['max_dd_pct']:.1f}% (limit {self.dd_limit:g}%)",
        ]
        closed = st.get("closed", [])
        if closed:
            wins = sum(1 for c in closed if c["pnl"] > 0)
            checks.append(f"📊 closed trades {len(closed)} · winners {wins} · total P&L {_f(sum(c['pnl'] for c in closed))}")
        lines += ["", f"🏁 Scorecard (first review {review:%d %b %Y}):"] + [f"   {c}" for c in checks]
        if st["max_dd_pct"] > self.dd_limit:
            lines.append("🚨 Drawdown limit breached - under the risk rules this strategy would be PAUSED for review.")
        return "\n".join(lines)


__all__ = ["RotationPaperTracker"]
