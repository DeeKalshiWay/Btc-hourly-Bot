"""SQLite state for paper bot."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  qty INTEGER NOT NULL,
  price REAL NOT NULL,
  strike REAL,
  event_ticker TEXT,
  close_time TEXT,
  cost REAL NOT NULL,
  model_p REAL,
  market_ask REAL,
  edge REAL,
  status TEXT NOT NULL DEFAULT 'open',
  pnl REAL,
  settled_ts TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  spot REAL,
  dvol REAL,
  sigma REAL,
  note TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def cash() -> float:
    v = get_meta("cash")
    return float(v) if v is not None else config.STARTING_CASH


def set_cash(x: float) -> None:
    set_meta("cash", f"{x:.6f}")


def ensure_seeded() -> None:
    if get_meta("cash") is None:
        set_cash(config.STARTING_CASH)
        set_meta("starting_cash", str(config.STARTING_CASH))
        set_meta("live_trading", "False")
