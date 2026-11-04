"""News check: RSS parsing, recency filter, risk flags, graceful failure."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from swingbot import news
from swingbot.news import NewsCheck, classify, fetch_news, format_news, parse_google_rss


def _rss(items):
    body = "".join(
        f"<item><title>{t} - {src}</title><pubDate>{format_datetime(d)}</pubDate><source url='x'>{src}</source></item>"
        for t, src, d in items)
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>"


def test_parse_recent_items_only_and_strip_source():
    now = datetime.now(timezone.utc)
    xml = _rss([("Cyient bags large order from US client", "ET Markets", now - timedelta(hours=5)),
                ("Old story", "BS", now - timedelta(days=30))])
    items = parse_google_rss(xml, days=7, limit=3)
    assert len(items) == 1 and items[0].title == "Cyient bags large order from US client" and items[0].source == "ET Markets"


def test_flags():
    f = classify(["Board meeting on Nov 10 to consider Q2 results", "SEBI issues show-cause notice", "Stock split record date"])
    assert "📅 results/board meeting" in f and "⚠️ regulatory/legal" in f and "🔔 corporate action" in f


def test_fetch_falls_back_and_reports_failure(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("blocked")
    monkeypatch.setattr(news, "_google", boom)
    monkeypatch.setattr(news, "_yahoo", boom)
    chk = fetch_news("XYZ", "Xyz Ltd")
    assert not chk.ok and "couldn't fetch" in format_news(chk)[0]
    assert "nothing in the last 7 days" in format_news(NewsCheck([], [], True))[0]
