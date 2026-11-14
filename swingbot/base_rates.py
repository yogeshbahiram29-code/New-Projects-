"""Historical odds ("base rates") for watchlist picks.

For every past day and every stock that would have passed the watchlist's
filters, we record what the watchlist would have said (setup type, score,
buy-above and stop levels) and what actually happened next:

  * not triggered - price never traded above the buy-above level next day
  * target first  - Target 1 (2x the risk) was hit before the stop-loss
  * stop first    - the stop-loss was hit first (same-day ambiguity = stop)
  * neither       - neither within ``horizon`` trading days -> closed at the end

Cases are grouped by setup type, score band and market trend. One case per
stock per ``gap_days`` avoids counting the same move many times.

These are honest historical frequencies, not a prediction. Biases: today's
NIFTY 500 list (survivorship), no dividends, results before charges.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis
from .config import Settings
from .scanner import passes_universe
from .setups import find_setup

BANDS = [(0, 60, "<60"), (60, 70, "60-70"), (70, 80, "70-80"), (80, 90, "80-90"), (90, 101, "90+")]


def score_band(score: float) -> str:
    for lo, hi, name in BANDS:
        if lo <= score < hi:
            return name
    return "90+"


def normalize_setup(setup: str) -> str:
    s = str(setup).lower()
    return s if s in ("breakout", "pullback") else "none"


def evaluate_candidate(sym: str, f: pd.DataFrame, i: int, rs_pct: float, settings: Settings) -> dict | None:
    """What the watchlist would say about ``sym`` on bar ``i`` (shared with top_picks)."""
    row = f.iloc[i]
    window = f.iloc[max(0, i - 259): i + 1]
    found = find_setup(sym, window, settings.section("setups"), settings.section("execution"), settings.section("risk"))
    card = analysis.score(sym, row, rs_pct=rs_pct, plan=found.plan if found else None,
                          setup_quality=found.quality if found else 0.0)
    if not card.passed_hard_filters:
        return None
    atr = float(row["atr14"])
    if found:
        trigger, stop, setup = found.plan.trigger_price, found.plan.stop_price, found.plan.setup.value
        resistance = found.plan.target_price
    else:
        trigger = round(float(row["high"]) * 1.001, 2)
        stop, setup, resistance = round(trigger - 2 * atr, 2), "no setup yet", None
    if not trigger > stop > 0:
        return None
    risk = trigger - stop
    return {"setup": setup, "score": card.total, "trigger": trigger, "stop": stop,
            "t1": round(trigger + 2 * risk, 2), "t2": round(max(resistance or 0, trigger + 3 * risk), 2)}


def simulate(o, h, lo, c, i: int, trigger: float, stop: float, t1: float, horizon: int = 20,
             max_chase: float = 0.02) -> tuple[str, float, int]:
    """Outcome of a pick made on the close of bar i. Returns (outcome, R multiple, days held)."""
    j = i + 1
    if j >= len(c) or h[j] < trigger or o[j] > trigger * (1 + max_chase):
        return "not_triggered", 0.0, 0
    entry = max(o[j], trigger)
    risk = entry - stop
    if risk <= 0:
        return "not_triggered", 0.0, 0
    last = min(j + horizon, len(c)) - 1
    for k in range(j, last + 1):
        if lo[k] <= stop:
            px = stop if k == j else min(o[k], stop)  # gap below the stop fills at the open
            return "stop", (px - entry) / risk, k - j + 1
        if h[k] >= t1:
            return "target", (t1 - entry) / risk, k - j + 1
    return "timeout", (c[last] - entry) / risk, last - j + 1


FEATURE_COLS = ["rsi14", "rsi2", "adx14", "atr_pct", "vol_ratio", "bbw", "updown_vol_ratio", "dist_days10",
                "rs_composite", "sma200_slope", "sma50_slope", "ret_63", "ret_126", "idx_ret_63"]


def _ratio(a, b) -> float:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return np.nan
    return a / b - 1 if b and np.isfinite(a) and np.isfinite(b) else np.nan


def signal_features(row: pd.Series, cand: dict, rs: float, regime_on: bool) -> dict:
    """What was knowable on the signal day's close (used by the ML test - no future data)."""
    out = {k: float(row[k]) if k in row and pd.notna(row[k]) else np.nan for k in FEATURE_COLS}
    tv = row.get("traded_value_avg20", np.nan)
    out.update({
        "score": cand["score"], "rs_pct": float(rs) if pd.notna(rs) else np.nan,
        "d_high52": _ratio(row["close"], row.get("high_52w")), "d_sma50": _ratio(row["close"], row.get("sma50")),
        "d_sma200": _ratio(row["close"], row.get("sma200")), "trig_gap": _ratio(cand["trigger"], row["close"]),
        "stop_pct": (cand["trigger"] - cand["stop"]) / cand["trigger"] * 100,
        "log_value": float(np.log10(tv)) if pd.notna(tv) and tv > 0 else np.nan,
        "regime_on": float(regime_on),
        "is_breakout": float(normalize_setup(cand["setup"]) == "breakout"),
        "is_pullback": float(normalize_setup(cand["setup"]) == "pullback"),
    })
    return out


