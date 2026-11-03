"""Technical indicators implemented with pandas only (no TA-Lib needed).

Input frames use lowercase columns: open, high, low, close, volume, indexed by date.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0.0), n)
    loss = _wilder(-delta.clip(upper=0.0), n)
    rs = gain / loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # all gains, no losses -> RSI 100
    out = out.where(~((loss == 0) & (gain > 0)), 100.0)
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = _wilder(true_range(df), n)
    plus_di = 100 * _wilder(plus_dm, n) / tr
    minus_di = 100 * _wilder(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return _wilder(dx, n), plus_di, minus_di


def bollinger_width(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std()
    return (2 * k * std) / mid


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0.0)
    return (direction * df["volume"]).cumsum()


def chandelier_stop(df: pd.DataFrame, period: int = 22, mult: float = 3.0) -> pd.Series:
    return df["high"].rolling(period, min_periods=period).max() - mult * atr(df, period)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with every indicator the strategy needs."""
    out = df.copy()
    c = out["close"]
    out["ema10"] = ema(c, 10)
    out["ema20"] = ema(c, 20)
    out["sma5"] = sma(c, 5)
    out["sma50"] = sma(c, 50)
    out["sma150"] = sma(c, 150)
    out["sma200"] = sma(c, 200)
    out["sma200_slope"] = out["sma200"] - out["sma200"].shift(20)
    out["sma50_slope"] = out["sma50"] - out["sma50"].shift(10)
    out["rsi14"] = rsi(c, 14)
    out["rsi2"] = rsi(c, 2)
    out["atr14"] = atr(out, 14)
    out["atr_pct"] = out["atr14"] / c * 100
    adx_v, pdi, mdi = adx(out, 14)
    out["adx14"], out["plus_di"], out["minus_di"] = adx_v, pdi, mdi
    out["vol_avg50"] = sma(out["volume"], 50)
    out["vol_ratio"] = out["volume"] / out["vol_avg50"]
    out["traded_value_avg20"] = sma(c * out["volume"], 20)
    out["high_52w"] = out["high"].rolling(252, min_periods=200).max()
    out["low_52w"] = out["low"].rolling(252, min_periods=200).min()
    out["ret_63"] = c / c.shift(63) - 1
    out["ret_126"] = c / c.shift(126) - 1
    out["bbw"] = bollinger_width(c, 20)
    out["obv"] = obv(out)
    out["obv_slope"] = out["obv"] - out["obv"].shift(20)
    out["chandelier"] = chandelier_stop(out, 22, 3.0)
    out["swing_low_10"] = out["low"].rolling(10, min_periods=10).min()
    out["swing_high_20"] = out["high"].rolling(20, min_periods=20).max()
    return out


def weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily candles to weekly (week ending Friday)."""
    w = df.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return w.dropna(subset=["close"])


def find_resistance(df: pd.DataFrame, price: float, lookback: int = 250, pivot: int = 5) -> float | None:
    """Nearest swing-high pivot above ``price`` within ``lookback`` bars.

    A pivot high is a bar whose high is the highest of the ``pivot`` bars on each side.
    Returns None if there is no overhead resistance (price at/near new highs).
    """
    hist = df.iloc[-lookback:]
    highs = hist["high"].to_numpy()
    levels = []
    for i in range(pivot, len(highs) - pivot):
        window = highs[i - pivot : i + pivot + 1]
        if highs[i] == window.max() and highs[i] > price * 1.005:
            levels.append(highs[i])
    return float(min(levels)) if levels else None
