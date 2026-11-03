"""Synthetic market data for tests and demos.

Used only to prove the pipeline runs end to end. It has no predictive value;
never judge the strategy on synthetic results.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _ohlcv(close: np.ndarray, rng: np.random.Generator, base_volume: float, dates: pd.DatetimeIndex) -> pd.DataFrame:
    n = len(close)
    prev = np.concatenate([[close[0]], close[:-1]])
    open_ = prev * (1 + rng.normal(0, 0.004, n))
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.007, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.007, n)))
    ret = close / prev - 1
    vol = base_volume * np.exp(rng.normal(0, 0.35, n)) * (1 + 8 * np.clip(ret, 0, None) + 3 * np.abs(ret))
    return pd.DataFrame(
        {"open": open_.round(2), "high": hi.round(2), "low": lo.round(2), "close": close.round(2), "volume": vol.round(0)},
        index=dates,
    )


def make_market(
    n_stocks: int = 40, years: float = 6, seed: int = 42, start: str = "2019-01-01"
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict[str, str]]:
    """Return (stocks, index, sectors) with regime-switching trends."""
    rng = np.random.default_rng(seed)
    n = int(years * 252)
    dates = pd.bdate_range(start=start, periods=n)

    # index: alternating bull / chop / bear phases
    drift = np.empty(n)
    i = 0
    while i < n:
        length = int(rng.integers(80, 260))
        mu = rng.choice([0.0009, 0.0002, -0.0007], p=[0.5, 0.3, 0.2])
        drift[i : i + length] = mu
        i += length
    idx_ret = drift + rng.normal(0, 0.009, n)
    idx_close = 10000 * np.cumprod(1 + idx_ret)
    index_df = _ohlcv(idx_close, rng, 1e8, dates)

    sectors_list = ["IT", "BANK", "PHARMA", "AUTO", "FMCG", "METAL", "ENERGY", "INFRA"]
    stocks, sectors = {}, {}
    for k in range(n_stocks):
        beta = rng.uniform(0.6, 1.4)
        own = np.empty(n)
        j = 0
        while j < n:
            length = int(rng.integers(40, 180))
            own[j : j + length] = rng.normal(0.0002, 0.0012)
            j += length
        ret = beta * idx_ret + own + rng.normal(0, 0.014, n)
        price0 = rng.uniform(80, 2500)
        close = price0 * np.cumprod(1 + ret)
        sym = f"STOCK{k:02d}"
        stocks[sym] = _ohlcv(close, rng, rng.uniform(3e5, 3e6), dates)
        sectors[sym] = sectors_list[k % len(sectors_list)]
    return stocks, index_df, sectors
