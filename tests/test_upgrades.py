"""Upgrades 1-3: historical odds, fundamentals, market dashboard."""
import numpy as np
import pandas as pd

from swingbot.base_rates import build_events, format_odds, lookup, score_band, simulate, summarize
from swingbot.config import load_settings
from swingbot.dashboard import breadth, market_dashboard, sector_strength
from swingbot.fundamentals import assess, format_fundamentals
from swingbot.synthetic import make_market
from swingbot.top_picks import format_picks


def _arr(*rows):
    o, h, lo, c = (np.array(x, dtype=float) for x in zip(*rows))
    return o, h, lo, c


# ---------------------------------------------------------------- 1. odds
def test_simulate_outcomes():
    # bar 0 = signal day; trigger 101, stop 95, T1 = 113
    base = (100, 100.5, 99, 100)
    o, h, lo, c = _arr(base, (100.5, 104, 100, 103), (103, 114, 102, 113))
    assert simulate(o, h, lo, c, 0, 101, 95, 113)[0] == "target"
    o, h, lo, c = _arr(base, (100.5, 102, 99, 100), (99, 99.5, 94, 95))
    out, r, days = simulate(o, h, lo, c, 0, 101, 95, 113)
    assert out == "stop" and abs(r + 1) < 1e-9 and days == 2
    o, h, lo, c = _arr(base, (100.5, 102, 99, 100), (90, 91, 89, 90))  # gap below the stop
    out, r, _ = simulate(o, h, lo, c, 0, 101, 95, 113)
    assert out == "stop" and r < -1
    o, h, lo, c = _arr(base, (99, 100.8, 98, 100))  # never traded above the trigger
    assert simulate(o, h, lo, c, 0, 101, 95, 113)[0] == "not_triggered"
    o, h, lo, c = _arr(base, *[(101, 102, 100, 101.5)] * 25)
    out, r, days = simulate(o, h, lo, c, 0, 101, 95, 113, horizon=20)
    assert out == "timeout" and days == 20


def test_build_summarize_lookup():
    stocks, idx, _ = make_market(n_stocks=15, years=3, seed=4)
    s = load_settings(overrides={"universe": {"min_avg_traded_value_cr": 0}})
    ev = build_events(stocks, idx, s, idx.index[300])
    assert len(ev) > 50 and set(ev["outcome"]) <= {"not_triggered", "target", "stop", "timeout"}
    table = summarize(ev)
    trig = table[["target_first_pct", "stop_first_pct", "neither_pct"]].sum(axis=1)
    assert ((trig - 100).abs() < 0.5).all()
    any_setup = table.iloc[0]["setup"]
    hit = lookup(table, any_setup, 75, True, min_cases=1)
    assert hit is not None and "level" in hit
    assert lookup(table, any_setup, 75, True, min_cases=10**9) is None  # not enough cases -> nothing shown
    assert "not enough history" in format_odds(None)[0] and "similar cases" in format_odds(hit)[0]
    assert score_band(59.9) == "<60" and score_band(85) == "80-90" and score_band(100) == "90+"


# ---------------------------------------------------------------- 2. fundamentals
def test_fundamental_flags():
    weak = {"trailingEps": -2, "profitMargins": -0.05, "debtToEquity": 220.0, "trailingPE": None,
            "earningsGrowth": -0.4, "returnOnEquity": 0.02, "marketCap": 5e10}
    warn, good = assess(weak, "Capital Goods")
    assert "Loss-making" in warn and any("High debt" in w for w in warn) and any("Profit falling" in w for w in warn)
    strong = {"trailingEps": 40, "profitMargins": 0.18, "debtToEquity": 10.0, "trailingPE": 25,
              "earningsGrowth": 0.35, "returnOnEquity": 0.22, "marketCap": 2.3e11}
    warn, good = assess(strong, "Information Technology")
    assert not warn and any("High ROE" in g for g in good) and "Low debt" in good
    bank = {"debtToEquity": 900.0, "trailingEps": 10}
    assert not any("debt" in w.lower() for w in assess(bank, "Financial Services")[0])
    lines = format_fundamentals(strong, "IT")
    assert lines[0].startswith("🏢 Fundamentals: MCap ₹23,000 Cr") and "ROE 22%" in lines[0] and "D/E 0.10" in lines[0]
    assert "not available" in format_fundamentals(None)[0]