def build_events(raw: dict[str, pd.DataFrame], index_df: pd.DataFrame, settings: Settings, start, end=None,
                 horizon: int = 20, gap_days: int = 10, progress: bool = False, features: bool = False) -> pd.DataFrame:
    frames, rs_panel = analysis.prepare_universe(raw, index_df)
    ic = index_df["close"]
    m = ic.rolling(200).mean()
    regime_on = ((ic > m) & (m > m.shift(20))).fillna(False)
    ucfg = settings.section("universe")
    t0, t1_ = pd.Timestamp(start), pd.Timestamp(end) if end else index_df.index[-1]
    rows = []
    for n, (sym, f) in enumerate(frames.items()):
        if progress and n % 50 == 0:
            print(f"  {n}/{len(frames)} stocks, {len(rows)} cases")
        o, h, lo, c = (f[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        idx = f.index
        hard = f["hard_ok"].to_numpy(dtype=bool)
        last_i = -10_000
        for i in np.flatnonzero(hard):
            t = idx[i]
            if t < t0 or t > t1_ or i - last_i < gap_days or i + 1 >= len(c):
                continue
            if passes_universe(f.iloc[i], i + 1, ucfg):
                continue
            rs = rs_panel.at[t, sym] if rs_panel is not None and t in rs_panel.index else np.nan
            cand = evaluate_candidate(sym, f, i, rs, settings)
            if cand is None:
                continue
            outcome, r, days = simulate(o, h, lo, c, i, cand["trigger"], cand["stop"], cand["t1"], horizon)
            reg = bool(regime_on.get(t, False))
            row = {"date": t.date(), "symbol": sym, "setup": normalize_setup(cand["setup"]),
                   "band": score_band(cand["score"]), "regime": "on" if reg else "off",
                   "outcome": outcome, "r": round(r, 3), "days": days}
            if features:
                row.update(signal_features(f.iloc[i], cand, rs, reg))
            rows.append(row)
            last_i = i
    return pd.DataFrame(rows)


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    period = f"{pd.Timestamp(events['date'].min()):%Y}-{pd.Timestamp(events['date'].max()):%Y}"
    ev = events.copy()
    out = []
    for keys in (["setup", "band", "regime"], ["setup", "regime"], ["setup"]):
        for k, g in ev.groupby(keys):
            k = k if isinstance(k, tuple) else (k,)
            key = dict(zip(keys, k))
            trig = g[g["outcome"] != "not_triggered"]
            if trig.empty:
                continue
            out.append({
                "setup": key["setup"], "band": key.get("band", "all"), "regime": key.get("regime", "all"),
                "signals": len(g), "cases": len(trig),
                "triggered_pct": round(len(trig) / len(g) * 100, 1),
                "target_first_pct": round((trig["outcome"] == "target").mean() * 100, 1),
                "stop_first_pct": round((trig["outcome"] == "stop").mean() * 100, 1),
                "neither_pct": round((trig["outcome"] == "timeout").mean() * 100, 1),
                "avg_r": round(float(trig["r"].mean()), 3),
                "median_days": int(trig["days"].median()),
                "period": period,
            })
    return pd.DataFrame(out)


def load_table(path: str | Path) -> pd.DataFrame | None:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    return pd.read_csv(p, dtype={"band": str, "regime": str, "setup": str})


def lookup(table: pd.DataFrame | None, setup: str, score: float, regime_on: bool, min_cases: int = 100) -> dict | None:
    """Most specific matching row with at least ``min_cases`` triggered cases."""
    if table is None or table.empty:
        return None
    s, b, r = normalize_setup(setup), score_band(score), "on" if regime_on else "off"
    for band, reg, level in ((b, r, "same setup, score band & market trend"), ("all", r, "same setup & market trend"),
                             ("all", "all", "same setup, any market")):
        m = table[(table["setup"] == s) & (table["band"] == band) & (table["regime"] == reg)]
        if len(m) and int(m.iloc[0]["cases"]) >= min_cases:
            out = m.iloc[0].to_dict()
            out["level"] = level
            return out
    return None


def edge_level(avg_r: float) -> str:
    """'positive' / 'thin' / 'none' - the label shown next to the historical average R."""
    return "positive" if avg_r >= 0.25 else "thin" if avg_r >= 0.05 else "none"


def format_odds(o: dict | None) -> list[str]:
    if not o:
        return ["🎲 Odds: not enough history yet (run Actions → Refresh odds)."]
    avg = o["avg_r"]
    return [f"🎲 History ({o['period']}, {int(o['cases']):,} similar cases): 🎯 T1 first {o['target_first_pct']:.0f}% | "
            f"🔴 SL first {o['stop_first_pct']:.0f}% | ⏳ neither {o['neither_pct']:.0f}%",
            f"   Avg {avg:+.2f}R before charges · triggered {o['triggered_pct']:.0f}% of the time · "
            + {"positive": "✅ positive edge", "thin": "⚠️ thin edge – charges may erase it",
               "none": "❌ no real edge"}[edge_level(avg)]]


__all__ = ["edge_level", "build_events", "signal_features", "FEATURE_COLS", "summarize", "lookup", "format_odds", "evaluate_candidate", "simulate", "score_band"]
