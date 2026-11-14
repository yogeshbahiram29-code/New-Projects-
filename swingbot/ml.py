"""Upgrade 5: can machine learning pick better swing trades than the simple score?

An honest out-of-sample test, not a prediction engine:

  * Cases: every past watchlist candidate (same rules as the historical-odds
    table) with what was knowable on the signal day's close - indicators,
    score, relative strength, stop distance, market trend.
  * Label: did Target 1 (2R) come before the stop-loss?
  * Train ONLY on 2015-2022. A gap of ~1 month before 2023 is dropped so no
    training trade "sees" a test-period price. Test on 2023 onwards.
  * Two deliberately simple models with fixed, conservative settings (no
    tuning on the test years): logistic regression and small gradient-boosted
    trees. Both are compared with simply ranking by the bot's score.
  * Results are after an assumed round-trip cost, converted to R.

Adoption rule (written before running): the model is used only if, on the
test years, AUC >= 0.55 AND its top-20% picks beat the score's top-20% by at
least +0.10R after costs AND those picks are profitable after costs AND its
daily top-5 also beats the score's daily top-5 (by at least +0.02R). Otherwise: "not adopted".
"""
from __future__ import annotations

import numpy as np
import pandas as pd

META = {"date", "symbol", "setup", "band", "regime", "outcome", "r", "days"}
MIN_TRAIN, MIN_TEST = 300, 150


def feature_names(ev: pd.DataFrame) -> list[str]:
    return [c for c in ev.columns if c not in META]


def net_r(ev: pd.DataFrame, cost_pct: float) -> pd.Series:
    """R after a round-trip cost of ``cost_pct``% of the position (risk = stop_pct% of the price)."""
    return ev["r"] - cost_pct / ev["stop_pct"].clip(lower=0.5)


def split(ev: pd.DataFrame, test_start: str = "2023-01-01", embargo_days: int = 31) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = pd.to_datetime(ev["date"])
    ts = pd.Timestamp(test_start)
    return ev[d < ts - pd.Timedelta(days=embargo_days)].copy(), ev[d >= ts].copy()


def make_models(seed: int = 0) -> dict:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return {
        "Logistic regression": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                                             LogisticRegression(C=0.1, max_iter=2000)),
        "Gradient boosting": HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200,
                                                            min_samples_leaf=200, l2_regularization=1.0,
                                                            random_state=seed),
    }


def auc(y: np.ndarray, s: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, s)) if len(set(y)) == 2 else float("nan")


def top_frac(scores: np.ndarray, values: np.ndarray, frac: float = 0.2) -> float:
    n = max(1, int(len(scores) * frac))
    return float(np.mean(values[np.argsort(-scores, kind="stable")[:n]]))


def daily_top(cands: pd.DataFrame, col: str, k: int = 5) -> tuple[float, int]:
    """Each day take the top ``k`` candidates by ``col`` (like the watchlist); average net R of those that triggered."""
    picked = cands.sort_values(["date", col], ascending=[True, False]).groupby("date").head(k)
    trig = picked[picked["outcome"] != "not_triggered"]
    return (float(trig["net_r"].mean()) if len(trig) else float("nan")), len(trig)


