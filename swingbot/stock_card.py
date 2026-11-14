"""Stock card sections for Telegram (watchlist and Analyze stock).

Presentation only: every value comes from the existing calculations
(volume_quality, fundamentals, base_rates, news, tracker.pick_uncertainty).
"""
from __future__ import annotations

from .tgfmt import esc, head, kv, short

FLAG_TEXT = {
    "📅 results/board meeting": "Results / board meeting",
    "⚠️ regulatory/legal": "Regulatory / legal",
    "⚠️ rating/management": "Rating / management / promoter",
    "🔔 corporate action": "Corporate action",
    "✅ positive": "Positive news",
}


def technical_block(profile: dict | None, deliveries: list[float] | None, delivery_ok: bool) -> str:
    from .volume_quality import delivery_level

    if not profile:
        return ""
    rows = [("Volume", f"{profile['vol_ratio']:.1f}x 50D avg"),
            ("Money flow", f"{profile['cmf']:+.2f}"),
            ("Strong days", f"{profile['acc_days']} up / {profile['dist_days']} down")]
    note = ""
    if deliveries:
        avg = sum(deliveries) / len(deliveries)
        lvl = delivery_level(avg)
        rows.append(("Delivery", f"{deliveries[0]:.0f}% · {len(deliveries)}D {avg:.0f}%" + (f" {lvl}" if lvl else "")))
        note = {"high": "High delivery: genuine investor buying.",
                "low": "Low delivery: mostly short-term trading."}.get(lvl, "")
    elif not delivery_ok:
        rows.append(("Delivery", "n/a (NSE blocked)"))
    rows.append(("Flow", profile["verdict"]))
    return head("Technical Data") + "\n" + kv(rows, 13) + ("\n" + esc(note) if note else "")


def fundamentals_block(f: dict | None, sector: str = "") -> str:
    from .fundamentals import assess, fundamental_fields

    if not f:
        return head("Fundamentals") + "\n" + esc("Not available today (data source busy). "
                                                 "Check screener.in or the annual report.")
    v = fundamental_fields(f)
    out = head("Fundamentals") + "\n" + kv([("Market cap", v["mcap"]), ("P/E", v["pe"]), ("ROE", v["roe"]),
                                            ("Debt/Equity", v["de"]), ("Profit growth", v["growth"])], 14)
    warn, good = assess(f, sector)
    if warn:
        out += "\n" + esc("Flag: " + ", ".join(warn) + ".")
    elif good:
        out += "\n" + esc("Positives: " + ", ".join(good) + ".")
    return out


def history_block(o: dict | None) -> str:
    from .base_rates import edge_level

    if not o:
        return head("Historical Performance") + "\n" + esc("Not enough history yet (Actions → Refresh odds).")
    edge = {"positive": "POSITIVE", "thin": "THIN (cost risk)", "none": "NONE"}[edge_level(o["avg_r"])]
    return (head("Historical Performance") + "\n" + esc(f"{o['period']} · {int(o['cases']):,} similar cases") + "\n"
            + kv([("T1 first", f"{o['target_first_pct']:.0f}%"), ("SL first", f"{o['stop_first_pct']:.0f}%"),
                  ("Neither", f"{o['neither_pct']:.0f}%"), ("Average R", f"{o['avg_r']:+.2f} before costs"),
                  ("Triggered", f"{o['triggered_pct']:.0f}%"), ("Edge", edge)], 12))


def uncertainty_text(reasons: list[str]) -> str:
    if not reasons:
        return "LOW – no conflicting signals (not a guarantee)"
    return ("HIGH – " if len(reasons) >= 2 else "MEDIUM – ") + ", ".join(reasons)


def news_block(check, reasons: list[str] | None = None, headlines: int = 2) -> str:
    body = []
    if check is not None:
        if not check.ok:
            body.append("News: could not fetch – check NSE/BSE announcements before deciding.")
        elif not check.items:
            body.append("No major news in the last 7 days.")
        else:
            body += [f"- {short(it.title.split(' | ')[0], 64)}" for it in check.items[:headlines]]
            if check.flags:
                body.append("Flags: " + " · ".join(FLAG_TEXT.get(f, f) for f in check.flags))
    if reasons is not None:
        body.append("Uncertainty: " + uncertainty_text(reasons))
    return head("News & Risk") + "\n" + esc("\n".join(body)) if body else ""


def decision(stop_pct: float, trigger: float, regime_on: bool, reasons: list[str], max_stop_pct: float,
             limit_rs: float) -> tuple[str, list[str]]:
    """FINAL DECISION label from the existing rules (no new trading logic):
    SKIP       - breaks a hard rule: stop wider than the max-stop rule, or 1 share above the per-stock limit
    WATCH ONLY - market filter OFF (no fresh buying), or high uncertainty (2+ conflicting signals)
    BUY WATCH  - market filter ON and at most one conflict: buy only if it trades above the entry (paper)
    """
    why = []
    if stop_pct > max_stop_pct:
        why.append(f"Stop-loss {stop_pct:g}% exceeds the {max_stop_pct:g}% rule.")
    if trigger > limit_rs:
        why.append(f"1 share ≈ ₹{trigger:,.0f} exceeds the ₹{limit_rs:,.0f} per-stock limit.")
    if why:
        return "SKIP", why
    if not regime_on:
        return "WATCH ONLY", ["Market filter OFF – no fresh buying."]
    if len(reasons) >= 2:
        return "WATCH ONLY", ["High uncertainty – conflicting signals."]
    return "BUY WATCH", [f"Buy only above ₹{trigger:,.2f}, stop-loss order placed at once (paper)."]


__all__ = ["technical_block", "fundamentals_block", "history_block", "news_block", "decision", "uncertainty_text"]
