"""Daily scan: universe filter -> hard filters -> setup -> score -> ranked trade plans.

Used identically by the backtester and the live engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from . import analysis
from .config import Settings
from .models import ScoreCard, TradePlan
from .setups import find_setup


@dataclass
class Candidate:
    plan: TradePlan
    card: ScoreCard


def passes_universe(row: pd.Series, n_bars: int, ucfg: dict) -> str | None:
    """Return a rejection reason, or None if the stock is tradeable."""
    if n_bars < int(ucfg.get("min_history_days", 260)):
        return "not enough history"
    if row["close"] < float(ucfg.get("min_price", 50)):
        return "price below minimum"
    tv = row.get("traded_value_avg20", np.nan)
    if pd.isna(tv) or tv < float(ucfg.get("min_avg_traded_value_cr", 10)) * 1e7:
        return "not liquid enough"
    return None


def trading_days_until(today: date, event_day: date) -> int:
    return int(np.busday_count(today, event_day))


def event_within(symbol: str, today: date, events: dict[str, list[date]] | None, days: int) -> int | None:
    """Trading days until the next known event for symbol (None if none within ``days``)."""
    if not events or symbol not in events:
        return None
    upcoming = [trading_days_until(today, d) for d in events[symbol] if d >= today]
    upcoming = [u for u in upcoming if u <= days]
    return min(upcoming) if upcoming else None


def scan(
    frames: dict[str, pd.DataFrame],
    when: pd.Timestamp,
    settings: Settings,
    rs_panel: pd.DataFrame | None = None,
    sectors: dict[str, str] | None = None,
    events: dict[str, list[date]] | None = None,
    fundamentals: dict[str, bool] | None = None,
    lookback: int = 260,
) -> list[Candidate]:
    ucfg = settings.section("universe")
    acfg = settings.section("analysis")
    setups_cfg = settings.section("setups")
    exec_cfg = settings.section("execution")
    risk_cfg = settings.section("risk")
    excluded = set(ucfg.get("exclude", []) or [])
    blackout = int(acfg.get("earnings_blackout_days", 5))
    out: list[Candidate] = []

    for sym, df in frames.items():
        if sym in excluded or when not in df.index:
            continue
        i = df.index.get_loc(when)
        if not df["hard_ok"].iat[i]:
            continue  # cheap vectorised gate before any per-row work
        row = df.iloc[i]
        if passes_universe(row, i + 1, ucfg):
            continue
        ev = event_within(sym, when.date(), events, blackout)
        if analysis.hard_filter_reasons(row, ev is not None):
            continue
        window = df.iloc[max(0, i - lookback + 1) : i + 1]
        found = find_setup(sym, window, setups_cfg, exec_cfg, risk_cfg)
        if found is None:
            continue
        rs_pct = None
        if rs_panel is not None and when in rs_panel.index and sym in rs_panel.columns:
            rs_pct = rs_panel.at[when, sym]
        card = analysis.score(
            sym,
            row,
            rs_pct=rs_pct,
            plan=found.plan,
            setup_quality=found.quality,
            event_within_blackout=ev is not None,
            fundamentals_ok=(fundamentals or {}).get(sym),
        )
        if not card.passed_hard_filters:
            continue
        found.plan.score = card.total
        found.plan.sector = (sectors or {}).get(sym, "UNKNOWN")
        out.append(Candidate(found.plan, card))

    out.sort(key=lambda c: c.card.total, reverse=True)
    return out


def returns_panel(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return pd.DataFrame({s: f["close"].pct_change() for s, f in frames.items()})


def correlations_with(
    rets: pd.DataFrame, symbol: str, held: list[str], when: pd.Timestamp, window: int = 60
) -> dict[str, float]:
    """60-day return correlation between ``symbol`` and each held stock."""
    cols = [h for h in dict.fromkeys(held) if h in rets.columns and h != symbol]
    if not cols or symbol not in rets.columns:
        return {}
    block = rets.loc[:when, [symbol, *cols]].iloc[-window:]
    out = {}
    for h in cols:
        both = block[[symbol, h]].dropna()
        if len(both) < 20:
            continue
        a, b = both[symbol].to_numpy(), both[h].to_numpy()
        if a.std() < 1e-12 or b.std() < 1e-12:
            continue  # flat series (e.g. suspended stock): correlation undefined
        out[h] = float(np.corrcoef(a, b)[0, 1])
    return out
