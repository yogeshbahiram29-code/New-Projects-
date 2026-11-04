"""Daily "Top 5 swing watchlist" with an honest forward track record.

Ranking (bot rules, technical only):
  * liquid NIFTY 500 stock, price above a non-falling 200-day average, weekly
    trend not down (the bot's hard filters)
  * ranked by the bot's 0-100 score: trend, relative strength vs ~500 stocks,
    setup quality (pullback / breakout today), volume, volatility
  * at most ``max_per_sector`` picks from one sector (diversification)

Every list is saved to journal/top5_history.csv. After ``eval_days`` trading
days the bot measures what each pick actually did versus NIFTY, so you can
see with real evidence whether the list is worth following.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis
from .config import Settings
from .scanner import passes_universe
from .setups import find_setup


def top_swing_picks(raw: dict[str, pd.DataFrame], index_df: pd.DataFrame, settings: Settings,
                    sectors: dict[str, str] | None = None, n: int = 5, max_per_sector: int = 2) -> pd.DataFrame:
    frames, rs_panel = analysis.prepare_universe(raw, index_df)
    when = index_df.index[-1]
    ucfg = settings.section("universe")
    rows = []
    for sym, f in frames.items():
        if when not in f.index:
            continue
        i = f.index.get_loc(when)
        row = f.iloc[i]
        if passes_universe(row, i + 1, ucfg) or not bool(row.get("hard_ok", False)):
            continue
        window = f.iloc[max(0, i - 259): i + 1]
        found = find_setup(sym, window, settings.section("setups"), settings.section("execution"), settings.section("risk"))
        rs = rs_panel.at[when, sym] if rs_panel is not None and sym in rs_panel.columns else np.nan
        card = analysis.score(sym, row, rs_pct=rs, plan=found.plan if found else None,
                              setup_quality=found.quality if found else 0.0)
        if not card.passed_hard_filters:
            continue
        atr = float(row["atr14"])
        if found:
            trigger, stop, setup = found.plan.trigger_price, found.plan.stop_price, found.plan.setup.value
        else:
            trigger = round(float(row["high"]) * 1.001, 2)
            stop, setup = round(trigger - 2 * atr, 2), "no setup yet (wait for pullback/breakout)"
        rows.append({
            "symbol": sym, "sector": (sectors or {}).get(sym, "UNKNOWN"), "score": card.total,
            "rs_pct": round(float(rs), 0) if not pd.isna(rs) else np.nan, "close": round(float(row["close"]), 2),
            "ret_6m_pct": round(float(row["ret_126"]) * 100, 1) if not pd.isna(row.get("ret_126")) else np.nan,
            "setup": setup, "trigger": trigger, "stop": stop,
            "stop_pct": round((trigger - stop) / trigger * 100, 1),
        })
    if not rows:
        return pd.DataFrame()
    ranked = pd.DataFrame(rows).sort_values(["score", "rs_pct"], ascending=False)
    picked, per_sector = [], {}
    for r in ranked.itertuples(index=False):
        if per_sector.get(r.sector, 0) >= max_per_sector and r.sector != "UNKNOWN":
            continue
        picked.append(r._asdict())
        per_sector[r.sector] = per_sector.get(r.sector, 0) + 1
        if len(picked) >= n:
            break
    return pd.DataFrame(picked).reset_index(drop=True)


def record_and_evaluate(picks: pd.DataFrame, raw: dict[str, pd.DataFrame], index_df: pd.DataFrame,
                        path: str | Path, eval_days: int = 10) -> dict:
    """Append today's list (once per date) and score every list that is at least ``eval_days`` old."""
    path = Path(path)
    hist = pd.read_csv(path) if path.exists() and path.stat().st_size else pd.DataFrame(
        columns=["date", "symbol", "close", "score"])
    today = str(index_df.index[-1].date())
    if len(picks) and today not in set(hist["date"].astype(str)):
        new = picks[["symbol", "close", "score"]].copy()
        new.insert(0, "date", today)
        hist = pd.concat([hist, new], ignore_index=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        hist.to_csv(path, index=False)
    days = index_df.index
    res, bench = [], []
    for r in hist.itertuples(index=False):
        d = pd.Timestamp(r.date)
        if d not in days:
            continue
        pos = days.get_loc(d)
        if pos + eval_days >= len(days) or r.symbol not in raw:
            continue
        later = days[pos + eval_days]
        c = raw[r.symbol]["close"]
        if later not in c.index:
            continue
        res.append(float(c.loc[later]) / float(r.close) - 1)
        bench.append(float(index_df["close"].loc[later]) / float(index_df["close"].loc[d]) - 1)
    out = {"lists": int(hist["date"].nunique()), "evaluated": len(res)}
    if res:
        out.update({"avg_pct": round(np.mean(res) * 100, 2), "nifty_avg_pct": round(np.mean(bench) * 100, 2),
                    "positive_pct": round(np.mean(np.array(res) > 0) * 100, 0),
                    "beat_nifty_pct": round(np.mean(np.array(res) > np.array(bench)) * 100, 0)})
    return out


def format_picks(picks: pd.DataFrame, track: dict, regime_on: bool, when: pd.Timestamp, eval_days: int = 10) -> str:
    L = [f"TOP 5 SWING WATCHLIST - {when:%a %d %b %Y}",
         "⚠️ Learning/paper only: the 2-20 day swing strategy did not beat charges in its backtest.",
         f"Market filter: {'ON' if regime_on else 'OFF - the bot would NOT buy today; watch only'}", ""]
    if picks.empty:
        L.append("No stock passes the trend filters today (price above a rising 200-day average). Cash is a position.")
    for i, r in enumerate(picks.itertuples(index=False), 1):
        L.append(f"{i}) {r.symbol} ({r.sector}) score {r.score:g}/100, strength {r.rs_pct:g}th pct, 6M {r.ret_6m_pct:+.0f}%")
        L.append(f"   ₹{r.close:,.2f} | {r.setup} | watch above ₹{r.trigger:,.2f}, stop ₹{r.stop:,.2f} (-{r.stop_pct}%)")
    L.append("")
    if track.get("evaluated"):
        L.append(f"Track record of past lists ({track['evaluated']} picks, {eval_days}-day return): avg {track['avg_pct']:+.2f}% "
                 f"vs NIFTY {track['nifty_avg_pct']:+.2f}% | {track['positive_pct']:.0f}% positive, "
                 f"{track['beat_nifty_pct']:.0f}% beat NIFTY")
    else:
        L.append(f"Track record: starts after the first lists are {eval_days} trading days old "
                 f"({track.get('lists', 0)} list(s) saved so far).")
    L.append("Deep dive on any name: GitHub -> Actions -> Analyze stock.")
    return "\n".join(L)
