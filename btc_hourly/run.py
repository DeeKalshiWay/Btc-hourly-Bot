#!/usr/bin/env python3
"""Paper-only Kalshi hourly BTC bot. Never places live orders."""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import data as market_data
import db
import edge_checks
import model
from broker import Broker, PaperFill

PT = ZoneInfo("America/Los_Angeles")


def _status_text(broker: Broker) -> str:
    cash, equity, opens = broker.equity_and_open()
    lines = [
        f"as_of_pt={datetime.now(PT).strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"LIVE_TRADING={config.LIVE_TRADING}",
        f"cash={cash:.2f}",
        f"equity_mark_at_cost={equity:.2f}",
        f"open_positions={len(opens)}",
        f"kill_switch_armed_below={config.KILL_EQUITY}",
        f"kill_tripped={broker.kill_tripped()}",
        f"MIN_PRICE={config.MIN_PRICE}",
        f"EDGE_BUFFER={config.EDGE_BUFFER}",
        f"db={config.DB_PATH}",
    ]
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
        w = conn.execute("SELECT COUNT(*) AS c FROM trades WHERE status='won'").fetchone()["c"]
        l = conn.execute("SELECT COUNT(*) AS c FROM trades WHERE status='lost'").fetchone()["c"]
    lines.append(f"trades_total={n} won={w} lost={l} (history reset on 2026-11-05 restore)")
    if opens:
        lines.append("open:")
        for r in opens[:20]:
            lines.append(
                f"  {r['ticker']} {r['side']} x{r['qty']} @{r['price']:.2f} cost={r['cost']:.2f}"
            )
    return "\n".join(lines)


