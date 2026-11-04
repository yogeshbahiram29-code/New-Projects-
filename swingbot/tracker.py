"""Prediction tracker: did every watchlist prediction work? Checked every session.

Each Top-5 pick is a prediction: "if the price trades above the entry level
next session, Target 1 will be hit before the stop-loss". Every daily run
re-checks every prediction against the real candles, with the same rules as
the historical odds table:

  WAITING        the next session hasn't happened yet
  NOT_TRIGGERED  next session never traded above the entry level
  MISSED         next session opened >2% above the entry (no chasing)
  ACTIVE         bought; neither stop nor Target 1 yet
  WORKED ✅      Target 1 hit first
  FAILED ❌      stop-loss hit first (same-day ambiguity = stop; gaps fill at the open)
  EXPIRED ⏳     neither within 20 sessions -> closed at that close
  REPEAT         same stock picked again while its earlier prediction was open (not double counted)

It also raises UNCERTAINTY ALERTS whenever something makes the predictions
less trustworthy: stale or missing data, sources that failed, a stop hit by a
gap, open trades near the stop, losing streaks, results worse (or suspiciously
better) than history, the market filter turning OFF, high volatility, results
or legal news on an open trade, and the Option B paper drawdown.

File: journal/predictions.csv (recomputed from prices every run, so it can't drift).
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

COLS = ["id", "date", "symbol", "sector", "setup", "score", "regime_on", "close", "trigger", "stop", "t1", "t2",
        "exp_t1_pct", "exp_avg_r", "source", "status", "entry_date", "entry", "exit_date", "exit", "r", "days",
        "last_close", "gap", "updated"]
CLOSED = ("WORKED", "FAILED", "EXPIRED")
DEFAULTS = {"horizon": 20, "max_chase_pct": 2.0, "near_stop_r": -0.7, "losing_streak": 4, "drift_min_closed": 10,
            "drift_p": 0.05, "vix_high": 20.0, "vix_jump_pct": 15.0, "nifty_move_pct": 2.0, "paper_dd_warn_pct": 10.0,
            "cost_pct": 0.55}


def cfg(settings) -> dict:
    return {**DEFAULTS, **(settings.section("tracker") if settings is not None else {})}


# ---------------------------------------------------------------- ledger
def load_ledger(path: Path) -> pd.DataFrame:
    if path.exists() and path.stat().st_size:
        return pd.read_csv(path, dtype={"id": str, "date": str, "symbol": str, "status": str, "source": str})
    return pd.DataFrame(columns=COLS)


def record_predictions(ledger: pd.DataFrame, picks: pd.DataFrame, when, regime_on: bool,
                       odds: dict | None = None, source: str = "live") -> pd.DataFrame:
    """Add today's picks. A re-run on the same date replaces that date's list (its session hasn't happened)."""
    day = str(pd.Timestamp(when).date())
    rows = []
    for r in picks.itertuples(index=False):
        o = (odds or {}).get(r.symbol) or {}
        rows.append({"id": f"{day}:{r.symbol}", "date": day, "symbol": r.symbol, "sector": getattr(r, "sector", ""),
                     "setup": r.setup, "score": float(r.score), "regime_on": bool(regime_on),
                     "close": float(r.close), "trigger": float(r.trigger), "stop": float(r.stop), "t1": float(r.t1),
                     "t2": float(r.t2), "exp_t1_pct": o.get("target_first_pct", np.nan),
                     "exp_avg_r": o.get("avg_r", np.nan), "source": source, "status": "WAITING"})
    keep = ledger[ledger["date"].astype(str) != day] if len(ledger) else ledger
    out = pd.concat([keep, pd.DataFrame(rows, columns=COLS)], ignore_index=True) if rows else keep
    return out.reindex(columns=COLS)


def track_one(df: pd.DataFrame | None, d, trigger: float, stop: float, t1: float, horizon: int = 20,
              max_chase: float = 0.02) -> dict:
    """Outcome of one prediction made on the close of ``d`` (same rules as base_rates.simulate)."""
    if df is None or df.empty:
        return {"status": "NO_DATA"}
    after = df.loc[df.index > pd.Timestamp(d)]
    if after.empty:
        return {"status": "WAITING", "last_close": float(df["close"].iloc[-1])}
    o, h, lo, c = (after[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    idx = after.index
    base = {"last_close": float(c[-1])}
    if o[0] > trigger * (1 + max_chase):
        return {**base, "status": "MISSED"}
    if h[0] < trigger:
        return {**base, "status": "NOT_TRIGGERED"}
    entry = max(o[0], trigger)
    risk = entry - stop
    if risk <= 0:
        return {**base, "status": "NOT_TRIGGERED"}
    base.update({"entry_date": str(idx[0].date()), "entry": round(entry, 2)})
    n = min(horizon, len(c))
    for k in range(n):
        if lo[k] <= stop:
            px = stop if k == 0 else min(o[k], stop)
            return {**base, "status": "FAILED", "exit_date": str(idx[k].date()), "exit": round(px, 2),
                    "r": round((px - entry) / risk, 2), "days": k + 1, "gap": bool(px < stop * 0.995)}
        if h[k] >= t1:
            return {**base, "status": "WORKED", "exit_date": str(idx[k].date()), "exit": round(t1, 2),
                    "r": round((t1 - entry) / risk, 2), "days": k + 1}
    if len(c) >= horizon:
        return {**base, "status": "EXPIRED", "exit_date": str(idx[horizon - 1].date()), "exit": round(c[horizon - 1], 2),
                "r": round((c[horizon - 1] - entry) / risk, 2), "days": horizon}
    return {**base, "status": "ACTIVE", "r": round((c[-1] - entry) / risk, 2), "days": len(c)}


def update_ledger(ledger: pd.DataFrame, raw: dict[str, pd.DataFrame], horizon: int = 20,
                  max_chase: float = 0.02) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Re-check every prediction. Returns (ledger, rows whose status changed this run)."""
    if ledger.empty:
        return ledger, ledger
    led = ledger.sort_values(["date", "symbol"]).reset_index(drop=True).astype(object)
    prev = led["status"].copy()
    busy: dict[str, str] = {}  # symbol -> date its open prediction ends ("9999" = still open)
    now = datetime.now().isoformat(timespec="seconds")
    for i, r in led.iterrows():
        if r["date"] < busy.get(r["symbol"], ""):
            res = {"status": "REPEAT"}
        else:
            res = track_one(raw.get(r["symbol"]), r["date"], float(r["trigger"]), float(r["stop"]), float(r["t1"]),
                            horizon, max_chase)
            if res["status"] == "NO_DATA":  # keep what we knew; the alert says data is missing
                res = {"status": prev[i] if isinstance(prev[i], str) and prev[i] else "WAITING"}
                led.at[i, "updated"] = now
                led.at[i, "status"] = res["status"]
                continue
            if res["status"] in CLOSED:
                busy[r["symbol"]] = res["exit_date"]
            elif res["status"] == "ACTIVE":
                busy[r["symbol"]] = "9999"
        for k in ("status", "entry_date", "entry", "exit_date", "exit", "r", "days", "last_close", "gap"):
            led.at[i, k] = res.get(k, np.nan if k not in ("status",) else "")
        led.at[i, "updated"] = now
    changed = led[led["status"] != prev.reindex(led.index)]
    return led.reindex(columns=COLS), changed


def save_ledger(ledger: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(path, index=False)


def backfill(ledger: pd.DataFrame, hist_path: Path, raw: dict, index_df: pd.DataFrame, settings, sectors) -> pd.DataFrame:
    """Rebuild entry/stop levels for watchlists sent before the tracker existed (same rules, same data cut)."""
    from .top_picks import top_swing_picks

    if not hist_path.exists():
        return ledger
    hist = pd.read_csv(hist_path, dtype={"date": str})
    have = set(ledger["date"].astype(str)) if len(ledger) else set()
    tp = settings.section("top_picks")
    for day, g in hist.groupby("date"):
        d = pd.Timestamp(day)
        if day in have or d not in index_df.index or d == index_df.index[-1]:
            continue
        cut = {s: df.loc[:d] for s, df in raw.items()}
        picks = top_swing_picks(cut, index_df.loc[:d], settings, sectors, int(tp.get("count", 5)) * 3,
                                int(tp.get("max_per_sector", 2)) * 3)
        picks = picks[picks["symbol"].isin(set(g["symbol"]))]
        ic = index_df["close"].loc[:d]
        m = ic.rolling(200).mean()
        reg = bool(ic.iloc[-1] > m.iloc[-1] and m.iloc[-1] > m.iloc[-21])
        ledger = record_predictions(ledger, picks, d, reg, None, source="rebuilt")
    return ledger


def fill_expectations(ledger: pd.DataFrame, table: pd.DataFrame | None, min_cases: int = 100) -> pd.DataFrame:
    """Historical T1-first odds for predictions recorded without them (e.g. rebuilt lists)."""
    from .base_rates import lookup

    if table is None or ledger.empty:
        return ledger
    led = ledger.copy()
    for i in led.index[pd.to_numeric(led["exp_t1_pct"], errors="coerce").isna()]:
        o = lookup(table, led.at[i, "setup"], float(led.at[i, "score"]), str(led.at[i, "regime_on"]) == "True",
                   min_cases)
        if o:
            led.at[i, "exp_t1_pct"], led.at[i, "exp_avg_r"] = o["target_first_pct"], o["avg_r"]
    return led


# ---------------------------------------------------------------- scorecard
def binom_cdf(k: int, n: int, p: float) -> float:
    return float(sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1)))


def scorecard(ledger: pd.DataFrame, cost_pct: float = 0.55) -> dict:
    led = ledger[ledger["status"] != "REPEAT"] if len(ledger) else ledger
    if led.empty:
        return {"n": 0}
    trig = led[led["status"].isin(("ACTIVE",) + CLOSED)]
    decided = led[~led["status"].isin(("WAITING",))]
    closed = led[led["status"].isin(CLOSED)].copy()
    out = {"n": len(led), "since": str(led["date"].min()), "decided": len(decided), "triggered": len(trig),
           "closed": len(closed), "worked": int((closed["status"] == "WORKED").sum()),
           "failed": int((closed["status"] == "FAILED").sum()), "expired": int((closed["status"] == "EXPIRED").sum())}
    if len(closed):
        r = pd.to_numeric(closed["r"], errors="coerce")
        stop_pct = (closed["trigger"].astype(float) - closed["stop"].astype(float)) / closed["trigger"].astype(float) * 100
        out["avg_r"] = float(r.mean())
        out["avg_net_r"] = float((r - cost_pct / stop_pct.clip(lower=0.5)).mean())
        out["hit_pct"] = out["worked"] / len(closed) * 100
        exp = pd.to_numeric(closed["exp_t1_pct"], errors="coerce").dropna()
        if len(exp):
            sub = closed.loc[exp.index]
            out["exp_hit_pct"] = float(exp.mean())
            out["exp_n"] = len(sub)
            out["exp_wins"] = int((sub["status"] == "WORKED").sum())
            out["p_worse"] = binom_cdf(out["exp_wins"], len(sub), float(exp.mean()) / 100)
    return out


# ---------------------------------------------------------------- uncertainty
def pick_uncertainty(profile: dict | None = None, odds: dict | None = None, has_odds: bool = False,
                     funda: dict | None = None, has_funda: bool = False, sector: str = "", news=None) -> list[str]:
    """Reasons a single pick is less certain than its score suggests."""
    out = []
    if profile and profile.get("verdict") == "DISTRIBUTION":
        out.append("money flowing out (distribution)")
    if has_odds:
        if not odds:
            out.append("too little history for this setup")
        elif float(odds.get("avg_r", 0)) < 0.05:
            out.append("history shows no real edge")
        elif float(odds.get("avg_r", 0)) < 0.25:
            out.append("thin historical edge")
    if has_funda:
        if not funda:
            out.append("fundamentals unchecked")
        else:
            from .fundamentals import assess

            warn, _ = assess(funda, sector)
            if warn:
                out.append("fundamental red flag (" + ", ".join(w.split(" (")[0].lower() for w in warn) + ")")
    if news is not None:
        if not news.ok:
            out.append("news unchecked")
        else:
            flags = " ".join(news.flags)
            if "results" in flags:
                out.append("results/board meeting – gap risk")
            if "regulatory" in flags or "rating" in flags:
                out.append("risky news (legal/rating/promoter)")
    return out


def format_pick_uncertainty(reasons: list[str]) -> str:
    if not reasons:
        return "✅ Confidence check: no conflicting signals (still not a guarantee)"
    icon = "🔴 High uncertainty" if len(reasons) >= 2 else "🟡 Uncertainty"
    return f"{icon}: " + " · ".join(reasons)


def expected_last_session(now: datetime | None = None) -> date:
    from zoneinfo import ZoneInfo

    now = now or datetime.now(ZoneInfo("Asia/Kolkata"))
    d = now.date()
    if d.weekday() < 5 and (now.hour, now.minute) >= (15, 45):
        return d
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def uncertainty_alerts(ledger: pd.DataFrame, changed: pd.DataFrame, card: dict, c: dict, *,
                       index_df: pd.DataFrame | None = None, regime_on: bool | None = None, extra: dict | None = None,
                       raw: dict | None = None, sources: dict | None = None, news: dict | None = None,
                       paper: dict | None = None, now: datetime | None = None) -> list[str]:
    """🔴 = act / don't trust today's signals blindly, 🟡 = be careful."""
    A: list[str] = []
    led = ledger.reindex(columns=COLS) if len(ledger) else pd.DataFrame(columns=COLS)
    active = led[led["status"] == "ACTIVE"]
    open_ = led[led["status"].isin(("ACTIVE", "WAITING"))]
    # -- data
    if index_df is not None and len(index_df):
        last = index_df.index[-1].date()
        exp = expected_last_session(now)
        if last < exp:
            A.append(f"🟡 Latest price data is {last:%a %d %b} (expected {exp:%a %d %b}). If the market was open, "
                     "data is late – levels may be stale.")
    if raw is not None:
        missing = sorted(s for s in open_["symbol"] if s not in raw)
        if missing:
            A.append(f"🔴 No price data for open prediction(s): {', '.join(missing)} – check them yourself in the app.")
    src = sources or {}
    if src.get("news_failed"):
        A.append(f"🟡 News could not be checked for: {', '.join(src['news_failed'])}.")
    if src.get("delivery_ok") is False:
        A.append("🟡 NSE delivery % unavailable today (NSE blocked the server).")
    if src.get("funda_missing"):
        A.append(f"🟡 Fundamentals missing for: {', '.join(src['funda_missing'])}.")
    if src.get("odds_missing"):
        A.append("🟡 Historical odds table missing – run Actions → Refresh odds.")
    # -- predictions
    for r in changed.itertuples(index=False):
        if r.status == "FAILED" and str(r.gap) == "True":
            A.append(f"🔴 {r.symbol}: price gapped below the stop – loss bigger than planned ({float(r.r):+.1f}R). "
                     "Gaps are why position size is kept small.")
    for r in active.itertuples(index=False):
        if pd.notna(r.r) and float(r.r) <= c["near_stop_r"]:
            A.append(f"🟡 {r.symbol} is close to its stop-loss ({float(r.r):+.1f}R) – make sure the GTT stop is live.")
        if regime_on is False and str(r.regime_on) == "True":
            A.append(f"🟡 Market filter turned OFF after {r.symbol} was predicted – no new entries; don't widen its stop.")
    results_news = []
    for sym, chk in (news or {}).items():
        if sym in set(active["symbol"]) and chk is not None and chk.ok:
            flags = " ".join(chk.flags)
            if "results" in flags:
                results_news.append(sym)
            if "regulatory" in flags or "rating" in flags:
                A.append(f"🔴 {sym} (open): legal/rating/promoter news – read it today.")
    if results_news:
        A.append(f"🟡 Results/board-meeting news on open predictions: {', '.join(results_news)} – "
                 "a gap past the stop is possible.")
    closed = led[led["status"].isin(CLOSED)].sort_values("exit_date")
    streak = 0
    for s in reversed(closed["status"].tolist()):
        if s != "FAILED":
            break
        streak += 1
    if streak >= c["losing_streak"]:
        A.append(f"🔴 {streak} predictions in a row hit the stop-loss – conditions may have changed. "
                 "Paper-trade only until a winner appears.")
    if card.get("exp_n", 0) >= c["drift_min_closed"]:
        if card["p_worse"] < c["drift_p"]:
            A.append(f"🔴 Predictions are working WORSE than history: {card['exp_wins']}/{card['exp_n']} hit Target 1 "
                     f"vs {card['exp_hit_pct']:.0f}% expected (chance of bad luck only {card['p_worse'] * 100:.1f}%). "
                     "Trust the watchlist less until this recovers.")
    if card.get("closed", 0) >= c["drift_min_closed"] and card.get("hit_pct", 0) > 75:
        A.append(f"🚩 Hit rate {card['hit_pct']:.0f}% is suspiciously high – check the data for errors before trusting it.")
    # -- market
    if index_df is not None and len(index_df) >= 2:
        mv = (float(index_df["close"].iloc[-1]) / float(index_df["close"].iloc[-2]) - 1) * 100
        if abs(mv) >= c["nifty_move_pct"]:
            A.append(f"🟡 NIFTY moved {mv:+.1f}% today – big market days make stops and triggers less reliable.")
    vix = (extra or {}).get("INDIAVIX")
    if vix is not None and len(vix) >= 2:
        v, pv = float(vix["close"].iloc[-1]), float(vix["close"].iloc[-2])
        if v >= c["vix_high"]:
            A.append(f"🟡 India VIX {v:.1f} – high volatility; normal noise can hit stops.")
        elif pv > 0 and (v / pv - 1) * 100 >= c["vix_jump_pct"]:
            A.append(f"🟡 India VIX jumped {(v / pv - 1) * 100:+.0f}% today – fear is rising.")
    if paper and paper.get("history"):
        eq = [h[1] for h in paper["history"]]
        dd = (1 - eq[-1] / max(eq + [paper.get("start_capital", eq[0])])) * 100
        if dd >= c["paper_dd_warn_pct"]:
            A.append(f"🟡 Option B paper portfolio is {dd:.1f}% below its peak (stop-review limit 20%).")
    return A


# ---------------------------------------------------------------- message
LINE = "━━━━━━━━━━━━━━━━━━"


def _rs(x) -> str:
    return f"₹{float(x):,.2f}"


def format_tracker(ledger: pd.DataFrame, changed: pd.DataFrame, card: dict, alerts: list[str], when,
                   paper: dict | None = None, horizon: int = 20) -> str:
    L = ["🔍 PREDICTION TRACKER", f"📅 {pd.Timestamp(when):%A, %d %B %Y}", "", LINE]
    if alerts:
        L.append(f"🚨 UNCERTAINTY ALERTS ({len(alerts)})")
        L += [f"• {a}" for a in alerts]
    else:
        L.append("✅ No uncertainty alerts today.")
    L += [LINE, "📰 TODAY'S UPDATES"]
    ups = []
    ch = changed[changed["status"] != "REPEAT"] if len(changed) else changed
    for r in ch.itertuples(index=False):
        if r.status == "WORKED":
            ups.append(f"✅ WORKED: {r.symbol} hit Target 1 {_rs(r.t1)} ({float(r.r):+.1f}R in {int(r.days)} days)")
        elif r.status == "FAILED":
            ups.append(f"❌ FAILED: {r.symbol} hit the stop-loss {_rs(r.stop)} ({float(r.r):+.1f}R"
                       + (", gap" if str(r.gap) == "True" else "") + ")")
        elif r.status == "EXPIRED":
            ups.append(f"⏳ TIME-OUT: {r.symbol} {horizon} sessions without target or stop ({float(r.r):+.1f}R)")
        elif r.status == "ACTIVE":
            ups.append(f"🟢 TRIGGERED: {r.symbol} traded above {_rs(r.trigger)} – paper entry {_rs(r.entry)}")
    nt = [r.symbol for r in ch.itertuples(index=False) if r.status in ("NOT_TRIGGERED", "MISSED")]
    if nt:
        ups.append(f"⚪ Not triggered (prediction void): {', '.join(nt)}")
    L += ups or ["No status changes since the last session."]
    act = ledger[ledger["status"] == "ACTIVE"] if len(ledger) else ledger
    L += [LINE, f"📂 OPEN PREDICTIONS ({len(act)})"]
    for r in act.itertuples(index=False):
        L.append(f"• {r.symbol} (from {pd.Timestamp(r.date):%d %b}): {_rs(r.last_close)} now, {float(r.r):+.1f}R, "
                 f"day {int(r.days)}/{horizon} | SL {_rs(r.stop)} · T1 {_rs(r.t1)}")
    if act.empty:
        L.append("None right now.")
    waiting = int((ledger["status"] == "WAITING").sum()) if len(ledger) else 0
    if waiting:
        L.append(f"⏳ Waiting for the next session: {waiting} (today's list)")
    L += [LINE, f"📊 SCORECARD (since {pd.Timestamp(card['since']):%d %b %Y})" if card.get("n") else "📊 SCORECARD"]
    if not card.get("n"):
        L.append("No predictions recorded yet.")
    else:
        trig_pct = card["triggered"] / card["decided"] * 100 if card["decided"] else 0
        L.append(f"Predictions: {card['n']} | Triggered: {card['triggered']} ({trig_pct:.0f}% of decided)")
        L.append(f"Closed: {card['closed']} → ✅ {card['worked']} worked | ❌ {card['failed']} failed | "
                 f"⏳ {card['expired']} time-out")
        if card["closed"]:
            exp = f" (history expects {card['exp_hit_pct']:.0f}%)" if "exp_hit_pct" in card else ""
            L.append(f"🎯 Hit rate: {card['hit_pct']:.0f}%{exp}")
            L.append(f"💰 Avg {card['avg_r']:+.2f}R before charges · {card['avg_net_r']:+.2f}R after")
        if card["closed"] < 10:
            L.append(f"📏 Only {card['closed']} closed – need 10+ before judging whether predictions work.")
    if paper and paper.get("history"):
        eq, nifty = paper["history"][-1][1], paper["history"][-1][2]
        L += [LINE, f"📁 Option B paper: {(eq / paper['start_capital'] - 1) * 100:+.1f}% vs NIFTY "
                    f"{(nifty / paper['start_nifty'] - 1) * 100:+.1f}% since {pd.Timestamp(paper['start_date']):%d %b}"]
    L += [LINE, "🧪 Paper tracking only. \"Worked\" = Target 1 hit before the stop-loss (2R).",
          "📊 Discipline First | Capital Protection Always."]
    return "\n".join(L)


def load_paper(journal: Path) -> dict | None:
    p = journal / "paper_rotation.json"
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except ValueError:
        return None


__all__ = ["record_predictions", "update_ledger", "track_one", "scorecard", "uncertainty_alerts", "format_tracker",
           "pick_uncertainty", "format_pick_uncertainty", "backfill", "fill_expectations", "load_ledger", "save_ledger"]
