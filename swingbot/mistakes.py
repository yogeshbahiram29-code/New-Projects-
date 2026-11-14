"""Mistake tracker: most trading losses come from behaviour, not missing data.

Every recorded buy/sell is checked against the bot's own plan and rules:

BUY  - off-plan (not in a recent watchlist) · bought while the market filter
       was OFF · chased above the planned entry · entered before the trigger ·
       no / too-wide stop · risk above 1% of capital · position above the
       per-stock limit · revenge trade (bought within a day of a loss) ·
       overtrading · emotional entry (FOMO / greed / revenge / fear)
SELL - stop not honoured · early exit out of fear · held past the plan

Each trade gets instant feedback, and a weekly review summarises discipline.
Files: journal/latest_watchlist.json, journal/trade_log.csv
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

LOG_COLS = ["time", "action", "symbol", "qty", "price", "stop", "target", "reason", "emotion",
            "followed_plan", "pnl", "mistakes"]
BAD_EMOTIONS = {"fomo": "FOMO (fear of missing out)", "greed": "greed", "revenge": "revenge", "fear": "fear"}
TIPS = {
    "Off-plan": "Only trade names from the bot's watchlist – random tips are where big losses start.",
    "Market filter OFF": "When the market filter is OFF, sit in cash. Most stocks fall with the market.",
    "Chased": "Place the stop-limit BUY at the planned price and let it fill – never chase a running stock.",
    "Before trigger": "Wait for the price to cross the trigger – it confirms buyers are in control.",
    "No stop": "Set the GTT stop-loss the same minute the buy fills. No exceptions.",
    "Wide stop": "If the logical stop is more than 8% away, skip the trade.",
    "Oversized": "Size = (1% of capital) ÷ (entry − stop). One bad trade must never hurt much.",
    "Big position": "Keep each stock within the per-stock limit so one gap-down can't damage the account.",
    "Revenge": "After a loss, take a break for a day. Revenge trades are the costliest trades.",
    "Overtrading": "Fewer, better trades. Quality beats quantity after charges.",
    "Emotion": "Name the emotion, then follow the written plan instead of the feeling.",
    "Not plan": "Write the plan before the trade and follow it – or don't trade.",
    "Stop not honoured": "Let the GTT stop execute. Hoping a loser comes back is how small losses become big.",
    "Early exit": "Let the stop or target decide. Selling early out of fear cuts winners short.",
    "Held too long": "Respect the time exit – dead money has an opportunity cost.",
}


# ---------------------------------------------------------------- watchlist state
def save_watchlist_state(settings, picks: pd.DataFrame, regime_on: bool, when) -> None:
    path = Path(settings.get("signals.dir", "journal")) / "latest_watchlist.json"
    st = json.loads(path.read_text()) if path.exists() else {"history": []}
    day = str(pd.Timestamp(when).date())
    entry = {"date": day, "regime_on": bool(regime_on), "picks": [
        {k: (float(r[k]) if k not in ("symbol", "setup") else str(r[k])) for k in
         ("symbol", "setup", "score", "trigger", "stop", "t1", "t2") if k in r}
        for _, r in (picks.iterrows() if len(picks) else [])]}
    st["history"] = [h for h in st.get("history", []) if h["date"] != day] + [entry]
    st["history"] = st["history"][-10:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, indent=1))


def _watchlists(journal: Path) -> list[dict]:
    p = journal / "latest_watchlist.json"
    return json.loads(p.read_text()).get("history", []) if p.exists() else []


def find_plan(journal: Path, symbol: str, days: int = 5) -> tuple[dict | None, dict | None]:
    """Most recent watchlist pick for ``symbol`` within ``days`` lists, and the latest list."""
    hist = _watchlists(journal)
    latest = hist[-1] if hist else None
    for h in reversed(hist[-days:]):
        for p in h["picks"]:
            if p["symbol"] == symbol.upper():
                return p, latest
    return None, latest


# ---------------------------------------------------------------- trade log
def load_log(journal: Path) -> pd.DataFrame:
    p = journal / "trade_log.csv"
    if p.exists() and p.stat().st_size:
        return pd.read_csv(p, dtype={"mistakes": str, "reason": str, "emotion": str, "followed_plan": str})
    return pd.DataFrame(columns=LOG_COLS)


def append_log(journal: Path, row: dict) -> None:
    df = pd.concat([load_log(journal), pd.DataFrame([row])], ignore_index=True)
    journal.mkdir(parents=True, exist_ok=True)
    df[LOG_COLS].to_csv(journal / "trade_log.csv", index=False)


def _emotion_flags(emotion: str | None, followed_plan: str | None) -> list[str]:
    out = []
    e = (emotion or "").strip().lower()
    if e in BAD_EMOTIONS:
        out.append(f"Emotion: traded on {BAD_EMOTIONS[e]}")
    if (followed_plan or "").strip().lower() in ("no", "n", "false"):
        out.append("Not plan: you said you didn't follow the plan")
    return out


def check_buy(settings, journal: Path, symbol: str, qty: int, price: float, stop: float | None,
              emotion: str | None = None, followed_plan: str | None = None, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    capital = float(settings.get("initial_capital", 10000))
    prof = settings.profile_for(capital)
    risk_pct = prof.risk_pct.get("bull", 1.0)
    max_stop = float(settings.get("risk.max_stop_distance_pct", 8))
    slip = float(settings.get("execution.entry_limit_slippage_pct", 0.5)) / 100
    plan, latest = find_plan(journal, symbol)
    m: list[str] = []
    if plan is None:
        m.append("Off-plan: not in the bot's watchlist of the last 5 days")
    if latest is not None and not latest.get("regime_on", True):
        m.append("Market filter OFF: bought while the bot said 'watch only'")
    if plan:
        trig = plan["trigger"]
        if price > trig * (1 + slip):
            m.append(f"Chased: paid {(price / trig - 1) * 100:.1f}% above the planned entry ₹{trig:,.2f}")
        elif price < trig * 0.995:
            m.append(f"Before trigger: bought below the ₹{trig:,.2f} confirmation level")
    if not stop:
        m.append("No stop: no stop-loss recorded")
    else:
        dist = (price - stop) / price * 100
        if dist > max_stop:
            m.append(f"Wide stop: stop is {dist:.1f}% away (rule: max {max_stop:g}%)")
        risk = qty * (price - stop)
        allowed = capital * risk_pct / 100
        if risk > allowed * 1.1:
            m.append(f"Oversized: risked ₹{risk:,.0f} (rule: ₹{allowed:,.0f} = {risk_pct:g}% of capital)")
    limit = capital * prof.max_position_pct / 100
    if qty * price > limit * 1.02:
        m.append(f"Big position: ₹{qty * price:,.0f} in one stock (limit ₹{limit:,.0f})")
    log = load_log(journal)
    if len(log):
        t = pd.to_datetime(log["time"])
        recent_loss = log[(log["action"] == "SELL") & (pd.to_numeric(log["pnl"], errors="coerce") < 0)
                          & (t >= now - timedelta(days=1))]
        if len(recent_loss):
            m.append("Revenge: bought within a day of a losing trade")
        if int(((log["action"] == "BUY") & (t >= now - timedelta(days=5))).sum()) >= 3:
            m.append("Overtrading: this is the 4th+ new trade in 5 days")
    return m + _emotion_flags(emotion, followed_plan)


def check_sell(settings, symbol: str, price: float, stop: float | None, target: float | None, entry_date: str | None,
               emotion: str | None = None, followed_plan: str | None = None, today: date | None = None) -> list[str]:
    today = today or date.today()
    m: list[str] = []
    if stop and price < stop * 0.99:
        m.append(f"Stop not honoured: sold {(1 - price / stop) * 100:.1f}% below the ₹{stop:,.2f} stop (ok only if it gapped down)")
    scared = (emotion or "").lower() in ("fear", "greed") or (followed_plan or "").lower() in ("no", "n")
    if stop and target and stop < price < target and scared:
        m.append("Early exit: sold between stop and target without a plan signal")
    if entry_date:
        held = int(pd.bdate_range(pd.Timestamp(entry_date), pd.Timestamp(today)).size) - 1
        max_hold = int(settings.get("momentum.max_hold_days", 20))
        if held > max_hold + 2:
            m.append(f"Held too long: {held} trading days (plan: max {max_hold})")
    return m + _emotion_flags(emotion, followed_plan)


def feedback(action: str, symbol: str, qty: int, price: float, stop, target, mistakes: list[str], pnl=None):
    from .tgfmt import TAGLINE, Html, esc, head, kv, title

    B = [title("Trade Recorded", f"{action} {qty} {symbol} @ ₹{price:,.2f}")]
    if action == "BUY":
        B.append(kv([("Stop loss", f"₹{stop:,.2f}" if stop else "NOT SET"),
                     ("Target", f"₹{target:,.2f}" if target else "NOT SET")], 11)
                 + "\n" + esc("Set the GTT stop-loss in the app now if you haven't."))
    elif pnl is not None:
        B.append(kv([("P&L", f"₹{pnl:,.0f} after charges")], 11))
    if mistakes:
        rows = []
        for i, x in enumerate(mistakes, 1):
            rows.append(f"{i}. {x}")
            tip = TIPS.get(x.split(":")[0])
            if tip:
                rows.append(f"   Tip: {tip}")
        B.append(head("Mistake Check", f"{len(mistakes)} issue(s)") + "\n" + esc("\n".join(rows)))
    else:
        B.append(head("Mistake Check") + "\n" + esc("No issues – rules followed. Well done."))
    B.append(TAGLINE)
    return Html("\n\n".join(B))


def weekly_review(settings, journal: Path, today: date | None = None):
    from .tgfmt import TAGLINE, Html, esc, head, kv, title

    today = today or date.today()
    start = today - timedelta(days=7)
    log = load_log(journal)
    wk = log[pd.to_datetime(log["time"]).dt.date >= start] if len(log) else log
    B = [title("Weekly Discipline Review", f"{start:%d %b} – {today:%d %b %Y}")]
    if wk.empty:
        B.append(esc("No trades recorded this week.\n"
                     "Not trading is fine – waiting for good setups and a green market filter IS the strategy.\n"
                     "Recorded a real trade? Use Actions → Record trade so the bot can check it."))
    else:
        buys, sells = int((wk["action"] == "BUY").sum()), int((wk["action"] == "SELL").sum())
        pnl = pd.to_numeric(wk.loc[wk["action"] == "SELL", "pnl"], errors="coerce").dropna()
        flagged = wk["mistakes"].fillna("").str.len() > 0
        score = (1 - flagged.mean()) * 100
        rows = [("Buys", f"{buys}"), ("Sells", f"{sells}")]
        if len(pnl):
            rows += [("Closed P&L", f"₹{pnl.sum():,.0f}"), ("Winners", f"{int((pnl > 0).sum())}/{len(pnl)}")]
        rows.append(("Discipline", f"{score:.0f}%"))
        B.append(head("Trades") + "\n" + kv(rows, 13) + "\n" + esc("Discipline = share of trades with no mistakes."))
        counts: dict[str, int] = {}
        for ms in wk["mistakes"].fillna(""):
            for x in [y for y in ms.split(" | ") if y]:
                key = x.split(":")[0]
                counts[key] = counts.get(key, 0) + 1
        if counts:
            B.append(head("Mistakes This Week") + "\n" + kv(
                [(k, f"x{v}") for k, v in sorted(counts.items(), key=lambda kv_: -kv_[1])], 20))
            top = max(counts, key=counts.get)
            B.append(head("Focus Next Week", top) + "\n" + esc(TIPS.get(top, "Follow the written plan.")))
        else:
            B.append(esc("No mistakes this week – keep it up."))
    paper = journal / "paper_rotation.json"
    if paper.exists():
        st = json.loads(paper.read_text())
        if st.get("history"):
            eq, nifty = st["history"][-1][1], st["history"][-1][2]
            B.append(head("Option B") + "  " + esc(
                f"paper {(eq / st['start_capital'] - 1) * 100:+.1f}% vs NIFTY "
                f"{(nifty / st['start_nifty'] - 1) * 100:+.1f}% since {st['start_date']}"))
    B.append(TAGLINE)
    return Html("\n\n".join(B))
