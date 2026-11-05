# BTC Hourly Paper Bot

Sanitized source for a paper-only Kalshi hourly BTC (`KXBTCD`) bot, a separate read-only market collector, and a short research summary. This repository does not schedule or host a running process.

## Paper-only

`LIVE_TRADING = False` in `btc_hourly/config.py`, and the collector sets the same flag. The paper broker raises if the flag is true. This tree has no live order client and no order endpoint. Import checks do not place live or paper trades.

`btc_hourly/README.md` still documents local commands, including `python3 run.py --once`. That command fetches public market data and can write paper fills into a local SQLite file. It was not run for this import.

## Collector

`btc_hourly/collector/` is non-AI. It reads public market endpoints and, when `loop` is running on a machine you control, writes a snapshot every 30 minutes (`COLLECTOR_INTERVAL`, default 1800 seconds) into its own SQLite file. It uses no API keys and does not call the paper bot.

The loop process stays down after a crash or reboot until someone starts it again.

Optional 4-hour AI reviews are a separate workflow. They are not in this collector, and this repository does not bundle or guarantee hosting for them.

## Macro calendar

NFP, CPI, and FOMC skip windows are the hand-maintained `MACRO_EVENTS` lists in `btc_hourly/edge_checks.py` and `btc_hourly/collector/collector.py`. Nothing fetches a calendar. Those dates stay stale until someone edits them.

## Market timestamp

`kalshi_quote_age_s` is the age of the newest Kalshi market `updated_time` (else `last_updated_time`) on the open KXBTCD page. That is a market-record update age. It is not executable quote freshness.

## Research

`research_hourly_btc/FINDINGS.md` is a restored summary and a link to the research Page. It is not the complete deep-dive dataset.

## Layout

| Path | Contents |
| --- | --- |
| `btc_hourly/` | Paper bot. Python standard library only. |
| `btc_hourly/collector/` | Read-only 30-minute collector and offline tests. |
| `research_hourly_btc/FINDINGS.md` | Restored research summary. |
| `MANIFEST.json` | Import checksums and the runtime files left out (databases, logs, pid/lock files). |

Databases, logs, pid files, locks, caches, credential files, and `.env` files are gitignored.

## Offline test

Python 3.9+.

```bash
python3 btc_hourly/collector/test_collector.py
```
