from datetime import date

import numpy as np
import pandas as pd

from swingbot import indicators as ind
from swingbot.costs import CostModel
from swingbot.exits import manage_position
from swingbot.models import Position, Regime


# ---------------------------------------------------------------- costs
def test_zerodha_delivery_costs(costs):
    buy = costs.buy_cost(4000)
    sell = costs.sell_cost(4000)
    assert 4.5 < buy < 5.5  # STT ₹4 + stamp ₹0.6 + small fees
    assert 19 < sell < 21  # STT ₹4 + DP ₹15.93
    assert 24 < costs.round_trip(4000) < 26


def test_groww_brokerage_min_and_cap(settings):
    g = CostModel.from_settings(settings.section("costs"), "groww")
    assert g.brokerage(1000) == 5.0  # 0.1% = ₹1 -> min ₹5
    assert g.brokerage(50_000) == 20.0  # 0.1% = ₹50 -> cap ₹20


# ---------------------------------------------------------------- indicators
def _frame(close):
    close = np.asarray(close, dtype=float)
    idx = pd.bdate_range("2024-01-01", periods=len(close))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e5}, index=idx)


def test_rsi_bounds_and_direction():
    up = ind.rsi(pd.Series(np.linspace(100, 200, 60)), 14)
    down = ind.rsi(pd.Series(np.linspace(200, 100, 60)), 14)
    assert up.iloc[-1] > 95 and down.iloc[-1] < 5
    rnd = ind.rsi(pd.Series(100 + np.random.default_rng(1).normal(0, 1, 300).cumsum()), 14).dropna()
    assert rnd.between(0, 100).all()


def test_sma_and_atr():
    df = _frame(np.arange(1, 31))
    assert ind.sma(df["close"], 5).iloc[-1] == 28.0
    assert (ind.atr(df, 14).dropna() > 0).all()


def test_find_resistance():
    close = [100] * 10 + [120] + [100] * 20
    df = _frame(close)
    assert ind.find_resistance(df, 105) == df["high"].iloc[10]
    assert ind.find_resistance(df, 125) is None


# ---------------------------------------------------------------- exits
def _row(close, high=None, low=None, ema20=None, chandelier=None, atr=2.0):
    return pd.Series({"open": close, "high": high or close, "low": low or close, "close": close,
                      "ema20": ema20 if ema20 is not None else close * 0.97, "chandelier": chandelier,
                      "atr14": atr, "sma5": close})


def _pos():
    return Position("ABC", "pullback", date(2026, 1, 1), 100.0, 10, 95.0, 95.0, 115.0, "IT")


CFG = {"breakeven_at_r": 1.0, "partial_at_r": 2.0, "trail_ema": 20, "time_stop_days": 10, "exit_before_earnings_days": 1}


def test_breakeven_after_1r():
    p = _pos()
    r = manage_position(p, _row(104, high=105.5), Regime.BULL, CFG, False, per_share_costs=0.5)
    assert p.reached_1r and r.new_stop == 100.5


def test_stop_never_lowered():
    p = _pos()
    p.stop_price = 103.0
    r = manage_position(p, _row(104, high=104, chandelier=98.0), Regime.BULL, CFG, False)
    assert r.new_stop is None


def test_trailing_chandelier_raises_stop():
    p = _pos()
    p.reached_1r = True
    p.stop_price = 100.0
    r = manage_position(p, _row(112, high=113, chandelier=106.0), Regime.BULL, CFG, False)
    assert r.new_stop == 106.0


def test_time_stop():
    p = _pos()
    p.bars_held = 9
    r = manage_position(p, _row(101, high=102), Regime.BULL, CFG, False)
    assert r.exit_reason and "time stop" in r.exit_reason


def test_trend_break_after_1r_and_earnings_exit():
    p = _pos()
    p.reached_1r = True
    p.stop_price = 100.0
    r = manage_position(p, _row(103, ema20=104.0), Regime.BULL, CFG, False)
    assert "trend break" in r.exit_reason
    p2 = _pos()
    r2 = manage_position(p2, _row(101), Regime.BULL, CFG, False, earnings_within_days=1)
    assert "earnings" in r2.exit_reason


def test_bear_regime_tightens_stop():
    p = _pos()
    r = manage_position(p, _row(110, high=110, atr=2.0), Regime.BEAR, CFG, False)
    assert r.new_stop == 107.0  # 110 - 1.5 * ATR


def test_partial_flag():
    p = _pos()
    r = manage_position(p, _row(111, high=111), Regime.BULL, CFG, True)
    assert r.partial_due
    p2 = _pos()
    assert not manage_position(p2, _row(111, high=111), Regime.BULL, CFG, False).partial_due
