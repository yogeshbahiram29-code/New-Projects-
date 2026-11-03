"""Risk rules are the most important code in the bot - test every one."""
from datetime import date

from swingbot.config import HARD_MAX_RISK_PER_TRADE_PCT, load_settings
from swingbot.models import Position, Regime
from swingbot.risk import AccountState, RiskEngine, check_circuit_breakers


def _pos(sym="XYZ", entry=100.0, stop=95.0, qty=10, sector="Bank"):
    return Position(sym, "pullback", date(2026, 1, 1), entry, qty, stop, stop, entry * 1.2, sector)


def test_plan_worked_example_standard_account(settings, costs, make_plan):
    """Section 9.2 of the plan: ₹10L, entry 500, stop 476 -> capped to 400 shares (20%)."""
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=1_000_000, cash=1_000_000)
    d = eng.evaluate(make_plan(target=572), acct, [], Regime.BULL, avg_daily_volume=5_000_000)
    assert d.approved, d.reasons
    # 1% risk would be 416 shares; the 20% position cap is applied at the worst-case
    # fill (limit ₹502.50), so 200,000 / 502.5 -> 398 shares
    assert d.quantity == 398
    assert d.position_value <= 200_000
    assert d.risk_amount < 10_000


def test_micro_account_10k(settings, costs, make_plan):
    """₹10k: 1% risk = ₹100 (your choice), max position 50% = ₹5,000."""
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=10_000, cash=10_000)
    plan = make_plan(trigger=200, stop=192, target=230)  # ₹8 risk/share
    d = eng.evaluate(plan, acct, [], Regime.BULL, avg_daily_volume=1_000_000)
    assert d.approved, d.reasons
    assert d.quantity == 12  # 100 / 8 = 12.5 -> 12 shares
    assert d.position_value <= 5_000
    assert d.risk_amount <= 100


def test_trailing_only_plan_checks_charges_against_risk(settings, costs, make_plan):
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=10_000, cash=10_000)
    plan = make_plan(trigger=200, stop=192, target=280)
    plan.trailing_only = True
    d = eng.evaluate(plan, acct, [], Regime.BULL, avg_daily_volume=1_000_000)
    assert d.approved and "trailing stop" in d.reasons[-1]
    tiny = make_plan(trigger=200, stop=198, target=280)  # ₹2 risk/share -> qty capped by position size
    tiny.trailing_only = True
    d2 = eng.evaluate(tiny, acct, [], Regime.BULL, avg_daily_volume=1_000_000)
    assert not d2.approved  # 1% stop is inside normal noise / charges too large vs risk


def test_price_too_high_for_small_account(settings, costs, make_plan):
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=10_000, cash=10_000)
    d = eng.evaluate(make_plan(trigger=6000, stop=5800, target=6700), acct, [], Regime.BULL)
    assert not d.approved
    assert "0 shares" in d.reasons[-1]


def test_rejects_low_reward_risk(settings, costs, make_plan):
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=1_000_000, cash=1_000_000)
    d = eng.evaluate(make_plan(target=530), acct, [], Regime.BULL)  # 1.25R
    assert not d.approved and "reward:risk" in d.reasons[-1]


def test_rejects_wide_and_tight_stops(settings, costs, make_plan):
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=1_000_000, cash=1_000_000)
    wide = eng.evaluate(make_plan(stop=450, target=700), acct, [], Regime.BULL)  # 10%
    tight = eng.evaluate(make_plan(stop=497, target=520), acct, [], Regime.BULL)  # 0.6%
    assert not wide.approved and "max" in wide.reasons[-1]
    assert not tight.approved and "noise" in tight.reasons[-1]


def test_no_new_longs_in_bear_or_shock(settings, costs, make_plan):
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=1_000_000, cash=1_000_000)
    for reg in (Regime.BEAR, Regime.SHOCK):
        d = eng.evaluate(make_plan(target=600), acct, [], reg)
        assert not d.approved and "regime" in d.reasons[-1]


def test_neutral_regime_needs_higher_score_and_half_risk(settings, costs, make_plan):
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=1_000_000, cash=1_000_000)
    assert not eng.evaluate(make_plan(score=75, target=600), acct, [], Regime.NEUTRAL).approved
    d = eng.evaluate(make_plan(score=85, target=600, trigger=100, stop=95), acct, [], Regime.NEUTRAL)
    assert d.approved
    assert d.risk_amount <= 1_000_000 * 0.005 + 1


