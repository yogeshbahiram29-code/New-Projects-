"""End-to-end: run the live engine in paper mode over simulated days with a fake clock.

Checks the safety invariants that matter most with real money:
* every position the bot holds has a broker-side stop
* the bot never sells or buys shares you hold yourself
* the bot never spends more than its capital
* the kill switch blocks new entries
"""
from datetime import datetime, time

import pandas as pd
import pytest

from swingbot import engine as engine_mod
from swingbot.brokers.paper import PaperBroker
from swingbot.config import load_settings
from swingbot.costs import CostModel
from swingbot.engine import LiveEngine
from swingbot.notify import Notifier
from swingbot.store import Store
from swingbot.synthetic import make_market


class FakeData:
    def __init__(self, frames):
        self.frames = frames

    def load(self, symbols, history_days, end):
        return {s: self.frames[s].loc[: pd.Timestamp(end)] for s in symbols if s in self.frames}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    stocks, idx, sectors = make_market(n_stocks=20, years=3, seed=5)
    frames = dict(stocks)
    frames["NIFTY50"] = idx
    uni = tmp_path / "universe.csv"
    uni.write_text("symbol,sector\n" + "\n".join(f"{s},{sectors[s]}" for s in stocks))
    settings = load_settings(overrides={
        "initial_capital": 1_000_000,
        "universe": {"file": str(uni), "min_avg_traded_value_cr": 0},
        "paths": {"state_dir": str(tmp_path / "state")},
        "notifications": {"whatsapp_provider": "none"},
    })
    monkeypatch.setattr(engine_mod, "events_calendar", lambda s: ({}, True))
    monkeypatch.setattr(engine_mod._time, "sleep", lambda s: None)
    costs = CostModel.from_settings(settings.section("costs"), "paper")
    broker = PaperBroker(tmp_path / "state" / "paper.json", 1_000_000, costs)
    broker.connect()
    # the user's own long-term holding in the same account - the bot must never touch it
    broker.s["holdings"]["STOCK03"] = {"qty": 7, "avg": 100.0}
    broker._save()
    clock = {"now": None}
    eng = LiveEngine(settings, broker, Notifier("none", tmp_path / "state"), Store(tmp_path / "state" / "t.db"),
                     FakeData(frames), now_fn=lambda: clock["now"])
    return eng, broker, idx, clock


def _run_days(eng, idx, clock, days, check):
    tz = eng.tz
    for d in days:
        for t in (time(8, 50), time(9, 20), time(9, 35), time(11, 0), time(15, 25), time(16, 35)):
            clock["now"] = datetime.combine(d.date(), t, tz)
            eng.tick()
            check()


def test_engine_paper_end_to_end(setup):
    eng, broker, idx, clock = setup

    def check():
        holdings = broker.holdings()
        for pos in eng.db.positions():
            if (pos.pending_exit_reason or "").startswith("SENT"):
                continue
            assert broker.protection_active(pos.protection_id), f"{pos.symbol} has no active stop!"
            assert holdings[pos.symbol].quantity >= pos.quantity
        assert holdings["STOCK03"].quantity == 7  # user's own shares untouched
        assert broker.available_cash() > 0
        assert eng.bot_cash() > -1

    days = [d for d in idx.index[-160:]]
    _run_days(eng, idx, clock, days, check)
    trades = eng.db.trades()
    ran_eod = eng.db.get("ran:eod")
    assert ran_eod == days[-1].date().isoformat()
    assert all(t["symbol"] != "STOCK03" for t in trades)
    assert len(trades) + len(eng.db.positions()) > 0, "engine never traded in 160 simulated days"
    # bot equity bookkeeping matches the paper broker (both include the same fills/charges)
    lp = eng.last_prices()
    broker_eq = broker.available_cash() + sum(h.quantity * lp.get(s, h.avg_price) for s, h in broker.holdings().items() if s != "STOCK03")
    user_cash_offset = 0  # paper broker cash started equal to the bot capital
    assert abs(broker_eq - eng.bot_equity() - user_cash_offset) / broker_eq < 0.01


def test_kill_switch_blocks_entries(setup):
    eng, broker, idx, clock = setup
    eng.kill_file.parent.mkdir(parents=True, exist_ok=True)
    eng.kill_file.write_text("test")
    _run_days(eng, idx, clock, list(idx.index[-40:]), lambda: None)
    assert not eng.db.positions() and not eng.db.trades()
    assert all(o["kind"] != "entry" for o in broker.s["orders"].values())
