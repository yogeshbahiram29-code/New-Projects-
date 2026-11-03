"""Performance metrics, Monte Carlo drawdown analysis and go-live acceptance checks."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .models import ClosedTrade

# Go-live criteria (docs/SWING_TRADING_BOT_PLAN.md, section 12.2)
ACCEPTANCE = {
    "trades": (">=", 200),
    "profit_factor": (">=", 1.5),
    "expectancy_r": (">=", 0.25),
    "max_drawdown_pct": ("<=", 20.0),
    "mc_dd_95_pct": ("<=", 30.0),
}


def max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((peak - equity) / peak).max() * 100)


def equity_metrics(equity: pd.Series, initial: float) -> dict[str, float]:
    if equity.empty:
        return {}
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    final = float(equity.iloc[-1])
    rets = equity.pct_change().dropna()
    sd = rets.std()
    downside = rets[rets < 0].std()
    cagr = ((final / initial) ** (1 / years) - 1) * 100 if final > 0 else -100.0
    mdd = max_drawdown_pct(equity)
    return {
        "start_equity": initial,
        "final_equity": round(final, 2),
        "total_return_pct": round((final / initial - 1) * 100, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(mdd, 2),
        "sharpe": round(float(rets.mean() / sd * math.sqrt(252)), 2) if sd and sd > 0 else 0.0,
        "sortino": round(float(rets.mean() / downside * math.sqrt(252)), 2) if downside and downside > 0 else 0.0,
        "mar": round(cagr / mdd, 2) if mdd > 0 else 0.0,
        "years": round(years, 2),
    }


def trade_metrics(trades: list[ClosedTrade]) -> dict[str, float]:
    real = [t for t in trades if t.exit_reason != "end of backtest"]
    if not real:
        return {"trades": 0}
    r = np.array([t.r_multiple for t in real])
    pnl = np.array([t.pnl for t in real])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    streak = worst = 0
    for p in pnl:
        streak = streak + 1 if p <= 0 else 0
        worst = max(worst, streak)
    return {
        "trades": len(real),
        "win_rate_pct": round(len(wins) / len(real) * 100, 1),
        "avg_win_r": round(float(r[pnl > 0].mean()), 2) if len(wins) else 0.0,
        "avg_loss_r": round(float(r[pnl <= 0].mean()), 2) if len(losses) else 0.0,
        "expectancy_r": round(float(r.mean()), 3),
        "profit_factor": round(float(gross_win / gross_loss), 2) if gross_loss > 0 else float("inf"),
        "total_charges": round(float(sum(t.costs for t in real)), 2),
        "avg_bars_held": round(float(np.mean([t.bars_held for t in real])), 1),
        "max_losing_streak": int(worst),
        "best_trade_r": round(float(r.max()), 2),
        "worst_trade_r": round(float(r.min()), 2),
    }


def monte_carlo_drawdown(trades: list[ClosedTrade], runs: int = 2000, seed: int = 7) -> dict[str, float]:
    """Shuffle the order of trade returns to see the realistic range of drawdowns."""
    rets = np.array([t.return_pct / 100 for t in trades if t.exit_reason != "end of backtest"])
    if len(rets) < 10:
        return {}
    rng = np.random.default_rng(seed)
    dds = np.empty(runs)
    for i in range(runs):
        path = np.cumprod(1 + rng.permutation(rets))
        peak = np.maximum.accumulate(np.concatenate([[1.0], path]))[1:]
        dds[i] = ((peak - path) / peak).max() * 100
    return {
        "mc_dd_median_pct": round(float(np.median(dds)), 2),
        "mc_dd_95_pct": round(float(np.percentile(dds, 95)), 2),
        "mc_dd_99_pct": round(float(np.percentile(dds, 99)), 2),
    }


def by_group(trades: list[ClosedTrade], key: str) -> pd.DataFrame:
    df = pd.DataFrame([t.to_dict() for t in trades if t.exit_reason != "end of backtest"])
    if df.empty:
        return df
    g = df.groupby(key)
    return pd.DataFrame(
        {
            "trades": g.size(),
            "win_rate_pct": g["pnl"].apply(lambda s: round((s > 0).mean() * 100, 1)),
            "expectancy_r": g["r_multiple"].mean().round(3),
            "pnl": g["pnl"].sum().round(0),
        }
    ).sort_values("trades", ascending=False)


def yearly(equity: pd.Series) -> pd.DataFrame:
    if equity.empty:
        return pd.DataFrame()
    rows = []
    for y, s in equity.groupby(equity.index.year):
        rows.append({"year": y, "return_pct": round((s.iloc[-1] / s.iloc[0] - 1) * 100, 2), "max_dd_pct": round(max_drawdown_pct(s), 2)})
    return pd.DataFrame(rows).set_index("year")


@dataclass
class Check:
    name: str
    value: float
    rule: str
    passed: bool


def acceptance(summary: dict[str, float], benchmark: dict[str, float] | None = None) -> list[Check]:
    checks = []
    for name, (op, thr) in ACCEPTANCE.items():
        v = summary.get(name)
        if v is None:
            checks.append(Check(name, float("nan"), f"{op} {thr}", False))
            continue
        ok = v >= thr if op == ">=" else v <= thr
        checks.append(Check(name, v, f"{op} {thr}", ok))
    if benchmark and "mar" in benchmark:
        checks.append(Check("mar_vs_buy_and_hold", summary.get("mar", 0.0), f">= {benchmark['mar']}", summary.get("mar", 0.0) >= benchmark["mar"]))
    return checks


def summarize(result, index_df: pd.DataFrame | None = None) -> tuple[dict[str, float], dict[str, float] | None]:
    summary = {**equity_metrics(result.equity, result.initial_capital), **trade_metrics(result.trades), **monte_carlo_drawdown(result.trades)}
    if not result.equity.empty:
        exposure_days = sum(1 for _ in result.equity.index)
        summary["signals"] = result.signals
        summary["days"] = exposure_days
    bench = None
    if index_df is not None and not result.equity.empty:
        idx = index_df["close"].loc[result.equity.index[0] : result.equity.index[-1]]
        if len(idx) > 2:
            bench = equity_metrics(idx / idx.iloc[0] * result.initial_capital, result.initial_capital)
    return summary, bench


def format_report(result, summary: dict, bench: dict | None, synthetic: bool = False) -> str:
    lines = []
    if synthetic:
        lines += [
            "=" * 72,
            " SYNTHETIC DATA - this run only proves the machinery works.",
            " The numbers below say NOTHING about real-market performance.",
            "=" * 72,
        ]
    lines.append("\nPERFORMANCE")
    keys = [
        "start_equity", "final_equity", "total_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "sortino", "mar",
        "trades", "win_rate_pct", "avg_win_r", "avg_loss_r", "expectancy_r", "profit_factor", "total_charges",
        "avg_bars_held", "max_losing_streak", "best_trade_r", "worst_trade_r", "mc_dd_median_pct", "mc_dd_95_pct", "mc_dd_99_pct",
        "signals", "years",
    ]
    for k in keys:
        if k in summary:
            lines.append(f"  {k:<22} {summary[k]}")
    if bench:
        lines.append("\nBENCHMARK (index buy & hold)")
        for k in ("total_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "mar"):
            lines.append(f"  {k:<22} {bench.get(k)}")
    if result.trades:
        lines.append("\nBY SETUP\n" + by_group(result.trades, "setup").to_string())
        lines.append("\nBY EXIT REASON\n" + by_group(result.trades, "exit_reason").to_string())
    yr = yearly(result.equity)
    if not yr.empty:
        lines.append("\nBY YEAR\n" + yr.to_string())
    if result.rejected:
        lines.append("\nTOP RISK-ENGINE REJECTIONS")
        for k, v in list(result.rejected.items())[:10]:
            lines.append(f"  {v:>6}  {k}")
    lines.append("\nGO-LIVE ACCEPTANCE CHECK")
    checks = acceptance(summary, bench)
    for c in checks:
        lines.append(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name:<22} {c.value}  (needs {c.rule})")
    verdict = all(c.passed for c in checks)
    lines.append(f"\n  VERDICT: {'READY FOR PAPER TRADING' if verdict else 'NOT READY - do not trade this live'}")
    return "\n".join(lines)
