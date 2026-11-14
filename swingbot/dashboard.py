"""Daily market dashboard: the big picture before looking at single stocks."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .tgfmt import GREEN, RED, TAGLINE, YELLOW, Html, dot, esc, head, pre, quote, title

GLOBALS = [  # key, label, value format
    ("SP500", "S&P 500", "{:,.0f}"),
    ("NASDAQ", "Nasdaq", "{:,.0f}"),
    ("BRENT", "Brent $", "{:,.1f}"),
    ("USDINR", "USD/INR", "{:,.2f}"),
    ("GOLD", "Gold $", "{:,.0f}"),
    ("US10Y", "US 10Y %", "{:.2f}"),
]


def _chg(s: pd.Series, n: int) -> float:
    s = s.dropna()
    return (s.iloc[-1] / s.iloc[-1 - n] - 1) * 100 if len(s) > n else float("nan")


def _p(x: float) -> str:
    return "n/a" if x is None or np.isnan(x) else f"{x:+.1f}%"


def _c(x: float) -> str:  # table cell: change without the % sign (column header says %)
    return "n/a" if x is None or np.isnan(x) else f"{x:+.1f}"


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
                     extra: dict[str, pd.DataFrame] | None = None) -> Html:
    from .tgfmt import sector_name

    extra = extra or {}
    ic = index_df["close"]
    when = index_df.index[-1]
    m200 = ic.rolling(200).mean()
    rising = bool(m200.iloc[-1] > m200.iloc[-21])
    on = bool(ic.iloc[-1] > m200.iloc[-1] and rising)
    hi52 = float(index_df["high"].iloc[-252:].max())
    blocks = [title("Market Dashboard", f"{when:%a, %d %b %Y}")]

    # -- indices
    def row(label, last, ch1, ch21, invert=False):  # every row starts with a colour dot (keeps columns aligned)
        return f"{dot(ch1, invert)} {label:<9}{last:>7}{_c(ch1):>6}{_c(ch21):>6}"

    rows = [row("NIFTY 50", f"{ic.iloc[-1]:,.0f}", _chg(ic, 1), _chg(ic, 21))]
    bn = extra.get("BANKNIFTY")
    if bn is not None and len(bn) > 22:
        rows.append(row("BANKNIFTY", f"{bn['close'].iloc[-1]:,.0f}", _chg(bn["close"], 1), _chg(bn["close"], 21)))
    vix = extra.get("INDIAVIX")
    vix_lvl = None
    if vix is not None and len(vix) > 2:
        vix_lvl = float(vix["close"].iloc[-1])
        rows.append(row("INDIA VIX", f"{vix_lvl:.1f}", _chg(vix["close"], 1), _chg(vix["close"], 21), invert=True))
    trend = (f"UPTREND – above rising 200 DMA ({m200.iloc[-1]:,.0f})" if on else
             f"DOWNTREND – {'below' if ic.iloc[-1] < m200.iloc[-1] else 'above'} "
             f"{'rising' if rising else 'falling'} 200 DMA ({m200.iloc[-1]:,.0f})")
    idx_lines = [f"{GREEN if on else RED} Trend: {trend}",
                 f"NIFTY 1W: {_p(_chg(ic, 5))} · From 52W high: {(ic.iloc[-1] / hi52 - 1) * 100:+.1f}%"]
    if vix_lvl is not None:
        mood = "calm" if vix_lvl < 14 else "normal" if vix_lvl < 20 else "elevated" if vix_lvl < 25 else "HIGH fear"
        idx_lines.append(f"Volatility: {mood}")
    blocks.append(head("Indices", "last · 1D% · 1M%") + "\n" + pre(rows) + "\n" + esc("\n".join(idx_lines)))

    # -- breadth
    b = breadth(raw, when)
    blocks.append(head("Breadth", f"NIFTY 500 · {b['n']} stocks") + "\n" + pre([
        f"{GREEN} {'Advancing':<14}{b['adv']:>5}",
        f"{RED} {'Declining':<14}{b['dec']:>5}",
        f"{GREEN if b['above200'] >= 50 else RED} {'Above 200 DMA':<14}{b['above200']:>4.0f}%",
        f"{GREEN if b['above50'] >= 50 else RED} {'Above 50 DMA':<14}{b['above50']:>4.0f}%"]))

    # -- sectors
    ss = sector_strength(raw, sectors, when)
    if len(ss) >= 6:
        rows = [f"{GREEN} STRONGEST"] + [f"   {sector_name(k, 21):<21}{v:>+6.1f}%" for k, v in ss.head(3).items()]
        rows += [f"{RED} WEAKEST"] + [f"   {sector_name(k, 21):<21}{v:>+6.1f}%" for k, v in ss.tail(3)[::-1].items()]
        blocks.append(head("Sectors", "1-month median") + "\n" + pre(rows))

    # -- global cues
    g = []
    for key, label, fmt in GLOBALS:
        df = extra.get(key)
        if df is None or len(df) < 23:
            continue
        c1 = _chg(df["close"], 1)
        g.append(f"{dot(c1)} {label:<9}{fmt.format(df['close'].iloc[-1]):>8}{_c(c1):>6}{_c(_chg(df['close'], 21)):>6}")
    if g:
        blocks.append(head("Global Cues", "last · 1D% · 1M%") + "\n" + pre(g))

    # -- outlook
    a200 = b["above200"]
    if on and a200 >= 60:
        out = [f"{GREEN} Healthy uptrend. Most stocks are rising with the index – setups allowed (paper)."]
    elif on:
        out = [f"{YELLOW} Uptrend but narrow. Only a minority of stocks are strong – be very selective."]
    elif a200 < 40:
        out = [f"{RED} Risk-off. Index in downtrend and most stocks below their 200 DMA – protect capital, "
               "no fresh buying."]
    else:
        out = [f"{YELLOW} Mixed. Market filter OFF (index below its 200 DMA, or the 200 DMA still falling) – "
               "wait for it to turn ON."]
    if vix_lvl is not None and vix_lvl >= 20:
        out.append(f"{YELLOW} Volatility is elevated – expect bigger swings; use smaller positions.")
    blocks.append(head("Outlook") + "\n" + quote(esc("\n".join(out))) + "\n" + TAGLINE)
    return Html("\n\n".join(blocks))
