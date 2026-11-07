# Edge monitor log

Restored 2026-11-05 after erase. Fresh paper book. LIVE_TRADING stays False.

| PT time | Change | Why |
|---------|--------|-----|
| 2026-11-05 restore | Seeded MIN_PRICE=0.10, macro NFP window, vol-shock buffer | Rebuild from research Page deep dive |
| 2026-11-05 12:35 PT | Extended MACRO_EVENTS: +CPI 2026-11-10 08:30 ET, +NFP 2026-12-04 08:30 ET, +FOMC 2026-12-09 14:00 ET, +CPI 2026-12-10 08:30 ET (synced edge_checks.py + collector.py). Knobs unchanged (MIN_PRICE=0.10, EDGE_BUFFER=0.03). | BLS Nov/Dec 2026 schedule + Fed FOMC calendar; next prints beyond tomorrow's NFP; collector was found STOPPED (stale pid, last snapshot ~34m) |
| 2026-11-05 21:15 PT | Ledger reconciled: meta cash 1773.77 → 1452.77 (inflated +$321 = 3×$107 duplicate payout credits). Reconstructed = 1000 − Σcost + Σ settled payouts. Backup: btc_hourly.db.bak-20261106-051426. broker.buy_yes_paper now debits cash + inserts trade in one BEGIN IMMEDIATE txn; settle_due wrapped in BEGIN IMMEDIATE; db.db() rolls back on error; added db.ledger_check(); run.py refuses new paper risk (exit 3) on ledger mismatch >$0.01. LIVE_TRADING=False unchanged. | Fix nested-write/lock + double-credit; stop recurrence |
| 2026-11-07 | Shadow learner added (`learning.py`). Future paper settlement waits for a Kalshi `finalized` yes/no. Historical won/lost rows were not relabeled and cash math was not rewritten. Cold start abstains. No change to EDGE_BUFFER, MIN_PRICE, stake, or LIVE_TRADING. | Record forecasts and score only official outcomes. No edge claimed from the current book. |
