"""Options B and C: monthly decisions on the close, executed next open; limits respected."""
import numpy as np
import pandas as pd

from swingbot.costs import CostModel
from swingbot.portfolio import backtest_index_trend, backtest_momentum_rotation, month_end_days
from swingbot.synthetic import make_market


def _index(path):
    idx = pd.bdate_range("2014-01-01", periods=len(path))
    c = np.asarray(path, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.001, "low": c * 0.999, "close": c, "volume": 0.0}, index=idx)


def test_index_trend_follows_trend():
    costs = CostModel(slippage_pct=0.0)
    up = _index(np.linspace(100, 300, 900))
    r = backtest_index_trend(up, up.index[300], up.index[-1], 10_000, costs)
    assert r.exposure_pct > 90 and r.equity.iloc[-1] > 10_000 * 1.5
    down = _index(np.linspace(300, 100, 900))
    r2 = backtest_index_trend(down, down.index[300], down.index[-1], 10_000, costs)
    assert r2.exposure_pct == 0 and abs(r2.equity.iloc[-1] - 10_000) < 1


def test_trades_execute_day_after_month_end():
    stocks, idx, _ = make_market(n_stocks=30, years=5, seed=4)
    costs = CostModel.from_settings({"zerodha": {}, "slippage_pct": 0.15}, "zerodha")
    res = backtest_momentum_rotation(stocks, idx, idx.index[300], idx.index[-1], 1_000_000, costs, min_value_cr=0)
    me = set(month_end_days(idx.index))
    next_days = {idx.index[idx.index.get_loc(d) + 1].date() for d in me if idx.index.get_loc(d) + 1 < len(idx)}
    real = [t for t in res.trades if t["exit_reason"] == "rebalance"]
    assert real, "expected some rebalancing trades"
    for t in real:
        assert t["entry_date"] in next_days and t["exit_date"] in next_days
    assert (res.equity > 0).all()


def test_rotation_never_exceeds_top_n_and_random_control_differs():
    stocks, idx, _ = make_market(n_stocks=40, years=5, seed=8)
    costs = CostModel(slippage_pct=0.15)
    mom = backtest_momentum_rotation(stocks, idx, idx.index[300], idx.index[-1], 1_000_000, costs, top_n=5, min_value_cr=0)
    open_at = {}
    for t in mom.trades:
        for d in pd.bdate_range(t["entry_date"], t["exit_date"])[:-1]:
            open_at[d] = open_at.get(d, 0) + 1
    assert max(open_at.values()) <= 5
    rnd = backtest_momentum_rotation(stocks, idx, idx.index[300], idx.index[-1], 1_000_000, costs, top_n=5,
                                     min_value_cr=0, pick="random", seed=1)
    assert [t["symbol"] for t in rnd.trades] != [t["symbol"] for t in mom.trades]
