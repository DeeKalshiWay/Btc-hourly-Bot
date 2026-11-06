#!/usr/bin/env python3
"""One paper tick of the BTC nano desk. Live switch stays off."""
import json, math, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "desk.json"
JOURNAL = ROOT / "state" / "journal.log"
UA = "PaperDesk/1.0 (paper trading desk)"
CB = "https://api.coinbase.com/api/v3/brokerage/market"
CONTRACT = 0.01
TAKER = 0.001
FCM = 0.0002
ROUND = TAKER + FCM
BUFFER = 0.92
CAP = 15_000
BASIS_RICH = 0.006
BASIS_CHEAP = -0.002
HARD_BEAR = 32
HARD_BULL = 68
LONG_SCORE = 51
SHORT_SCORE = 49
ROLL_DAYS = 7
FRONT = ["BIT-27NOV26-CDE", "BIT-24DEC26-CDE", "BIT-29JAN27-CDE"]

def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read().decode())

def session(now=None):
    now = now or datetime.now(timezone.utc)
    et = now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    d = et.weekday()  # Mon 0
    minutes = et.hour * 60 + et.minute
    if d == 5:
        return "overnight"
    if d == 6 and minutes < 18 * 60:
        return "overnight"
    if d == 4 and minutes >= 16 * 60:
        return "overnight"
    if 16 * 60 <= minutes < 18 * 60:
        return "overnight"
    return "intraday"

def ema(values, period):
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0
    gain = loss = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gain += diff if diff >= 0 else 0
        loss += -diff if diff < 0 else 0
    gain /= period
    loss /= period
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        g = diff if diff > 0 else 0
        l = -diff if diff < 0 else 0
        gain = (gain * (period - 1) + g) / period
        loss = (loss * (period - 1) + l) / period
    if loss == 0:
        return 100.0
    rs = gain / loss
    return 100 - 100 / (1 + rs)

def parse_candles(raw):
    rows = raw.get("candles") if isinstance(raw, dict) else raw
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            t = float(row["start"]); o=float(row["open"]); h=float(row["high"]); l=float(row["low"]); c=float(row["close"]); v=float(row["volume"])
        else:
            t,l,h,o,c,v = map(float, row[:6])
        out.append({"t": t * (1000 if t < 10_000_000_000 else 1), "o": o, "h": h, "l": l, "c": c, "v": v})
    out.sort(key=lambda x: x["t"])
    return out[-72:]

def pattern(candles):
    if len(candles) < 16:
        return "thin tape"
    recent, prior = candles[-8:], candles[-16:-8]
    rec_h, rec_l = max(c["h"] for c in recent), min(c["l"] for c in recent)
    pri_h, pri_l = max(c["h"] for c in prior), min(c["l"] for c in prior)
    last, prev = candles[-1], candles[-2]
    window = candles[-21:-1]
    max20 = max(c["h"] for c in window) if window else rec_h
    min20 = min(c["l"] for c in window) if window else rec_l
    if last["c"] > max20: return "range break high"
    if last["c"] < min20: return "range break low"
    if last["h"] < prev["h"] and last["l"] > prev["l"]: return "inside bar"
    if rec_h > pri_h and rec_l > pri_l: return "higher highs, higher lows"
    if rec_h < pri_h and rec_l < pri_l: return "lower highs, lower lows"
    if rec_h > pri_h and rec_l < pri_l: return "expanding range"
    return "coiling"