def run_once(verbose: bool = True) -> int:
    if config.LIVE_TRADING:
        print("REFUSING: LIVE_TRADING is True", file=sys.stderr)
        return 2
    broker = Broker()
    spot_info = market_data.fetch_spot()
    spot = float(spot_info.get("spot") or float("nan"))
    if not math.isfinite(spot):
        print("DATA FAIL: no spot", spot_info)
        return 1

    # settle anything due
    settled = broker.settle_due(spot)
    if verbose and settled:
        print(f"settled {len(settled)} paper trades: {settled}")

    meta_cash, recon_cash = __import__("db").ledger_check()
    if abs(meta_cash - recon_cash) > 0.01:
        print(f"LEDGER MISMATCH: meta cash {meta_cash:.2f} vs reconstructed {recon_cash:.2f}; no new paper risk", file=sys.stderr)
        return 3

    if broker.kill_tripped():
        print(f"KILL SWITCH: equity below {config.KILL_EQUITY}; no new paper risk")
        print(_status_text(broker))
        return 0

    dvol_info = market_data.fetch_dvol()
    candles = market_data.fetch_coinbase_candles_1m(240)
    closes = [c for _, c in candles]
    rv = model.realized_vol_annual(closes)
    # short vs long RV for shock
    short_rv = model.realized_vol_annual(closes[-30:]) if len(closes) >= 30 else rv
    long_rv = model.realized_vol_annual(closes) if closes else rv
    dvol = dvol_info.get("dvol")
    dvol_prev = dvol_info.get("dvol_prev")
    sigma = model.blend_vol(rv, dvol)

    shock = edge_checks.vol_shock_buffer(dvol, dvol_prev, short_rv, long_rv)
    edge_buffer = shock.edge_buffer

    macro = edge_checks.in_macro_skip()
    if not macro.ok:
        print(f"SKIP: {macro.reason}")
        with db.db() as conn:
            conn.execute(
                "INSERT INTO runs(ts,spot,dvol,sigma,note) VALUES (?,?,?,?,?)",
                (datetime.now(PT).isoformat(), spot, dvol, sigma, macro.reason),
            )
        print(_status_text(broker))
        return 0

    markets = market_data.fetch_kalshi_hourly_markets()
    if markets and markets[0].get("_error"):
        print("DATA FAIL: Kalshi", markets[0])
        return 1

    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        ticker = m.get("ticker") or ""
        strike = market_data.parse_strike(ticker)
        ask = market_data.market_yes_ask(m)
        ct = market_data.close_time_utc(m)
        if strike is None or ask is None or ct is None:
            continue
        # also re-check macro against event close
        event_end_pt = ct.astimezone(PT)
        m2 = edge_checks.in_macro_skip(event_end_pt=event_end_pt)
        if not m2.ok:
            continue
        minutes_left = (ct - now).total_seconds() / 60.0
        # Trade the next few hourly events (up to ~4h); skip already closed.
        if minutes_left <= 0 or minutes_left > 240:
            continue
        tg = edge_checks.minutes_to_close_ok(minutes_left)
        if not tg.ok:
            continue
        pg = edge_checks.price_gate(ask)
        if not pg.ok:
            continue
        tau = max(minutes_left / (365.25 * 24 * 60), 1e-8)
        p = model.p_above(spot, strike, sigma, tau)
        # also consider NO side
        yes_ev = model.ev_yes(p, ask, edge_buffer)
        no_ask = None
        # derive NO ask ~ 1 - yes_bid; approximate with 1 - ask + spread guess
        yes_bid = None
        try:
            yb = m.get("yes_bid_dollars") or m.get("yes_bid")
            if yb is not None:
                yes_bid = float(yb)
                if yes_bid > 1.5:
                    yes_bid /= 100.0
        except Exception:
            pass
        if yes_bid is not None and 0 < yes_bid < 1:
            no_ask = max(0.01, 1.0 - yes_bid)
        else:
            no_ask = max(0.01, 1.0 - ask)
        no_p = 1.0 - p
        no_ev = model.ev_yes(no_p, no_ask, edge_buffer) if edge_checks.price_gate(no_ask).ok else -1e9

        if yes_ev >= no_ev and yes_ev > 0:
            candidates.append(
                {
                    "ticker": ticker,
                    "side": "YES",
                    "ask": ask,
                    "p": p,
                    "ev": yes_ev,
                    "strike": strike,
                    "event": m.get("event_ticker") or ticker.rsplit("-", 1)[0],
                    "close_time": ct.astimezone(PT).isoformat(),
                    "minutes_left": minutes_left,
                }
            )
        elif no_ev > 0:
            candidates.append(
                {
                    "ticker": ticker,
                    "side": "NO",
                    "ask": no_ask,
                    "p": no_p,
                    "ev": no_ev,
                    "strike": strike,
                    "event": m.get("event_ticker") or ticker.rsplit("-", 1)[0],
                    "close_time": ct.astimezone(PT).isoformat(),
                    "minutes_left": minutes_left,
                }
            )

    candidates.sort(key=lambda c: c["ev"], reverse=True)

    # top N overall, but cap per event
    picked = []
    spent_by_event: dict[str, float] = {}
    cash, equity, _ = broker.equity_and_open()
    for c in candidates:
        if len(picked) >= config.TOP_N_CONTRACTS:
            break
        ev_spent = spent_by_event.get(c["event"], 0.0)
        if ev_spent >= config.MAX_STAKE_PER_EVENT:
            continue
        f_star = model.kelly_fraction(c["p"], c["ask"])
        stake = min(
            config.MAX_STAKE_PER_TRADE,
            config.MAX_STAKE_PER_EVENT - ev_spent,
            config.KELLY_FRACTION * f_star * equity,
            cash * 0.5,
        )
        if stake < 1.0 or c["ask"] <= 0:
            continue
        qty = max(1, int(stake / c["ask"]))
        cost = qty * c["ask"]
        # fees already in EV; paper pays ask (taker)
        if cost > cash:
            qty = int(cash / c["ask"])
            cost = qty * c["ask"]
        if qty < 1 or cost < 0.5:
            continue
        fill = PaperFill(
            ticker=c["ticker"],
            side=c["side"],
            qty=qty,
            price=c["ask"],
            strike=c["strike"],
            event_ticker=c["event"],
            close_time=c["close_time"],
            cost=cost,
            model_p=c["p"],
            market_ask=c["ask"],
            edge=c["ev"],
        )
        try:
            broker.buy_yes_paper(fill)
        except Exception as e:
            print(f"paper fill skipped: {e}")
            continue
        cash -= cost
        spent_by_event[c["event"]] = ev_spent + cost
        picked.append(fill)
        if verbose:
            print(
                f"PAPER {fill.side} {fill.ticker} x{fill.qty} @{fill.price:.2f} "
                f"cost={fill.cost:.2f} p={fill.model_p:.3f} ev={fill.edge:.3f}"
            )

    note = (
        f"spot={spot:.2f} dvol={dvol} sigma={sigma:.3f} buffer={edge_buffer:.3f} "
        f"shock={shock.reason} candidates={len(candidates)} filled={len(picked)} "
        f"markets={len(markets)}"
    )
    with db.db() as conn:
        conn.execute(
            "INSERT INTO runs(ts,spot,dvol,sigma,note) VALUES (?,?,?,?,?)",
            (datetime.now(PT).isoformat(), spot, dvol, sigma, note),
        )
    if verbose:
        print(note)
        print(_status_text(broker))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Paper-only BTC hourly Kalshi bot")
    ap.add_argument("--once", action="store_true", help="Run one paper cycle")
    ap.add_argument("--status", action="store_true", help="Print paper status")
    args = ap.parse_args()
    if config.LIVE_TRADING:
        print("REFUSING: LIVE_TRADING True", file=sys.stderr)
        return 2
    if args.status or not args.once:
        if args.status and not args.once:
            broker = Broker()
            print(_status_text(broker))
            return 0
    if args.once:
        return run_once()
    # default: status
    broker = Broker()
    print(_status_text(broker))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
