"""Market data: daily candles, universe list, earnings/event calendar.

Providers
* yfinance (default) - free end-of-day NSE data (split-adjusted). Fine for a
  swing bot that decides after the close. Unofficial: the bot validates every
  download and refuses to trade on stale or broken data.
* groww - official candles through your Groww API session.
* csv   - your own files in data_cache/<SYMBOL>.csv (date,open,high,low,close,volume)
"""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger("swingbot.data")

NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NSE_EVENTS_URL = "https://www.nseindia.com/api/event-calendar"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) swingbot/1.0", "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
COLS = ["open", "high", "low", "close", "volume"]


class DataError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Drop obviously broken rows; raise if the frame is unusable."""
    if df is None or df.empty:
        raise DataError(f"{symbol}: no data")
    df = df[COLS].astype(float).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    bad = (df["high"] < df["low"]) | (df["close"] <= 0) | (df["close"] > df["high"] * 1.001) | (df["close"] < df["low"] * 0.999)
    if bad.any():
        log.warning("%s: dropping %d malformed candles", symbol, int(bad.sum()))
        df = df[~bad]
    jumps = df["close"].pct_change().abs() > 0.35
    if jumps.any():
        # >35% one-day moves in liquid large/mid caps are usually unadjusted splits/bonuses
        log.warning("%s: %d suspicious >35%% jumps (unadjusted corporate action?)", symbol, int(jumps.sum()))
    return df


def is_fresh(df: pd.DataFrame, expected_last: date) -> bool:
    return not df.empty and df.index[-1].date() >= expected_last


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class YFinanceProvider:
    name = "yfinance"
    index_tickers = {"NIFTY50": "^NSEI", "NIFTY500": "^CRSLDX", "INDIAVIX": "^INDIAVIX", "BANKNIFTY": "^NSEBANK",
                     "SP500": "^GSPC", "NASDAQ": "^IXIC", "BRENT": "BZ=F", "USDINR": "INR=X", "GOLD": "GC=F",
                     "US10Y": "^TNX"}

    def __init__(self):
        try:
            import yfinance  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise DataError("pip install yfinance") from e

    def _ticker(self, symbol: str) -> str:
        return self.index_tickers.get(symbol, f"{symbol}.NS")

    def daily_many(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        out: dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i : i + 50]
            tickers = [self._ticker(s) for s in chunk]
            raw = yf.download(tickers, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                              auto_adjust=False, group_by="ticker", progress=False, threads=True)
            for s, t in zip(chunk, tickers):
                try:
                    sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                    sub = sub.rename(columns=str.lower)[COLS].dropna(subset=["close"])
                    sub.index = pd.to_datetime(sub.index).tz_localize(None).normalize()
                    if s in self.index_tickers:
                        sub["volume"] = sub["volume"].fillna(0)
                    out[s] = sub
                except Exception as e:  # missing ticker etc.
                    log.warning("yfinance: %s failed: %s", s, e)
            time.sleep(1)
        return out


class GrowwProvider:
    name = "groww"

    def __init__(self, broker):
        self.b = broker  # connected GrowwBroker
        self._gsym: dict[str, str] = {}

    def _groww_symbol(self, symbol: str) -> str:
        if symbol not in self._gsym:
            inst = self.b.api.get_instrument_by_exchange_and_trading_symbol("NSE", symbol)
            self._gsym[symbol] = inst["groww_symbol"]
        return self._gsym[symbol]

    def daily_many(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        out = {}
        for s in symbols:
            try:
                r = self.b._call(
                    self.b.api.get_historical_candles,
                    exchange="NSE", segment="CASH", groww_symbol=self._groww_symbol(s),
                    start_time=f"{start.isoformat()} 09:15:00", end_time=f"{end.isoformat()} 15:30:00",
                    candle_interval="1day",
                )
                out[s] = parse_candles(r.get("candles", []))
            except Exception as e:
                log.warning("groww candles %s failed: %s", s, e)
        return out


def parse_candles(rows: list) -> pd.DataFrame:
    """Parse [[ts, o, h, l, c, v, ...], ...] with ts as epoch s/ms or ISO string."""
    recs = []
    for r in rows:
        ts = r[0]
        if isinstance(ts, (int, float)):
            ts = pd.to_datetime(ts, unit="ms" if ts > 1e11 else "s", utc=True).tz_convert("Asia/Kolkata")
        else:
            ts = pd.to_datetime(ts)
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.tz_localize(None)
        recs.append((ts.normalize(), *map(float, r[1:6])))
    df = pd.DataFrame(recs, columns=["date", *COLS]).set_index("date")
    return df


class CSVProvider:
    name = "csv"

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def daily_many(self, symbols, start, end):
        out = {}
        for s in symbols:
            p = self.folder / f"{s}.csv"
            if p.exists():
                df = pd.read_csv(p, index_col=0, parse_dates=True)
                df.columns = [c.lower() for c in df.columns]
                out[s] = df.loc[pd.Timestamp(start) : pd.Timestamp(end)]
        return out


class CachedData:
    """Keeps a local CSV per symbol and only downloads what's missing."""

    def __init__(self, provider, cache_dir: str | Path):
        self.p = provider
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, s: str) -> Path:
        return self.dir / f"{s.replace('^', '_').replace('&', '_and_')}.csv"

    def load(self, symbols: list[str], history_days: int, end: date | None = None) -> dict[str, pd.DataFrame]:
        end = end or date.today()
        start = end - timedelta(days=int(history_days * 1.5))
        frames, need = {}, {}
        for s in symbols:
            p = self._path(s)
            if p.exists():
                df = pd.read_csv(p, index_col=0, parse_dates=True)
                frames[s] = df
                last = df.index[-1].date() if len(df) else start
                if last < end:
                    need.setdefault(last - timedelta(days=5), []).append(s)
            else:
                need.setdefault(start, []).append(s)
        for since, syms in need.items():
            fresh = self.p.daily_many(syms, since, end)
            for s, df in fresh.items():
                if df is None or df.empty:
                    continue
                merged = pd.concat([frames.get(s, pd.DataFrame()), df])
                merged = merged[~merged.index.duplicated(keep="last")].sort_index()
                merged.to_csv(self._path(s))
                frames[s] = merged
        out = {}
        for s, df in frames.items():
            try:
                out[s] = validate(df.loc[pd.Timestamp(start) :], s)
            except DataError as e:
                log.warning(str(e))
        return out