def run_ml_test(events: pd.DataFrame, test_start: str = "2023-01-01", cost_pct: float = 0.55,
                embargo_days: int = 31, seed: int = 0) -> dict:
    ev = events.copy()
    ev["net_r"] = net_r(ev, cost_pct)
    feats = feature_names(ev.drop(columns=["net_r"]))
    train_all, test_all = split(ev, test_start, embargo_days)
    train = train_all[train_all["outcome"] != "not_triggered"]
    test = test_all[test_all["outcome"] != "not_triggered"]
    res = {"features": feats, "n_train": len(train), "n_test": len(test), "cost_pct": cost_pct,
           "train_period": _period(train), "test_period": _period(test), "models": {}}
    if len(train) < MIN_TRAIN or len(test) < MIN_TEST:
        res["error"] = f"not enough cases (train {len(train)}, test {len(test)})"
        return res
    ytr = (train["outcome"] == "target").to_numpy(int)
    yte = (test["outcome"] == "target").to_numpy(int)
    r_te = test["net_r"].to_numpy(float)
    base_score = test["score"].to_numpy(float)
    res["all"] = {"avg_net_r": float(r_te.mean()), "win_pct": float(yte.mean() * 100)}
    res["score"] = {"auc": auc(yte, base_score), "top20_net_r": top_frac(base_score, r_te),
                    "daily5": daily_top(test_all, "score")}
    test_all = test_all.copy()
    for name, model in make_models(seed).items():
        model.fit(train[feats].to_numpy(float), ytr)
        p = model.predict_proba(test[feats].to_numpy(float))[:, 1]
        test_all[name] = model.predict_proba(test_all[feats].to_numpy(float))[:, 1]
        years = {}
        for y, g in test.assign(_p=p, _y=yte).groupby(pd.to_datetime(test["date"]).dt.year):
            years[int(y)] = (auc(g["_y"].to_numpy(), g["_p"].to_numpy()), auc(g["_y"].to_numpy(), g["score"].to_numpy()))
        q = pd.qcut(pd.Series(p).rank(method="first"), 5, labels=False)
        calib = [(float(p[q == b].mean() * 100), float(yte[q.to_numpy() == b].mean() * 100)) for b in range(5)]
        top_n = max(1, int(len(p) * 0.2))
        top_idx = np.argsort(-p, kind="stable")[:top_n]
        m = {"auc": auc(yte, p), "train_auc": auc(ytr, model.predict_proba(train[feats].to_numpy(float))[:, 1]),
             "top20_net_r": top_frac(p, r_te), "top20_win_pct": float(yte[top_idx].mean() * 100),
             "daily5": daily_top(test_all, name), "years": years, "calibration": calib}
        if name == "Logistic regression":
            coefs = model[-1].coef_[0]
            m["drivers"] = sorted(zip(feats, coefs), key=lambda kv: -abs(kv[1]))[:5]
        m["adopted"], m["checks"] = adoption(m, res["score"])
        res["models"][name] = m
    res["red_flags"] = [f"{n}: test AUC {m['auc']:.2f} / top-20% win rate {m['top20_win_pct']:.0f}% is suspiciously "
                        "good - check for look-ahead before trusting it"
                        for n, m in res["models"].items() if m["auc"] > 0.70 or m["top20_win_pct"] > 75]
    return res


def adoption(m: dict, score: dict) -> tuple[bool, dict]:
    checks = {
        "AUC ≥ 0.55": m["auc"] >= 0.55,
        "top-20% beats score by ≥ +0.10R": m["top20_net_r"] - score["top20_net_r"] >= 0.10,
        "top-20% profitable after costs": m["top20_net_r"] > 0,
        "daily top-5 beats score": bool(m["daily5"][0] >= score["daily5"][0] + 0.02) if np.isfinite(m["daily5"][0]) else False,
    }
    return all(checks.values()), checks


def _period(df: pd.DataFrame) -> str:
    if df.empty:
        return "-"
    d = pd.to_datetime(df["date"])
    return f"{d.min():%b %Y} – {d.max():%b %Y}"


FRIENDLY = {"score": "bot score", "rs_pct": "relative strength rank", "rsi14": "RSI(14)", "rsi2": "RSI(2)",
            "adx14": "trend strength (ADX)", "atr_pct": "volatility (ATR%)", "vol_ratio": "volume vs average",
            "bbw": "Bollinger width", "updown_vol_ratio": "up/down volume", "dist_days10": "distribution days",
            "rs_composite": "relative strength", "sma200_slope": "200-day trend slope", "sma50_slope": "50-day slope",
            "ret_63": "3-month return", "ret_126": "6-month return", "idx_ret_63": "NIFTY 3-month return",
            "d_high52": "distance from 52-week high", "d_sma50": "distance from 50-day avg",
            "d_sma200": "distance from 200-day avg", "trig_gap": "trigger above close", "stop_pct": "stop distance",
            "log_value": "liquidity", "regime_on": "market filter ON", "is_breakout": "breakout setup",
            "is_pullback": "pullback setup"}


