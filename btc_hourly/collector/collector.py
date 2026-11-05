#!/usr/bin/env python3
"""Read-only BTC market data collector. NO AI, NO trading, NO orders, NO keys.
Writes only to its own SQLite (market_data.db). Never touches btc_hourly.db.
Usage: collector.py once | loop | status | stop
"""
from __future__ import annotations
import fcntl, json, math, os, signal, sqlite3, sys, time
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
import logging
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

LIVE_TRADING = False  # hard-off; this script has no order code at all
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("COLLECTOR_DB", os.path.join(HERE, "market_data.db"))
LOCK = os.environ.get("COLLECTOR_LOCK", os.path.join(HERE, "collector.lock"))
PIDF = os.path.join(HERE, "collector.pid")
LOGF = os.path.join(HERE, "collector.log")
INTERVAL = int(os.environ.get("COLLECTOR_INTERVAL", "1800"))  # 30 min
TIMEOUT, RETRIES, MIN_GAP = 15, 3, 1.0
UA = "btc-paper-collector/1.0 (read-only)"
DVOL_SHOCK_PTS, RV_SHOCK_MULT = 3.0, 2.0
ET = ZoneInfo("America/New_York")
# Documented public endpoints only
URLS = {
    "coinbase_spot": "https://api.coinbase.com/v2/prices/BTC-USD/spot",
    "kraken": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
    "coinbase_candles": "https://api.exchange.coinbase.com/products/BTC-USD/candles",
    "deribit_dvol": "https://www.deribit.com/api/v2/public/get_volatility_index_data",
    "kalshi_markets": "https://api.elections.kalshi.com/trade-api/v2/markets",
}
MACRO_EVENTS = [  # verified BLS / Fed calendars 2026-11-05
    (date(2026, 11, 6), "08:30", "NFP"),
    (date(2026, 11, 10), "08:30", "CPI"),
    (date(2026, 12, 4), "08:30", "NFP"),
    (date(2026, 12, 9), "14:00", "FOMC"),
    (date(2026, 12, 10), "08:30", "CPI"),
]

log = logging.getLogger("collector")
log.setLevel(logging.INFO)
_h = RotatingFileHandler(LOGF, maxBytes=512_000, backupCount=2)
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)
_last = [0.0]

