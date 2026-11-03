"""Trading strategies behind one interface, so the backtester, the live engine
and the free signal mode always run exactly the same rules.

* ``classic``  - the first version: 7-factor score, pullback/breakout setups,
                 fixed targets. Kept only as a baseline for comparison.
* ``momentum`` - the simplified Step-2 strategy (8 parameters, see below).

Momentum strategy rules (plain language)
----------------------------------------
1. Universe   : liquid stocks only (20-day avg traded value >= X crore, price >= min).
2. Regime     : new entries only while NIFTY 50 closes above its 200-day SMA
                AND that SMA is higher than 20 days ago. Otherwise no new trades.
3. Strength   : stock in an uptrend (close > 50-SMA > 200-SMA) and in the top
                ``top_pct`` % of the liquid universe by ``mom_lookback``-day return.
4. Entry      : ``breakout`` = close above the highest close of the previous
                ``breakout_lookback`` days; ``pullback`` = the day's low touched the
                20-EMA and it closed back above it. Signal on the close; buy NEXT day
                only if price trades above the signal day's high (stop-limit, small
                limit buffer, no chasing gaps).
5. Stop       : entry - ``stop_atr`` x ATR(14).
6. Trailing   : highest close since entry - ``trail_atr`` x ATR(14); only moves up.
7. Time exit  : sell at the next open after ``max_hold_days`` trading days.
No profit target: winners run until the trailing stop or the time exit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from . import analysis, regime as regime_mod
from .config import Settings
from .exits import ManageResult, manage_position
from .indicators import atr, ema, sma
from .models import Position, Regime, ScoreCard, SetupType, TradePlan
from .scanner import Candidate, event_within, scan as classic_scan


@dataclass
class Prepared:
    frames: dict[str, pd.DataFrame]
    panel: pd.DataFrame | None  # cross-sectional rank panel (RS or momentum), 0-100
    breadth: pd.Series | None


class ClassicStrategy:
    name = "classic"
    allows_partials = True

    def __init__(self, settings: Settings):
        self.s = settings

    def prepare(self, raw: dict[str, pd.DataFrame], index_df: pd.DataFrame) -> Prepared:
        frames, panel = analysis.prepare_universe(raw, index_df)
        return Prepared(frames, panel, regime_mod.breadth_series(frames))

    def regime_series(self, index_df: pd.DataFrame, prep: Prepared) -> pd.Series:
        return regime_mod.regime_series(index_df, prep.breadth, self.s.section("regime"))

    def current_regime(self, index_df: pd.DataFrame, prep: Prepared) -> Regime:
        b = prep.breadth
        last = float(b.iloc[-1]) if b is not None and len(b) and not pd.isna(b.iloc[-1]) else None
        return regime_mod.current_regime(index_df, last, self.s.section("regime"))

    def scan(self, prep: Prepared, when: pd.Timestamp, sectors=None, events=None) -> list[Candidate]:
        return classic_scan(prep.frames, when, self.s, prep.panel, sectors, events)

    def manage(self, pos: Position, row: pd.Series, regime: Regime, partial_enabled: bool,
               earnings_days: int | None, per_share_cost: float) -> ManageResult:
        return manage_position(pos, row, regime, self.s.section("exits"), partial_enabled, earnings_days, per_share_cost)


class MomentumStrategy:
    name = "momentum"
    allows_partials = False

    def __init__(self, settings: Settings):
        self.s = settings
        c = settings.section("momentum")
        self.entry = str(c.get("entry", "breakout"))
        self.mom_lookback = int(c.get("mom_lookback", 126))
        self.top_pct = float(c.get("top_pct", 20))
        self.breakout_lookback = int(c.get("breakout_lookback", 20))
        self.stop_atr = float(c.get("stop_atr", 2.0))
        self.trail_atr = float(c.get("trail_atr", 3.0))
        self.max_hold = int(c.get("max_hold_days", 20))
        self.regime_sma = int(c.get("regime_sma", 200))
        u = settings.section("universe")
        self.min_value = float(u.get("min_avg_traded_value_cr", 10)) * 1e7
        self.min_price = float(u.get("min_price", 50))
        self.min_hist = int(u.get("min_history_days", 260))
        ex = settings.section("execution")
        self.buf = float(ex.get("entry_buffer_pct", 0.1)) / 100
        self.limit_slip = float(ex.get("entry_limit_slippage_pct", 0.5)) / 100

    # -- features -------------------------------------------------------------
    def features(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df[["open", "high", "low", "close", "volume"]].copy()
        c = out["close"]
        out["sma50"], out["sma200"], out["ema20"] = sma(c, 50), sma(c, 200), ema(c, 20)
        out["atr14"] = atr(out, 14)
        out["vol_avg50"] = sma(out["volume"], 50)
        out["traded_value_avg20"] = sma(c * out["volume"], 20)
        out["mom"] = c / c.shift(self.mom_lookback) - 1
        n = np.arange(1, len(out) + 1)
        out["liquid"] = (out["traded_value_avg20"] >= self.min_value) & (c >= self.min_price) & (n >= self.min_hist)
        out["trend"] = (c > out["sma50"]) & (out["sma50"] > out["sma200"])
        prior_high = c.shift(1).rolling(self.breakout_lookback, min_periods=self.breakout_lookback).max()
        out["sig_breakout"] = c > prior_high
        out["sig_pullback"] = (out["low"] <= out["ema20"]) & (c > out["ema20"])
        return out

    def prepare(self, raw: dict[str, pd.DataFrame], index_df: pd.DataFrame) -> Prepared:
        frames = {s: self.features(df) for s, df in raw.items() if len(df)}
        mom = pd.DataFrame({s: f["mom"].where(f["liquid"]) for s, f in frames.items()})
        panel = mom.rank(axis=1, pct=True) * 100 if len(frames) else None
        return Prepared(frames, panel, None)

    # -- regime -------------------------------------------------------------------
    def regime_series(self, index_df: pd.DataFrame, prep: Prepared | None = None) -> pd.Series:
        c = index_df["close"]
        m = sma(c, self.regime_sma)
        on = ((c > m) & (m > m.shift(20))).to_numpy()
        return pd.Series([Regime.BULL if x else Regime.BEAR for x in on], index=index_df.index, dtype=object)

    def current_regime(self, index_df: pd.DataFrame, prep: Prepared | None = None) -> Regime:
        return self.regime_series(index_df).iloc[-1]

    # -- signals ------------------------------------------------------------------
    def scan(self, prep: Prepared, when: pd.Timestamp, sectors=None, events=None) -> list[Candidate]:
        sig_col = "sig_breakout" if self.entry == "breakout" else "sig_pullback"
        cut = 100 - self.top_pct
        blackout = int(self.s.get("analysis.earnings_blackout_days", 5))
        if prep.panel is None or when not in prep.panel.index:
            return []
        ranks = prep.panel.loc[when]
        out: list[Candidate] = []
        for sym, rank in ranks[ranks >= cut].items():
            df = prep.frames[sym]
            if when not in df.index:
                continue
            row = df.loc[when]
            if not (row["liquid"] and row["trend"] and row[sig_col]) or pd.isna(row["atr14"]):
                continue
            if event_within(sym, when.date(), events, blackout) is not None:
                continue
            trigger = float(row["high"]) * (1 + self.buf)
            stop = trigger - self.stop_atr * float(row["atr14"])
            if stop <= 0:
                continue
            plan = TradePlan(
                symbol=sym,
                setup=SetupType.BREAKOUT if self.entry == "breakout" else SetupType.PULLBACK,
                score=round(float(rank), 1),
                trigger_price=round(trigger, 2),
                limit_price=round(trigger * (1 + self.limit_slip), 2),
                stop_price=round(stop, 2),
                target_price=round(trigger + 10 * (trigger - stop), 2),  # placeholder: no profit cap
                atr=float(row["atr14"]),
                signal_date=when.date(),
                sector=(sectors or {}).get(sym, "UNKNOWN"),
                notes=f"{self.entry}, momentum rank {rank:.0f}/100, {self.mom_lookback}d return {row['mom'] * 100:.0f}%",
                trailing_only=True,
            )
            card = ScoreCard(sym, float(rank), {"momentum_rank": float(rank)}, True)
            out.append(Candidate(plan, card))
        out.sort(key=lambda c: c.card.total, reverse=True)
        return out

    # -- trade management -------------------------------------------------------------
    def manage(self, pos: Position, row: pd.Series, regime: Regime, partial_enabled: bool,
               earnings_days: int | None, per_share_cost: float) -> ManageResult:
        pos.bars_held += 1
        close = float(row["close"])
        pos.highest_high = max(pos.highest_high, float(row["high"]))
        pos.highest_close = max(pos.highest_close or pos.entry_price, close)
        stop = pos.stop_price
        a = row.get("atr14")
        if a is not None and not pd.isna(a):
            stop = max(stop, pos.highest_close - self.trail_atr * float(a))
        stop = min(stop, close * 0.995)  # never a stop above the market
        new_stop = round(stop, 2) if stop > pos.stop_price + 0.01 else None
        exit_reason = None
        if earnings_days is not None and earnings_days <= int(self.s.get("exits.exit_before_earnings_days", 1)):
            exit_reason = "earnings/corporate event tomorrow"
        elif pos.bars_held >= self.max_hold:
            exit_reason = f"time exit ({pos.bars_held} days)"
        return ManageResult(new_stop=new_stop, exit_reason=exit_reason, partial_due=False, notes=[])


def get_strategy(settings: Settings):
    name = str(settings.get("strategy", "momentum"))
    if name == "classic":
        return ClassicStrategy(settings)
    if name == "momentum":
        return MomentumStrategy(settings)
    raise ValueError(f"unknown strategy {name!r} (use momentum or classic)")


__all__ = ["Prepared", "ClassicStrategy", "MomentumStrategy", "get_strategy", "date"]