def test_max_positions_and_heat(settings, costs, make_plan):
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=10_000, cash=10_000)
    two = [_pos("A", sector="X"), _pos("B", sector="Y")]
    d = eng.evaluate(make_plan(trigger=200, stop=192, target=240), acct, two, Regime.BULL)
    assert not d.approved and "max open positions" in d.reasons[-1]
    # heat: one position already risking 3.5% on a micro account (cap 4%)
    hot = [Position("H", "pullback", date(2026, 1, 1), 100, 70, 95, 95, 130, "Z")]  # 70*5 = 350 = 3.5%
    d = eng.evaluate(make_plan(trigger=200, stop=192, target=240), acct, hot, Regime.BULL)
    assert not d.approved and "heat" in d.reasons[-1]


def test_sector_and_correlation_limits(settings, costs, make_plan):
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=1_000_000, cash=1_000_000)
    held = [_pos("A", sector="IT")]
    d = eng.evaluate(make_plan(sector="IT", target=600), acct, held, Regime.BULL)
    assert not d.approved and "sector" in d.reasons[-1]
    d = eng.evaluate(make_plan(sector="Pharma", target=600), acct, held, Regime.BULL, correlations={"A": 0.92})
    assert not d.approved and "correlated" in d.reasons[-1]
    lazy_called = []
    d = eng.evaluate(make_plan(sector="Pharma", target=600), acct, held, Regime.BULL,
                     correlations=lambda: lazy_called.append(1) or {"A": 0.3})
    assert d.approved and lazy_called


def test_never_uses_margin(settings, costs, make_plan):
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=1_000_000, cash=20_000)  # most money already in stocks
    d = eng.evaluate(make_plan(target=600), acct, [], Regime.BULL)
    assert d.approved and d.position_value <= 20_000


def test_duplicate_symbol_rejected(settings, costs, make_plan):
    eng = RiskEngine(settings, costs)
    acct = AccountState(equity=1_000_000, cash=1_000_000)
    d = eng.evaluate(make_plan(symbol="A", target=600), acct, [_pos("A", sector="Q")], Regime.BULL)
    assert not d.approved and "already" in d.reasons[-1]


def test_circuit_breakers(settings):
    cfg = settings.section("circuit_breakers")
    a = AccountState(equity=10_000, cash=10_000)
    assert check_circuit_breakers(a, cfg).allow_entries
    a.mark(9_790)  # -2.1% today
    st = check_circuit_breakers(a, cfg)
    assert not st.allow_entries and "daily loss" in st.reasons[0]

    b = AccountState(equity=10_000, cash=10_000)
    b.mark(8_400)  # 16% below peak -> full halt, sticky
    st = check_circuit_breakers(b, cfg)
    assert not st.allow_entries and b.halted
    b.mark(9_900)
    assert not check_circuit_breakers(b, cfg).allow_entries  # needs manual resume

    assert not check_circuit_breakers(AccountState(equity=1, cash=1), cfg, kill_switch=True).allow_entries


def test_losing_streak_halves_size(settings):
    cfg = settings.section("circuit_breakers")
    a = AccountState(equity=10_000, cash=10_000)
    for _ in range(3):
        a.on_trade_closed(-50, 3, 2)
    assert check_circuit_breakers(a, cfg).risk_multiplier == 0.5
    a.on_trade_closed(+80, 3, 2)
    assert check_circuit_breakers(a, cfg).risk_multiplier == 0.5
    a.on_trade_closed(+80, 3, 2)
    assert check_circuit_breakers(a, cfg).risk_multiplier == 1.0


def test_missing_earnings_calendar_halves_size(settings):
    a = AccountState(equity=10_000, cash=10_000)
    assert check_circuit_breakers(a, settings.section("circuit_breakers"), event_data_ok=False).risk_multiplier == 0.5


def test_hard_ceilings_cannot_be_configured_away(tmp_path):
    import yaml
    from swingbot.config import DEFAULT_SETTINGS

    raw = yaml.safe_load(open(DEFAULT_SETTINGS))
    raw["risk_profiles"]["micro"]["risk_pct"]["bull"] = 20  # typo: 20% instead of 2%
    raw["risk_profiles"]["micro"]["max_position_pct"] = 100
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    s = load_settings(p)
    prof = s.profile_for(10_000)
    assert prof.risk_pct["bull"] == HARD_MAX_RISK_PER_TRADE_PCT
    assert prof.max_position_pct == 50


def test_profiles_by_equity(settings):
    assert settings.profile_for(10_000).name == "micro"
    assert settings.profile_for(200_000).name == "small"
    assert settings.profile_for(5_000_000).name == "standard"
