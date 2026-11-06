# BTC Hourly Paper Bot

Paper-only Kalshi hourly BTC (`KXBTCD`) pricer and paper broker.

**LIVE_TRADING = False** in `config.py`. The broker raises if that flag is ever True. There is no live order code and this bot must never call `coinbase_orders_create`.

## Run

```bash
cd /workspace/btc_hourly
python3 run.py --status
python3 run.py --once
```

`run.py --once` settles due paper trades first. It then refuses new paper risk (exit 3) when meta cash and reconstructed cash differ by more than $0.01. Buys and settlements each run as one database transaction.

## Gates (Nov 5 deep dive)

1. `MIN_PRICE = 0.10` — no longshot buys under 10¢
2. Macro skip for NFP/CPI/FOMC (`edge_checks.py`) — currently includes Fri Nov 6 2026 NFP 8:30 ET
3. Vol-shock buffer bump when DVOL jumps 3+ pts or short RV ≥ 2× long RV

## Restore note

SQLite trade history was wiped on 2026-11-05 erase. Fresh DB seeded at $1,000 paper cash.