def make_provider(settings, broker=None):
    name = settings.get("data.provider", "yfinance")
    if name == "groww":
        if broker is None or getattr(broker, "name", "") != "groww":
            raise DataError("data.provider=groww needs a connected Groww broker")
        return GrowwProvider(broker)
    if name == "csv":
        return CSVProvider(settings.get("data.cache_dir", "data_cache"))
    return YFinanceProvider()


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
def load_universe(path: str | Path) -> dict[str, str]:
    """symbol -> sector, from a CSV with columns symbol,sector."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or row.get("Symbol") or "").strip()
            if sym:
                out[sym] = (row.get("sector") or row.get("Industry") or "UNKNOWN").strip() or "UNKNOWN"
    return out


def load_names(settings) -> dict[str, str]:
    """symbol -> company name (from the NIFTY 500 file), used for news search."""
    out = {}
    for key in ("universe.file", "universe.extra_file"):
        p = settings.get(key)
        if p and Path(p).exists():
            with open(p, encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if row.get("symbol") and row.get("name"):
                        out[row["symbol"].strip()] = row["name"].strip()
    return out


def load_full_universe(settings) -> dict[str, str]:
    """Main universe file plus the 'fallen stocks' extra file (reduces survivorship bias)."""
    uni = load_universe(settings.get("universe.file", "config/universe.csv"))
    extra = settings.get("universe.extra_file")
    if extra and Path(extra).exists():
        for sym, sec in load_universe(extra).items():
            uni.setdefault(sym, sec)
    return uni


NIFTY500_URLS = [
    NIFTY500_URL,
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
]


def download_nifty500(dest: str | Path) -> int:
    """Refresh the universe from the official NIFTY 500 constituents file (tries several NSE mirrors)."""
    rows, errors = [], []
    for url in NIFTY500_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(r.text)))
            if len(rows) >= 400:
                log.info("NIFTY 500 list from %s", url)
                break
            errors.append(f"{url}: only {len(rows)} rows")
        except Exception as e:
            errors.append(f"{url}: {type(e).__name__}")
    if len(rows) < 400:
        raise DataError("could not download the NIFTY 500 list: " + "; ".join(errors))
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "sector", "name"])
        for row in rows:
            w.writerow([row["Symbol"].strip(), row.get("Industry", "UNKNOWN").strip(), (row.get("Company Name") or "").strip()])
    return len(rows)


# ---------------------------------------------------------------------------
# Earnings / corporate events
# ---------------------------------------------------------------------------
def load_events_csv(path: str | Path) -> dict[str, list[date]]:
    out: dict[str, list[date]] = {}
    p = Path(path)
    if not p.exists():
        return out
    with open(p, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                out.setdefault(row["symbol"].strip(), []).append(date.fromisoformat(row["date"].strip()))
            except (KeyError, ValueError):
                continue
    return out


def fetch_nse_events() -> dict[str, list[date]]:
    """Best-effort download of NSE's board-meeting / results calendar.

    NSE's website sometimes blocks automated clients. Callers must treat an
    exception as "calendar unknown" (the risk engine then halves position size).
    """
    s = requests.Session()
    s.headers.update(UA)
    s.get("https://www.nseindia.com/", timeout=20)  # sets cookies
    r = s.get(NSE_EVENTS_URL, timeout=20)
    r.raise_for_status()
    out: dict[str, list[date]] = {}
    for item in r.json():
        sym = item.get("symbol")
        d = item.get("date") or item.get("bm_date")
        if not sym or not d:
            continue
        try:
            out.setdefault(sym, []).append(datetime.strptime(d, "%d-%b-%Y").date())
        except ValueError:
            continue
    if not out:
        raise DataError("NSE event calendar returned no events")
    return out


def events_calendar(settings) -> tuple[dict[str, list[date]], bool]:
    """Merge the NSE calendar with config/events.csv. Returns (events, calendar_ok)."""
    manual = load_events_csv(settings.get("data.events_file", "config/events.csv"))
    try:
        auto = fetch_nse_events()
        ok = True
    except Exception as e:
        log.warning("earnings calendar unavailable: %s", e)
        auto, ok = {}, False
    for sym, ds in manual.items():
        auto.setdefault(sym, []).extend(ds)
    return auto, ok