def score_tech(candles):
    closes = [c["c"] for c in candles]
    price = closes[-1]
    e12, e26 = ema(closes, 12), ema(closes, 26)
    macd_line = [a - b for a, b in zip(e12, e26)]
    signal = ema(macd_line, 9)
    macd, macd_sig = macd_line[-1], signal[-1]
    hist = macd - macd_sig
    rsi_v = rsi(closes)
    spread = e12[-1] - e26[-1]
    trend = "flat" if abs(spread) / max(price, 1) < 0.0015 else ("up" if spread > 0 else "down")
    pat = pattern(candles)
    score = 50
    score += 16 if trend == "up" else (-16 if trend == "down" else 0)
    score += 10 if hist > 0 else -8
    if 45 <= rsi_v <= 62: score += 10
    elif 62 < rsi_v <= 72: score += 4 if trend == "up" else -6
    elif rsi_v > 72: score -= 12
    elif rsi_v < 32: score += -6 if trend == "down" else 8
    elif rsi_v < 45: score += 6 if trend == "up" else -4
    if "higher highs" in pat: score += 8
    if "lower highs" in pat: score -= 8
    if pat == "range break high": score += 10
    if pat == "range break low": score -= 10
    change8 = (price - closes[-9]) / closes[-9] if len(closes) > 8 else 0
    if change8 > 0.015: score += 6
    if change8 < -0.015: score -= 6
    score = max(2, min(98, round(score)))
    signal = "wait"
    if score >= LONG_SCORE and trend != "down" and rsi_v < 78: signal = "long"
    elif score <= SHORT_SCORE and trend != "up" and rsi_v > 22: signal = "short"
    return {"score": score, "signal": signal, "trend": trend, "rsi": rsi_v, "pattern": pat, "note": f"EMA {trend} RSI {rsi_v:.0f} {pat}"}

def pick_product():
    for pid in FRONT:
        p = get(f"{CB}/products/{pid}")
        fd = p.get("future_product_details") or {}
        ttl = float(fd.get("time_to_expiry_ms") or 0) / 1000
        price = float(p.get("price") or 0)
        if price <= 0 or ttl <= 0:
            continue
        if ttl < ROLL_DAYS * 86400:
            continue
        return p, pid
    for pid in reversed(FRONT):
        p = get(f"{CB}/products/{pid}")
        if float(p.get("price") or 0) > 0:
            return p, pid
    raise SystemExit("no live BIT contract")

def target_qty(equity, price, rate):
    if equity <= 0 or price <= 0 or rate <= 0:
        return 0
    contract_value = price * CONTRACT
    exposure = min((equity * BUFFER) / rate, CAP) * (1 - ROUND * 2)
    qty = math.floor(exposure / contract_value)
    min_margin = contract_value * rate + contract_value * ROUND
    if qty < 1 and equity >= min_margin:
        return 1
    return max(0, qty)

def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {
        "cash": 1000.0,
        "equity": 1000.0,
        "dayOpenEquity": 1000.0,
        "day": "",
        "position": None,
        "live": False,
        "started": datetime.now(timezone.utc).isoformat(),
    }

def mark(desk, price):
    pos = desk.get("position")
    upl = 0.0
    margin = 0.0
    if pos:
        delta = (price - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - price)
        upl = delta * pos["qty"] * CONTRACT
        margin = pos["margin"]
    desk["upl"] = round(upl, 4)
    desk["equity"] = round(desk["cash"] + margin + upl, 4)
    return desk

