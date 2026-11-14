"""Telegram presentation layer: minimal, terminal-style messages.

Formatting only - no trading logic lives here. Messages are Telegram HTML
(bold headings, <pre> blocks for aligned columns). Aligned lines are kept to
WIDTH characters so they don't wrap on an Android phone in portrait.

WhatsApp and the console get the same text converted (bold -> *bold*,
<pre> -> ``` for WhatsApp; tags stripped for logs).
"""
from __future__ import annotations

import html
import math
import re

WIDTH = 32          # max characters per aligned (monospace) line
LIMIT = 3500        # safety margin under Telegram's 4096-character message limit
SEP = "─" * 18      # thin separator, same visual width as the old heavy line
TAGLINE = "<i>Discipline first. Capital protection always.</i>"


class Html(str):
    """Text that is already Telegram HTML (sent with parse_mode=HTML)."""


def esc(x) -> str:
    return html.escape(str(x), quote=False)


def bold(x) -> str:
    return f"<b>{esc(x)}</b>"


def head(title: str, note: str | None = None) -> str:
    """Uppercase section heading with an optional short note after it."""
    return f"<b>{esc(title.upper())}</b>" + (f"  {esc(note)}" if note else "")


def title(name: str, sub: str | None = None) -> str:
    return "\n".join([f"<b>{esc(name.upper())}</b>"] + ([esc(sub)] if sub else []) + [SEP])


def pre(lines: list[str]) -> str:
    """Monospace block (aligned columns)."""
    return "<pre>" + esc("\n".join(lines)) + "</pre>"


def kv(rows: list[tuple[str, str]], w: int = 12) -> str:
    """Aligned label/value rows in a monospace block."""
    return pre([f"{k:<{w}}{v}" for k, v in rows if v is not None])


def lines(*items: str) -> str:
    return "\n".join(esc(i) for i in items if i)


def isnan(x) -> bool:
    try:
        return x is None or math.isnan(float(x))
    except (TypeError, ValueError):
        return True


def inr(x, nd: int = 2) -> str:
    return "n/a" if isnan(x) else f"₹{float(x):,.{nd}f}"


def num(x, nd: int = 2) -> str:
    return "n/a" if isnan(x) else f"{float(x):,.{nd}f}"


def pct(x, nd: int = 1, sign: bool = True) -> str:
    if isnan(x):
        return "n/a"
    return f"{float(x):+.{nd}f}%" if sign else f"{float(x):.{nd}f}%"


def short(text: str, n: int = 60) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1].rstrip(" ,.;:-") + "…"


SECTOR_SHORT = {
    "Oil Gas & Consumable Fuels": "Oil & Gas",
    "Automobile and Auto Components": "Auto Components",
    "Fast Moving Consumer Goods": "FMCG",
    "Media Entertainment & Publication": "Media",
    "Telecommunication": "Telecom",
    "Information Technology": "IT",
    "Construction Materials": "Construction Mat.",
}


def sector_name(name: str, n: int = 22) -> str:
    return short(SECTOR_SHORT.get(str(name), str(name)), n)


def date_line(when) -> str:
    import pandas as pd

    return f"{pd.Timestamp(when):%a, %d %b %Y}"


# ---------------------------------------------------------------- splitting
def pack(blocks: list[str], limit: int = LIMIT) -> list[Html]:
    """Join self-contained blocks into as few messages as fit (splits only between blocks)."""
    parts, cur = [], ""
    for blk in [b for b in blocks if b]:
        if cur and len(cur) + 2 + len(blk) > limit:
            parts.append(Html(cur))
            cur = ""
        cur = f"{cur}\n\n{blk}" if cur else blk
    if cur:
        parts.append(Html(cur))
    return parts


def split_html(text: str, limit: int = LIMIT) -> list[str]:
    """Split an over-long message at blank lines that are not inside a <pre> block."""
    if len(text) <= limit:
        return [text]
    chunks, cur, depth = [], [], 0
    for para in text.split("\n\n"):
        depth_before = depth
        depth += para.count("<pre>") - para.count("</pre>")
        if cur and depth_before == 0 and len("\n\n".join(cur + [para])) > limit:
            chunks.append("\n\n".join(cur))
            cur = []
        cur.append(para)
    if cur:
        chunks.append("\n\n".join(cur))
    out = []
    for c in chunks:  # a single block that is still too long: split by lines (closing/reopening <pre>)
        while len(c) > limit:
            cut = c.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            head_, c = c[:cut], c[cut:].lstrip("\n")
            if head_.count("<pre>") > head_.count("</pre>"):
                head_, c = head_ + "</pre>", "<pre>" + c
            out.append(head_)
        out.append(c)
    return out


# ---------------------------------------------------------------- other channels
def plain(text) -> str:
    """Console / log / plain-text version (tags removed)."""
    if isinstance(text, (list, tuple)):
        return "\n\n".join(plain(t) for t in text)
    if not isinstance(text, Html):
        return str(text)
    t = re.sub(r"</?(b|i|u|s|code|pre)>", "", text)
    return html.unescape(t)


def whatsapp(text: str) -> str:
    """WhatsApp markup version of a Telegram HTML message."""
    if not isinstance(text, Html):
        return str(text)
    t = text.replace("<pre>", "```\n").replace("</pre>", "\n```")
    t = re.sub(r"</?b>", "*", t)
    t = re.sub(r"</?i>", "_", t)
    t = re.sub(r"</?(u|s|code)>", "", t)
    return html.unescape(t)


__all__ = ["Html", "esc", "bold", "head", "title", "pre", "kv", "lines", "inr", "num", "pct", "short", "pack",
           "split_html", "plain", "whatsapp", "sector_name", "SECTOR_SHORT", "SEP", "TAGLINE", "WIDTH", "LIMIT", "date_line"]
