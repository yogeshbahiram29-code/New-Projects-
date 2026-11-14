"""Volume quality: is money flowing INTO or OUT OF a stock?

From daily OHLCV (always available):
  * today's volume vs its 50-day average
  * Chaikin Money Flow (20 days): closes near the day's high on big volume = buying
  * strong up-days vs strong down-days in the last 20 sessions
    (a "strong" day = volume above the 50-day average)
  * On-Balance Volume trend (20 days)
  -> ACCUMULATION / NEUTRAL / DISTRIBUTION

From NSE's daily bhavcopy (best effort - NSE often blocks cloud servers):
  * delivery % = share of traded quantity actually taken into demat accounts.
    High delivery on up-days suggests genuine investors rather than intraday churn.
"""
from __future__ import annotations

import io
import logging
from datetime import date

import numpy as np
import pandas as pd
import requests

log = logging.getLogger("swingbot.volume")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Accept": "*/*"}
BHAV_URLS = [
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv",
    "https://archives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv",
]


def volume_profile(df: pd.DataFrame) -> dict | None:
    if df is None or len(df) < 60:
        return None
    h, lo, c, v = (df[k].astype(float) for k in ("high", "low", "close", "volume"))
    rng = (h - lo).replace(0, np.nan)
    mfm = ((c - lo) - (h - c)) / rng
    cmf = float((mfm * v).iloc[-20:].sum() / v.iloc[-20:].sum()) if v.iloc[-20:].sum() > 0 else 0.0
    avg50 = v.rolling(50).mean()
    up = c.diff() > 0
    strong = v > avg50
    acc = int((up & strong).iloc[-20:].sum())
    dist = int((~up & strong & (c.diff() < 0)).iloc[-20:].sum())
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    obv_trend = float(obv.iloc[-1] - obv.iloc[-21])
    upv = v.where(up, 0).iloc[-50:].sum()
    dnv = v.where(c.diff() < 0, 0).iloc[-50:].sum()
    ud = float(upv / dnv) if dnv > 0 else np.nan
    pts = (cmf > 0.05) - (cmf < -0.05) + (acc - dist >= 2) - (dist - acc >= 2) + (obv_trend > 0) - (obv_trend < 0)
    pts += (ud > 1.2) - (ud < 0.8) if not np.isnan(ud) else 0
    verdict = "ACCUMULATION" if pts >= 2 else "DISTRIBUTION" if pts <= -2 else "NEUTRAL"
    return {"vol_ratio": float(v.iloc[-1] / avg50.iloc[-1]) if avg50.iloc[-1] > 0 else np.nan,
            "cmf": cmf, "acc_days": acc, "dist_days": dist, "obv_up": obv_trend > 0, "updown": ud,
            "verdict": verdict}


def _parse_bhav(text: str) -> dict[str, tuple[float, float]]:
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    df["SERIES"] = df["SERIES"].astype(str).str.strip()
    df = df[df["SERIES"] == "EQ"]
    out = {}
    for r in df.itertuples(index=False):
        try:
            out[str(r.SYMBOL).strip()] = (float(str(r.DELIV_PER).strip()), float(str(r.TTL_TRD_QNTY).strip()))
        except ValueError:
            continue
    return out


def fetch_delivery(trading_days: list[date], symbols: list[str], max_days: int = 5) -> dict[str, list[float]] | None:
    """Delivery % for ``symbols`` over the latest ``max_days`` trading days (newest first). None = blocked."""
    sess = requests.Session()
    sess.headers.update(UA)
    got: dict[str, list[float]] = {s: [] for s in symbols}
    files = fails = 0
    for d in sorted(trading_days, reverse=True)[: max_days + 3]:
        text = None
        for url in BHAV_URLS:
            try:
                r = sess.get(url.format(d=d.strftime("%d%m%Y")), timeout=15)
                if r.status_code == 200 and "DELIV_PER" in r.text[:500]:
                    text = r.text
                    break
            except requests.RequestException as e:
                log.warning("bhavcopy %s: %s", d, e)
        if text is None:
            fails += 1
            if files == 0 and fails >= 2:  # newest file may be unpublished; two misses = blocked
                return None
            continue
        data = _parse_bhav(text)
        for s in symbols:
            if s in data:
                got[s].append(data[s][0])
        files += 1
        if files >= max_days:
            break
    return got if files else None


def delivery_level(avg: float) -> str | None:
    """'high' (>= 55%: genuine investor buying) / 'low' (< 30%: mostly short-term trading) / None."""
    return "high" if avg >= 55 else "low" if avg < 30 else None


def format_volume(p: dict | None, deliveries: list[float] | None, delivery_ok: bool) -> list[str]:
    if not p:
        return []
    icon = {"ACCUMULATION": "🟢", "DISTRIBUTION": "🔴"}.get(p["verdict"], "⚪")
    lines = [f"🔥 Volume: {p['vol_ratio']:.1f}x avg today | {icon} {p['verdict']} "
             f"(money flow {p['cmf']:+.2f} · {p['acc_days']} strong up-days vs {p['dist_days']} down)"]
    if deliveries:
        avg = sum(deliveries) / len(deliveries)
        lines.append(f"📦 Delivery: {deliveries[0]:.0f}% latest | {len(deliveries)}-day avg {avg:.0f}%"
                     + {"high": " – strong investor buying", "low": " – mostly short-term trading",
                        None: ""}[delivery_level(avg)])
    elif not delivery_ok:
        lines.append("📦 Delivery %: not available today (NSE blocks cloud servers) – check nseindia.com.")
    return lines
