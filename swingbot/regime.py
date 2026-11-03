"""Market regime: decides how aggressive the bot is allowed to be.

Most stocks follow the index. Buying good-looking setups in a falling market
is the most common way swing traders lose money, so the regime gates entries.
"""
from __future__ import annotations

import math

import pandas as pd

from .indicators import add_indicators
from .models import Regime


def breadth_series(indicator_frames: dict[str, pd.DataFrame]) -> pd.Series:
    """% of stocks closing above their 50-day SMA, per date."""
    above = []
    for df in indicator_frames.values():
        valid = df["sma50"].notna()
        s = (df["close"] > df["sma50"]).astype(float).where(valid)
        above.append(s.rename(None))
    if not above:
        return pd.Series(dtype=float)
    panel = pd.concat(above, axis=1)
    counts = panel.notna().sum(axis=1)
    pct = panel.sum(axis=1, min_count=1) / counts * 100
    return pct.where(counts >= 10).astype(float)  # need a minimum sample for breadth to mean anything


def classify(row: pd.Series, breadth: float | None, recent_returns: pd.Series, cfg: dict) -> Regime:
    """Classify one day.

    row            - index indicator row (close, sma50, sma200, sma200_slope)
    breadth        - % of universe above 50 SMA (None if unknown)
    recent_returns - last few daily index returns (fractions), newest last
    """
    shock_drop = float(cfg.get("shock_index_drop_pct", 3)) / 100
    freeze = int(cfg.get("shock_freeze_days", 2))
    tail = recent_returns.dropna().iloc[-freeze:] if len(recent_returns) else recent_returns
    if len(tail) and (tail <= -shock_drop).any():
        return Regime.SHOCK

    close, sma50, sma200, slope = row["close"], row["sma50"], row["sma200"], row["sma200_slope"]
    if any(pd.isna(x) for x in (close, sma50, sma200, slope)):
        return Regime.NEUTRAL  # not enough history: be cautious

    has_breadth = breadth is not None and not (isinstance(breadth, float) and math.isnan(breadth))
    bull_b = float(cfg.get("bull_breadth_pct", 60))
    bear_b = float(cfg.get("bear_breadth_pct", 40))

    if close < sma200 and (sma50 < sma200 or (has_breadth and breadth < bear_b) or slope < 0):
        return Regime.BEAR
    if close > sma50 > sma200 and slope > 0 and (not has_breadth or breadth >= bull_b):
        return Regime.BULL
    return Regime.NEUTRAL


def regime_series(index_df: pd.DataFrame, breadth: pd.Series | None, cfg: dict) -> pd.Series:
    """Regime for every date of the index (used by the backtester)."""
    ind = index_df if "sma200" in index_df.columns else add_indicators(index_df)
    rets = ind["close"].pct_change()
    out = {}
    for i, (ts, row) in enumerate(ind.iterrows()):
        b = None
        if breadth is not None and ts in breadth.index:
            b = breadth.loc[ts]
        out[ts] = classify(row, b, rets.iloc[max(0, i - 5) : i + 1], cfg)
    return pd.Series(out)


def current_regime(index_df: pd.DataFrame, breadth: float | None, cfg: dict) -> Regime:
    ind = index_df if "sma200" in index_df.columns else add_indicators(index_df)
    rets = ind["close"].pct_change()
    return classify(ind.iloc[-1], breadth, rets.iloc[-6:], cfg)
