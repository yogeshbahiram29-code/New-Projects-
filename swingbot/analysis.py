"""Deep stock analysis: hard filters + a 0-100 score with a full breakdown.

Score weights (max points):
    trend quality        25
    relative strength    20
    setup quality        20   (from the setup detector)
    volume/accumulation  15
    reward-to-risk       10
    volatility fit        5
    event/fundamentals    5
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import add_indicators, sma, weekly
from .models import ScoreCard, TradePlan


# ---------------------------------------------------------------------------
# Feature preparation (done once per stock, then scoring is a cheap row lookup)
# ---------------------------------------------------------------------------
def _weekly_flags(df: pd.DataFrame) -> pd.DataFrame:
    w = weekly(df)
    w_sma10 = sma(w["close"], 10)
    w_sma30 = sma(w["close"], 30)
    slope30 = w_sma30 - w_sma30.shift(4)
    flags = pd.DataFrame(
        {
            "wk_up": (w["close"] > w_sma30) & (w_sma10 > w_sma30),
            "wk_down": (w["close"] < w_sma30) & (w_sma10 < w_sma30) & (slope30 < 0),
        },
        index=w.index,
    )
    # A weekly bar labelled Friday is complete at Friday's close, so forward-filling
    # onto daily dates never leaks future information.
    return flags.reindex(df.index, method="ffill").fillna(False).astype(bool)


def prepare_stock(df: pd.DataFrame, index_close: pd.Series | None = None) -> pd.DataFrame:
    """Add indicators and analysis features to a daily OHLCV frame."""
    out = add_indicators(df)
    out = out.join(_weekly_flags(df))

    up = out["close"].diff() > 0
    vol_up = out["volume"].where(up, 0.0).rolling(50, min_periods=50).sum()
    vol_down = out["volume"].where(~up, 0.0).rolling(50, min_periods=50).sum()
    out["updown_vol_ratio"] = vol_up / vol_down.replace(0.0, np.nan)

    dist = (out["close"].pct_change() < -0.01) & (out["volume"] > out["vol_avg50"])
    out["dist_days10"] = dist.astype(float).rolling(10, min_periods=1).sum()

    if index_close is not None:
        idx = index_close.reindex(out.index).ffill()
        out["rs_line"] = out["close"] / idx
        out["rs_line_high126"] = out["rs_line"].rolling(126, min_periods=60).max()
        out["idx_ret_63"] = idx / idx.shift(63) - 1
        out["idx_ret_126"] = idx / idx.shift(126) - 1
    else:
        for c in ("rs_line", "rs_line_high126", "idx_ret_63", "idx_ret_126"):
            out[c] = np.nan
    out["rs_composite"] = 0.6 * out["ret_63"] + 0.4 * out["ret_126"]
    # vectorised version of hard_filter_reasons (minus events) for fast scanning
    out["hard_ok"] = (
        (out["close"] > out["sma200"]) & (out["sma200_slope"] >= 0) & ~out["wk_down"]
    ).fillna(False).astype(bool)
    return out


def rs_percentile_panel(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Cross-sectional percentile (0-100) of the RS composite, per date."""
    panel = pd.DataFrame({s: f["rs_composite"] for s, f in frames.items()})
    return panel.rank(axis=1, pct=True) * 100


# ---------------------------------------------------------------------------
# Hard filters
# ---------------------------------------------------------------------------
def hard_filter_reasons(row: pd.Series, event_within_blackout: bool = False) -> list[str]:
    reasons = []
    if pd.isna(row.get("sma200")) or pd.isna(row.get("sma200_slope")):
        reasons.append("not enough history")
        return reasons
    if row["close"] <= row["sma200"]:
        reasons.append("price below 200-day SMA")
    if row["sma200_slope"] < 0:
        reasons.append("200-day SMA falling")
    if bool(row.get("wk_down", False)):
        reasons.append("weekly downtrend")
    if event_within_blackout:
        reasons.append("earnings/corporate event inside blackout window")
    return reasons


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _lin(x: float, x0: float, x1: float) -> float:
    """Linear 0..1 ramp between x0 and x1 (works for decreasing ramps too)."""
    if pd.isna(x):
        return 0.0
    if x0 == x1:
        return 1.0 if x >= x1 else 0.0
    t = (x - x0) / (x1 - x0)
    return float(min(max(t, 0.0), 1.0))


