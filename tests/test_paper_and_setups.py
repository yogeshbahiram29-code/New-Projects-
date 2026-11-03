from datetime import date

import numpy as np
import pandas as pd
import pytest

from swingbot.analysis import prepare_stock
from swingbot.backtest import Backtester
from swingbot.brokers.base import BrokerError, CANCELLED, FILLED
from swingbot.brokers.paper import PaperBroker
from swingbot.config import load_settings
from swingbot.setups import detect_breakout
from swingbot.synthetic import make_market


# ---------------------------------------------------------------- paper broker
@pytest.fixture
def paper(tmp_path, costs):
    b = PaperBroker(tmp_path / "paper.json", 10_000, costs)
    b.connect()
    return b


def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_paper_entry_fills_above_trigger_only(paper):
    oid = paper.place_entry("ABC", 10, trigger=100, limit=100.5, ref="1")
    paper.simulate_day({"ABC": _bar(98, 99.5, 97, 99)}, date(2026, 1, 5))
    assert paper.order_status(oid).status == CANCELLED  # never traded above trigger; DAY order expired
    oid = paper.place_entry("ABC", 10, trigger=100, limit=100.5, ref="2")
    paper.simulate_day({"ABC": _bar(99, 103, 98, 102)}, date(2026, 1, 6))
    st = paper.order_status(oid)
    assert st.status == FILLED and 100 <= st.avg_price <= 100.5
    assert paper.holdings()["ABC"].quantity == 10


def test_paper_no_chasing_gap_above_limit(paper):
    oid = paper.place_entry("ABC", 10, trigger=100, limit=100.5, ref="1")
    paper.simulate_day({"ABC": _bar(104, 106, 103, 105)}, date(2026, 1, 5))
    assert paper.order_status(oid).status == CANCELLED


def test_paper_oco_stop_gap_and_target(paper):
    paper.place_entry("ABC", 10, trigger=100, limit=101, ref="1")
    paper.simulate_day({"ABC": _bar(100, 101, 99.5, 100.5)}, date(2026, 1, 5))
    pid = paper.place_protection("ABC", 10, stop=95, stop_limit=90, target=115, last_price=100, ref="p")
    paper.simulate_day({"ABC": _bar(92, 93, 90, 91)}, date(2026, 1, 6))  # gaps below the stop
    px, why = paper.protection_fill(pid)
    assert px < 95 and "gap" in why and "ABC" not in paper.holdings()
    with pytest.raises(BrokerError):
        paper.sell_market("ABC", 1, "x")  # can't sell what you don't have


def test_paper_target(paper):
    paper.place_entry("XYZ", 5, trigger=200, limit=201, ref="1")
    paper.simulate_day({"XYZ": _bar(200, 201, 199, 200)}, date(2026, 1, 5))
    pid = paper.place_protection("XYZ", 5, stop=190, stop_limit=180, target=220, last_price=200, ref="p")
    paper.simulate_day({"XYZ": _bar(210, 222, 209, 221)}, date(2026, 1, 6))
    assert paper.protection_fill(pid) == (220, "target")


# ---------------------------------------------------------------- setups
def _breakout_frame():
    n_trend, n_base = 260, 25
    trend = np.linspace(100, 200, n_trend)
    rng = np.random.default_rng(0)
    base = 200 + rng.uniform(-2.5, 2.5, n_base)
    close = np.concatenate([trend, base, [209.0]])
    idx = pd.bdate_range("2024-01-01", periods=len(close))
    vol = np.full(len(close), 1e5)
    vol[-1] = 3.5e5
    df = pd.DataFrame({"open": close * 0.998, "high": close * 1.005, "low": close * 0.995, "close": close, "volume": vol}, index=idx)
    df.iloc[-1, df.columns.get_loc("open")] = 203.5
    df.iloc[-1, df.columns.get_loc("low")] = 203.0
    df.iloc[-1, df.columns.get_loc("high")] = 210.0
    return df


def test_detect_breakout():
    df = prepare_stock(_breakout_frame())
    res = detect_breakout("ABC", df, {"min_volume_ratio": 1.5, "max_extension_pct": 5, "entry_buffer_pct": 0.1,
                                      "entry_limit_slippage_pct": 0.5, "min_stop_distance_pct": 1.5, "max_stop_distance_pct": 8})
    assert res is not None
    p = res.plan
    assert p.stop_price < p.trigger_price < p.limit_price
    assert p.target_price > p.trigger_price
    assert p.trigger_price == pytest.approx(210 * 1.001, rel=1e-3)
    assert 0 < res.quality <= 1


def test_no_breakout_without_volume():
    raw = _breakout_frame()
    raw.iloc[-1, raw.columns.get_loc("volume")] = 1e5
    df = prepare_stock(raw)
    assert detect_breakout("ABC", df, {"min_volume_ratio": 1.5}) is None


# ---------------------------------------------------------------- backtest invariants
def test_backtest_invariants():
    s = load_settings(overrides={"mode": "backtest", "initial_capital": 1_000_000})
    stocks, idx, sectors = make_market(n_stocks=25, years=5, seed=11)
    res = Backtester(s).run(stocks, idx, start=str(idx.index[300].date()), sectors=sectors)
    assert (res.equity > 0).all()
    assert len(res.trades) > 10
    for t in res.trades:
        assert t.entry_date <= t.exit_date
        if "gap" not in t.exit_reason and t.exit_reason != "end of backtest":
            # a normal stop-out can't lose much more than 1R (slippage + charges only)
            assert t.r_multiple > -1.6, t
        assert t.costs > 0