# ---------------------------------------------------------------- 3. dashboard
def test_market_dashboard():
    stocks, idx, sectors = make_market(n_stocks=40, years=2, seed=9)
    when = idx.index[-1]
    b = breadth(stocks, when)
    assert b["n"] > 0 and 0 <= b["above200"] <= 100 and b["adv"] + b["dec"] <= b["n"]
    assert len(sector_strength(stocks, sectors, when)) >= 6
    fake = idx.iloc[-60:].copy()
    text = market_dashboard(stocks, idx, sectors, {"INDIAVIX": fake.assign(close=22.0), "SP500": fake, "USDINR": fake})
    for part in ("🌍 MARKET DASHBOARD", "📊 MARKET BREADTH", "🏆 STRONGEST SECTORS", "🌐 GLOBAL CUES",
                 "😨 India VIX: 22.0", "🧭 WHAT IT MEANS", "Volatility is elevated"):
        assert part in text, part
    assert "🌐 GLOBAL CUES" not in market_dashboard(stocks, idx, sectors, {})  # missing global data is skipped


def test_watchlist_shows_odds_and_fundamentals():
    pick = pd.DataFrame([{"symbol": "X", "sector": "IT", "score": 82.0, "rs_pct": 95.0, "close": 500.0, "ret_6m_pct": 40.0,
                          "setup": "breakout", "trigger": 505.0, "stop": 480.0, "stop_pct": 5.0,
                          "t1": 555.0, "t1_pct": 9.9, "t2": 580.0, "t2_pct": 14.9}])
    odds = {"X": {"period": "2015-2026", "cases": 1240, "target_first_pct": 41.0, "stop_first_pct": 47.0,
                  "neither_pct": 12.0, "avg_r": 0.15, "triggered_pct": 38.0}}
    text = format_picks(pick, {}, True, pd.Timestamp("2026-11-02"), odds=odds,
                        fundamentals={"X": {"marketCap": 1e11, "trailingPE": 30.0, "returnOnEquity": 0.2}})
    assert "🎲 History (2015-2026, 1,240 similar cases): 🎯 T1 first 41%" in text
    assert "🏢 Fundamentals: MCap ₹10,000 Cr | P/E 30.0 | ROE 20%" in text


def test_unfinished_bar_dropped_only_during_market_hours():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from swingbot.data import drop_unfinished_bar

    ist = ZoneInfo("Asia/Kolkata")
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    assert len(drop_unfinished_bar(df, datetime(2026, 11, 4, 14, 56, tzinfo=ist))) == 1
    assert len(drop_unfinished_bar(df, datetime(2026, 11, 4, 16, 45, tzinfo=ist))) == 2
    assert len(drop_unfinished_bar(df, datetime(2026, 11, 5, 10, 0, tzinfo=ist))) == 2


def test_odds_labels_are_conservative():
    base = {"period": "2015-2026", "cases": 500, "target_first_pct": 30.0, "stop_first_pct": 50.0,
            "neither_pct": 20.0, "triggered_pct": 45.0}
    assert "no real edge" in format_odds({**base, "avg_r": 0.03})[1]
    assert "thin edge" in format_odds({**base, "avg_r": 0.12})[1]
    assert "positive edge" in format_odds({**base, "avg_r": 0.3})[1]
    assert format_fundamentals({"earningsGrowth": -0.001, "marketCap": 1e10})[0].endswith("Profit growth 0%")
