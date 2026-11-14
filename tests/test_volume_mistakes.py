"""Upgrades 4 (volume quality / delivery) and 6 (mistake tracker)."""
from datetime import date, datetime

import numpy as np
import pandas as pd

from swingbot import volume_quality as vq
from swingbot.config import load_settings
from swingbot.mistakes import (append_log, check_buy, check_sell, feedback, find_plan, load_log,
                               save_watchlist_state, weekly_review)


# ---------------------------------------------------------------- 4. volume quality
def _ohlcv(n=80, up=True, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0.4 if up else -0.4, 1.0, n))
    vol = np.full(n, 1e5)
    diff = np.diff(close, prepend=close[0])
    vol[(diff > 0) if up else (diff < 0)] *= 2.5  # heavy volume on the trend days
    high = close + 0.2 if up else close + 1.5      # close near the high = buying, near the low = selling
    low = close - 1.5 if up else close - 0.2
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": vol},
                        index=pd.bdate_range("2026-01-01", periods=n))


def test_volume_profile_verdicts():
    acc = vq.volume_profile(_ohlcv(up=True))
    dist = vq.volume_profile(_ohlcv(up=False))
    assert acc["verdict"] == "ACCUMULATION" and acc["cmf"] > 0 and acc["acc_days"] > acc["dist_days"]
    assert dist["verdict"] == "DISTRIBUTION" and dist["cmf"] < 0
    assert vq.volume_profile(_ohlcv(n=40)) is None  # not enough history
    lines = vq.format_volume(acc, [62.0, 50.0, 55.0], True)
    assert lines[0].startswith("🔥 Volume:") and "🟢 ACCUMULATION" in lines[0]
    assert lines[1].startswith("📦 Delivery: 62% latest | 3-day avg 56%") and "strong investor buying" in lines[1]
    assert "not available today" in vq.format_volume(acc, None, False)[1]
    assert len(vq.format_volume(acc, None, True)) == 1 and vq.format_volume(None, None, True) == []


BHAV = (" SYMBOL, SERIES, DATE1, TTL_TRD_QNTY, DELIV_QTY, DELIV_PER\n"
        "ABC, EQ, 03-Nov-2026, 1000, 600, 60.00\n"
        "ABC, BE, 03-Nov-2026, 50, 50, 100.00\n"
        "XYZ, EQ, 03-Nov-2026, 2000, -, -\n")


class _Resp:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


def test_bhavcopy_parse_and_blocked(monkeypatch):
    assert vq._parse_bhav(BHAV) == {"ABC": (60.0, 1000.0)}  # BE series and '-' rows skipped
    days = [date(2026, 11, d) for d in (2, 3, 4)]
    monkeypatch.setattr(vq.requests.Session, "get", lambda self, url, timeout=15: _Resp(200, BHAV))
    got = vq.fetch_delivery(days, ["ABC", "XYZ"], max_days=2)
    assert got == {"ABC": [60.0, 60.0], "XYZ": []}
    monkeypatch.setattr(vq.requests.Session, "get", lambda self, url, timeout=15: _Resp(403, "Access Denied"))
    assert vq.fetch_delivery(days, ["ABC"]) is None  # NSE blocks the cloud server -> "not available"


# ---------------------------------------------------------------- 6. mistake tracker
def _settings(tmp_path):
    return load_settings(overrides={"signals": {"dir": str(tmp_path)}, "initial_capital": 10000})


def _picks():
    return pd.DataFrame([{"symbol": "ABC", "setup": "breakout", "score": 80.0, "trigger": 100.0, "stop": 95.0,
                          "t1": 110.0, "t2": 115.0}])


def test_watchlist_state_and_plan(tmp_path):
    s = _settings(tmp_path)
    for d in pd.bdate_range("2026-10-01", periods=12):
        save_watchlist_state(s, _picks() if d.day == 1 else _picks().iloc[0:0], True, d)
    plan, latest = find_plan(tmp_path, "abc")
    assert plan is None and latest["date"] == "2026-10-16"  # only 10 lists kept, ABC pick is too old
    save_watchlist_state(s, _picks(), False, pd.Timestamp("2026-10-19"))
    plan, latest = find_plan(tmp_path, "ABC")
    assert plan["stop"] == 95.0 and plan["t1"] == 110.0 and latest["regime_on"] is False


