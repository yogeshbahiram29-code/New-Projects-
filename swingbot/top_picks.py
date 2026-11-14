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
from .base_rates import evaluate_candidate


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
        rs = rs_panel.at[when, sym] if rs_panel is not None and sym in rs_panel.columns else np.nan
        cand = evaluate_candidate(sym, f, i, rs, settings)  # same logic as the historical odds
        if cand is None:
            continue
        trigger, stop, setup, t1, t2 = cand["trigger"], cand["stop"], cand["setup"], cand["t1"], cand["t2"]
        rows.append({
            "symbol": sym, "sector": (sectors or {}).get(sym, "UNKNOWN"), "score": cand["score"],
            "rs_pct": round(float(rs), 0) if not pd.isna(rs) else np.nan, "close": round(float(row["close"]), 2),
            "ret_6m_pct": round(float(row["ret_126"]) * 100, 1) if not pd.isna(row.get("ret_126")) else np.nan,
            "setup": setup, "trigger": trigger, "stop": stop,
            "stop_pct": round((trigger - stop) / trigger * 100, 1),
            "t1": t1, "t1_pct": round((t1 / trigger - 1) * 100, 1),
            "t2": t2, "t2_pct": round((t2 / trigger - 1) * 100, 1),
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


from .tgfmt import SECTOR_SHORT, TAGLINE, Html, esc, head, kv, pack, pre, sector_name, title  # noqa: F401


def ordinal(n: float) -> str:
    n = int(round(n))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_picks(picks: pd.DataFrame, track: dict, regime_on: bool, when: pd.Timestamp, eval_days: int = 10,
                 max_stop_pct: float = 8.0, capital: float = 10_000, max_position_pct: float = 50.0,
                 news: dict | None = None, odds: dict | None = None, fundamentals: dict | None = None,
                 volume: dict | None = None, delivery_ok: bool = True) -> list[Html]:
    """Watchlist as Telegram messages: header + summary, one card per stock, then checklist/disclaimer.
    Split only between cards, so no card or table is ever cut in half."""
    from .stock_card import decision, fundamentals_block, history_block, news_block, technical_block
    from .tracker import pick_uncertainty

    limit_rs = capital * max_position_pct / 100
    cards, summary = [], []
    for i, r in enumerate(picks.itertuples(index=False), 1):
        v = (volume or {}).get(r.symbol) or {}
        reasons = pick_uncertainty(v.get("profile"), (odds or {}).get(r.symbol), odds is not None,
                                   (fundamentals or {}).get(r.symbol), fundamentals is not None, r.sector,
                                   (news or {}).get(r.symbol))
        label, why = decision(float(r.stop_pct), float(r.trigger), regime_on, reasons, max_stop_pct, limit_rs)
        summary.append(f"{i:<3}{r.symbol:<11}{r.score:>5.0f}  {label}")
        B = [f"<b>{i}. {esc(r.symbol)} | {r.score:.0f}/100</b>\n" + esc(
            f"Sector: {sector_name(r.sector)}\nCMP: ₹{r.close:,.2f}\nSetup: {str(r.setup).capitalize()}\n"
            f"Strength: {ordinal(r.rs_pct)} percentile · 6M {r.ret_6m_pct:+.0f}%")]
        B.append(head("Trading Levels") + "\n" + pre([
            f"{'Entry above':<12}{'₹' + format(r.trigger, ',.2f'):>11}",
            f"{'Stop loss':<12}{'₹' + format(r.stop, ',.2f'):>11}{'-' + format(r.stop_pct, 'g') + '%':>7}",
            f"{'Target 1':<12}{'₹' + format(r.t1, ',.2f'):>11}{'+' + format(r.t1_pct, 'g') + '%':>7}",
            f"{'Target 2':<12}{'₹' + format(r.t2, ',.2f'):>11}{'+' + format(r.t2_pct, 'g') + '%':>7}"]))
        if volume is not None and r.symbol in volume:
            B.append(technical_block(v.get("profile"), v.get("delivery"), delivery_ok))
        if fundamentals is not None:
            B.append(fundamentals_block(fundamentals.get(r.symbol), r.sector))
        if odds is not None:
            B.append(history_block(odds.get(r.symbol)))
        B.append(news_block((news or {}).get(r.symbol), reasons))
        B.append(f"<b>FINAL DECISION: {label}</b>\n" + esc("\n".join(why)))
        cards.append(SEP_CARD + "\n" + "\n\n".join(b for b in B if b))

    top = [title("Top 5 Swing Watchlist", f"{pd.Timestamp(when):%a, %d %b %Y} · paper trading only"),
           head("Market", "UPTREND" if regime_on else "DOWNTREND") + "\n" + esc(
               "Strategy: setups allowed (paper only)." if regime_on else "Strategy: WATCH ONLY – no fresh buying.")]
    if picks.empty:
        top.append(esc("No stock passes the trend filters today. Cash is a position."))
    else:
        top.append(head("Summary") + "\n" + pre([f"{'#':<3}{'STOCK':<11}{'SCORE':>5}  DECISION"] + summary))

    if track.get("evaluated"):
        rc = kv([("Picks checked", f"{track['evaluated']}"), (f"Avg {eval_days}D return", f"{track['avg_pct']:+.2f}%"),
                 (f"NIFTY {eval_days}D", f"{track['nifty_avg_pct']:+.2f}%"), ("Went up", f"{track['positive_pct']:.0f}%"),
                 ("Beat NIFTY", f"{track['beat_nifty_pct']:.0f}%"), ("Lists saved", f"{track.get('lists', 0)}")], 16)
    else:
        rc = esc(f"Tracking starts after the first list completes {eval_days} trading days.\n"
                 f"Saved watchlists: {track.get('lists', 0)}")
    foot = "\n\n".join([
        SEP_CARD + "\n" + head("Report Card") + "\n" + rc,
        head("Pre-Trade Checklist") + "\n" + esc("\n".join([
            "1. Read the latest company news.", "2. Verify results / board meeting dates.",
            "3. Confirm breakout and volume.", "4. Check the entry price before placing the order.",
            "5. Never skip the stop-loss.", f"6. Maximum stop-loss: {max_stop_pct:g}%.",
            f"7. Per-stock limit: ₹{limit_rs:,.0f}."])),
        head("Disclaimer") + "\n" + esc(
            "Experimental watchlist for paper trading and learning. Scores and setups are not independently "
            "validated. No guaranteed returns. Do not place real-money trades based only on this report."),
        esc("Deep dive: GitHub → Actions → Analyze Stock") + "\n" + TAGLINE])
    return pack(["\n\n".join(top)] + cards + [foot])


SEP_CARD = "─" * 18
