"""Basic fundamentals for a stock, with simple warning flags.

Source: Yahoo Finance via yfinance (free). Coverage of Indian stocks is good
but not perfect - missing fields are shown as "n/a" and nothing is guessed.
Banks/NBFCs carry debt as their business, so the debt flag is skipped for
Financial Services.
"""
from __future__ import annotations

import logging
import math

log = logging.getLogger("swingbot.fundamentals")

FIELDS = ("marketCap", "trailingPE", "priceToBook", "returnOnEquity", "debtToEquity", "earningsGrowth",
          "revenueGrowth", "profitMargins", "trailingEps", "dividendYield")


def _num(x):
    try:
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


def fetch_fundamentals(symbol: str) -> dict | None:
    try:
        import yfinance as yf

        info = yf.Ticker(f"{symbol}.NS").info or {}
    except Exception as e:  # rate limit / network / unknown symbol
        log.warning("fundamentals for %s failed: %s", symbol, e)
        return None
    out = {k: _num(info.get(k)) for k in FIELDS}
    return out if any(v is not None for v in out.values()) else None


def assess(f: dict | None, sector: str = "") -> tuple[list[str], list[str]]:
    """Returns (warnings, positives)."""
    if not f:
        return [], []
    warn, good = [], []
    financial = "financial" in (sector or "").lower()
    eps, margin, pe = f.get("trailingEps"), f.get("profitMargins"), f.get("trailingPE")
    de = f.get("debtToEquity")
    de = de / 100 if de is not None else None  # Yahoo reports debt/equity in %
    roe, eg = f.get("returnOnEquity"), f.get("earningsGrowth")
    if (eps is not None and eps <= 0) or (margin is not None and margin < 0):
        warn.append("Loss-making")
    if de is not None and not financial and de > 1.5:
        warn.append(f"High debt (D/E {de:.1f})")
    if pe is not None and pe > 80:
        warn.append(f"Very expensive (P/E {pe:.0f})")
    if eg is not None and eg < -0.2:
        warn.append(f"Profit falling ({eg * 100:+.0f}% YoY)")
    if roe is not None and roe >= 0.15:
        good.append(f"High ROE ({roe * 100:.0f}%)")
    if eg is not None and eg >= 0.2:
        good.append(f"Profit growing ({eg * 100:+.0f}% YoY)")
    if de is not None and not financial and de < 0.3:
        good.append("Low debt")
    return warn, good


def format_fundamentals(f: dict | None, sector: str = "") -> list[str]:
    if not f:
        return ["🏢 Fundamentals: not available today (data source busy) - check screener.in / the annual report."]

    def pct(x):
        if x is None:
            return "n/a"
        v = round(x * 100)
        return "0%" if v == 0 else f"{v:+d}%"

    mcap = f.get("marketCap")
    de = f.get("debtToEquity")
    parts = [
        f"MCap ₹{mcap / 1e7:,.0f} Cr" if mcap else "MCap n/a",
        f"P/E {f['trailingPE']:.1f}" if f.get("trailingPE") else "P/E n/a",
        f"ROE {f['returnOnEquity'] * 100:.0f}%" if f.get("returnOnEquity") is not None else "ROE n/a",
        f"D/E {de / 100:.2f}" if de is not None else "D/E n/a",
        f"Profit growth {pct(f.get('earningsGrowth'))}",
    ]
    lines = ["🏢 Fundamentals: " + " | ".join(parts)]
    warn, good = assess(f, sector)
    if warn:
        lines.append("🚩 Fundamental check: " + ", ".join(warn) + ".")
    elif good:
        lines.append("✅ Fundamentals: " + ", ".join(good) + ".")
    return lines
