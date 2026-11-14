"""Telegram presentation: aligned blocks fit a phone, splitting never breaks a table, HTML falls back safely."""
import re
from datetime import datetime, timezone

import pandas as pd
import pytest

from swingbot import notify
from swingbot.news import NewsCheck, NewsItem
from swingbot.notify import Notifier
from swingbot.tgfmt import WIDTH, Html, pack, plain, split_html, whatsapp


def _pre_lines(msg: str) -> list[str]:
    out = []
    for blk in re.findall(r"<pre>(.*?)</pre>", msg, re.S):
        out += plain(Html(blk)).split("\n")
    return out


def _picks(n=5):
    rows = []
    for i in range(n):
        rows.append({"symbol": f"LONGSYMBL{i}", "sector": "Oil Gas & Consumable Fuels", "score": 91.4, "rs_pct": 97.0,
                     "close": 124555.0, "ret_6m_pct": 149.0, "setup": "pullback", "trigger": 124709.68,
                     "stop": 115868.46, "stop_pct": 7.1, "t1": 142392.12, "t1_pct": 14.2, "t2": 150233.37,
                     "t2_pct": 20.5})
    return pd.DataFrame(rows)


def _full_watchlist():
    from swingbot.top_picks import format_picks

    p = _picks()
    vol = {s: {"profile": {"vol_ratio": 12.3, "cmf": -0.15, "acc_days": 13, "dist_days": 11, "verdict": "DISTRIBUTION"},
               "delivery": [88.0, 70, 71, 72, 73]} for s in p["symbol"]}
    odds = {s: {"period": "2015-2026", "cases": 12742, "target_first_pct": 26.0, "stop_first_pct": 53.0,
                "neither_pct": 21.0, "avg_r": -0.04, "triggered_pct": 48.0} for s in p["symbol"]}
    funda = {s: {"marketCap": 1.8e13, "trailingPE": 1291.2, "returnOnEquity": -0.09, "debtToEquity": 318.0,
                 "earningsGrowth": -4.66} for s in p["symbol"]}
    now = datetime.now(timezone.utc)
    news = {s: NewsCheck([NewsItem("A <very> long headline & more " * 5, "ET", now)] * 2,
                         ["⚠️ regulatory/legal", "📅 results/board meeting"], True) for s in p["symbol"]}
    return format_picks(p, {"lists": 3, "evaluated": 25, "avg_pct": 1.2, "nifty_avg_pct": 0.4, "positive_pct": 60,
                            "beat_nifty_pct": 55}, True, pd.Timestamp("2026-11-04"), news=news, odds=odds,
                        fundamentals=funda, volume=vol)


def test_watchlist_fits_phone_and_splits_between_cards():
    parts = _full_watchlist()
    assert len(parts) >= 2 and all(isinstance(p, Html) and len(p) <= 3500 for p in parts)
    for p in parts:
        assert p.count("<pre>") == p.count("</pre>")  # no table cut in half
        assert all(len(line) <= WIDTH for line in _pre_lines(p)), [x for x in _pre_lines(p) if len(x) > WIDTH]
    text = plain(parts)
    assert "&lt;" not in text and "<very>" in text  # headlines are escaped for Telegram, readable in plain text
    assert text.count("FINAL DECISION: SKIP") == 5  # 1 share > ₹5,000 limit
    assert "DISTRIBUTION" in text and "Uncertainty: HIGH" in text and "DISCLAIMER" in text


def test_tracker_and_dashboard_fit_phone():
    from swingbot.dashboard import market_dashboard
    from swingbot.synthetic import make_market
    from swingbot.tracker import format_tracker

    led = pd.DataFrame([{"id": "x", "date": "2026-11-02", "symbol": "BAJAJFINSV", "status": "ACTIVE", "r": -0.83,
                         "days": 12, "last_close": 124113.0, "stop": 119097.35, "t1": 131266.25, "trigger": 1.0,
                         "entry": 1.0}])
    changed = pd.DataFrame([{"symbol": "BAJAJFINSV", "status": "FAILED", "gap": True, "r": -1.6, "days": 3,
                             "trigger": 1.0, "entry": 1.0, "t1": 2.0, "stop": 0.5}])
    msgs = format_tracker(led, changed, {"n": 1, "since": "2026-11-02", "decided": 1, "triggered": 1, "closed": 0,
                                         "worked": 0, "failed": 0, "expired": 0}, ["HIGH: test alert"], "2026-11-04")
    stocks, idx, sectors = make_market(n_stocks=40, years=2, seed=9)
    fake = idx.iloc[-60:].copy()
    dash = market_dashboard(stocks, idx, sectors, {"INDIAVIX": fake.assign(close=22.0), "SP500": fake})
    for m in list(msgs) + [dash]:
        assert all(len(line) <= WIDTH for line in _pre_lines(m)), [x for x in _pre_lines(m) if len(x) > WIDTH]
    t = plain(msgs)
    assert "HIGH · test alert" in t and "BAJAJFINSV" in t and "119,097.35 → 131,266.25" in t


def test_split_never_inside_pre_and_pack():
    blocks = ["<b>A</b>\n<pre>" + "\n".join(f"row {i}" for i in range(50)) + "</pre>"] * 6
    msg = "\n\n".join(blocks)
    parts = split_html(msg, 1000)
    assert len(parts) > 1 and all(p.count("<pre>") == p.count("</pre>") for p in parts)
    assert len(pack(["x" * 2000, "y" * 2000, "z"], 3500)) == 2


def test_whatsapp_and_plain_conversion():
    m = Html("<b>TITLE</b>\n<pre>Entry  ₹1&lt;2</pre>\n<i>tag</i>")
    assert whatsapp(m) == "*TITLE*\n```\nEntry  ₹1<2\n```\n_tag_" and plain(m) == "TITLE\nEntry  ₹1<2\ntag"
    assert plain("not html <b>") == "not html <b>"


class Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._p = status, text, payload or {}

    def json(self):
        return self._p


@pytest.fixture
def tg(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.delenv("NOTIFY_PROVIDER", raising=False)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    return Notifier("telegram", tmp_path)


def test_telegram_html_mode_and_plain_fallback(monkeypatch, tg):
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json)
        if json.get("parse_mode") == "HTML" and "BROKEN" in json["text"]:
            return Resp(400, '{"ok":false,"description":"Bad Request: can\'t parse entities"}', {"ok": False})
        return Resp(200, payload={"ok": True})

    monkeypatch.setattr(notify.requests, "post", post)
    assert tg.send([Html("<b>ONE</b>"), Html("<b>TWO</b>")]) is True
    assert [s["parse_mode"] for s in sent] == ["HTML", "HTML"] and sent[0]["text"] == "<b>ONE</b>"
    sent.clear()
    assert tg.send(Html("<b>BROKEN</b>")) is True  # formatting rejected -> same words as plain text
    assert "parse_mode" not in sent[-1] and sent[-1]["text"] == "BROKEN"
    sent.clear()
    assert tg.send("plain <text> & stuff") is True and "parse_mode" not in sent[0]  # plain text stays plain
    sent.clear()
    assert tg.send("halt", urgent=True) is True and sent[0]["text"].startswith("<b>ALERT · SWINGBOT</b>")