def test_check_buy_flags(tmp_path):
    s = _settings(tmp_path)
    save_watchlist_state(s, _picks(), True, pd.Timestamp("2026-11-03"))
    good = check_buy(s, tmp_path, "ABC", 20, 100.2, 95.0, "calm", "yes")
    assert good == []  # risk 20 x 5.2 = ₹104 ≈ 1% of ₹10,000, within the plan
    bad = check_buy(s, tmp_path, "ABC", 100, 104.0, 80.0, "fomo", "no")
    keys = {m.split(":")[0] for m in bad}
    assert {"Chased", "Wide stop", "Oversized", "Big position", "Emotion", "Not plan"} <= keys
    assert check_buy(s, tmp_path, "QQQ", 1, 50.0, None)[0].startswith("Off-plan")
    assert any(m.startswith("No stop") for m in check_buy(s, tmp_path, "QQQ", 1, 50.0, None))
    save_watchlist_state(s, _picks(), False, pd.Timestamp("2026-11-04"))
    assert any(m.startswith("Market filter OFF") for m in check_buy(s, tmp_path, "ABC", 20, 100.2, 95.0))


def test_revenge_and_overtrading(tmp_path):
    s = _settings(tmp_path)
    save_watchlist_state(s, _picks(), True, pd.Timestamp("2026-11-03"))
    now = datetime(2026, 11, 4, 10, 0)
    append_log(tmp_path, {"time": "2026-11-03T15:00:00", "action": "SELL", "symbol": "X", "qty": 5, "price": 90,
                          "pnl": -120.0, "mistakes": ""})
    for i in range(3):
        append_log(tmp_path, {"time": f"2026-11-0{i + 1}T10:00:00", "action": "BUY", "symbol": f"S{i}", "qty": 1,
                              "price": 10, "pnl": "", "mistakes": ""})
    keys = {m.split(":")[0] for m in check_buy(s, tmp_path, "ABC", 20, 100.2, 95.0, now=now)}
    assert {"Revenge", "Overtrading"} <= keys
    assert len(load_log(tmp_path)) == 4


def test_check_sell_and_feedback(tmp_path):
    s = _settings(tmp_path)
    m = check_sell(s, "ABC", 90.0, 95.0, 110.0, "2026-10-01", today=date(2026, 11, 4))
    assert any(x.startswith("Stop not honoured") for x in m) and any(x.startswith("Held too long") for x in m)
    assert any(x.startswith("Early exit") for x in check_sell(s, "ABC", 101.0, 95.0, 110.0, None, "fear"))
    assert check_sell(s, "ABC", 110.5, 95.0, 110.0, "2026-10-28", "calm", "yes", today=date(2026, 11, 4)) == []
    txt = feedback("BUY", "ABC", 20, 100.2, 95.0, 110.0, ["Chased: paid 3.0% above"])
    from swingbot.tgfmt import plain

    txt = plain(txt)
    assert "TRADE RECORDED" in txt and "MISTAKE CHECK  1 issue(s)" in txt and "Tip: Place the stop-limit" in txt
    ok = plain(feedback("SELL", "ABC", 20, 110.5, 95.0, 110.0, [], pnl=180.0))
    assert "No issues – rules followed" in ok and "₹180 after charges" in ok


def test_weekly_review(tmp_path):
    s = _settings(tmp_path)
    today = date(2026, 11, 7)
    import re

    from swingbot.tgfmt import plain

    assert "No trades recorded this week" in plain(weekly_review(s, tmp_path, today))
    append_log(tmp_path, {"time": "2026-11-03T10:00:00", "action": "BUY", "symbol": "ABC", "qty": 20, "price": 104,
                          "pnl": "", "mistakes": "Chased: x | Emotion: traded on FOMO"})
    append_log(tmp_path, {"time": "2026-11-05T10:00:00", "action": "SELL", "symbol": "ABC", "qty": 20, "price": 96,
                          "pnl": -175.0, "mistakes": ""})
    append_log(tmp_path, {"time": "2026-10-01T10:00:00", "action": "BUY", "symbol": "OLD", "qty": 1, "price": 1,
                          "pnl": "", "mistakes": "Revenge: x"})  # older than a week -> ignored
    txt = plain(weekly_review(s, tmp_path, today))
    assert re.search(r"Buys\s+1", txt) and re.search(r"Sells\s+1", txt) and re.search(r"Closed P&L\s+₹-175", txt)
    assert re.search(r"Discipline\s+50%", txt) and re.search(r"Chased\s+x1", txt)
    assert "Revenge" not in txt and "FOCUS NEXT WEEK" in txt
