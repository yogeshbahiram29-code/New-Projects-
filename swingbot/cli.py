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
        from .data import CachedData, load_universe, make_provider

        uni = load_universe(settings.get("universe.file"))
        bench = settings.get("regime.benchmark", "NIFTY50")
        days = int((pd.Timestamp(args.end or date.today()) - pd.Timestamp(args.start or "2014-01-01")).days * 0.7) + 400
        cache = CachedData(make_provider(settings), settings.get("data.cache_dir", "data_cache"))
        raw = cache.load(sorted(uni) + [bench], days, pd.Timestamp(args.end).date() if args.end else date.today())
        index_df = raw.pop(bench)
        stocks, sectors = raw, uni
        start = args.start or str(index_df.index[260].date())
        print(f"Loaded {len(stocks)} stocks, {index_df.index[0].date()} -> {index_df.index[-1].date()}")
    print(f"Backtesting from {start} with capital ₹{settings.get('initial_capital'):,} ...")
    res = Backtester(settings, settings.get("broker", "paper")).run(stocks, index_df, start, args.end, sectors, progress=True)
    summary, bench_m = metrics.summarize(res, index_df)
    print(metrics.format_report(res, summary, bench_m, synthetic=args.synthetic))
    out = Path(settings.get("paths.reports_dir", "reports"))
    out.mkdir(parents=True, exist_ok=True)
    res.trades_frame().to_csv(out / "backtest_trades.csv", index=False)
    res.equity.to_csv(out / "backtest_equity.csv", header=["equity"])
    print(f"\nTrades -> {out / 'backtest_trades.csv'}, equity curve -> {out / 'backtest_equity.csv'}")
    return 0


def cmd_scan(args, settings) -> int:
    from . import analysis, regime as regime_mod
    from .costs import CostModel
    from .data import CachedData, events_calendar, load_universe, make_provider
    from .models import Regime
    from .risk import AccountState, RiskEngine
    from .scanner import scan

    uni = load_universe(settings.get("universe.file"))
    bench = settings.get("regime.benchmark", "NIFTY50")
    cache = CachedData(make_provider(settings), settings.get("data.cache_dir", "data_cache"))
    raw = cache.load(sorted(uni) + [bench], int(settings.get("data.history_days", 600)))
    index_df = raw.pop(bench)
    frames, rs_panel = analysis.prepare_universe(raw, index_df)
    breadth = regime_mod.breadth_series(frames)
    b = float(breadth.iloc[-1]) if len(breadth) and not pd.isna(breadth.iloc[-1]) else None
    reg = regime_mod.current_regime(index_df, b, settings.section("regime"))
    events, ok = events_calendar(settings)
    when = index_df.index[-1]
    print(f"Data up to {when.date()} | regime {reg.value.upper()} | breadth {f"{b:.0f}%" if b is not None else "n/a"} | earnings calendar {'OK' if ok else 'UNAVAILABLE'}")
    cands = scan(frames, when, settings, rs_panel, uni, events)
    cap = float(settings.get("initial_capital", 10000))
    risk = RiskEngine(settings, CostModel.from_settings(settings.section("costs"), settings.get("broker", "paper")))
    acct = AccountState(equity=cap, cash=cap)
    eval_regime = reg if reg.allows_entries else Regime.NEUTRAL
    for c in cands[: args.top]:
        d = risk.evaluate(c.plan, acct, [], eval_regime, float(frames[c.plan.symbol]["vol_avg50"].iloc[-1]))
        p = c.plan
        print(f"\n{analysis.explain(c.card)}\n  {p.setup.value}: {p.notes}\n  buy above ₹{p.trigger_price} (max ₹{p.limit_price}), "
              f"stop ₹{p.stop_price} ({p.stop_distance_pct:.1f}%), target ₹{p.target_price} (R:R {p.reward_risk:.2f})\n"
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
    settings = load_settings(args.settings, overrides or None)
    os.chdir(ROOT)
    _setup_logging(settings.get("paths.state_dir", "state"))
    handlers = {
        "backtest": cmd_backtest, "scan": cmd_scan, "run": cmd_run, "job": cmd_job, "status": cmd_status,
        "kill": cmd_kill, "resume": cmd_resume, "preflight": cmd_preflight, "kite-login-url": cmd_kite_login,
        "kite-login": cmd_kite_login, "update-universe": cmd_update_universe, "test-whatsapp": cmd_test_whatsapp,
        "signals": cmd_signals, "took": cmd_took, "sold": cmd_sold, "journal": cmd_journal,
    }
    return handlers[args.cmd](args, settings)


if __name__ == "__main__":
    sys.exit(main())
