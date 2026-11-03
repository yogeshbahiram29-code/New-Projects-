import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from swingbot.config import load_settings  # noqa: E402
from swingbot.costs import CostModel  # noqa: E402
from swingbot.models import SetupType, TradePlan  # noqa: E402


@pytest.fixture
def settings():
    return load_settings(ROOT / "config" / "settings.yaml")


@pytest.fixture
def costs(settings):
    return CostModel.from_settings(settings.section("costs"), "zerodha")


@pytest.fixture
def make_plan():
    def _make(symbol="ABC", trigger=500.0, stop=476.0, target=560.0, score=85.0, sector="IT", limit=None):
        return TradePlan(
            symbol=symbol, setup=SetupType.PULLBACK, score=score, trigger_price=trigger,
            limit_price=limit or round(trigger * 1.005, 2), stop_price=stop, target_price=target,
            atr=12.0, signal_date=date(2026, 1, 5), sector=sector,
        )
    return _make
