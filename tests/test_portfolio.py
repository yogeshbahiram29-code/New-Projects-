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


def test_stock_report_runs_and_handles_unknown_symbol():
    from swingbot.config import load_settings
    from swingbot.stock_report import analyze_stock

    stocks, idx, sectors = make_market(n_stocks=25, years=3, seed=3)
    s = load_settings(overrides={"universe": {"min_avg_traded_value_cr": 0}})
    rep = analyze_stock("STOCK05", stocks, idx, s, sectors)
    assert "BOT SCORE" in rep and "VERDICT" in rep and "Not investment advice" in rep
    assert "no price data" in analyze_stock("NOPE", stocks, idx, s, sectors)


def test_top_picks_respect_count_sector_cap_and_track_record(tmp_path):
    from swingbot.config import load_settings
    from swingbot.top_picks import format_picks, record_and_evaluate, top_swing_picks

    stocks, idx, sectors = make_market(n_stocks=40, years=3, seed=6)
    s = load_settings(overrides={"universe": {"min_avg_traded_value_cr": 0}})
    hist = tmp_path / "top5.csv"
    track = {}
    for d in idx.index[-40::5]:  # several daily lists, then enough days pass to score them
        cut = {k: v.loc[:d] for k, v in stocks.items()}
        picks = top_swing_picks(cut, idx.loc[:d], s, sectors, n=5, max_per_sector=2)
        assert len(picks) <= 5
        if len(picks):
            assert picks["sector"].value_counts().max() <= 2
            assert (picks["stop"] < picks["trigger"]).all()
        track = record_and_evaluate(picks, cut, idx.loc[:d], hist, eval_days=10)
    assert track["lists"] >= 2 and track["evaluated"] > 0
    text = format_picks(picks, track, True, idx.index[-1])
    assert text.startswith("📊 TOP 5 SWING WATCHLIST") and "Paper/learning only" in text
    assert "🎯 Target 1" in text or picks.empty
    if len(picks):
        assert (picks["stop"] < picks["trigger"]).all() and (picks["trigger"] < picks["t1"]).all() and (picks["t1"] <= picks["t2"]).all()
    wide = pd.DataFrame([{"symbol": "X", "sector": "S", "score": 80.0, "rs_pct": 90.0, "close": 9000.0, "ret_6m_pct": 50.0,
                          "setup": "breakout", "trigger": 9010.0, "stop": 8000.0, "stop_pct": 11.2,
                          "t1": 11030.0, "t1_pct": 22.4, "t2": 12040.0, "t2_pct": 33.6}])
    flagged = format_picks(wide, {}, True, idx.index[-1])
    assert "stop wider than the 8% rule" in flagged and "1 share >" in flagged