def format_report(res: dict):
    from .tgfmt import TAGLINE, Html, esc, head, kv, title

    B = [title("Machine-Learning Test", "out-of-sample")]
    if res.get("error"):
        return Html("\n\n".join(B + [esc(f"Not run: {res['error']}")]))
    B.append(kv([("Trained", f"{res['train_period']}"), ("", f"{res['n_train']:,} trades"),
                 ("Tested", f"{res['test_period']}"), ("", f"{res['n_test']:,} unseen trades"),
                 ("Costs", f"{res['cost_pct']:.2f}% round trip")], 9))
    sc = res["score"]
    B.append(head("Baseline") + "\n" + kv([
        ("All trades", f"{res['all']['avg_net_r']:+.2f}R after costs"), ("T1 first", f"{res['all']['win_pct']:.0f}%"),
        ("Score AUC", f"{sc['auc']:.3f}"), ("Score top 20%", f"{sc['top20_net_r']:+.2f}R"),
        ("Score daily 5", f"{sc['daily5'][0]:+.2f}R ({sc['daily5'][1]:,})")], 14))
    for name, m in res["models"].items():
        rows = [("AUC", f"{m['auc']:.3f} (train {m['train_auc']:.3f})"), ("Top 20%", f"{m['top20_net_r']:+.2f}R"),
                ("Top 20% T1", f"{m['top20_win_pct']:.0f}%"),
                ("Daily top 5", f"{m['daily5'][0]:+.2f}R ({m['daily5'][1]:,})")]
        blk = head(name) + "\n" + kv(rows, 12)
        blk += "\n" + esc("By year (model/score AUC): " + ", ".join(f"{y} {a:.2f}/{b:.2f}" for y, (a, b) in m["years"].items()))
        blk += "\n" + esc("Calibration (predicted → actual T1%): " + ", ".join(f"{p:.0f}→{a:.0f}" for p, a in m["calibration"]))
        if m.get("drivers"):
            blk += "\n" + esc("Drivers: " + ", ".join(f"{FRIENDLY.get(k, k)} ({'+' if c > 0 else '−'})" for k, c in m["drivers"]))
        blk += "\n" + esc("Rules: " + " · ".join(f"{'PASS' if ok else 'FAIL'} {k}" for k, ok in m["checks"].items()))
        blk += f"\n<b>{'ADOPT' if m['adopted'] else 'NOT ADOPTED'}</b>"
        B.append(blk)
    if res.get("red_flags"):
        B.append(head("Red Flags") + "\n" + esc("\n".join(res["red_flags"])))
    adopted = [n for n, m in res["models"].items() if m["adopted"]]
    if adopted:
        mean = [f"{adopted[0]} passed every rule on unseen years. Next: forward paper-test it before any real use."]
    else:
        best = max(m["auc"] for m in res["models"].values())
        mean = ["ML did not reliably beat the simple score on unseen years, so the bot keeps the simple score.",
                "AUC near 0.50 = coin flip: these daily indicators don't predict which trade wins."
                if best < 0.55 else "The model sees a weak pattern, but not enough to beat the score after costs."]
    mean.append("Past results are not a promise. Risk management stays the main edge.")
    B.append(head("What It Means") + "\n" + esc("\n".join(mean)) + "\n\n" + TAGLINE)
    return Html("\n\n".join(B))


__all__ = ["run_ml_test", "format_report", "split", "net_r", "daily_top", "top_frac", "adoption"]