def trend_points(row: pd.Series) -> float:
    pts = 0.0
    pts += 3 if row["close"] > row["ema20"] else 0
    pts += 4 if row["ema20"] > row["sma50"] else 0
    pts += 5 if row["sma50"] > row["sma200"] else 0
    pts += 4 if row["sma200_slope"] > 0 else 0
    adx_v = row.get("adx14", np.nan)
    pts += 3 if adx_v > 25 else (2 if adx_v > 20 else 0)
    pts += 6 if bool(row.get("wk_up", False)) else 0
    return pts  # max 25


def rs_points(row: pd.Series, rs_pct: float | None) -> float:
    pts = 0.0
    if rs_pct is not None and not pd.isna(rs_pct):
        pts += 12 * _lin(rs_pct, 50, 95)
    else:
        excess = row.get("ret_126", np.nan) - row.get("idx_ret_126", np.nan)
        pts += 12 * _lin(excess, 0.0, 0.25)
    if row.get("ret_63", np.nan) > row.get("idx_ret_63", np.nan):
        pts += 4
    hi = row.get("rs_line_high126", np.nan)
    if not pd.isna(hi) and row.get("rs_line", np.nan) >= 0.95 * hi:
        pts += 4
    return pts  # max 20


def volume_points(row: pd.Series) -> float:
    pts = 4.0 if row.get("obv_slope", 0) > 0 else 0.0
    pts += 6 * _lin(row.get("updown_vol_ratio", np.nan), 1.0, 1.5)
    d = row.get("dist_days10", 0)
    pts += 5 if d <= 1 else (3 if d == 2 else 0)
    return pts  # max 15


def reward_risk_points(rr: float) -> float:
    if rr < 2.0:
        return 0.0
    return 4 + 6 * _lin(rr, 2.0, 4.0)  # 2R -> 4 pts, 4R+ -> 10 pts


def volatility_points(atr_pct: float) -> float:
    if pd.isna(atr_pct):
        return 0.0
    if 1.5 <= atr_pct <= 5.0:
        return 5.0
    if 1.0 <= atr_pct < 1.5 or 5.0 < atr_pct <= 7.0:
        return 2.0
    return 0.0


def event_points(event_ok: bool, fundamentals_ok: bool | None) -> float:
    pts = 3.0 if event_ok else 0.0
    if fundamentals_ok is None:
        pts += 1.0
    elif fundamentals_ok:
        pts += 2.0
    return pts  # max 5


def score(
    symbol: str,
    row: pd.Series,
    rs_pct: float | None = None,
    plan: TradePlan | None = None,
    setup_quality: float = 0.0,
    event_within_blackout: bool = False,
    fundamentals_ok: bool | None = None,
) -> ScoreCard:
    reasons = hard_filter_reasons(row, event_within_blackout)
    if reasons and "not enough history" in reasons:
        return ScoreCard(symbol, 0.0, {}, False, reasons)
    parts = {
        "trend": trend_points(row),
        "relative_strength": rs_points(row, rs_pct),
        "setup": 20 * float(min(max(setup_quality, 0.0), 1.0)),
        "volume": volume_points(row),
        "reward_risk": reward_risk_points(plan.reward_risk) if plan else 0.0,
        "volatility": volatility_points(row.get("atr_pct", np.nan)),
        "events_fundamentals": event_points(not event_within_blackout, fundamentals_ok),
    }
    total = round(sum(parts.values()), 1)
    return ScoreCard(symbol, total, {k: round(v, 1) for k, v in parts.items()}, not reasons, reasons)


def explain(card: ScoreCard) -> str:
    bits = ", ".join(f"{k} {v:g}" for k, v in card.parts.items())
    status = "PASS" if card.passed_hard_filters else "REJECT: " + "; ".join(card.reject_reasons)
    return f"{card.symbol}: {card.total:g}/100 ({bits}) [{status}]"


def prepare_universe(
    raw: dict[str, pd.DataFrame], index_df: pd.DataFrame | None
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame | None]:
    """Prepare all stocks; returns (frames, rs_percentile_panel)."""
    idx_close = index_df["close"] if index_df is not None else None
    frames = {s: prepare_stock(df, idx_close) for s, df in raw.items() if len(df) > 0}
    panel = rs_percentile_panel(frames) if frames else None
    return frames, panel


__all__ = [
    "prepare_stock",
    "prepare_universe",
    "rs_percentile_panel",
    "hard_filter_reasons",
    "score",
    "explain",
    "add_indicators",
]
