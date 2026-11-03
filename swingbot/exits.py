"""Trade management after entry - the same rules drive backtests and live trading.

Order of priority:
  1. stop-loss (held at the broker, fires intraday)
  2. emergency exits: earnings ahead, regime turns bear/shock (tighten)
  3. trailing stop: close below 20 EMA after +1R, chandelier stop
  4. target (held at the broker as the OCO take-profit leg)
  5. time stop: no +1R progress within N days
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .models import Position, Regime, SetupType


@dataclass
class ManageResult:
    new_stop: float | None = None  # raise the stop to this level (never lowered)
    exit_reason: str | None = None  # close-based exit -> sell at next session
    partial_due: bool = False
    notes: list[str] | None = None


def manage_position(
    pos: Position,
    row: pd.Series,
    regime: Regime,
    cfg: dict,
    partial_enabled: bool,
    earnings_within_days: int | None = None,
    per_share_costs: float = 0.0,
) -> ManageResult:
    """Evaluate an open position on a completed daily bar (after the close).

    Mutates ``pos`` bookkeeping (bars held, highest high, 1R flag) and returns
    what should change at the broker.
    """
    notes: list[str] = []
    pos.bars_held += 1
    pos.highest_high = max(pos.highest_high, float(row["high"]))
    one_r = pos.initial_risk_per_share
    stop = pos.stop_price

    # +1R reached -> move stop to breakeven (entry + charges)
    be_r = float(cfg.get("breakeven_at_r", 1.0))
    if pos.highest_high >= pos.entry_price + be_r * one_r:
        if not pos.reached_1r:
            notes.append(f"reached +{be_r:g}R - stop to breakeven")
        pos.reached_1r = True
        stop = max(stop, pos.entry_price + per_share_costs)

    # trailing stop once the trade is working
    if pos.reached_1r:
        ch = row.get("chandelier")
        if ch is not None and not pd.isna(ch):
            stop = max(stop, float(ch))

    # bear / shock market: tighten stops to 1.5 ATR under the close
    if regime in (Regime.BEAR, Regime.SHOCK):
        atr = row.get("atr14")
        if atr is not None and not pd.isna(atr):
            tight = float(row["close"]) - 1.5 * float(atr)
            if tight > stop:
                stop = tight
                notes.append(f"{regime.value} market - stop tightened")

    # never place a stop above the current close (would trigger instantly)
    stop = min(stop, float(row["close"]) * 0.995)
    new_stop = round(stop, 2) if stop > pos.stop_price + 0.01 else None

    # close-based exits ----------------------------------------------------------
    exit_reason = None
    trail_col = f"ema{int(cfg.get('trail_ema', 20))}"
    if earnings_within_days is not None and earnings_within_days <= int(cfg.get("exit_before_earnings_days", 1)):
        exit_reason = "earnings/corporate event tomorrow"
    elif pos.setup == SetupType.MEAN_REVERSION.value and row["close"] > row.get("sma5", float("inf")):
        exit_reason = "mean-reversion exit (close above 5-day SMA)"
    elif pos.reached_1r and trail_col in row and not pd.isna(row[trail_col]) and row["close"] < row[trail_col]:
        exit_reason = f"trend break (close below {trail_col.upper()})"
    elif not pos.reached_1r and pos.bars_held >= int(cfg.get("time_stop_days", 10)):
        exit_reason = f"time stop ({pos.bars_held} days without +1R)"

    partial_due = (
        partial_enabled
        and not pos.partial_taken
        and pos.quantity >= 2
        and pos.highest_high >= pos.entry_price + float(cfg.get("partial_at_r", 2.0)) * one_r
    )
    return ManageResult(new_stop=new_stop, exit_reason=exit_reason, partial_due=partial_due, notes=notes)


def partial_price(pos: Position, cfg: dict) -> float:
    return round(pos.entry_price + float(cfg.get("partial_at_r", 2.0)) * pos.initial_risk_per_share, 2)


def partial_quantity(pos: Position, cfg: dict) -> int:
    return max(1, int(pos.quantity * float(cfg.get("partial_fraction", 0.5))))
