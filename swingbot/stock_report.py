"""One-stock analysis report, using the bot's own rules.

Technical analysis only (price/volume). It says what the bot's strategies
think of the stock today; it is not investment advice and does not cover
company fundamentals (results, debt, valuation).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import analysis
from .config import Settings
from .indicators import find_resistance
from .portfolio import month_end_days, rotation_inputs
from .setups import find_setup
from .strategies import MomentumStrategy


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else float("nan")


def _ret(s: pd.Series, n: int) -> float:
    return _pct(float(s.iloc[-1]), float(s.iloc[-1 - n])) if len(s) > n else float("nan")


def _fmt(x: float, nd: int = 1, sign: bool = True) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def analyze_stock(symbol: str, raw: dict[str, pd.DataFrame], index_df: pd.DataFrame, settings: Settings,
                  sectors: dict[str, str] | None = None) -> str:
    symbol = symbol.upper()
    if symbol not in raw or raw[symbol].empty:
        return f"{symbol}: no price data found (check the NSE symbol, e.g. VMM for Vishal Mega Mart)."
    df = raw[symbol]
    idx = index_df["close"]
    f = analysis.prepare_stock(df, idx)
    row = f.iloc[-1]
    last = f.index[-1]
    close = float(row["close"])
    n_bars = len(f)
    L = [f"STOCK ANALYSIS: {symbol} ({(sectors or {}).get(symbol, 'sector n/a')}) - data to {last:%d %b %Y}",
         "Technical view by the bot's rules. Not investment advice; fundamentals not covered.", ""]

    # ---- price & returns
    L.append(f"Price ₹{close:,.2f} | 52-week high ₹{row['high_52w']:,.2f} ({_fmt(_pct(close, row['high_52w']))}%) | "
             f"52-week low ₹{row['low_52w']:,.2f} ({_fmt(_pct(close, row['low_52w']))}%)"
             if not pd.isna(row.get("high_52w")) else f"Price ₹{close:,.2f} (less than 1 year of history)")
    rets = []
    for label, n in (("1M", 21), ("3M", 63), ("6M", 126), ("1Y", 252)):
        rets.append(f"{label} {_fmt(_ret(f['close'], n))}% (NIFTY {_fmt(_ret(idx.reindex(f.index).ffill(), n))}%)")
    L.append("Returns: " + " | ".join(rets))

    # ---- trend
    def side(a, b):
        return "above" if a > b else "below"
    L += ["", "TREND"]
    for name in ("ema20", "sma50", "sma200"):
        v = row.get(name)
        if v is not None and not pd.isna(v):
            L.append(f"- {name.upper()}: ₹{v:,.2f} -> price {side(close, v)} ({_fmt(_pct(close, v))}%)")
    if not pd.isna(row.get("sma200_slope")):
        L.append(f"- 200-day average is {'rising' if row['sma200_slope'] > 0 else 'falling'}; "
                 f"weekly trend: {'UP' if row.get('wk_up') else 'DOWN' if row.get('wk_down') else 'sideways'}")
    L.append(f"- ADX {_fmt(row.get('adx14'), 0, False)} ({'trending' if row.get('adx14', 0) > 25 else 'weak/no trend'}), "
             f"RSI(14) {_fmt(row.get('rsi14'), 0, False)}")

    # ---- volatility, volume, levels
    L += ["", "RISK & VOLUME"]
    L.append(f"- Daily range (ATR14) ₹{row['atr14']:,.2f} = {row['atr_pct']:.1f}% of price -> "
             f"a 2xATR stop would be ~{2 * row['atr_pct']:.1f}% below")
    L.append(f"- Avg traded value (20d) ₹{row['traded_value_avg20'] / 1e7:,.0f} crore/day; volume vs 50-day avg {row['vol_ratio']:.2f}x; "
             f"up-day/down-day volume {_fmt(row.get('updown_vol_ratio'), 2, False)}")
    res = find_resistance(f, close)
    sup = float(f["low"].iloc[-20:].min())
    L.append(f"- Nearest resistance: {'₹' + format(res, ',.2f') + ' (' + _fmt(_pct(res, close)) + '%)' if res else 'none (near highs)'}; "
             f"20-day low (support) ₹{sup:,.2f} ({_fmt(_pct(sup, close))}%)")

    # ---- relative strength vs the NIFTY 500 universe
    comp = {}
    for s, d in raw.items():
        c = d["close"]
        if len(c) > 127:
            comp[s] = 0.6 * (c.iloc[-1] / c.iloc[-64] - 1) + 0.4 * (c.iloc[-1] / c.iloc[-127] - 1)
    rs_pct = float(pd.Series(comp).rank(pct=True).get(symbol, np.nan) * 100) if comp else np.nan

    # ---- classic score card
    found = find_setup(symbol, f, settings.section("setups"), settings.section("execution"), settings.section("risk"))
    card = analysis.score(symbol, row, rs_pct=rs_pct, plan=found.plan if found else None,
                          setup_quality=found.quality if found else 0.0)
    L += ["", f"BOT SCORE {card.total:g}/100 (relative strength percentile {_fmt(rs_pct, 0, False)} of ~500 stocks)"]
    L.append("  " + ", ".join(f"{k} {v:g}" for k, v in card.parts.items()))
    if card.reject_reasons:
        L.append("  Red flags: " + "; ".join(card.reject_reasons))
    if found:
        p = found.plan
        L.append(f"  Setup today: {p.setup.value} - {p.notes}")

    # ---- market regime + Option B + momentum swing
    ic = index_df["close"]
    m200 = ic.rolling(200).mean()
    regime_on = bool(ic.iloc[-1] > m200.iloc[-1] and m200.iloc[-1] > m200.iloc[-21])
    feats, score, reg = rotation_inputs(raw, index_df,
                                        float(settings.get("universe.min_avg_traded_value_cr", 10)),
                                        float(settings.get("universe.min_price", 50)))
    days = index_df.index
    me = month_end_days(days)
    last_me = me[-2] if len(me) > 1 and days[-1].month == (days[-1] + pd.offsets.BDay(1)).month else me[-1]
    L += ["", "WHAT THE STRATEGIES SAY"]
    L.append(f"- Market filter: NIFTY {ic.iloc[-1]:,.0f} vs 200-day avg {m200.iloc[-1]:,.0f} -> {'ON' if regime_on else 'OFF (no new buying)'}")
    for when, label in ((last_me, f"last month-end {last_me:%d %b}"), (days[-1], "today")):
        row_s = score.loc[when].dropna() if when in score.index else pd.Series(dtype=float)
        if symbol in row_s.index:
            rank = int(row_s.rank(ascending=False)[symbol])
            L.append(f"- Option B momentum rank ({label}): #{rank} of {len(row_s)} eligible "
                     f"({'IN top 10' if rank <= 10 else 'in top 20 (kept if already held)' if rank <= 20 else 'not in top 20'})")
        else:
            reason = "below its 200-day average" if not pd.isna(row.get("sma200")) and close <= row["sma200"] else \
                "not enough history / not liquid enough"
            L.append(f"- Option B ({label}): not eligible - {reason}")
    ms = MomentumStrategy(settings)
    mf = ms.features(df).iloc[-1]
    L.append(f"- Swing momentum signals today: trend {'yes' if mf['trend'] else 'no'}, "
             f"breakout {'yes' if mf['sig_breakout'] else 'no'}, pullback-bounce {'yes' if mf['sig_pullback'] else 'no'}")

    # ---- plain-language verdict
    L += ["", "VERDICT (bot rules)"]
    if not regime_on:
        L.append("- Whole market is in a downtrend (filter OFF): the bot would NOT buy anything now, including this stock.")
    if n_bars < 260:
        L.append(f"- Only {n_bars} trading days of history - many rules need about 1 year; treat signals with extra caution.")
    if close < row.get("sma200", np.inf):
        L.append("- Stock is below its 200-day average: long-term trend is down -> avoid / wait.")
    elif close > row.get("sma50", np.inf) > row.get("sma200", np.inf):
        L.append("- Stock is in an uptrend (price > 50-day > 200-day average).")
    else:
        L.append("- Trend is mixed (above 200-day but not cleanly trending).")
    if not np.isnan(rs_pct):
        L.append(f"- Strength vs other stocks: {'strong' if rs_pct >= 80 else 'average' if rs_pct >= 40 else 'weak'} "
                 f"({rs_pct:.0f}th percentile).")
    L.append("- Remember: all strategies are still in PAPER testing - none is validated for real money.")
    return "\n".join(L)
