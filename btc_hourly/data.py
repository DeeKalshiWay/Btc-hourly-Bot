"""Public market data fetches for the paper bot (no trading auth)."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import config

TIMEOUT = 20


def _get_json(url: str) -> Any:
    req = Request(url, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=TIMEOUT) as resp:
        import json
        return json.loads(resp.read().decode())


def fetch_spot() -> dict:
    """Median-ish of Coinbase + Kraken BTC-USD."""
    out: dict[str, float] = {}
    try:
        j = _get_json(config.COINBASE_SPOT_URL)
        out["coinbase"] = float(j["data"]["amount"])
    except Exception as e:
        out["coinbase_error"] = str(e)  # type: ignore
    try:
        j = _get_json(config.KRAKEN_TICKER_URL)
        result = j.get("result") or {}
        key = next(iter(result))
        out["kraken"] = float(result[key]["c"][0])
    except Exception as e:
        out["kraken_error"] = str(e)  # type: ignore
    prices = [v for k, v in out.items() if isinstance(v, float)]
    out["spot"] = float(sum(prices) / len(prices)) if prices else float("nan")
    return out


def fetch_dvol() -> dict:
    """Deribit DVOL latest point (annualized % vol)."""
    end = int(time.time() * 1000)
    start = end - 3 * 24 * 3600 * 1000
    url = f"{config.DERIBIT_DVOL_URL}&start_timestamp={start}&end_timestamp={end}"
    try:
        j = _get_json(url)
        data = (j.get("result") or {}).get("data") or []
        if not data:
            return {"dvol": None, "dvol_prev": None}
        # each row: [ts, open, high, low, close]
        closes = [float(row[4]) for row in data if len(row) >= 5]
        dvol = closes[-1] if closes else None
        dvol_prev = closes[-2] if len(closes) >= 2 else dvol
        return {"dvol": dvol, "dvol_prev": dvol_prev, "n": len(closes)}
    except Exception as e:
        return {"dvol": None, "dvol_prev": None, "error": str(e)}


def fetch_coinbase_candles_1m(limit: int = 240) -> list[tuple[int, float]]:
    """Return [(unix_ts, close), ...] oldest->newest via Coinbase exchange candles."""
    # Coinbase Advanced public candles: granularity 60
    end = int(time.time())
    start = end - limit * 60
    url = (
        "https://api.exchange.coinbase.com/products/BTC-USD/candles"
        f"?granularity=60&start={start}&end={end}"
    )
    try:
        rows = _get_json(url)
        # [time, low, high, open, close, volume], newest first
        pts = sorted(((int(r[0]), float(r[4])) for r in rows), key=lambda x: x[0])
        return pts[-limit:]
    except Exception:
        return []


def fetch_kalshi_hourly_markets(limit: int = 200) -> list[dict]:
    """Open KXBTCD markets (hourly BTC above/below strikes)."""
    params = {
        "series_ticker": config.KALSHI_SERIES,
        "status": "open",
        "limit": str(limit),
    }
    url = f"{config.KALSHI_MARKETS_URL}?{urlencode(params)}"
    try:
        j = _get_json(url)
        return list(j.get("markets") or [])
    except Exception as e:
        return [{"_error": str(e)}]


def parse_strike(ticker: str) -> Optional[float]:
    """KXBTCD-26NOV0514-T76399.99 -> 76399.99"""
    try:
        if "-T" in ticker:
            return float(ticker.rsplit("-T", 1)[1])
    except Exception:
        return None
    return None


def market_yes_ask(m: dict) -> Optional[float]:
    """Best available YES ask in dollars (0-1). Kalshi quotes in cents sometimes."""
    for key in ("yes_ask_dollars", "yes_ask"):
        v = m.get(key)
        if v is None:
            continue
        try:
            x = float(v)
            if key == "yes_ask" and x > 1.5:  # cents
                x = x / 100.0
            if 0 < x < 1:
                return x
        except Exception:
            continue
    # fallback mid from bid/ask
    try:
        bid = m.get("yes_bid_dollars") or m.get("yes_bid")
        ask = m.get("yes_ask_dollars") or m.get("yes_ask")
        if bid is not None and ask is not None:
            b, a = float(bid), float(ask)
            if b > 1.5:
                b /= 100.0
            if a > 1.5:
                a /= 100.0
            if a > 0:
                return a
    except Exception:
        pass
    return None


def close_time_utc(m: dict) -> Optional[datetime]:
    for key in ("close_time", "expected_expiration_time", "expiration_time"):
        raw = m.get(key)
        if not raw:
            continue
        try:
            if isinstance(raw, (int, float)):
                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
            s = str(raw).replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        except Exception:
            continue
    return None
