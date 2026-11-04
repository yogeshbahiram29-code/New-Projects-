"""Option B forward paper test: must apply exactly the backtested rules."""
import pandas as pd
import pytest

from swingbot import paper_rotation as pr
from swingbot.config import load_settings
from swingbot.costs import CostModel
from swingbot.paper_rotation import RotationPaperTracker
from swingbot.portfolio import backtest_momentum_rotation
from swingbot.synthetic import make_market


class FakeData:
    def __init__(self, frames):
        self.frames = frames

    def load(self, symbols, history_days, end):
        return {s: self.frames[s].loc[: pd.Timestamp(end)] for s in symbols if s in self.frames}


@pytest.fixture
def market(monkeypatch):
    stocks, idx, sectors = make_market(n_stocks=40, years=4, seed=12)
    monkeypatch.setattr(pr, "load_full_universe", lambda s: sectors)
    frames = dict(stocks)
    frames["NIFTY50"] = idx
    settings = load_settings(overrides={"initial_capital": 1_000_000, "universe": {"min_avg_traded_value_cr": 0},
                                        "data": {"history_days": 2000}})
    return stocks, idx, frames, settings


def _start_on_month_end(idx, after=320):
    for d in idx.index[after:]:
        if pr._is_calendar_month_end(d.date()):
            return d
    raise AssertionError


def test_paper_tracker_matches_backtest_exactly(market, tmp_path):
    stocks, idx, frames, settings = market
    start = _start_on_month_end(idx)
    days = idx.index[(idx.index >= start)][:260]
    tracker = RotationPaperTracker(settings, FakeData(frames), None, tmp_path / "paper.json")
    for d in days:
        tracker.run(today=d.date(), send=False)
    st = tracker.load()
    paper = pd.Series({pd.Timestamp(d): e for d, e, _ in st["history"]})

    costs = CostModel.from_settings(settings.section("costs"), "zerodha")
    bt = backtest_momentum_rotation(stocks, idx, start, days[-1], 1_000_000, costs, min_value_cr=0)
    common = paper.index.intersection(bt.equity.index)
    assert len(common) > 200
    diff = (paper.loc[common] - bt.equity.loc[common]).abs().max()
    assert diff < 1.0, f"paper and backtest equity differ by up to ₹{diff:.2f}"
    assert len(st["holdings"]) <= 10 and st["cash"] >= 0


def test_repeat_run_changes_nothing_and_message_is_paper_only(market, tmp_path):
    stocks, idx, frames, settings = market
    d = idx.index[400]
    tracker = RotationPaperTracker(settings, FakeData(frames), None, tmp_path / "paper.json")
    msg = tracker.run(today=d.date(), send=False)
    assert msg.startswith("📁 OPTION B PAPER PORTFOLIO") and "no real orders" in msg
    before = (tmp_path / "paper.json").read_text()
    again = tracker.run(today=d.date(), send=False)
    assert again.startswith("(repeat)")
    assert (tmp_path / "paper.json").read_text() == before


def test_missed_month_end_is_caught_up(market, tmp_path):
    stocks, idx, frames, settings = market
    start = _start_on_month_end(idx)
    days = list(idx.index[idx.index >= start])
    # find the next month-end after the start and pretend it was an exchange holiday (no bar)
    later = [d for d in days[30:] if pr._is_calendar_month_end(d.date())][0]
    holiday_frames = {k: v.drop(index=later, errors="ignore") for k, v in frames.items()}
    tracker = RotationPaperTracker(settings, FakeData(holiday_frames), None, tmp_path / "paper.json")
    for d in [x for x in days if x <= later + pd.Timedelta(days=5) and x != later]:
        tracker.run(today=d.date(), send=False)
    st = tracker.load()
    prev_bar = max(x for x in days if x < later)
    assert st["last_decision_month"] >= prev_bar.strftime("%Y-%m")  # the missed month got its decision
    assert st.get("pending") is None  # ...and it was executed at the next available open
