"""Entry setups. A strong score says *what* to buy; a setup says *when*.

Every detector works on data up to and including the signal day's close and
produces a TradePlan for the NEXT trading day:
    trigger - buy only if price trades above this level (buyers proved active)
    limit   - never pay more than this (no chasing)
    stop    - where the trade idea is proven wrong
    target  - nearest major resistance, or 3R when there's no overhead supply
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .indicators import find_resistance
from .models import SetupType, TradePlan


@dataclass
class SetupResult:
    plan: TradePlan
    quality: float  # 0..1


def _lin(x: float, x0: float, x1: float) -> float:
    if pd.isna(x):
        return 0.0
    t = (x - x0) / (x1 - x0)
    return float(min(max(t, 0.0), 1.0))


def plan_target(window: pd.DataFrame, trigger: float, risk: float, near_high_pct: float = 8.0) -> float:
    """Nearest *major* resistance above the trigger, else trigger + 3R.

    When the stock trades within ``near_high_pct`` of its 52-week high, the
    recent swing high is the level a trend trade expects to break, so it is
    not treated as a ceiling.
    """
    last = window.iloc[-1]
    high_52w = last.get("high_52w", np.nan)
    projected = trigger + 3.0 * risk
    res = find_resistance(window, trigger)
    if res is None:
        return projected
    if not pd.isna(high_52w) and trigger >= high_52w * (1 - near_high_pct / 100) and res >= high_52w * 0.99:
        return projected
    return float(res)


def _finalise_stop(trigger: float, stop: float, atr: float, min_stop_pct: float) -> float:
    """Make sure the stop is not inside normal daily noise."""
    min_dist = max(trigger * min_stop_pct / 100, 1.0 * atr)
    if trigger - stop < min_dist:
        stop = trigger - min_dist
    return float(stop)


def detect_pullback(symbol: str, window: pd.DataFrame, cfg: dict) -> SetupResult | None:
    """Setup A - orderly pullback to the 20 EMA / 50 SMA inside an uptrend."""
    row = window.iloc[-1]
    if not (row["ema20"] > row["sma50"] > row["sma200"] and row["close"] > row["sma50"]):
        return None
    recent = window.iloc[-15:]
    hi_pos = int(np.argmax(recent["high"].to_numpy()))
    bars_since_high = len(recent) - 1 - hi_pos
    if not 3 <= bars_since_high <= 10:
        return None
    swing_high = float(recent["high"].iloc[hi_pos])
    pull = recent.iloc[hi_pos + 1 :]
    pull_low = float(pull["low"].min())
    atr = float(row["atr14"])
    depth_atr = (swing_high - pull_low) / atr if atr > 0 else 0
    depth_pct = (swing_high - pull_low) / swing_high * 100
    if not (1.0 <= depth_atr <= 5.0 and 2.0 <= depth_pct <= 15.0):
        return None
    touched_support = (pull["low"] <= pull["ema20"] * 1.01).any() or (pull["low"] <= pull["sma50"] * 1.01).any()
    if not touched_support or not 38 <= row["rsi14"] <= 62:
        return None

    vol_dry = float(pull["volume"].mean() / row["vol_avg50"]) if row["vol_avg50"] > 0 else 1.0
    rng = row["high"] - row["low"]
    reversal = row["close"] > row["open"] and rng > 0 and (row["close"] - row["low"]) / rng >= 0.5

    buf = float(cfg.get("entry_buffer_pct", 0.1)) / 100
    trigger = float(row["high"]) * (1 + buf)
    stop = _finalise_stop(trigger, pull_low - 0.25 * atr, atr, float(cfg.get("min_stop_distance_pct", 1.5)))
    target = plan_target(window, trigger, trigger - stop)

    quality = (
        0.35 * _lin(vol_dry, 1.1, 0.7)
        + 0.25 * (1.0 if 1.5 <= depth_atr <= 4.0 else 0.5)
        + 0.20 * 1.0
        + 0.20 * (1.0 if reversal else 0.0)
    )
    plan = TradePlan(
        symbol=symbol,
        setup=SetupType.PULLBACK,
        score=0.0,
        trigger_price=round(trigger, 2),
        limit_price=round(trigger * (1 + float(cfg.get("entry_limit_slippage_pct", 0.5)) / 100), 2),
        stop_price=round(stop, 2),
        target_price=round(target, 2),
        atr=atr,
        signal_date=window.index[-1].date(),
        notes=f"pullback {depth_pct:.1f}% ({depth_atr:.1f} ATR) on {vol_dry:.2f}x volume",
    )
    return SetupResult(plan, quality)


def detect_breakout(symbol: str, window: pd.DataFrame, cfg: dict) -> SetupResult | None:
    """Setup B - close above a tight 3-10 week base on strong volume."""
    row = window.iloc[-1]
    if not (row["close"] > row["sma50"] > row["sma200"]):
        return None
    min_vol = float(cfg.get("min_volume_ratio", 1.5))
    if not row["vol_ratio"] >= min_vol:
        return None
    max_ext = float(cfg.get("max_extension_pct", 5.0))
    best = None
    for base_len in (15, 25, 35, 50):
        base = window.iloc[-base_len - 1 : -1]
        if len(base) < base_len:
            continue
        base_high = float(base["high"].max())
        base_low = float(base["low"].min())
        depth = (base_high - base_low) / base_high * 100
        if depth > 15 or row["close"] <= base_high:
            continue
        ext = (row["close"] - base_high) / base_high * 100
        if ext > max_ext:
            continue
        last10 = base.iloc[-10:]
        contraction = (last10["high"].max() - last10["low"].min()) / max(base_high - base_low, 1e-9)
        best = (base_len, base_high, base_low, depth, ext, contraction, float(last10["low"].min()))
        break
    if best is None:
        return None
    base_len, base_high, base_low, depth, ext, contraction, contraction_low = best
    high_52w = row.get("high_52w", np.nan)
    if not pd.isna(high_52w) and base_high < 0.85 * high_52w:
        return None  # breakouts far below the 52-week high run into overhead supply

    rng = row["high"] - row["low"]
    close_pos = (row["close"] - row["low"]) / rng if rng > 0 else 0.0
    if close_pos < 0.5:
        return None  # closed weak - sellers absorbed the breakout

    atr = float(row["atr14"])
    buf = float(cfg.get("entry_buffer_pct", 0.1)) / 100
    trigger = float(row["high"]) * (1 + buf)
    stop = contraction_low - 0.25 * atr
    max_stop = float(cfg.get("max_stop_distance_pct", 8.0))
    if (trigger - stop) / trigger * 100 > max_stop:
        stop = float(row["low"]) - 0.25 * atr
    stop = _finalise_stop(trigger, stop, atr, float(cfg.get("min_stop_distance_pct", 1.5)))
    target = plan_target(window, trigger, trigger - stop)

    quality = (
        0.35 * _lin(row["vol_ratio"], min_vol, 2.5)
        + 0.25 * _lin(depth, 15, 8)
        + 0.20 * _lin(contraction, 0.8, 0.4)
        + 0.20 * _lin(close_pos, 0.5, 0.75)
    )
    plan = TradePlan(
        symbol=symbol,
        setup=SetupType.BREAKOUT,
        score=0.0,
        trigger_price=round(trigger, 2),
        limit_price=round(trigger * (1 + float(cfg.get("entry_limit_slippage_pct", 0.5)) / 100), 2),
        stop_price=round(stop, 2),
        target_price=round(target, 2),
        atr=atr,
        signal_date=window.index[-1].date(),
        notes=f"{base_len}-day base, depth {depth:.1f}%, volume {row['vol_ratio']:.1f}x, ext {ext:.1f}%",
    )
    return SetupResult(plan, quality)


def detect_mean_reversion(symbol: str, window: pd.DataFrame, cfg: dict) -> SetupResult | None:
    """Setup C - short-term oversold (RSI(2) < 10) inside a long-term uptrend."""
    row = window.iloc[-1]
    if not (row["close"] > row["sma200"] and row["sma50"] > row["sma200"] and row["rsi2"] < 10):
        return None
    atr = float(row["atr14"])
    buf = float(cfg.get("entry_buffer_pct", 0.1)) / 100
    trigger = float(row["close"]) * (1 + buf)
    stop = _finalise_stop(trigger, trigger - 2.5 * atr, atr, float(cfg.get("min_stop_distance_pct", 1.5)))
    target = max(float(row["ema20"]), float(window["high"].iloc[-10:].max()))
    if target <= trigger:
        return None
    quality = 0.5 * _lin(row["rsi2"], 10, 2) + 0.5 * (1.0 if row["close"] > row["sma50"] else 0.3)
    plan = TradePlan(
        symbol=symbol,
        setup=SetupType.MEAN_REVERSION,
        score=0.0,
        trigger_price=round(trigger, 2),
        limit_price=round(trigger * (1 + float(cfg.get("entry_limit_slippage_pct", 0.5)) / 100), 2),
        stop_price=round(stop, 2),
        target_price=round(target, 2),
        atr=atr,
        signal_date=window.index[-1].date(),
        notes=f"RSI(2) {row['rsi2']:.1f} in uptrend",
    )
    return SetupResult(plan, quality)


DETECTORS = {
    SetupType.BREAKOUT: detect_breakout,
    SetupType.PULLBACK: detect_pullback,
    SetupType.MEAN_REVERSION: detect_mean_reversion,
}


def find_setup(symbol: str, window: pd.DataFrame, setups_cfg: dict, exec_cfg: dict, risk_cfg: dict) -> SetupResult | None:
    """Run every enabled detector; return the highest-quality match."""
    found = []
    for stype, fn in DETECTORS.items():
        scfg = setups_cfg.get(stype.value, {})
        if not scfg.get("enabled", False):
            continue
        cfg = {**exec_cfg, **risk_cfg, **scfg}
        res = fn(symbol, window, cfg)
        if res is not None:
            found.append(res)
    return max(found, key=lambda r: r.quality) if found else None
