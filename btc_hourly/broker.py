"""Paper broker. Raises if LIVE_TRADING is ever True. No real order code."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import config
import db

PT = ZoneInfo("America/Los_Angeles")


@dataclass
class PaperFill:
    ticker: str
    side: str
    qty: int
    price: float
    strike: Optional[float]
    event_ticker: str
    close_time: str
    cost: float
    model_p: float
    market_ask: float
    edge: float


class Broker:
    def __init__(self) -> None:
        if config.LIVE_TRADING:
            raise RuntimeError(
                "LIVE_TRADING is True — refusing to construct broker. "
                "Paper only. Never place live orders."
            )
        db.ensure_seeded()

    def equity_and_open(self) -> tuple[float, float, list]:
        cash = db.cash()
        with db.db() as conn:
            opens = conn.execute(
                "SELECT * FROM trades WHERE status='open' ORDER BY id"
            ).fetchall()
        open_cost = sum(float(r["cost"]) for r in opens)
        # mark-at-cost equity
        equity = cash + open_cost
        return cash, equity, list(opens)

    def kill_tripped(self) -> bool:
        _, equity, _ = self.equity_and_open()
        return equity < config.KILL_EQUITY

    def buy_yes_paper(self, fill: PaperFill) -> None:
        if config.LIVE_TRADING:
            raise RuntimeError("Refusing live order path — LIVE_TRADING must stay False")
        # No real order API exists in this module by design.
        ts = datetime.now(PT).isoformat()
        # Single transaction: cash debit + trade insert are atomic (no nested connections).
        with db.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM meta WHERE key='cash'").fetchone()
            cash = float(row["value"]) if row else config.STARTING_CASH
            if fill.cost > cash + 1e-9:
                raise ValueError(f"insufficient paper cash: need {fill.cost:.2f} have {cash:.2f}")
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('cash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"{cash - fill.cost:.6f}",),
            )
            conn.execute(
                """INSERT INTO trades
                (ts,ticker,side,qty,price,strike,event_ticker,close_time,cost,model_p,market_ask,edge,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'open')""",
                (
                    ts,
                    fill.ticker,
                    fill.side,
                    fill.qty,
                    fill.price,
                    fill.strike,
                    fill.event_ticker,
                    fill.close_time,
                    fill.cost,
                    fill.model_p,
                    fill.market_ask,
                    fill.edge,
                ),
            )

    def settle_due(self, spot: float) -> list[dict]:
        """Settle open trades whose close_time has passed using spot vs strike (YES wins if spot > strike)."""
        now = datetime.now(PT)
        settled = []
        with db.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            opens = conn.execute("SELECT * FROM trades WHERE status='open'").fetchall()
            for r in opens:
                try:
                    ct = datetime.fromisoformat(r["close_time"])
                except Exception:
                    continue
                if ct.tzinfo is None:
                    from datetime import timezone
                    ct = ct.replace(tzinfo=timezone.utc).astimezone(PT)
                if ct > now:
                    continue
                strike = r["strike"]
                side = r["side"]
                qty = int(r["qty"])
                price = float(r["price"])
                cost = float(r["cost"])
                yes_wins = strike is not None and spot > float(strike)
                if side == "YES":
                    win = yes_wins
                else:
                    win = not yes_wins
                payout = qty * 1.0 if win else 0.0
                pnl = payout - cost
                # Same connection for cash + trade update (nested db.set_cash locks SQLite).
                row = conn.execute("SELECT value FROM meta WHERE key='cash'").fetchone()
                cash = float(row["value"]) if row else config.STARTING_CASH
                # cash already reduced by cost at entry; add payout
                cash = cash + payout
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('cash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"{cash:.6f}",),
                )
                conn.execute(
                    "UPDATE trades SET status=?, pnl=?, settled_ts=? WHERE id=?",
                    ("won" if win else "lost", pnl, now.isoformat(), r["id"]),
                )
                settled.append({"id": r["id"], "ticker": r["ticker"], "pnl": pnl, "win": win})
        return settled
