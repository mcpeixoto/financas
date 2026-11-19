"""SQLite history: one snapshot per run, transactions deduped by (source, ext_id)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
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
