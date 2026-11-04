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


SECTOR_SHORT = {
    "Oil Gas & Consumable Fuels": "Oil & Gas",
    "Automobile and Auto Components": "Auto Components",
    "Fast Moving Consumer Goods": "FMCG",
    "Media Entertainment & Publication": "Media",
    "Telecommunication": "Telecom",
}

FLAG_TEXT = {
    "📅 results/board meeting": "Results / Board Meeting Date",
    "⚠️ regulatory/legal": "Regulatory / Legal News",
    "⚠️ rating/management": "Rating / Management / Promoter Activity",
    "🔔 corporate action": "Corporate Action (Bonus / Split / Dividend)",
    "✅ positive": "Positive News (Order Win / Upgrade)",
}

LINE = "━━━━━━━━━━━━━━━━━━"


def ordinal(n: float) -> str:
    n = int(round(n))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _news_line(check) -> list[str]:
    if check is None:
        return []
    if not check.ok:
        return ["📰 News: Could not fetch – check NSE/BSE announcements before deciding."]
    if not check.items:
        return ["📰 News: No major news in the last 7 days."]
    heads = []
    for it in check.items[:2]:
        t = it.title.split(" | ")[0].strip()
        heads.append(t if len(t) <= 80 else t[:77].rstrip() + "...")
    out = ["📰 News: " + "; ".join(heads) + "."]
    if check.flags:
        out.append("🚩 Check: " + "; ".join(FLAG_TEXT.get(f, f) for f in check.flags) + ".")
    return out


def format_picks(picks: pd.DataFrame, track: dict, regime_on: bool, when: pd.Timestamp, eval_days: int = 10,
                 max_stop_pct: float = 8.0, capital: float = 10_000, max_position_pct: float = 50.0,
                 news: dict | None = None, odds: dict | None = None, fundamentals: dict | None = None,
                 volume: dict | None = None, delivery_ok: bool = True) -> str:
    from .base_rates import format_odds
    from .fundamentals import format_fundamentals
    from .volume_quality import format_volume

    num = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    limit_rs = capital * max_position_pct / 100
    L = ["🤖 TOP 5 SWING TRADING WATCHLIST", f"📅 {when:%A, %d %B %Y}", "", LINE,
         "📈 MARKET OUTLOOK" if regime_on else "📉 MARKET OUTLOOK"]
    if regime_on:
        L += ["🟢 Market Trend: UPTREND", "✅ Strategy: SETUPS ALLOWED (PAPER ONLY)"]
    else:
        L += ["🔴 Market Trend: DOWNTREND", "⚠️ Strategy: WATCH ONLY – NO FRESH BUYING"]
    L += ["🧪 Paper Trading / Learning Purpose Only", LINE, ""]
    if picks.empty:
        L += ["💤 No stock passes the trend filters today. Cash is a position.", ""]
    for i, r in enumerate(picks.itertuples(index=False)):
        tag = num[i] if i < len(num) else f"{i + 1})"
        L.append(f"{tag} {r.symbol} | {SECTOR_SHORT.get(r.sector, r.sector)}")
        L.append(f"⭐ Score: {r.score:.0f}/100 | Strength: {ordinal(r.rs_pct)} Percentile")
        L.append(f"📈 6M Performance: {r.ret_6m_pct:+.0f}%")
        L.append(f"💰 CMP: ₹{r.close:,.2f} | Setup: {str(r.setup).capitalize()}")
        L.append("")
        L.append(f"🟢 Entry Above: ₹{r.trigger:,.2f}")
        L.append(f"🔴 Stop-Loss: ₹{r.stop:,.2f} (-{r.stop_pct}%)")
        L.append(f"🎯 Target 1: ₹{r.t1:,.2f} (+{r.t1_pct}%)")
        L.append(f"🎯 Target 2: ₹{r.t2:,.2f} (+{r.t2_pct}%)")
        L.append("")
        if r.stop_pct > max_stop_pct:
            L.append(f"⚠️ SL exceeds {max_stop_pct:g}% risk rule – SKIP.")
        if r.trigger > limit_rs:
            L.append(f"⚠️ Capital Alert: 1 share costs approximately ₹{r.trigger:,.0f}. "
                     f"Exceeds ₹{limit_rs:,.0f} per-stock limit.")
        if volume is not None and r.symbol in volume:
            v = volume[r.symbol]
            L += format_volume(v.get("profile"), v.get("delivery"), delivery_ok)
        if odds is not None:
            L += format_odds(odds.get(r.symbol))
        if fundamentals is not None:
            L += format_fundamentals(fundamentals.get(r.symbol), r.sector)
        L += _news_line((news or {}).get(r.symbol))
        L.append("")
    L += [LINE, "📊 REPORT CARD"]
    if track.get("evaluated"):
        L += [f"📈 Avg {eval_days}-day return of past picks: {track['avg_pct']:+.2f}% vs NIFTY {track['nifty_avg_pct']:+.2f}%",
              f"✅ Went up: {track['positive_pct']:.0f}% | Beat NIFTY: {track['beat_nifty_pct']:.0f}% "
              f"({track['evaluated']} picks checked)"]
    else:
        L.append(f"📅 Performance tracking starts after the first watchlist completes {eval_days} trading days.")
    L += [f"📌 Saved Watchlists: {track.get('lists', 0)}", "",
          "✅ PRE-TRADE CHECKLIST",
          "• Read latest company news.",
          "• Verify upcoming results / board meeting dates.",
          "• Confirm breakout and volume.",
          "• Check entry price before execution.",
          "• Never skip stop-loss.",
          f"• Follow maximum {max_stop_pct:g}% stop-loss rule.",
          f"• Respect ₹{limit_rs:,.0f} per-stock capital limit.", "",
          "⚠️ IMPORTANT DISCLAIMER",
          "This is an experimental watchlist for paper trading and learning. Scores and setups are not "
          "independently validated. No guaranteed returns. Avoid real-money trades based solely on this report.", "",
          "🔎 DEEP DIVE", "GitHub → Actions → Analyze Stock", "",
          LINE, "📊 Discipline First | Capital Protection Always."]
    return "\n".join(L)
