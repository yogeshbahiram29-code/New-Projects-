"""Prediction tracker: every watchlist prediction checked every session, with uncertainty alerts."""
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd

from swingbot.base_rates import simulate
from swingbot.tracker import (DEFAULTS, format_pick_uncertainty, format_tracker, load_ledger, pick_uncertainty,
                              record_predictions, save_ledger, scorecard, track_one, uncertainty_alerts,
                              update_ledger)


def _df(rows, start="2026-11-02"):
    o, h, lo, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": 1e6},
                        index=pd.bdate_range(start, periods=len(rows)))


SIG = (100, 100.5, 99, 100)  # signal day: trigger 101, stop 95, T1 113


def test_track_one_statuses():
    assert track_one(_df([SIG]), "2026-11-02", 101, 95, 113)["status"] == "WAITING"
    assert track_one(_df([SIG, (99, 100.8, 98, 100)]), "2026-11-02", 101, 95, 113)["status"] == "NOT_TRIGGERED"
    assert track_one(_df([SIG, (104, 105, 103, 104)]), "2026-11-02", 101, 95, 113)["status"] == "MISSED"
    a = track_one(_df([SIG, (100.5, 103, 100, 102), (102, 104, 101, 103)]), "2026-11-02", 101, 95, 113)
    assert a["status"] == "ACTIVE" and a["entry"] == 101 and a["days"] == 2 and abs(a["r"] - 2 / 6) < 0.01
    w = track_one(_df([SIG, (100.5, 104, 100, 103), (103, 114, 102, 113)]), "2026-11-02", 101, 95, 113)
    assert w["status"] == "WORKED" and w["r"] == 2.0 and w["days"] == 2
    f = track_one(_df([SIG, (100.5, 102, 99, 100), (90, 91, 89, 90)]), "2026-11-02", 101, 95, 113)
    assert f["status"] == "FAILED" and f["gap"] and f["r"] < -1
    e = track_one(_df([SIG] + [(101, 102, 100, 101.5)] * 25), "2026-11-02", 101, 95, 113, horizon=20)
    assert e["status"] == "EXPIRED" and e["days"] == 20


def test_matches_historical_odds_rules():
    rng = np.random.default_rng(3)
    for _ in range(200):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 30)))
        o = c * (1 + rng.normal(0, 0.005, 30))
        h, lo = np.maximum(o, c) * 1.01, np.minimum(o, c) * 0.99
        df = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c}, index=pd.bdate_range("2026-01-01", periods=30))
        trig, stop = h[0] * 1.001, h[0] * 0.95
        t1 = trig + 2 * (trig - stop)
        exp = simulate(o, h, lo, c, 0, trig, stop, t1, 20)
        got = track_one(df, df.index[0], trig, stop, t1, 20)
        name = {"target": "WORKED", "stop": "FAILED", "timeout": "EXPIRED", "not_triggered": None}[exp[0]]
        if name:
            assert got["status"] == name and abs(got["r"] - exp[1]) < 0.01
        else:
            assert got["status"] in ("NOT_TRIGGERED", "MISSED")


def _picks(*syms):
    return pd.DataFrame([{"symbol": s, "sector": "IT", "setup": "breakout", "score": 80.0, "close": 100.0,
                          "trigger": 101.0, "stop": 95.0, "t1": 113.0, "t2": 119.0} for s in syms])


def test_ledger_lifecycle_and_repeats(tmp_path):
    path = tmp_path / "predictions.csv"
    raw = {"AAA": _df([SIG]), "BBB": _df([SIG])}
    led = record_predictions(load_ledger(path), _picks("AAA", "BBB"), "2026-11-02", True,
                             {"AAA": {"target_first_pct": 25.0, "avg_r": 0.05}})
    led, changed = update_ledger(led, raw)
    assert set(led["status"]) == {"WAITING"} and changed.empty
    save_ledger(led, path)
    # next session: AAA triggers, BBB doesn't; AAA is picked again (must not be double counted)
    raw = {"AAA": _df([SIG, (100.5, 103, 100, 102)]), "BBB": _df([SIG, (99, 100, 98, 99)])}
    led = record_predictions(load_ledger(path), _picks("AAA"), "2026-11-03", True)
    led, changed = update_ledger(led, raw)
    st = dict(zip(led["id"], led["status"]))
    assert st == {"2026-11-02:AAA": "ACTIVE", "2026-11-02:BBB": "NOT_TRIGGERED", "2026-11-03:AAA": "REPEAT"}
    assert set(changed["status"]) == {"ACTIVE", "NOT_TRIGGERED", "REPEAT"}
    save_ledger(led, path)
    # third session: AAA hits target 1
    raw["AAA"] = _df([SIG, (100.5, 103, 100, 102), (102, 114, 101, 113)])
    led, changed = update_ledger(load_ledger(path), raw)
    assert changed.iloc[0]["status"] == "WORKED"
    card = scorecard(led)
    assert card["n"] == 2 and card["closed"] == 1 and card["worked"] == 1 and card["exp_hit_pct"] == 25.0
    from swingbot.tgfmt import plain

    txt = plain(format_tracker(led, changed, card, [], "2026-11-04"))
    assert "🟢 AAA        T1 hit +2.0R 2d" in txt
    assert "RISK ALERTS\n🟢 None today." in txt and "Need 10+" in txt


