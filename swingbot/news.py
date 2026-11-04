"""Latest news / announcements for a stock, with simple risk flags.

Sources (free, no API key):
  1. Google News RSS search for the company name (Indian edition)
  2. Yahoo Finance news via yfinance (fallback)

Headlines are external text: they are only shown to you and scanned for
keywords - never acted on automatically. Keyword flags are a reminder to READ
the news before deciding, not a verdict.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger("swingbot.news")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) swingbot/1.0"}

FLAGS = {
    "📅 results/board meeting": r"\b(q[1-4]\b|quarter(ly)? results?|results? (date|on|today|preview)|financial results|"
                                r"board meeting|earnings|profit|net loss|revenue)",
    "⚠️ regulatory/legal": r"\b(sebi|probe|raid|fraud|penalt(y|ies)|show[- ]cause|insolvency|nclt|default|court|"
                          r"investigation|ban|scam|whistle)",
    "⚠️ rating/management": r"\b(downgrade|cut target|resign(s|ed|ation)?|steps down|auditor|pledge|block deal|"
                            r"stake sale|promoter (sells|sold|selling)|ofs)\b",
    "🔔 corporate action": r"\b(bonus|split|dividend|record date|buyback|demerger|merger|rights issue|qip|fund ?rais)",
    "✅ positive": r"\b(upgrade|order (win|inflow|worth)|bags|wins|record high|raises target|approval)\b",
}


@dataclass
class NewsItem:
    title: str
    source: str
    published: datetime | None

    def age(self) -> str:
        if not self.published:
            return ""
        h = (datetime.now(timezone.utc) - self.published).total_seconds() / 3600
        return f"{h:.0f}h ago" if h < 24 else f"{h / 24:.0f}d ago"


@dataclass
class NewsCheck:
    items: list[NewsItem] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    ok: bool = True  # False = couldn't fetch any source


def classify(titles: list[str]) -> list[str]:
    text = " ".join(titles).lower()
    return [name for name, pat in FLAGS.items() if re.search(pat, text)]


def parse_google_rss(xml_text: str, days: int, limit: int) -> list[NewsItem]:
    root = ET.fromstring(xml_text)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "")
            pub = pub if pub.tzinfo else pub.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pub = None
        if title and (pub is None or pub >= cutoff):
            out.append(NewsItem(title[:110], src or "Google News", pub))
    out.sort(key=lambda n: n.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:limit]


def _google(query: str, days: int, limit: int) -> list[NewsItem]:
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{query} when:{days}d")
           + "&hl=en-IN&gl=IN&ceid=IN:en")
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return parse_google_rss(r.text, days, limit)


def _yahoo(symbol: str, days: int, limit: int) -> list[NewsItem]:
    import yfinance as yf

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for n in (yf.Ticker(f"{symbol}.NS").news or [])[:20]:
        c = n.get("content", n)  # newer yfinance nests fields under "content"
        title = c.get("title") or ""
        src = (c.get("provider") or {}).get("displayName") or c.get("publisher") or "Yahoo"
        pub = None
        if c.get("pubDate"):
            pub = datetime.fromisoformat(str(c["pubDate"]).replace("Z", "+00:00"))
        elif n.get("providerPublishTime"):
            pub = datetime.fromtimestamp(int(n["providerPublishTime"]), tz=timezone.utc)
        if title and (pub is None or pub >= cutoff):
            out.append(NewsItem(title[:110], src, pub))
    return out[:limit]


_SUFFIX = re.compile(r"\b(ltd\.?|limited|\(india\)|india ltd\.?|co\.?|corporation|corp\.?)\s*$", re.I)


def short_name(name: str | None) -> str | None:
    """'Petronet LNG Ltd.' -> 'Petronet LNG' (headlines rarely use the legal suffix)."""
    if not name:
        return None
    n = name.strip()
    for _ in range(2):
        n = _SUFFIX.sub("", n).strip(" .,")
    return n or None


def is_relevant(title: str, symbol: str, name: str | None) -> bool:
    """Keep only headlines that mention the company (symbol or first word of its name)."""
    def norm(x: str) -> str:  # "Divi's" == "Divis"
        return x.lower().replace("'", "").replace("’", "")

    t = norm(title)
    keys = {symbol.lower()}
    sn = short_name(name)
    if sn:
        words = [w for w in norm(sn).split() if w]
        if words and len(words[0]) >= 5:
            keys.add(words[0])
        elif len(words) >= 2:  # short first word (e.g. "PTC Industries" vs "PTC India") -> need two words
            keys.add(f"{words[0]} {words[1]}")
        elif words and len(words[0]) >= 3:
            keys.add(words[0])
    return any(re.search(r"\b" + re.escape(k), t) for k in keys)


def fetch_news(symbol: str, name: str | None = None, days: int = 7, limit: int = 3) -> NewsCheck:
    sn = short_name(name)
    query = f"{sn} share" if sn else f"{symbol} share NSE"
    reachable = False
    for fn, arg in ((_google, query), (_yahoo, symbol)):
        try:
            items = [i for i in fn(arg, days, limit * 4) if is_relevant(i.title, symbol, name)][:limit]
            reachable = True
            if items:
                return NewsCheck(items, classify([i.title for i in items]), True)
        except Exception as e:  # network / parsing / blocked
            log.warning("news source %s failed for %s: %s", fn.__name__, symbol, e)
    return NewsCheck([], [], ok=reachable)


def format_news(check: NewsCheck, indent: str = "   ") -> list[str]:
    if not check.ok:
        return [f"{indent}📰 News: couldn't fetch - check NSE/BSE announcements yourself before deciding"]
    if not check.items:
        return [f"{indent}📰 News: nothing in the last 7 days"]
    lines = [f"{indent}📰 Latest news:"]
    for it in check.items:
        lines.append(f"{indent} • {it.title} ({it.source}{', ' + it.age() if it.age() else ''})")
    if check.flags:
        lines.append(f"{indent}🚩 Check: " + ", ".join(check.flags))
    return lines
