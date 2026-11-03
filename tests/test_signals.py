"""Free signal mode: daily instructions, took/sold journal, automatic stop/target bookkeeping."""
import pandas as pd
import pytest

from swingbot import signals as signals_mod
from swingbot.config import load_settings
from swingbot.signals import SignalDesk, SignalError
from swingbot.synthetic import make_market


class FakeData:
    def __init__(self, frames):
        self.frames = frames

    def load(self, symbols, history_days, end):
        return {s: self.frames[s].loc[: pd.Timestamp(end)] for s in symbols if s in self.frames}


@pytest.fixture
def desk(tmp_path, monkeypatch):
    stocks, idx, sectors = make_market(n_stocks=20, years=3, seed=5)
    frames = dict(stocks)
    frames["NIFTY50"] = idx
    uni = tmp_path / "universe.csv"
    uni.write_text("symbol,sector\n" + "\n".join(f"{s},{sectors[s]}" for s in stocks))
    settings = load_settings(overrides={
        "initial_capital": 1_000_000,
        "universe": {"file": str(uni), "min_avg_traded_value_cr": 0},
    })
    monkeypatch.setattr(signals_mod, "events_calendar", lambda s: ({}, True))
    d = SignalDesk(settings, FakeData(frames), notifier=None, journal_dir=tmp_path / "journal")
    return d, frames, idx


def test_daily_run_is_idempotent_and_writes_plans(desk):
    d, frames, idx = desk
    day = idx.index[-100].date()
    msg = d.run(today=day, send=False)
    assert "SWING SIGNALS" in msg and "NEW TRADES" in msg
    assert d.plans_path.exists() and d.account_path.exists()
    again = d.run(today=day, send=False)
    assert again.startswith("(repeat) SWING SIGNALS")  # re-run re-sends the same report, changes nothing


def test_took_uses_plan_levels_and_rejects_bad_input(desk):
    d, frames, idx = desk
    plans = {}
    for ts in idx.index[-160:-60]:  # find a day that produced a plan
        d.run(today=ts.date(), send=False)
        plans = d.plans()
        if plans:
            break
    assert plans, "no plan produced in 100 simulated days"
    sym, plan = next(iter(plans.items()))
    pos = d.took(sym, plan.quantity, plan.trigger_price)
    assert pos.stop_price == plan.stop_price and pos.target_price == plan.target_price
    with pytest.raises(SignalError):
        d.took(sym, 1, plan.trigger_price)  # duplicate
    with pytest.raises(SignalError):
        d.took("NOPLAN", 1, 100.0)  # no plan and no stop/target given


def test_position_lifecycle_and_cash_accounting(desk):
    d, frames, idx = desk
    start = idx.index[-120]
    sym = "STOCK02"
    px = float(frames[sym]["close"].loc[start])
    d.run(today=start.date(), send=False)
    d.took(sym, 100, px, stop=round(px * 0.95, 2), target=round(px * 1.15, 2), when=start.date())
    closed = False
    for ts in idx.index[idx.index > start][:60]:
        msg = d.run(today=ts.date(), send=False)
        positions, _ = d.positions()
        if not any(p.symbol == sym for p in positions):
            closed = True
            assert sym in msg
            break
        p = next(p for p in positions if p.symbol == sym)
        assert p.stop_price >= round(px * 0.95, 2) - 1e-6  # stops only move up
    assert closed, "position should hit stop, target or a time/trend exit within 60 days"
    trades = d.closed_trades()
    t = trades[trades.symbol == sym].iloc[-1]
    assert t.costs > 0
    positions, _ = d.positions()
    # with everything closed, cash = capital + realised P&L exactly
    if not positions:
        assert abs(d.cash([]) - (1_000_000 + trades.pnl.sum())) < 1e-6


def test_manual_sold(desk):
    d, frames, idx = desk
    d.took("STOCK05", 10, 100.0, stop=95.0, target=115.0)
    t = d.sold("STOCK05", 104.0)
    assert t is not None and t.pnl < 40 and t.pnl > 0  # 40 gross minus charges
    assert not d.positions()[0]
    with pytest.raises(SignalError):
        d.sold("STOCK05", 104.0)
