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
from .tgfmt import Html, esc, head, kv, pre, title


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else float("nan")


def _ret(s: pd.Series, n: int) -> float:
    return _pct(float(s.iloc[-1]), float(s.iloc[-1 - n])) if len(s) > n else float("nan")


def _fmt(x: float, nd: int = 1, sign: bool = True) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def analyze_stock(symbol: str, raw: dict[str, pd.DataFrame], index_df: pd.DataFrame, settings: Settings,
                  sectors: dict[str, str] | None = None):
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
    B = [title(f"Stock Analysis · {symbol}", f"{(sectors or {}).get(symbol, 'sector n/a')} · data to {last:%d %b %Y}")
         + "\n" + esc("Technical view by the bot's rules. Not investment advice; fundamentals shown separately.")]

    # ---- price & returns
    if not pd.isna(row.get("high_52w")):
        price = kv([("Price", f"₹{close:,.2f}"),
                    ("52W high", f"₹{row['high_52w']:,.2f}  {_fmt(_pct(close, row['high_52w']))}%"),
                    ("52W low", f"₹{row['low_52w']:,.2f}  {_fmt(_pct(close, row['low_52w']))}%")], 10)
    else:
        price = esc(f"Price ₹{close:,.2f} (less than 1 year of history)")
    rets = [f"{'':<6}{'STOCK':>9}{'NIFTY':>9}"]
    for label, n in (("1M", 21), ("3M", 63), ("6M", 126), ("1Y", 252)):
        rets.append(f"{label:<6}{_fmt(_ret(f['close'], n)) + '%':>9}{_fmt(_ret(idx.reindex(f.index).ffill(), n)) + '%':>9}")
    B.append(head("Price") + "\n" + price + "\n\n" + head("Returns") + "\n" + pre(rets))

    # ---- trend
    def side(a, b):
        return "above" if a > b else "below"
    rows = []
    for name in ("ema20", "sma50", "sma200"):
        v = row.get(name)
        if v is not None and not pd.isna(v):
            rows.append(f"{name.upper():<7}{'₹' + format(v, ',.2f'):>11} {side(close, v):<5}{_fmt(_pct(close, v)) + '%':>7}")
    notes = []
    if not pd.isna(row.get("sma200_slope")):
        notes.append(f"200-day average {'rising' if row['sma200_slope'] > 0 else 'falling'} · weekly trend "
                     f"{'UP' if row.get('wk_up') else 'DOWN' if row.get('wk_down') else 'sideways'}")
    notes.append(f"ADX {_fmt(row.get('adx14'), 0, False)} ({'trending' if row.get('adx14', 0) > 25 else 'weak/no trend'})"
                 f" · RSI(14) {_fmt(row.get('rsi14'), 0, False)}")
    B.append(head("Trend") + "\n" + (pre(rows) + "\n" if rows else "") + esc("\n".join(notes)))

    # ---- volatility, volume, levels
    res = find_resistance(f, close)
    sup = float(f["low"].iloc[-20:].min())
    B.append(head("Risk & Volume") + "\n" + kv([
        ("ATR14", f"₹{row['atr14']:,.2f} = {row['atr_pct']:.1f}%"),
        ("2xATR stop", f"~{2 * row['atr_pct']:.1f}% below"),
        ("Traded value", f"₹{row['traded_value_avg20'] / 1e7:,.0f} Cr/day"),
        ("Volume", f"{row['vol_ratio']:.2f}x 50D avg"),
        ("Up/down vol", _fmt(row.get("updown_vol_ratio"), 2, False)),
        ("Resistance", f"₹{res:,.2f} ({_fmt(_pct(res, close))}%)" if res else "none (near highs)"),
        ("20D low", f"₹{sup:,.2f} ({_fmt(_pct(sup, close))}%)")], 13))

    # ---- relative strength vs the NIFTY 500 universe
    comp = {}
    for s_, d in raw.items():
        c = d["close"]
        if len(c) > 127:
            comp[s_] = 0.6 * (c.iloc[-1] / c.iloc[-64] - 1) + 0.4 * (c.iloc[-1] / c.iloc[-127] - 1)
    rs_pct = float(pd.Series(comp).rank(pct=True).get(symbol, np.nan) * 100) if comp else np.nan

    # ---- classic score card
    found = find_setup(symbol, f, settings.section("setups"), settings.section("execution"), settings.section("risk"))
    card = analysis.score(symbol, row, rs_pct=rs_pct, plan=found.plan if found else None,
                          setup_quality=found.quality if found else 0.0)
    sc = [", ".join(f"{k} {v:g}" for k, v in card.parts.items())]
    if card.reject_reasons:
        sc.append("Red flags: " + "; ".join(card.reject_reasons))
    if found:
        sc.append(f"Setup today: {found.plan.setup.value} – {found.plan.notes}")
    B.append(head("Bot Score", f"{card.total:g}/100 · RS {_fmt(rs_pct, 0, False)}th pct of ~500") + "\n"
             + esc("\n".join(sc)))

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
    st = [f"- Market filter: NIFTY {ic.iloc[-1]:,.0f} vs 200-day avg {m200.iloc[-1]:,.0f} → "
          f"{'ON' if regime_on else 'OFF (no new buying)'}"]
    for when, label in ((last_me, f"last month-end {last_me:%d %b}"), (days[-1], "today")):
        row_s = score.loc[when].dropna() if when in score.index else pd.Series(dtype=float)
        if symbol in row_s.index:
            rank = int(row_s.rank(ascending=False)[symbol])
            st.append(f"- Option B rank ({label}): #{rank} of {len(row_s)} eligible "
                      f"({'IN top 10' if rank <= 10 else 'in top 20 (kept if already held)' if rank <= 20 else 'not in top 20'})")
        else:
            reason = "below its 200-day average" if not pd.isna(row.get("sma200")) and close <= row["sma200"] else \
                "not enough history / not liquid enough"
            st.append(f"- Option B ({label}): not eligible – {reason}")
    ms = MomentumStrategy(settings)
    mf = ms.features(df).iloc[-1]
    st.append(f"- Swing signals today: trend {'yes' if mf['trend'] else 'no'}, "
              f"breakout {'yes' if mf['sig_breakout'] else 'no'}, pullback-bounce {'yes' if mf['sig_pullback'] else 'no'}")
    B.append(head("Strategies") + "\n" + esc("\n".join(st)))

    # ---- plain-language verdict
    v = []
    if not regime_on:
        v.append("- Whole market is in a downtrend (filter OFF): the bot would NOT buy anything now, including this stock.")
    if n_bars < 260:
        v.append(f"- Only {n_bars} trading days of history – many rules need about 1 year; treat signals with extra caution.")
    if close < row.get("sma200", np.inf):
        v.append("- Stock is below its 200-day average: long-term trend is down → avoid / wait.")
    elif close > row.get("sma50", np.inf) > row.get("sma200", np.inf):
        v.append("- Stock is in an uptrend (price > 50-day > 200-day average).")
    else:
        v.append("- Trend is mixed (above 200-day but not cleanly trending).")
    if not np.isnan(rs_pct):
        v.append(f"- Strength vs other stocks: {'strong' if rs_pct >= 80 else 'average' if rs_pct >= 40 else 'weak'} "
                 f"({rs_pct:.0f}th percentile).")
    B.append(head("Verdict", "bot rules") + "\n" + esc("\n".join(v)))
    B.append("<i>" + esc("All strategies are still in PAPER testing – none is validated for real money.") + "</i>")
    return Html("\n\n".join(B))