def test_same_day_rerun_replaces_list():
    led = record_predictions(load_ledger(__import__("pathlib").Path("/nonexistent")), _picks("AAA", "BBB"),
                             "2026-11-04", True)
    led = record_predictions(led, _picks("CCC"), "2026-11-04", False)
    assert list(led["symbol"]) == ["CCC"]


def _closed(statuses, exp=25.0):
    rows = []
    for i, s in enumerate(statuses):
        rows.append({"id": f"{i}", "date": f"2026-0{1 + i // 28}-{1 + i % 28:02d}", "symbol": f"S{i}", "status": s,
                     "trigger": 101.0, "stop": 95.0, "t1": 113.0, "r": 2.0 if s == "WORKED" else -1.0,
                     "exit_date": f"2026-0{1 + i // 28}-{1 + i % 28:02d}", "exp_t1_pct": exp, "regime_on": True})
    return pd.DataFrame(rows)


def test_alerts_drift_streak_and_too_good():
    led = _closed(["WORKED"] + ["FAILED"] * 14)
    card = scorecard(led)
    a = " ".join(uncertainty_alerts(led, led.iloc[0:0], card, DEFAULTS))
    assert "in a row hit the stop-loss" in a and "WORSE than history" not in a  # 1/15 vs 25%: p ≈ 0.08
    led = _closed(["WORKED"] * 2 + ["FAILED"] * 30)
    a = " ".join(uncertainty_alerts(led, led.iloc[0:0], scorecard(led), DEFAULTS))
    assert "WORSE than history" in a
    led = _closed(["WORKED"] * 12)
    a = " ".join(uncertainty_alerts(led, led.iloc[0:0], scorecard(led), DEFAULTS))
    assert "suspiciously high" in a


def test_alerts_market_data_and_open_trades():
    led = pd.DataFrame([{"id": "x", "date": "2026-11-02", "symbol": "AAA", "status": "ACTIVE", "r": -0.8,
                         "regime_on": True, "trigger": 101.0, "stop": 95.0},
                        {"id": "y", "date": "2026-11-04", "symbol": "ZZZ", "status": "WAITING", "regime_on": False,
                         "trigger": 101.0, "stop": 95.0}])
    changed = pd.DataFrame([{"symbol": "BBB", "status": "FAILED", "gap": True, "r": -1.6}])
    idx = pd.DataFrame({"close": [100.0, 97.5]}, index=pd.bdate_range("2026-11-02", periods=2))
    vix = pd.DataFrame({"close": [18.0, 21.0]}, index=idx.index)
    news = {"AAA": SimpleNamespace(ok=True, flags=["📅 results/board meeting"])}
    paper = {"start_capital": 10000, "history": [["d", 10000, 1], ["d", 8800, 1]]}
    a = uncertainty_alerts(led, changed, {"n": 2}, DEFAULTS, index_df=idx, regime_on=False, extra={"INDIAVIX": vix},
                           raw={"AAA": idx}, sources={"news_failed": ["QQQ"], "delivery_ok": False}, news=news,
                           paper=paper, now=datetime(2026, 11, 4, 17, 0))
    t = " ".join(a)
    for part in ("Latest price data is Tue 03 Nov", "No price data for open prediction(s): ZZZ", "gapped below the stop",
                 "AAA is close to its stop-loss", "Market filter turned OFF after AAA",
                 "Results/board-meeting news on open predictions: AAA",
                 "NIFTY moved -2.5%", "India VIX 21.0", "News could not be checked for: QQQ",
                 "delivery % unavailable", "12.0% below its peak"):
        assert part in t, part


def test_pick_uncertainty_line():
    assert format_pick_uncertainty([]).startswith("Uncertainty: LOW")
    r = pick_uncertainty({"verdict": "DISTRIBUTION"}, {"avg_r": 0.03}, True, {"trailingPE": 300.0}, True, "IT",
                         SimpleNamespace(ok=True, flags=["📅 results/board meeting"]))
    assert r == ["money flowing out (distribution)", "history shows no real edge",
                 "fundamental red flag (very expensive)", "results/board meeting – gap risk"]
    assert format_pick_uncertainty(r).startswith("Uncertainty: HIGH – money flowing out")
    assert pick_uncertainty(None, None, True) == ["too little history for this setup"]
    assert format_pick_uncertainty(pick_uncertainty(None, {"avg_r": 0.1}, True)).startswith("Uncertainty: MEDIUM – thin")


def test_fill_expectations_for_rebuilt_rows():
    from swingbot.tracker import fill_expectations

    table = pd.DataFrame([{"setup": "breakout", "band": "all", "regime": "on", "cases": 500, "target_first_pct": 24.0,
                           "avg_r": 0.07}])
    led = record_predictions(load_ledger(__import__("pathlib").Path("/nonexistent")), _picks("AAA"), "2026-11-02", True)
    out = fill_expectations(led, table)
    assert out.iloc[0]["exp_t1_pct"] == 24.0 and fill_expectations(led, None) is led
