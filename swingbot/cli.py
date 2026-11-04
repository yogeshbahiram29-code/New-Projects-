"""Command line: python -m swingbot <command>

  backtest        test the strategy on history (or --synthetic data)
  scan            analyse the market now and show today's best setups (no orders)
  run             start the bot (paper or live, per config/settings.yaml)
  job NAME        run one job now: pre_market | gap_guard | entries | monitor | cancel_unfilled | eod
  status          account, positions, plans, risk controls, recent events
  kill            KILL SWITCH: block all new orders (add --flatten to also sell the bot's positions)
  resume          turn the kill switch / drawdown halt off after you've reviewed things
  preflight       read-only broker check: prints raw broker responses, places NO orders
  kite-login-url  print today's Zerodha login link
  kite-login TOK  finish the daily Zerodha login with the request_token from the redirect URL
  update-universe download the latest NIFTY 500 list from NSE
  test-whatsapp   send a test WhatsApp message

FREE SIGNAL MODE (no server, no broker API - you place orders in the app):
  signals         daily after-close run: WhatsApps exact orders + position updates
  took SYM QTY PX record that you bought (stop/target come from the plan)
  sold SYM PX     record that you sold
  journal         your positions and closed-trade stats
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from .config import ROOT, load_dotenv, load_settings


def _setup_logging(state_dir: str) -> None:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(Path(state_dir) / "swingbot.log")],
    )


def build_broker(settings):
    from .costs import CostModel

    name = settings.get("broker", "paper")
    state_dir = Path(settings.get("paths.state_dir", "state"))
    if name == "groww":
        from .brokers.groww import GrowwBroker

        return GrowwBroker(oco_stop_order_type=settings.get("execution.groww_oco_stop_order_type", "MARKET"))
    if name == "zerodha":
        from .brokers.zerodha import ZerodhaBroker

        return ZerodhaBroker(state_dir / "kite_token.json")
    from .brokers.paper import PaperBroker

    return PaperBroker(state_dir / "paper_broker.json", float(settings.get("initial_capital", 10000)),
                       CostModel.from_settings(settings.section("costs"), "paper"))


def build_engine(settings, connect_data: bool = True):
    from .data import CachedData, make_provider
    from .engine import LiveEngine
    from .notify import Notifier
    from .store import Store

    state_dir = settings.get("paths.state_dir", "state")
    broker = build_broker(settings)
    notifier = Notifier(settings.get("notifications.whatsapp_provider", "callmebot"), state_dir)
    store = Store(Path(state_dir) / f"swingbot_{settings.mode}.db")
    data = None
    if connect_data:
        data = CachedData(make_provider(settings, broker if settings.get("data.provider") == "groww" else None),
                          settings.get("data.cache_dir", "data_cache"))
    return LiveEngine(settings, broker, notifier, store, data)


# ---------------------------------------------------------------------------
def cmd_backtest(args, settings) -> int:
    from . import metrics
    from .backtest import Backtester

    if args.synthetic:
        from .synthetic import make_market

        stocks, index_df, sectors = make_market(n_stocks=args.n_stocks, years=args.years, seed=args.seed)
        start = args.start or str(index_df.index[300].date())
    else:
        from .data import CachedData, load_full_universe, make_provider

        uni = load_full_universe(settings)
        bench = settings.get("regime.benchmark", "NIFTY50")
        days = int((pd.Timestamp(args.end or date.today()) - pd.Timestamp(args.start or "2014-01-01")).days * 0.7) + 400
        cache = CachedData(make_provider(settings), settings.get("data.cache_dir", "data_cache"))
        raw = cache.load(sorted(uni) + [bench], days, pd.Timestamp(args.end).date() if args.end else date.today())
        index_df = raw.pop(bench)
        stocks, sectors = raw, uni
        start = args.start or str(index_df.index[260].date())
        print(f"Loaded {len(stocks)} stocks, {index_df.index[0].date()} -> {index_df.index[-1].date()}")
    costs = args.costs or settings.get("signals.broker_costs", "zerodha")
    print(f"Backtesting from {start} with capital ₹{settings.get('initial_capital'):,.0f} and {costs} charges ...")
    res = Backtester(settings, costs, research=args.research).run(stocks, index_df, start, args.end, sectors, progress=True)
    summary, bench_m = metrics.summarize(res, index_df)
    print(metrics.format_report(res, summary, bench_m, synthetic=args.synthetic))
    out = Path(settings.get("paths.reports_dir", "reports"))
    out.mkdir(parents=True, exist_ok=True)
    res.trades_frame().to_csv(out / "backtest_trades.csv", index=False)
    res.equity.to_csv(out / "backtest_equity.csv", header=["equity"])
    print(f"\nTrades -> {out / 'backtest_trades.csv'}, equity curve -> {out / 'backtest_equity.csv'}")
    return 0


RESEARCH_VARIANTS = [
    ("classic (old v1)", {"strategy": "classic"}, [1_000_000]),
    ("momentum breakout", {"strategy": "momentum", "momentum": {"entry": "breakout"}}, [1_000_000, 10_000]),
    ("momentum pullback", {"strategy": "momentum", "momentum": {"entry": "pullback"}}, [1_000_000, 10_000]),
]


def cmd_research(args, settings) -> int:
    """Compare strategy variants on the SAME data, in research mode (halts recorded, testing continues)."""
    from . import metrics
    from .backtest import Backtester
    from .config import load_settings
    from .data import CachedData, load_full_universe, make_provider

    end = pd.Timestamp(args.end).date()
    uni = load_full_universe(settings)
    bench = settings.get("regime.benchmark", "NIFTY50")
    days = (pd.Timestamp(end) - pd.Timestamp(args.start)).days + 1300  # + warm-up for 200-day averages
    cache = CachedData(make_provider(settings), settings.get("data.cache_dir", "data_cache"))
    raw = cache.load(sorted(uni) + [bench], int(days / 1.5) + 1, end)  # data physically ends at --end
    index_df = raw.pop(bench)
    print(f"Universe: {len(uni)} symbols requested, {len(raw)} with data | data {index_df.index[0].date()} -> "
          f"{index_df.index[-1].date()} | test window {args.start} -> {end}")
    rows, bench_row = [], None
    for name, override, capitals in RESEARCH_VARIANTS:
        for cap in capitals:
            s = load_settings(args.settings, {**override, "mode": "backtest", "initial_capital": cap})
            res = Backtester(s, args.costs, research=True).run(raw, index_df, args.start, str(end), uni)
            summ, bench_m = metrics.summarize(res, index_df)
            print(f"\n{'#' * 72}\n# {name} | capital ₹{cap:,}\n{'#' * 72}")
            print(metrics.format_report(res, summ, bench_m))
            rows.append({"variant": name, "capital": cap, **{k: summ.get(k) for k in (
                "trades", "win_rate_pct", "expectancy_r", "profit_factor", "cagr_pct", "max_drawdown_pct",
                "avg_bars_held", "time_in_market_pct", "would_have_halted")}})
            bench_row = bench_m
    print("\n" + "=" * 72 + "\nSUMMARY (in-sample, after costs, research mode)\n" + "=" * 72)
    print(pd.DataFrame(rows).to_string(index=False))
    if bench_row:
        print(f"\nNIFTY 50 buy & hold same window: CAGR {bench_row.get('cagr_pct')}%, max drawdown {bench_row.get('max_drawdown_pct')}%")
    return 0


def cmd_portfolio_research(args, settings) -> int:
    """Options B (monthly momentum rotation) and C (index trend), plus controls and benchmark."""
    from . import metrics
    from .costs import CostModel
    from .data import CachedData, load_full_universe, make_provider
    from .portfolio import (backtest_index_trend, backtest_momentum_rotation, current_picks, max_jump_during,
                            portfolio_trade_metrics, suspicious_symbols)

    end = pd.Timestamp(args.end).date()
    uni = load_full_universe(settings)
    bench = settings.get("regime.benchmark", "NIFTY50")
    days = (pd.Timestamp(end) - pd.Timestamp(args.start)).days + 1300
    cache = CachedData(make_provider(settings), settings.get("data.cache_dir", "data_cache"))
    raw = cache.load(sorted(uni) + [bench], int(days / 1.5) + 1, end)  # data physically ends at --end
    index_df = raw.pop(bench)
    costs = CostModel.from_settings(settings.section("costs"), args.costs)
    ucfg = settings.section("universe")
    print(f"Universe: {len(uni)} requested, {len(raw)} with data | test window {args.start} -> {end} | "
          f"charges: {args.costs}, slippage {costs.slippage_pct}%/side")

    runs = []
    for cap in (1_000_000, 10_000):
        for y in (0.0, 5.0):
            runs.append((cap, backtest_index_trend(index_df, args.start, end, cap, costs, 10, y)))
        for y in ((0.0, 5.0) if cap == 1_000_000 else (0.0,)):
            runs.append((cap, backtest_momentum_rotation(raw, index_df, args.start, end, cap, costs,
                                                         min_value_cr=float(ucfg.get("min_avg_traded_value_cr", 10)),
                                                         min_price=float(ucfg.get("min_price", 50)), cash_yield_pct=y)))
    # red-flag check: rerun B without stocks whose data shows >25% one-day jumps (data errors / corporate actions)
    bad = suspicious_symbols(raw, args.start, end)
    clean_raw = {s: df for s, df in raw.items() if s not in bad}
    clean = backtest_momentum_rotation(clean_raw, index_df, args.start, end, 1_000_000, costs,
                                       min_value_cr=float(ucfg.get("min_avg_traded_value_cr", 10)),
                                       min_price=float(ucfg.get("min_price", 50)))
    clean.name = "B: momentum top 10, cash 0%, CLEAN data"
    runs.append((1_000_000, clean))
    controls = [backtest_momentum_rotation(raw, index_df, args.start, end, 1_000_000, costs, pick="random", seed=k,
                                           min_value_cr=float(ucfg.get("min_avg_traded_value_cr", 10)))
                for k in range(5)]

    idx = index_df["close"].loc[pd.Timestamp(args.start): pd.Timestamp(end)]
    bh = metrics.equity_metrics(idx / idx.iloc[0] * 1_000_000, 1_000_000)
    rows = []
    for cap, res in runs:
        em = metrics.equity_metrics(res.equity, cap)
        tm = portfolio_trade_metrics(res.trades)
        rows.append({"strategy": res.name, "capital": cap, "cagr_pct": em.get("cagr_pct"),
                     "max_dd_pct": em.get("max_drawdown_pct"), "sharpe": em.get("sharpe"), "mar": em.get("mar"),
                     "trades": tm.get("trades"), "win_pct": tm.get("win_rate_pct"), "exp_rs": tm.get("expectancy_rs"),
                     "exp_pct": tm.get("expectancy_pct"), "pf": tm.get("profit_factor"), "avg_days": tm.get("avg_days_held"),
                     "lose_streak": tm.get("max_losing_streak"), "in_market_pct": res.exposure_pct,
                     "final": em.get("final_equity")})
    pd.set_option("display.width", 250)
    print("\n" + "=" * 100 + "\nOPTIONS B & C - in-sample, after all charges\n" + "=" * 100)
    print(pd.DataFrame(rows).to_string(index=False))
    cc = [metrics.equity_metrics(c.equity, 1_000_000) for c in controls]
    print(f"\nCONTROL - same rotation rules but RANDOM picks (5 runs, ₹10L, cash 0%): "
          f"CAGR {min(x['cagr_pct'] for x in cc)}% .. {max(x['cagr_pct'] for x in cc)}% "
          f"(mean {np.mean([x['cagr_pct'] for x in cc]):.2f}%), max DD mean {np.mean([x['max_drawdown_pct'] for x in cc]):.1f}%")
    print(f"BENCHMARK - NIFTY 50 buy & hold (price index, no dividends): CAGR {bh['cagr_pct']}%, "
          f"max DD {bh['max_drawdown_pct']}%, Sharpe {bh['sharpe']}, MAR {bh['mar']}")

    print("\nYEAR BY YEAR (% return, ₹10L, cash 0%)")
    table = {"NIFTY 50": metrics.yearly(idx)["return_pct"]}
    for cap, res in runs:
        if cap == 1_000_000 and "cash 0%" in res.name:
            table[res.name.split(" (")[0].split(",")[0]] = metrics.yearly(res.equity)["return_pct"]
    print(pd.DataFrame(table).to_string())
    for cap, res in runs:
        if res.notes and cap == 1_000_000:
            print(f"\nNotes for {res.name}: " + "; ".join(res.notes))

    # ---- red-flag investigation: where did the big years come from?
    b_main = next(r for c, r in runs if c == 1_000_000 and r.name.startswith("B: momentum") and "cash 0%" in r.name)
    tr = pd.DataFrame(b_main.trades)
    if len(tr):
        tr["year"] = pd.to_datetime(tr["exit_date"]).dt.year
        tr["max_1day_jump_pct"] = [max_jump_during(raw[r.symbol], r.entry_date, r.exit_date) for r in tr.itertuples()]
        print("\nRED-FLAG CHECK - top 15 trades by profit (B, ₹10L, cash 0%)")
        cols = ["symbol", "entry_date", "exit_date", "days", "ret_pct", "pnl", "max_1day_jump_pct", "exit_reason"]
        print(tr.sort_values("pnl", ascending=False)[cols].head(15).round(1).to_string(index=False))
        print("\nProfit by year: " + ", ".join(f"{y}: ₹{v:,.0f}" for y, v in tr.groupby("year")["pnl"].sum().items()))
        n_bad = int((tr["max_1day_jump_pct"] > 25).sum())
        print(f"Trades with a >25% one-day jump while held: {n_bad} of {len(tr)}")
    print(f"\nStocks with >25% one-day jumps in the data (excluded in the CLEAN run): {len(bad)}")
    for sym, js in list(bad.items())[:25]:
        print(f"  {sym}: {js}")

    # ---- today's view (only meaningful when --end is the latest date)
    regime, picks = current_picks(raw, index_df, min_value_cr=float(ucfg.get("min_avg_traded_value_cr", 10)),
                                  min_price=float(ucfg.get("min_price", 50)), sectors=uni)
    print("\n" + "=" * 100 + f"\nCURRENT VIEW - data up to {regime['date']}\n" + "=" * 100)
    print(f"NIFTY 50 {regime['nifty']} vs 200-day SMA {regime['sma200']} (SMA rising: {regime['sma200_rising']}) -> "
          f"regime {'ON (invest)' if regime['on_today'] else 'OFF (cash)'} today; "
          f"at the last month-end ({regime['month_end']}): {'ON' if regime['on_at_month_end'] else 'OFF'}")
    if regime["on_at_month_end"] and len(picks):
        print(f"Option B top 10 from the {regime['month_end']} close (bought at the next open):")
        print(picks.to_string())
    elif len(picks):
        print("Regime was OFF at the last month-end -> Option B would hold CASH. Strongest stocks for reference:")
        print(picks.to_string())
    return 0


def cmd_scan(args, settings) -> int:
    from .costs import CostModel
    from .data import CachedData, events_calendar, load_full_universe, make_provider
    from .models import Regime
    from .risk import AccountState, RiskEngine
    from .strategies import get_strategy

    uni = load_full_universe(settings)
    bench = settings.get("regime.benchmark", "NIFTY50")
    cache = CachedData(make_provider(settings), settings.get("data.cache_dir", "data_cache"))
    raw = cache.load(sorted(uni) + [bench], int(settings.get("data.history_days", 600)))
    index_df = raw.pop(bench)
    strat = get_strategy(settings)
    prep = strat.prepare(raw, index_df)
    reg = strat.current_regime(index_df, prep)
    events, ok = events_calendar(settings)
    when = index_df.index[-1]
    print(f"Strategy {strat.name} | data up to {when.date()} | regime {reg.value.upper()} | "
          f"earnings calendar {'OK' if ok else 'UNAVAILABLE'}")
    cands = strat.scan(prep, when, uni, events)
    cap = float(settings.get("initial_capital", 10000))
    risk = RiskEngine(settings, CostModel.from_settings(settings.section("costs"), settings.get("signals.broker_costs", "zerodha")))
    acct = AccountState(equity=cap, cash=cap)
    eval_regime = reg if reg.allows_entries else Regime.BULL  # show what WOULD be traded
    for c in cands[: args.top]:
        d = risk.evaluate(c.plan, acct, [], eval_regime, float(prep.frames[c.plan.symbol]["vol_avg50"].iloc[-1]))
        p = c.plan
        target = "none (trailing stop)" if p.trailing_only else f"₹{p.target_price} (R:R {p.reward_risk:.2f})"
        print(f"\n{p.symbol}: score {c.card.total:g} - {p.setup.value}: {p.notes}\n"
              f"  buy above ₹{p.trigger_price} (max ₹{p.limit_price}), stop ₹{p.stop_price} ({p.stop_distance_pct:.1f}%), "
              f"target {target}\n"
              f"  risk engine: {'APPROVED ' + str(d.quantity) + ' shares' if d.approved else 'REJECTED'} - {d.reasons[-1]}")
    if not cands:
        print("No setups today. That's normal - the bot only trades when everything lines up.")
    if not reg.allows_entries:
        print(f"\nNOTE: regime is {reg.value.upper()} - the bot would NOT open new trades today.")
    return 0


def cmd_run(args, settings) -> int:
    if settings.mode == "live":
        if os.environ.get("SWINGBOT_CONFIRM_LIVE") != "YES":
            print("Refusing to start LIVE trading: set SWINGBOT_CONFIRM_LIVE=YES in .env after you have\n"
                  "1) passed the backtest acceptance check, 2) paper traded, 3) run `preflight` successfully.")
            return 2
        print(f"LIVE TRADING with real money (cap ₹{settings.get('live_capital_limit'):,}).")
    eng = build_engine(settings)
    eng.run_forever()
    return 0


def cmd_job(args, settings) -> int:
    eng = build_engine(settings)
    eng.run_job(args.name)
    return 0


def cmd_status(args, settings) -> int:
    eng = build_engine(settings, connect_data=False)
    acct = eng.account()
    print(f"Mode {settings.mode} | broker {settings.get('broker')} | kill switch {'ON' if eng.kill_active() else 'off'} | "
          f"halted {acct.halted} {acct.halt_reason}")
    print(f"Equity ₹{eng.bot_equity():,.0f} | bot cash ₹{eng.bot_cash():,.0f} | peak ₹{acct.peak_equity:,.0f} | "
          f"drawdown {acct.drawdown_pct:.1f}% | losing streak {acct.consecutive_losses}")
    print(f"Regime (last EOD): {eng.db.get('regime', 'unknown')}")
    for p in eng.db.positions():
        print(f"  POSITION {p.symbol} x{p.quantity} @ {p.entry_price:.2f} stop {p.stop_price:.2f} target {p.target_price:.2f} "
              f"held {p.bars_held}d protection {p.protection_id} {p.pending_exit_reason or ''}")
    for oid, plan in eng.db.pending():
        print(f"  PENDING {oid} {plan.symbol} x{plan.quantity} trigger {plan.trigger_price}")
    day, plans = eng.db.plans()
    for plan in plans:
        print(f"  PLAN for {day}: {plan.symbol} {plan.setup.value} score {plan.score} qty {plan.quantity}")
    trades = eng.db.trades()
    if trades:
        df = pd.DataFrame(trades)
        print(f"Closed trades {len(df)} | win rate {(df.pnl > 0).mean() * 100:.0f}% | total P&L ₹{df.pnl.sum():,.0f} | avg {df.r_multiple.mean():+.2f}R")
    for ts, kind, msg in eng.db.recent_events(10):
        print(f"  {ts[:19]} {kind}: {msg}")
    return 0


def cmd_kill(args, settings) -> int:
    eng = build_engine(settings, connect_data=False)
    eng.kill_file.parent.mkdir(parents=True, exist_ok=True)
    eng.kill_file.write_text("manual kill")
    print("Kill switch ON: the bot will place no new orders. Existing stop-losses stay active.")
    if eng.ensure_connected():
        for oid, _ in eng.db.pending():
            try:
                eng.b.cancel_order(oid)
            except Exception as e:
                print(f"  could not cancel {oid}: {e}")
            eng.db.remove_pending(oid)
        if args.flatten:
            for pos in eng.db.positions():
                try:
                    if pos.protection_id:
                        eng.b.cancel_protection(pos.protection_id)
                    oid = eng.b.sell_market(pos.symbol, pos.quantity, eng._ref())
                    eng._queue_exit_order(oid, pos.symbol, "manual flatten (kill switch)")
                    pos.pending_exit_reason = "SENT: manual flatten"
                    eng.db.save_position(pos)
                    print(f"  SELL {pos.quantity} {pos.symbol} at market sent")
                except Exception as e:
                    print(f"  FAILED to sell {pos.symbol}: {e} - close it manually in the app")
    eng.n.send("KILL SWITCH turned ON manually." + (" Flattening bot positions." if args.flatten else ""), urgent=True)
    return 0


def cmd_resume(args, settings) -> int:
    eng = build_engine(settings, connect_data=False)
    acct = eng.account()
    if not args.yes:
        ans = input(f"Turn trading back on? halted={acct.halted} ({acct.halt_reason}), kill={eng.kill_active()} [type YES]: ")
        if ans.strip() != "YES":
            print("Not resumed.")
            return 1
    if eng.kill_active():
        eng.kill_file.unlink()
    if acct.halted:
        acct.halted = False
        acct.halt_reason = ""
        acct.peak_equity = acct.equity  # restart drawdown measurement from here
        eng.db.save_account(acct)
    eng.n.send("Trading resumed manually.")
    print("Resumed.")
    return 0


def cmd_preflight(args, settings) -> int:
    broker = build_broker(settings)
    if broker.name == "paper":
        print("broker is 'paper' - nothing to check. Set broker: groww or zerodha in settings.yaml.")
        return 0
    broker.connect()
    print(json.dumps(broker.preflight(), indent=2, default=str)[:20000])
    print("\nCheck that parsed_cash and parsed_holdings match your app. If they don't, do NOT go live.")
    return 0


def cmd_kite_login(args, settings) -> int:
    from .brokers.zerodha import ZerodhaBroker

    b = ZerodhaBroker(Path(settings.get("paths.state_dir", "state")) / "kite_token.json")
    if args.cmd == "kite-login-url":
        print(b.login_url())
    else:
        b.complete_login(args.request_token)
        print("Zerodha session saved for today.")
    return 0


def cmd_update_universe(args, settings) -> int:
    from .data import download_nifty500

    n = download_nifty500(ROOT / settings.get("universe.file", "config/universe.csv"))
    print(f"Universe updated: {n} NIFTY 500 stocks")
    return 0


def cmd_test_whatsapp(args, settings) -> int:
    from .notify import Notifier

    ok = Notifier(settings.get("notifications.whatsapp_provider", "callmebot"), settings.get("paths.state_dir", "state")).send(
        "Test message - WhatsApp alerts are working.")
    print("sent" if ok else "FAILED - check WHATSAPP_PHONE / API key in .env (details in state/swingbot.log)")
    return 0 if ok else 1


def _desk(settings, need_data: bool = False):
    from .data import CachedData, make_provider
    from .notify import Notifier
    from .signals import SignalDesk

    loader = CachedData(make_provider(settings), settings.get("data.cache_dir", "data_cache")) if need_data else None
    notifier = Notifier(settings.get("notifications.whatsapp_provider", "callmebot"), settings.get("paths.state_dir", "state"),
                        store_path=Path(settings.get("signals.dir", "journal")) / "telegram.json")
    return SignalDesk(settings, loader, notifier)


def cmd_signals(args, settings) -> int:
    from .data import DataError
    from .signals import SignalError

    desk = _desk(settings, need_data=True)
    try:
        print(desk.run(send=not args.no_whatsapp))
    except (DataError, SignalError) as e:
        print(f"ERROR: {e}")
        return 1
    if desk.sent_ok is False:
        provider = desk.n.provider if desk.n else "?"
        hint = {
            "telegram": "Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see the warning lines above).",
            "callmebot": "Check WHATSAPP_PHONE and CALLMEBOT_API_KEY - the key must be the number CallMeBot sent you.",
        }.get(provider, "Check the notification settings.")
        print(f"ERROR: the {provider} alert was NOT delivered. {hint}")
        return 3  # makes the GitHub run go red so a silent failure can't go unnoticed
    return 0


def cmd_took(args, settings) -> int:
    from .signals import SignalError

    try:
        p = _desk(settings).took(args.symbol, args.qty, args.price, args.stop, args.target)
    except SignalError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Recorded BUY {p.quantity} {p.symbol} @ ₹{p.entry_price:.2f} - stop ₹{p.stop_price:.2f}, target ₹{p.target_price:.2f}.\n"
          f"Make sure your GTT OCO (stop + target) is set in the app.")
    return 0


def cmd_sold(args, settings) -> int:
    from .signals import SignalError

    try:
        t = _desk(settings).sold(args.symbol, args.price, args.qty, args.reason or "manual sell")
    except SignalError as e:
        print(f"ERROR: {e}")
        return 1
    if t:
        print(f"Recorded SELL {t.symbol} @ ₹{t.exit_price:.2f}: P&L ₹{t.pnl:,.2f} ({t.r_multiple:+.2f}R)")
    else:
        print("Recorded partial sell.")
    return 0


def cmd_journal(args, settings) -> int:
    from .signals import journal_summary

    print(journal_summary(_desk(settings)))
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="swingbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", default=None, help="path to settings.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest")
    b.add_argument("--synthetic", action="store_true", help="use generated data (tests the machinery only)")
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--capital", type=float)
    b.add_argument("--n-stocks", type=int, default=60)
    b.add_argument("--years", type=float, default=10)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--costs", choices=["zerodha", "groww", "paper"], help="charges model (default: zerodha)")
    b.add_argument("--research", action="store_true", help="record drawdown halts but keep testing the whole period")
    b.add_argument("--strategy", choices=["momentum", "classic"])
    b.add_argument("--entry", choices=["breakout", "pullback"], help="momentum strategy entry type")
    s = sub.add_parser("scan")
    s.add_argument("--top", type=int, default=10)
    sub.add_parser("run")
    j = sub.add_parser("job")
    j.add_argument("name", choices=["pre_market", "gap_guard", "entries", "monitor", "cancel_unfilled", "eod"])
    sub.add_parser("status")
    k = sub.add_parser("kill")
    k.add_argument("--flatten", action="store_true", help="also sell every position the bot opened")
    r = sub.add_parser("resume")
    r.add_argument("--yes", action="store_true")
    sub.add_parser("preflight")
    sub.add_parser("kite-login-url")
    kl = sub.add_parser("kite-login")
    kl.add_argument("request_token")
    sub.add_parser("update-universe")
    sub.add_parser("test-whatsapp")
    rs = sub.add_parser("research", help="compare strategy variants on the same data (in-sample)")
    rs.add_argument("--start", default="2015-01-01")
    rs.add_argument("--end", default="2022-12-31", help="data is cut here - keep later years for the final test")
    rs.add_argument("--costs", default="zerodha", choices=["zerodha", "groww", "paper"])
    pr = sub.add_parser("portfolio-research", help="options B (momentum rotation) and C (index trend), in-sample")
    pr.add_argument("--start", default="2015-01-01")
    pr.add_argument("--end", default="2022-12-31")
    pr.add_argument("--costs", default="zerodha", choices=["zerodha", "groww", "paper"])
    sg = sub.add_parser("signals", help="free mode: daily WhatsApp trade instructions")
    sg.add_argument("--no-whatsapp", action="store_true", help="only print")
    tk = sub.add_parser("took", help="free mode: record a buy")
    tk.add_argument("symbol")
    tk.add_argument("qty", type=int)
    tk.add_argument("price", type=float)
    tk.add_argument("--stop", type=float)
    tk.add_argument("--target", type=float)
    sd = sub.add_parser("sold", help="free mode: record a sell")
    sd.add_argument("symbol")
    sd.add_argument("price", type=float)
    sd.add_argument("--qty", type=int)
    sd.add_argument("--reason")
    sub.add_parser("journal", help="free mode: positions and stats")
    args = ap.parse_args(argv)

    overrides = {}
    if getattr(args, "capital", None):
        overrides["initial_capital"] = args.capital
    if args.cmd == "backtest":
        overrides["mode"] = "backtest"
        if args.strategy:
            overrides["strategy"] = args.strategy
        if args.entry:
            overrides["momentum"] = {"entry": args.entry}
    settings = load_settings(args.settings, overrides or None)
    os.chdir(ROOT)
    _setup_logging(settings.get("paths.state_dir", "state"))
    handlers = {
        "backtest": cmd_backtest, "scan": cmd_scan, "run": cmd_run, "job": cmd_job, "status": cmd_status,
        "kill": cmd_kill, "resume": cmd_resume, "preflight": cmd_preflight, "kite-login-url": cmd_kite_login,
        "kite-login": cmd_kite_login, "update-universe": cmd_update_universe, "test-whatsapp": cmd_test_whatsapp,
        "research": cmd_research, "portfolio-research": cmd_portfolio_research, "signals": cmd_signals, "took": cmd_took, "sold": cmd_sold, "journal": cmd_journal,
    }
    return handlers[args.cmd](args, settings)


if __name__ == "__main__":
    sys.exit(main())
