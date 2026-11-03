"""SQLite store: bot state, positions, pending orders, trade journal, event log.

The broker is the source of truth for what you actually own; this store is the
bot's memory of *why* it owns it (stops, targets, setup, R-multiples).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ClosedTrade, Position, TradePlan
from .risk import AccountState

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_entries (order_id TEXT PRIMARY KEY, symbol TEXT, data TEXT NOT NULL, placed_at TEXT);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL, closed_at TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, message TEXT);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- key/value -------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value, default=str)))

    # -- account state -----------------------------------------------------------
    def account(self) -> AccountState | None:
        d = self.get("account")
        return AccountState.from_dict(d) if d else None

    def save_account(self, a: AccountState) -> None:
        self.put("account", a.to_dict())

    # -- positions ---------------------------------------------------------------
    def positions(self) -> list[Position]:
        with self._conn() as c:
            rows = c.execute("SELECT data FROM positions").fetchall()
        return [Position.from_dict(json.loads(r[0])) for r in rows]

    def save_position(self, p: Position) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO positions(symbol,data) VALUES(?,?)", (p.symbol, json.dumps(p.to_dict())))

    def delete_position(self, symbol: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

    # -- pending entry orders ------------------------------------------------------
    def pending(self) -> list[tuple[str, TradePlan]]:
        with self._conn() as c:
            rows = c.execute("SELECT order_id, data FROM pending_entries").fetchall()
        return [(oid, TradePlan.from_dict(json.loads(d))) for oid, d in rows]

    def add_pending(self, order_id: str, plan: TradePlan) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO pending_entries VALUES(?,?,?,?)",
                      (order_id, plan.symbol, json.dumps(plan.to_dict()), datetime.now().isoformat()))

    def remove_pending(self, order_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM pending_entries WHERE order_id=?", (order_id,))

    # -- plans for the next session ------------------------------------------------
    def save_plans(self, plans: list[TradePlan], for_day: str) -> None:
        self.put("plans", {"for_day": for_day, "plans": [p.to_dict() for p in plans]})

    def plans(self) -> tuple[str | None, list[TradePlan]]:
        d = self.get("plans") or {}
        return d.get("for_day"), [TradePlan.from_dict(p) for p in d.get("plans", [])]

    # -- journal -------------------------------------------------------------------
    def add_trade(self, t: ClosedTrade) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO trades(data, closed_at) VALUES(?,?)", (json.dumps(t.to_dict()), t.exit_date.isoformat()))

    def trades(self) -> list[dict]:
        with self._conn() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT data FROM trades ORDER BY id").fetchall()]

    def log(self, kind: str, message: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO events(ts,kind,message) VALUES(?,?,?)", (datetime.now().isoformat(), kind, message))

    def recent_events(self, n: int = 30) -> list[tuple[str, str, str]]:
        with self._conn() as c:
            return c.execute("SELECT ts, kind, message FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
