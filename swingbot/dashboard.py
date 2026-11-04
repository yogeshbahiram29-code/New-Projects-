"""Daily market dashboard: the big picture before looking at single stocks."""
from __future__ import annotations

import numpy as np
import pandas as pd

LINE = "━━━━━━━━━━━━━━━━━━"

GLOBALS = [  # key, label, unit/format
    ("SP500", "🇺🇸 S&P 500", "{:,.0f}"),
    ("NASDAQ", "💻 Nasdaq", "{:,.0f}"),
    ("BRENT", "🛢️ Brent crude", "${:,.1f}"),
    ("USDINR", "💵 USD/INR", "₹{:,.2f}"),
    ("GOLD", "🥇 Gold", "${:,.0f}"),
    ("US10Y", "🏛️ US 10Y yield", "{:.2f}%"),
]


def _chg(s: pd.Series, n: int) -> float:
    s = s.dropna()
    return (s.iloc[-1] / s.iloc[-1 - n] - 1) * 100 if len(s) > n else float("nan")


def _p(x: float) -> str:
    return "n/a" if x is None or np.isnan(x) else f"{x:+.1f}%"


def breadth(raw: dict[str, pd.DataFrame], when: pd.Timestamp) -> dict:
    up = down = a200 = a50 = n = 0
    for df in raw.values():
        c = df["close"]
        if when not in c.index or len(c) < 201:
            continue
        i = c.index.get_loc(when)
        if i < 200:
            continue
        n += 1
        last, prev = c.iloc[i], c.iloc[i - 1]
        up += last > prev
        down += last < prev
        a200 += last > c.iloc[i - 199: i + 1].mean()
        a50 += last > c.iloc[i - 49: i + 1].mean()
    return {"n": n, "adv": up, "dec": down, "above200": a200 / n * 100 if n else np.nan,
            "above50": a50 / n * 100 if n else np.nan}


def sector_strength(raw: dict[str, pd.DataFrame], sectors: dict[str, str], when: pd.Timestamp, days: int = 21,
                    min_stocks: int = 4) -> pd.Series:
    rets: dict[str, list[float]] = {}
    for sym, df in raw.items():
        c = df["close"]
        if when not in c.index:
            continue
        i = c.index.get_loc(when)
        if i < days:
            continue
        rets.setdefault(sectors.get(sym, "UNKNOWN"), []).append((c.iloc[i] / c.iloc[i - days] - 1) * 100)
    med = {k: float(np.median(v)) for k, v in rets.items() if len(v) >= min_stocks and k != "UNKNOWN"}
    return pd.Series(med, dtype=float).sort_values(ascending=False)


def market_dashboard(raw: dict[str, pd.DataFrame], index_df: pd.DataFrame, sectors: dict[str, str],
                     extra: dict[str, pd.DataFrame] | None = None) -> str:
    extra = extra or {}
    ic = index_df["close"]
    when = index_df.index[-1]
    m200 = ic.rolling(200).mean()
    rising = bool(m200.iloc[-1] > m200.iloc[-21])
    on = bool(ic.iloc[-1] > m200.iloc[-1] and rising)
    hi52 = float(index_df["high"].iloc[-252:].max())
    L = ["🌍 MARKET DASHBOARD", f"📅 {when:%A, %d %B %Y}", "", LINE,
         f"🇮🇳 NIFTY 50: {ic.iloc[-1]:,.0f} (1D {_p(_chg(ic, 1))} | 1W {_p(_chg(ic, 5))} | 1M {_p(_chg(ic, 21))})",
         (f"📈 Trend: UPTREND – above rising 200-DMA ({m200.iloc[-1]:,.0f})" if on else
          f"📉 Trend: DOWNTREND – {'below' if ic.iloc[-1] < m200.iloc[-1] else 'above'} "
          f"{'rising' if rising else 'falling'} 200-DMA ({m200.iloc[-1]:,.0f})"),
         f"📏 From 52-week high: {(ic.iloc[-1] / hi52 - 1) * 100:+.1f}%"]
    bn = extra.get("BANKNIFTY")
    if bn is not None and len(bn) > 22:
        L.append(f"🏦 Bank NIFTY: {bn['close'].iloc[-1]:,.0f} (1D {_p(_chg(bn['close'], 1))} | 1M {_p(_chg(bn['close'], 21))})")
    vix = extra.get("INDIAVIX")
    vix_lvl = None
    if vix is not None and len(vix) > 2:
        vix_lvl = float(vix["close"].iloc[-1])
        mood = "calm" if vix_lvl < 14 else "normal" if vix_lvl < 20 else "elevated" if vix_lvl < 25 else "HIGH fear"
        L.append(f"😨 India VIX: {vix_lvl:.1f} (1D {_p(_chg(vix['close'], 1))}) – {mood}")

    b = breadth(raw, when)
    L += [LINE, f"📊 MARKET BREADTH (NIFTY 500, {b['n']} stocks)",
          f"🟢 Advancing: {b['adv']} | 🔴 Declining: {b['dec']}",
          f"📈 Above 200-DMA: {b['above200']:.0f}% | Above 50-DMA: {b['above50']:.0f}%"]

    ss = sector_strength(raw, sectors, when)
    if len(ss) >= 6:
        L += [LINE, "🏆 STRONGEST SECTORS (1M)"] + [f"{i}. {k} {v:+.1f}%" for i, (k, v) in enumerate(ss.head(3).items(), 1)]
        L += ["🐢 WEAKEST SECTORS (1M)"] + [f"{i}. {k} {v:+.1f}%" for i, (k, v) in enumerate(ss.tail(3)[::-1].items(), 1)]

    g = []
    for key, label, fmt in GLOBALS:
        df = extra.get(key)
        if df is None or len(df) < 23:
            continue
        g.append(f"{label}: {fmt.format(df['close'].iloc[-1])} ({_p(_chg(df['close'], 1))} | {_p(_chg(df['close'], 21))})")
    if g:
        L += [LINE, "🌐 GLOBAL CUES (1D | 1M)"] + g

    a200 = b["above200"]
    L += [LINE, "🧭 WHAT IT MEANS"]
    if on and a200 >= 60:
        L.append("🟢 Healthy uptrend: most stocks are rising with the index – setups allowed (paper).")
    elif on:
        L.append("🟡 Uptrend but narrow: only a minority of stocks are strong – be very selective.")
    elif a200 < 40:
        L.append("🔴 Risk-off: index in downtrend and most stocks below their 200-DMA – protect capital, no fresh buying.")
    else:
        L.append("🟠 Mixed: index below its trend line – wait for the market filter to turn ON.")
    if vix_lvl is not None and vix_lvl >= 20:
        L.append("⚠️ Volatility is elevated – expect bigger swings; use smaller positions.")
    L += ["", "📊 Discipline First | Capital Protection Always."]
    return "\n".join(L)