def main():
    desk = load()
    assert desk.get("live") is not True
    product, pid = pick_product()
    end = int(time.time())
    start = end - 3 * 86400
    candles = parse_candles(get(f"{CB}/products/{pid}/candles?granularity=ONE_HOUR&start={start}&end={end}"))
    spot = get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    price = float(product.get("price") or candles[-1]["c"])
    spot_px = float(spot.get("price") or 0)
    chg = float(product.get("price_percentage_change_24h") or 0) / 100
    fd = product.get("future_product_details") or {}
    sess = session()
    intra = (fd.get("intraday_margin_rate") or {})
    over = (fd.get("overnight_margin_rate") or {})
    rate_long = float((intra if sess == "intraday" else over).get("long_margin_rate") or (0.0999375 if sess == "intraday" else 0.234375))
    rate_short = float((intra if sess == "intraday" else over).get("short_margin_rate") or rate_long)
    days = float(fd.get("time_to_expiry_ms") or 0) / 86_400_000
    basis = (price - spot_px) / spot_px if spot_px else 0
    tech = score_tech(candles) if len(candles) >= 26 else {"score": 50, "signal": "wait", "trend": "flat", "rsi": 50, "pattern": "thin tape", "note": "thin tape"}
    side = tech["signal"] if tech["signal"] != "wait" else ("long" if tech["score"] >= 50 else "short")
    rate = rate_long if side == "long" else rate_short
    closed = "CLOSE" in str((product.get("fcm_trading_session_details") or {}).get("session_state") or "")
    now = datetime.now(timezone.utc)
    day = now.astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles")).date().isoformat()
    if desk.get("day") != day:
        desk["day"] = day
        desk["dayOpenEquity"] = desk["equity"]
    desk = mark(desk, price)
    pos = desk.get("position")
    intent = "hold"
    note = "Flat."
    fee = 0.0
    hard = False
    if pos:
        hard = (pos["side"] == "long" and tech["score"] <= HARD_BEAR) or (pos["side"] == "short" and tech["score"] >= HARD_BULL)
    rich = side == "long" and basis > BASIS_RICH
    if desk["equity"] > 1 and not closed:
        qty = target_qty(desk["equity"], price, rate)
        if not pos:
            if qty >= 1 and not rich:
                exposure = qty * price * CONTRACT
                fee = exposure * ROUND
                margin = exposure * rate
                slip = 0.01 * 0.35
                entry = price + slip if side == "long" else price - slip
                desk["cash"] = round(desk["cash"] - margin - fee, 4)
                desk["position"] = {
                    "side": side, "qty": qty, "entry": round(entry, 2), "margin": round(margin, 4),
                    "productId": pid, "openedAt": now.isoformat(), "session": sess,
                }
                intent = "open"
                note = f"Open {side} {qty} {pid}"
            else:
                note = "Flat. " + ("Nano rich vs spot \u2014 no add." if rich else "Size below 1 contract.")
        elif hard:
            old = pos
            old_exp = old["qty"] * price * CONTRACT
            close_fee = old_exp * ROUND
            upl = desk["upl"]
            desk["cash"] = round(desk["cash"] + old["margin"] + upl - close_fee, 4)
            desk["position"] = None
            desk = mark(desk, price)
            qty = target_qty(desk["equity"], price, rate)
            if qty >= 1:
                exposure = qty * price * CONTRACT
                fee = close_fee + exposure * ROUND
                margin = exposure * rate
                entry = price + 0.0035 if side == "long" else price - 0.0035
                desk["cash"] = round(desk["cash"] - margin - exposure * ROUND, 4)
                desk["position"] = {
                    "side": side, "qty": qty, "entry": round(entry, 2), "margin": round(margin, 4),
                    "productId": pid, "openedAt": now.isoformat(), "session": sess,
                }
                intent = "flip"
                note = f"Hard reverse into {side} {qty}"
            else:
                intent = "flatten"
                fee = close_fee
                note = "Hard reverse, not enough margin to reopen."
        elif pos.get("productId") != pid:
            intent = "roll"
            note = f"Roll flag {pos.get('productId')} -> {pid}. Held this tick."
        else:
            intent = "hold"
            note = f"Hold {pos['side']} {pos['qty']} {pos['productId']}. No 30-min churn."
    elif closed:
        note = "Session closed. No new ticket."
    desk = mark(desk, price)
    desk["last"] = {
        "at": now.isoformat(),
        "product": pid,
        "price": price,
        "spot": spot_px,
        "basis": round(basis, 6),
        "session": sess,
        "days": round(days, 2),
        "change24h": round(chg, 5),
        "tech": tech,
        "intent": intent,
        "fee": round(fee, 4),
        "note": note,
        "live": False,
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(desk, indent=2))
    line = f"{now.isoformat()} {intent} eq={desk['equity']:.2f} {note} px={price:.2f} basis={basis*100:.3f}% score={tech['score']} {tech['note']}\n"
    with JOURNAL.open("a") as f:
        f.write(line)
    print(line, end="")
    print(json.dumps(desk["last"], indent=2))

if __name__ == "__main__":
    main()
