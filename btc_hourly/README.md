# BTC Hourly Paper Bot

Paper-only Kalshi hourly BTC (`KXBTCD`) pricer and paper broker.

**LIVE_TRADING = False** in `config.py`. The broker raises if that flag is ever True. There is no live order code and this bot must never call `coinbase_orders_create`.

## Run

```bash
cd /workspace/btc_hourly
python3 run.py --status
python3 run.py --learn
python3 run.py --once
```

`run.py --once` settles due paper trades first, then refuses new paper risk (exit 3) when meta cash and reconstructed cash differ by more than $0.01. Buys and settlements each run as one database transaction.

Settlement uses a Kalshi public market only when `status` is `finalized` and `result` is `yes` or `no`. A missing result leaves the trade open. Spot is not a settlement and not a learning label. Rows that are already won or lost are not rewritten.

`run.py --learn` settles from those official results and refreshes the shadow learner. It does not place a new paper bet. `run.py --status` prints the learning JSON from local state and does not fetch. The learner does not change `EDGE_BUFFER`, `MIN_PRICE`, or stake. A local calendar that is longer than this repo's copy should be kept; do not replace `config.py`, `edge_checks.py`, or `collector/` from the learning bundle. The bundle's `INTEGRATION.md` lists the copy steps.

## Gates (Nov 5 deep dive)

1. `MIN_PRICE = 0.10` — no longshot buys under 10¢
2. Macro skip for NFP/CPI/FOMC (`edge_checks.py`) — currently includes Fri Nov 6 2026 NFP 8:30 ET
3. Vol-shock buffer bump when DVOL jumps 3+ pts or short RV ≥ 2× long RV

## Restore note

SQLite trade history was wiped on 2026-11-05 erase. Fresh DB seeded at $1,000 paper cash.
