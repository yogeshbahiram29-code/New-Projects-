"""Portfolio-style strategies (monthly decisions), tested with the same cost model.

Option B - Monthly momentum rotation
    On the last trading day of each month (signal on the close):
      * eligible = liquid stock (20-day avg traded value >= X cr, price >= min,
        >= 260 days of history) AND close > its 200-day SMA
      * score    = average of 6-month (126 d) and 12-month (252 d) return
      * regime   = NIFTY 50 close > its 200-day SMA and the SMA is rising;
                   if off -> sell everything, stay in cash
      * hold the top ``top_n`` (equal weight); keep a holding while it stays in
        the top ``top_n * buffer`` (fewer needless trades)
    Orders execute at the NEXT trading day's open (plus slippage). No per-stock
    stop: risk is controlled by diversification and the regime filter.
    ``pick="random"`` replaces the momentum ranking by a random choice among the
    same eligible stocks - a control that shows how much of the result comes
    from the universe itself (survivorship bias) rather than from momentum.

Option C - Index trend (Faber 10-month rule)
    On the last trading day of each month: if NIFTY 50 closes above its
    10-month average of month-end closes, hold the index ETF next month,
    otherwise hold cash. Executes at the next trading day's open.

Cash can earn a yield (e.g. a liquid ETF); the base case assumes 0%.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import CostModel
from .indicators import sma


@dataclass
class PortfolioResult:
    name: str
    equity: pd.Series
    trades: list[dict] = field(default_factory=list)
    exposure_pct: float = 0.0
    rebalances: int = 0
    notes: list[str] = field(default_factory=list)


def month_end_days(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby([index.year, index.month]).max().values)


def _next_day(days: pd.DatetimeIndex, d: pd.Timestamp) -> pd.Timestamp | None:
    i = days.searchsorted(d, side="right")
    return days[i] if i < len(days) else None


def _daily_rate(cash_yield_pct: float) -> float:
    return (1 + cash_yield_pct / 100) ** (1 / 252) - 1


# ---------------------------------------------------------------------------
# Option C - index trend
# ---------------------------------------------------------------------------
def backtest_index_trend(index_df: pd.DataFrame, start, end, capital: float, costs: CostModel,
                         sma_months: int = 10, cash_yield_pct: float = 0.0) -> PortfolioResult:
    slip = costs.slippage_pct / 100
    all_days = index_df.index
    me = month_end_days(all_days)
    me_close = index_df["close"].reindex(me)
    me_sma = me_close.rolling(sma_months, min_periods=sma_months).mean()
    want_in = {d: bool(c > m) for d, c, m in zip(me, me_close, me_sma) if not pd.isna(m)}  # decided at month-end close

    days = all_days[(all_days >= pd.Timestamp(start)) & (all_days <= pd.Timestamp(end))]
    me_set = set(me)
    r = _daily_rate(cash_yield_pct)
    cash, units, entry = capital, 0.0, None
    eq, trades, in_days = {}, [], 0
    pending = None  # "buy"/"sell" to execute at next open
    for t in days:
        o, c = float(index_df.at[t, "open"]), float(index_df.at[t, "close"])
        if pending == "buy" and units == 0:
            px = o * (1 + slip)
            spend = cash / (1 + 0.003)  # leave room for charges
            fee = costs.buy_cost(spend)
            units = spend / px
            cash -= spend + fee
            entry = {"entry_date": t.date(), "entry_price": px, "cost_basis": spend + fee}
        elif pending == "sell" and units > 0:
            px = o * (1 - slip)
            value = units * px
            fee = costs.sell_cost(value)
            cash += value - fee
            pnl = value - fee - entry["cost_basis"]
            trades.append({**entry, "exit_date": t.date(), "exit_price": px, "pnl": pnl,
                           "ret_pct": pnl / entry["cost_basis"] * 100,
                           "days": int(np.busday_count(entry["entry_date"], t.date()))})
            units, entry = 0.0, None
        pending = None
        cash *= 1 + r
        in_days += 1 if units > 0 else 0
        eq[t] = cash + units * c
        if t in me_set:
            go = want_in.get(t, False)
            if go and units == 0:
                pending = "buy"
            elif not go and units > 0:
                pending = "sell"
    if units > 0 and len(days):  # mark-to-market close-out (not a real trade)
        t = days[-1]
        value = units * float(index_df.at[t, "close"])
        trades.append({**entry, "exit_date": t.date(), "exit_price": float(index_df.at[t, "close"]),
                       "pnl": value - entry["cost_basis"], "ret_pct": (value / entry["cost_basis"] - 1) * 100,
                       "days": int(np.busday_count(entry["entry_date"], t.date())), "open_at_end": True})
    return PortfolioResult(f"C: index trend ({sma_months}-month avg, cash {cash_yield_pct:g}%)",
                           pd.Series(eq, dtype=float), trades, round(in_days / max(len(days), 1) * 100, 1),
                           notes=["tested on the NIFTY 50 price index as a stand-in for a NIFTY ETF (fractional units)"])


# ---------------------------------------------------------------------------
# Option B - momentum rotation
# ---------------------------------------------------------------------------
def _features(df: pd.DataFrame, min_value: float, min_price: float, min_hist: int) -> pd.DataFrame:
    c = df["close"]
    out = pd.DataFrame(index=df.index)
    out["open"], out["close"] = df["open"], c
    out["sma200"] = sma(c, 200)
    tv = sma(c * df["volume"], 20)
    n = np.arange(1, len(df) + 1)
    out["eligible"] = (tv >= min_value) & (c >= min_price) & (n >= min_hist) & (c > out["sma200"])
    out["score"] = 0.5 * (c / c.shift(126) - 1) + 0.5 * (c / c.shift(252) - 1)
    return out


def rotation_orders(order: list[str], held: list[str], regime_on: bool, top_n: int = 10,
                    buffer: float = 2.0) -> tuple[list[str], list[str]]:
    """The Option B decision rule, shared by the backtest and the paper tracker.

    order: eligible symbols, best first. Returns (sell, buy) lists to execute at
    the next open. Regime off -> sell everything, buy nothing.
    """
    if not regime_on:
        return list(held), []
    keep_zone = set(order[: int(top_n * buffer)])
    keep = [s for s in held if s in keep_zone]
    sells = [s for s in held if s not in keep_zone]
    buys = [s for s in order if s not in held][: max(top_n - len(keep), 0)]
    return sells, buys


def rotation_inputs(raw: dict[str, pd.DataFrame], index_df: pd.DataFrame, min_value_cr: float = 10,
                    min_price: float = 50, min_hist: int = 260, regime_sma: int = 200):
    """Score panel (NaN = not eligible), and the NIFTY regime series."""
    feats = {s: _features(df, min_value_cr * 1e7, min_price, min_hist) for s, df in raw.items() if len(df) > 0}
    score = pd.DataFrame({s: f["score"].where(f["eligible"]) for s, f in feats.items()})
    ic = index_df["close"]
    im = sma(ic, regime_sma)
    regime_on = ((ic > im) & (im > im.shift(20))).fillna(False)
    return feats, score, regime_on


def backtest_momentum_rotation(raw: dict[str, pd.DataFrame], index_df: pd.DataFrame, start, end, capital: float,
                               costs: CostModel, top_n: int = 10, buffer: float = 2.0, min_value_cr: float = 10,
                               min_price: float = 50, min_hist: int = 260, regime_sma: int = 200,
                               cash_yield_pct: float = 0.0, pick: str = "momentum", seed: int = 0) -> PortfolioResult:
    slip = costs.slippage_pct / 100
    feats, score, regime_on = rotation_inputs(raw, index_df, min_value_cr, min_price, min_hist, regime_sma)
    closes = pd.DataFrame({s: f["close"] for s, f in feats.items()})
    opens = pd.DataFrame({s: f["open"] for s, f in feats.items()})

    all_days = index_df.index
    days = all_days[(all_days >= pd.Timestamp(start)) & (all_days <= pd.Timestamp(end))]
    closes = closes.reindex(all_days).ffill()
    opens = opens.reindex(all_days)
    me_set = set(month_end_days(all_days))
    rng = np.random.default_rng(seed)
    r = _daily_rate(cash_yield_pct)

    cash = capital
    holdings: dict[str, dict] = {}  # sym -> {qty, entry_date, entry_price, cost_basis}
    pending_sells: list[str] = []
    pending_buys: list[str] = []
    eq, trades, in_days, rebalances = {}, [], 0, 0
    last_seen = {s: f.index[-1] for s, f in feats.items()}
    notes: list[str] = []

    def sell(sym: str, t, px: float, why: str):
        nonlocal cash
        h = holdings.pop(sym)
        value = h["qty"] * px
        fee = costs.sell_cost(value)
        cash += value - fee
        pnl = value - fee - h["cost_basis"]
        trades.append({"symbol": sym, "entry_date": h["entry_date"], "entry_price": h["entry_price"],
                       "exit_date": t.date(), "exit_price": px, "pnl": pnl, "ret_pct": pnl / h["cost_basis"] * 100,
                       "days": int(np.busday_count(h["entry_date"], t.date())), "exit_reason": why})

    for t in days:
        # 1) execute last month-end's decisions at today's open
        if pending_sells or pending_buys:
            for sym in list(pending_sells):
                o = opens.at[t, sym] if sym in opens.columns else np.nan
                if sym in holdings and not pd.isna(o):
                    sell(sym, t, float(o) * (1 - slip), "rebalance")
                    pending_sells.remove(sym)
            # size with YESTERDAY's closes - today's close isn't known at the open
            prev_t = all_days[max(all_days.get_loc(t) - 1, 0)]
            equity_now = cash + sum(h["qty"] * float(closes.at[prev_t, s]) for s, h in holdings.items())
            slot = equity_now / top_n
            for sym in list(pending_buys):
                o = opens.at[t, sym]
                if pd.isna(o) or sym in holdings:
                    pending_buys.remove(sym)
                    continue
                px = float(o) * (1 + slip)
                budget = min(slot, cash / 1.003)
                qty = math.floor(budget / px)
                if qty < 1:
                    pending_buys.remove(sym)
                    continue
                fee = costs.buy_cost(qty * px)
                cash -= qty * px + fee
                holdings[sym] = {"qty": qty, "entry_date": t.date(), "entry_price": px, "cost_basis": qty * px + fee}
                pending_buys.remove(sym)
            pending_sells, pending_buys = [], []

        # 2) a stock whose data ended (delisted/merged) is closed at its last price
        for sym in [s for s in holdings if last_seen[s] < t]:
            sell(sym, t, float(closes.at[t, sym]), "data ended (delisted/merged)")
            notes.append(f"{sym}: data ended {last_seen[sym].date()}, closed at last price")

        cash *= 1 + r
        mtm = cash + sum(h["qty"] * float(closes.at[t, s]) for s, h in holdings.items())
        eq[t] = mtm
        in_days += 1 if holdings else 0

        # 3) month-end decision on the close
        if t in me_set:
            rebalances += 1
            row = score.loc[t].dropna() if t in score.index else pd.Series(dtype=float)
            if pick == "random":
                order = list(rng.permutation(row.index.to_numpy()))
            else:
                order = list(row.sort_values(ascending=False).index)
            pending_sells, pending_buys = rotation_orders(order, list(holdings), bool(regime_on.at[t]), top_n, buffer)

    if holdings and len(days):  # mark-to-market close-out at the end (not real trades)
        t = days[-1]
        for sym in list(holdings):
            sell(sym, t, float(closes.at[t, sym]), "end of test")
    label = "momentum top" if pick == "momentum" else "RANDOM picks (control)"
    return PortfolioResult(f"B: {label} {top_n}, cash {cash_yield_pct:g}%", pd.Series(eq, dtype=float), trades,
                           round(in_days / max(len(days), 1) * 100, 1), rebalances, sorted(set(notes))[:10])


# ---------------------------------------------------------------------------
# what Option B holds right now
# ---------------------------------------------------------------------------
def current_picks(raw: dict[str, pd.DataFrame], index_df: pd.DataFrame, top_n: int = 10, min_value_cr: float = 10,
                  min_price: float = 50, min_hist: int = 260, regime_sma: int = 200, sectors: dict | None = None):
    """Picks from the most recent month-end close (the official rebalance), plus today's regime."""
    days = index_df.index
    me = month_end_days(days)
    # the newest "month-end" may be today's date inside an unfinished month -> use the previous one
    last_me = me[-1]
    if len(me) > 1 and days[-1].month == (days[-1] + pd.offsets.BDay(1)).month:
        last_me = me[-2]
    ic = index_df["close"]
    im = sma(ic, regime_sma)
    regime = {
        "date": days[-1].date(), "nifty": round(float(ic.iloc[-1]), 1), "sma200": round(float(im.iloc[-1]), 1),
        "sma200_rising": bool(im.iloc[-1] > im.iloc[-21]),
        "on_today": bool(ic.iloc[-1] > im.iloc[-1] and im.iloc[-1] > im.iloc[-21]),
        "on_at_month_end": bool(ic.loc[last_me] > im.loc[last_me] and im.loc[last_me] > im.shift(20).loc[last_me]),
        "month_end": last_me.date(),
    }
    rows = []
    for sym, df in raw.items():
        f = _features(df, min_value_cr * 1e7, min_price, min_hist)
        if last_me not in f.index or not bool(f.at[last_me, "eligible"]) or pd.isna(f.at[last_me, "score"]):
            continue
        c = df["close"]
        rows.append({"symbol": sym, "sector": (sectors or {}).get(sym, ""), "score_pct": round(f.at[last_me, "score"] * 100, 1),
                     "ret_6m_pct": round((c.loc[last_me] / c.shift(126).loc[last_me] - 1) * 100, 1),
                     "ret_12m_pct": round((c.loc[last_me] / c.shift(252).loc[last_me] - 1) * 100, 1),
                     "close_month_end": round(float(c.loc[last_me]), 2), "close_latest": round(float(c.iloc[-1]), 2)})
    picks = pd.DataFrame(rows).sort_values("score_pct", ascending=False).head(top_n).reset_index(drop=True) if rows else pd.DataFrame()
    return regime, picks