def get_json(url):
    for i in range(RETRIES):
        gap = MIN_GAP - (time.time() - _last[0])
        if gap > 0: time.sleep(gap)
        _last[0] = time.time()
        try:
            with urlopen(Request(url, headers={"User-Agent": UA, "Accept": "application/json"}), timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            log.warning("fetch fail %s try %d: %s", url.split("?")[0], i + 1, e)
            if i < RETRIES - 1: time.sleep(2 ** (i + 1))
    raise RuntimeError("fetch failed: " + url.split("?")[0])

def macro_flag(now):
    for d, hhmm, label in MACRO_EVENTS:
        h, m = map(int, hhmm.split(":"))
        p = datetime(d.year, d.month, d.day, h, m, tzinfo=ET)
        if p - timedelta(minutes=60) <= now <= p + timedelta(minutes=90):
            return label
    return None

def rv(closes):
    r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(r) < 10: return None
    return math.sqrt(sum(x * x for x in r) / len(r)) * math.sqrt(525600)

def collect(fetch=get_json):
    now = datetime.now(timezone.utc)
    row = {"ts_utc": now.isoformat(timespec="seconds"), "errors": []}
    try: row["coinbase"] = float(fetch(URLS["coinbase_spot"])["data"]["amount"])
    except Exception as e: row["errors"].append(f"coinbase:{e}")
    try:
        res = fetch(URLS["kraken"])["result"]; row["kraken"] = float(res[next(iter(res))]["c"][0])
    except Exception as e: row["errors"].append(f"kraken:{e}")
    px = [row[k] for k in ("coinbase", "kraken") if row.get(k)]
    row["spot"] = sum(px) / len(px) if px else None
    try:
        end = int(now.timestamp()); q = urlencode({"granularity": 60, "start": end - 240 * 60, "end": end})
        c = [float(x[4]) for x in sorted(fetch(URLS["coinbase_candles"] + "?" + q), key=lambda x: x[0])]
        row["rv_short"], row["rv_long"] = rv(c[-30:]), rv(c)
    except Exception as e: row["errors"].append(f"candles:{e}")
    try:
        ms = int(now.timestamp() * 1000)
        q = urlencode({"currency": "BTC", "resolution": 3600, "start_timestamp": ms - 3 * 86400000, "end_timestamp": ms})
        d = [float(x[4]) for x in fetch(URLS["deribit_dvol"] + "?" + q)["result"]["data"]]
        row["dvol"], row["dvol_prev"] = d[-1], (d[-2] if len(d) > 1 else d[-1])
    except Exception as e: row["errors"].append(f"dvol:{e}")
    try:
        mk = fetch(URLS["kalshi_markets"] + "?" + urlencode({"series_ticker": "KXBTCD", "status": "open", "limit": 200})).get("markets") or []
        row["kalshi_n"] = len(mk)
        ups = [m.get("updated_time") or m.get("last_updated_time") for m in mk]
        ups = [datetime.fromisoformat(u.replace("Z", "+00:00")) for u in ups if u]
        row["kalshi_quote_age_s"] = (now - max(ups)).total_seconds() if ups else None
    except Exception as e: row["errors"].append(f"kalshi:{e}")
    flags = []
    if row.get("dvol") is not None and abs(row["dvol"] - row["dvol_prev"]) >= DVOL_SHOCK_PTS: flags.append("DVOL_SHOCK")
    if row.get("rv_short") and row.get("rv_long") and row["rv_short"] >= RV_SHOCK_MULT * row["rv_long"]: flags.append("RV_SHOCK")
    row["vol_flags"] = ",".join(flags) or None
    row["macro_flag"] = macro_flag(now.astimezone(ET))
    row["errors"] = "; ".join(row["errors"]) or None
    return row

COLS = ["ts_utc", "spot", "coinbase", "kraken", "dvol", "dvol_prev", "rv_short", "rv_long",
        "kalshi_n", "kalshi_quote_age_s", "vol_flags", "macro_flag", "errors"]

def save(row, db=DB):
    con = sqlite3.connect(db, timeout=10)
    con.execute(f"CREATE TABLE IF NOT EXISTS market_snapshots ({', '.join(c + (' TEXT PRIMARY KEY' if c == 'ts_utc' else '') for c in COLS)})")
    con.execute(f"INSERT OR REPLACE INTO market_snapshots VALUES ({','.join('?' * len(COLS))})", [row.get(c) for c in COLS])
    con.commit(); con.close()

def once():
    r = collect(); save(r); log.info("snapshot %s", json.dumps(r)); print(json.dumps(r, indent=1)); return r

def acquire():
    f = open(LOCK, "w")
    try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: return None
    return f

def loop():
    lk = acquire()
    if not lk: print("already running"); return 1
    open(PIDF, "w").write(str(os.getpid()))
    stop = [False]
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__(0, True))
    log.info("loop start pid=%d interval=%ds", os.getpid(), INTERVAL)
    while not stop[0]:
        t0 = time.time()
        try: save(collect()); log.info("snapshot ok")
        except Exception as e: log.error("cycle error %s", e)
        while not stop[0] and time.time() - t0 < INTERVAL: time.sleep(5)
    log.info("loop stop"); os.path.exists(PIDF) and os.remove(PIDF); return 0

def status():
    pid = open(PIDF).read().strip() if os.path.exists(PIDF) else None
    alive = bool(pid) and os.path.exists(f"/proc/{pid}") or (pid and _alive(int(pid)))
    print(f"running={bool(alive)} pid={pid} interval={INTERVAL}s db={DB}")
    if os.path.exists(DB):
        con = sqlite3.connect(DB); print("rows", con.execute("select count(*), max(ts_utc) from market_snapshots").fetchone()); con.close()

def _alive(p):
    try: os.kill(p, 0); return True
    except OSError: return False

def stop():
    if os.path.exists(PIDF):
        os.kill(int(open(PIDF).read()), signal.SIGTERM); print("stop sent")
    else: print("not running")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit({"once": once, "loop": loop, "status": status, "stop": stop}[cmd]() or 0)
