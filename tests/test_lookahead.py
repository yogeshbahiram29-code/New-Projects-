"""Look-ahead bias guard: results on day T must not change when future data is appended."""
import numpy as np
import pandas as pd

from swingbot import analysis, regime as rg
from swingbot.config import load_settings
from swingbot.scanner import scan
from swingbot.synthetic import make_market


def test_no_lookahead_in_indicators_regime_and_signals():
    s = load_settings(overrides={"universe": {"min_avg_traded_value_cr": 0}})
    stocks, idx, sectors = make_market(n_stocks=15, years=4, seed=21)
    full, full_rs = analysis.prepare_universe(stocks, idx)
    full_breadth = rg.breadth_series(full)
    full_regime = rg.regime_series(idx, full_breadth, s.section("regime"))
    for t in idx.index[400:900:100]:
        cut_frames, cut_rs = analysis.prepare_universe({k: v.loc[:t] for k, v in stocks.items()}, idx.loc[:t])
        for sym in list(stocks)[:5]:
            a, b = cut_frames[sym].loc[t], full[sym].loc[t]
            for col in a.index:
                va, vb = a[col], b[col]
                if pd.isna(va) and pd.isna(vb):
                    continue
                assert np.isclose(float(va), float(vb)), f"{sym} {col} on {t.date()} uses future data"
        cut_breadth = rg.breadth_series(cut_frames)
        assert np.isclose(cut_breadth.loc[t], full_breadth.loc[t])
        assert rg.regime_series(idx.loc[:t], cut_breadth, s.section("regime")).loc[t] == full_regime.loc[t]
        sig_cut = [(c.plan.symbol, c.plan.trigger_price, c.plan.stop_price, c.card.total) for c in scan(cut_frames, t, s, cut_rs, sectors)]
        sig_full = [(c.plan.symbol, c.plan.trigger_price, c.plan.stop_price, c.card.total) for c in scan(full, t, s, full_rs, sectors)]
        assert sig_cut == sig_full