# ---------------------------------------------------------------------------
# data sanity
# ---------------------------------------------------------------------------
def suspicious_symbols(raw: dict[str, pd.DataFrame], start, end, threshold: float = 0.25) -> dict[str, list]:
    """Stocks with a one-day close-to-close move above ``threshold`` inside the window.

    NSE price bands cap most stocks at 20% a day, so a bigger move is almost
    always a data error or an unadjusted corporate action (split/bonus/demerger).
    """
    out = {}
    lo, hi = pd.Timestamp(start) - pd.Timedelta(days=400), pd.Timestamp(end)
    for sym, df in raw.items():
        c = df["close"].loc[lo:hi]
        jumps = c.pct_change().abs()
        bad = jumps[jumps > threshold]
        if len(bad):
            out[sym] = [(d.date(), round(float(c.pct_change().loc[d]) * 100, 1)) for d in bad.index[:3]]
    return out


def max_jump_during(df: pd.DataFrame, start, end) -> float:
    c = df["close"].loc[pd.Timestamp(start): pd.Timestamp(end)]
    j = c.pct_change().abs().max()
    return round(float(j) * 100, 1) if not pd.isna(j) else 0.0


# ---------------------------------------------------------------------------
# metrics for portfolio strategies
# ---------------------------------------------------------------------------
def portfolio_trade_metrics(trades: list[dict]) -> dict:
    real = [t for t in trades if t.get("exit_reason") not in ("end of test",) and not t.get("open_at_end")]
    if not real:
        return {"trades": 0}
    pnl = np.array([t["pnl"] for t in real])
    ret = np.array([t["ret_pct"] for t in real])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    streak = worst = 0
    for p in pnl:
        streak = streak + 1 if p <= 0 else 0
        worst = max(worst, streak)
    return {
        "trades": len(real),
        "win_rate_pct": round(len(wins) / len(real) * 100, 1),
        "avg_win_rs": round(float(wins.mean()), 0) if len(wins) else 0.0,
        "avg_loss_rs": round(float(losses.mean()), 0) if len(losses) else 0.0,
        "expectancy_rs": round(float(pnl.mean()), 0),
        "expectancy_pct": round(float(ret.mean()), 2),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if len(losses) and losses.sum() < 0 else float("inf"),
        "avg_days_held": round(float(np.mean([t["days"] for t in real])), 0),
        "max_losing_streak": int(worst),
    }
