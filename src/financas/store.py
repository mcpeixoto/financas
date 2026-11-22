"""SQLite history: one snapshot per run, transactions deduped by (source, ext_id)."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .config import DATA
from .models import FetchResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS balances(run_id INT, source TEXT, account TEXT, currency TEXT,
    amount TEXT, kind TEXT, amount_eur TEXT);
CREATE TABLE IF NOT EXISTS positions(run_id INT, source TEXT, account TEXT, symbol TEXT,
    name TEXT, quantity TEXT, value TEXT, currency TEXT, cost_basis TEXT, value_eur TEXT);
CREATE TABLE IF NOT EXISTS transactions(source TEXT, account TEXT, booked TEXT, amount TEXT,
    currency TEXT, description TEXT, ext_id TEXT, first_seen_run INT,
    PRIMARY KEY(source, ext_id));
CREATE TABLE IF NOT EXISTS cashflows(source TEXT, booked TEXT, amount_eur TEXT, kind TEXT,
    description TEXT, ext_id TEXT, PRIMARY KEY(source, ext_id));
CREATE TABLE IF NOT EXISTS prices(symbol TEXT, day TEXT, close TEXT, currency TEXT,
    PRIMARY KEY(symbol, day));
CREATE TABLE IF NOT EXISTS trades(source TEXT, booked TEXT, symbol TEXT, name TEXT, isin TEXT,
    asset_class TEXT, quantity TEXT, price TEXT, currency TEXT, net_cash TEXT, fx_to_eur TEXT,
    realized_pnl_eur TEXT, ext_id TEXT, PRIMARY KEY(source, ext_id));
CREATE TABLE IF NOT EXISTS tickers(source TEXT, symbol TEXT, yahoo TEXT, currency TEXT,
    PRIMARY KEY(source, symbol));
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DATA / "financas.db"
    path.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def new_run(db: sqlite3.Connection, at: datetime | None = None) -> int:
    cur = db.execute("INSERT INTO runs(at) VALUES(?)", ((at or datetime.now()).isoformat(timespec="seconds"),))
    return cur.lastrowid


def save(db: sqlite3.Connection, run_id: int, res: FetchResult, to_eur) -> int:
    """Persist one connector's result. `to_eur(amount, currency)` -> Decimal. Returns new tx count."""
    for b in res.balances:
        db.execute("INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
                   (run_id, b.source, b.account, b.currency, str(b.amount), b.kind,
                    str(to_eur(b.amount, b.currency))))
    for p in res.positions:
        db.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (run_id, p.source, p.account, p.symbol, p.name, str(p.quantity), str(p.value),
                    p.currency, None if p.cost_basis is None else str(p.cost_basis),
                    str(to_eur(p.value, p.currency))))
    new = 0
    for t in res.transactions:
        cur = db.execute("INSERT OR IGNORE INTO transactions VALUES(?,?,?,?,?,?,?,?)",
                         (t.source, t.account, t.booked.isoformat(), str(t.amount), t.currency,
                          t.description, t.ext_id, run_id))
        new += cur.rowcount
    db.commit()
    return new


def _inserted(db: sqlite3.Connection, before: int) -> int:
    return db.total_changes - before


def save_transactions(db: sqlite3.Connection, rows, run_id: int | None = None) -> int:
    """Insere movimentos novos. Não cria uma corrida. Devolve quantos ficaram gravados."""
    before = db.total_changes
    for t in rows:
        db.execute(
            "INSERT OR IGNORE INTO transactions VALUES(?,?,?,?,?,?,?,?)",
            (t.source, t.account, t.booked.isoformat(), str(t.amount), t.currency,
             t.description, t.ext_id, run_id),
        )
    db.commit()
    return _inserted(db, before)


def save_cashflows(db: sqlite3.Connection, rows) -> int:
    """INSERT OR IGNORE. Devolve o número de fluxos novos."""
    before = db.total_changes
    for row in rows:
        db.execute(
            """INSERT OR IGNORE INTO cashflows(source, booked, amount_eur, kind, description, ext_id)
               VALUES(?,?,?,?,?,?)""",
            (row.source, row.booked.isoformat(), str(row.amount_eur), row.kind,
             row.description, row.ext_id),
        )
    db.commit()
    return _inserted(db, before)


def save_trades(db: sqlite3.Connection, rows) -> int:
    """INSERT OR IGNORE. Devolve o número de negócios novos."""
    before = db.total_changes
    for row in rows:
        db.execute(
            """INSERT OR IGNORE INTO trades(
                   source, booked, symbol, name, isin, asset_class, quantity, price,
                   currency, net_cash, fx_to_eur, realized_pnl_eur, ext_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row.source, row.booked.isoformat(), row.symbol, row.name, row.isin, row.asset_class,
             str(row.quantity), str(row.price), row.currency, str(row.net_cash), str(row.fx_to_eur),
             None if row.realized_pnl_eur is None else str(row.realized_pnl_eur), row.ext_id),
        )
    db.commit()
    return _inserted(db, before)


def save_prices(db: sqlite3.Connection, symbol: str, currency: str, rows: dict[date, Decimal]) -> int:
    """Grava preços diários. Repetições do mesmo dia não criam outra linha."""
    before = db.total_changes
    for day, close in rows.items():
        db.execute(
            "INSERT OR IGNORE INTO prices(symbol, day, close, currency) VALUES(?,?,?,?)",
            (symbol, day.isoformat(), str(close), currency),
        )
    db.commit()
    return _inserted(db, before)
