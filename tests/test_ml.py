"""Upgrade 5: machine-learning out-of-sample test."""
import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from swingbot.base_rates import build_events
from swingbot.config import load_settings
from swingbot.ml import daily_top, format_report, run_ml_test, split
from swingbot.synthetic import make_market


def _events(n=6000, signal=0.0, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2015-01-01", "2026-10-30")
    x = rng.normal(size=n)
    p = 1 / (1 + np.exp(-(signal * x - 0.9)))                       # ~30% T1-first base rate
    win = rng.random(n) < p
    trig = rng.random(n) < 0.45
    outcome = np.where(~trig, "not_triggered", np.where(win, "target", "stop"))
    r = np.where(outcome == "target", 2.0, np.where(outcome == "stop", -1.0, 0.0))
    return pd.DataFrame({"date": np.sort(rng.choice(dates, n)).astype("datetime64[ns]"), "symbol": "S",
                         "setup": "breakout", "band": "70-80", "regime": "on", "outcome": outcome, "r": r, "days": 5,
                         "score": rng.uniform(50, 95, n), "stop_pct": rng.uniform(3, 8, n), "x": x,
                         "noise": rng.normal(size=n)})


def test_split_has_gap_and_no_overlap():
    tr, te = split(_events(), "2023-01-01", 31)
    assert pd.to_datetime(tr["date"]).max() < pd.Timestamp("2022-12-01")
    assert pd.to_datetime(te["date"]).min() >= pd.Timestamp("2023-01-01")


def test_noise_is_not_adopted():
    res = run_ml_test(_events(signal=0.0))
    for m in res["models"].values():
        assert abs(m["auc"] - 0.5) < 0.06 and not m["adopted"]
    from swingbot.tgfmt import plain

    txt = plain(format_report(res))
    assert "NOT ADOPTED" in txt and "keeps the simple score" in txt


def test_real_signal_is_found_and_adopted():
    res = run_ml_test(_events(signal=1.5, n=8000), cost_pct=0.3)
    lr = res["models"]["Logistic regression"]
    assert lr["auc"] > 0.65 and lr["top20_net_r"] > res["score"]["top20_net_r"] + 0.1
    assert lr["drivers"][0][0] == "x" and lr["adopted"]
    assert res["red_flags"]  # an AUC this high must be questioned, not celebrated
    import re

    from swingbot.tgfmt import plain

    assert re.search(r"^ADOPT$", plain(format_report(res)), re.M)


def test_daily_top_counts_only_triggered():
    c = pd.DataFrame({"date": ["d1"] * 3 + ["d2"] * 2, "outcome": ["target", "not_triggered", "stop", "stop", "target"],
                      "net_r": [2.0, -0.1, -1.0, -1.0, 2.0], "s": [3, 2, 1, 2, 1]})
    avg, n = daily_top(c, "s", k=2)
    assert n == 3 and abs(avg - (2.0 - 1.0 + 2.0) / 3) < 1e-9


def test_features_from_real_pipeline():
    stocks, idx, _ = make_market(n_stocks=12, years=3, seed=5)
    s = load_settings(overrides={"universe": {"min_avg_traded_value_cr": 0}})
    ev = build_events(stocks, idx, s, idx.index[300], features=True)
    assert len(ev) > 30
    for col in ("score", "rs_pct", "rsi14", "stop_pct", "d_high52", "regime_on", "is_breakout"):
        assert col in ev.columns
    assert (ev["stop_pct"] > 0).all()
    small = run_ml_test(ev)
    from swingbot.tgfmt import plain

    assert "error" in small and "not enough" in plain(format_report(small))
