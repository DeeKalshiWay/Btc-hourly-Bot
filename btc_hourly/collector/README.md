# BTC read-only market data collector (paper project)
Logs a BTC snapshot every 30 min into `market_data.db`, table `market_snapshots`. Each snapshot has spot (Coinbase + Kraken), Deribit DVOL, realized vol from Coinbase 1-min candles, Kalshi KXBTCD market count and quote age, plus DVOL/RV shock and macro-calendar flags.
It makes NO AI calls, places NO orders, uses NO API keys, and never touches the trading DB. It only reads public, documented endpoints.

## Run on your own computer (Python 3.9+, no installs)
    python3 collector.py once      # one snapshot
    nohup python3 collector.py loop >/dev/null 2>&1 &   # every 30 min, singleton lock
    python3 collector.py status
    python3 collector.py stop
Or use cron instead of loop: `*/30 * * * * cd /path/to/collector && python3 collector.py once`
Interval override: COLLECTOR_INTERVAL=1800. Logs rotate (collector.log, 512KB x3).
Add macro dates to MACRO_EVENTS in collector.py. Test: `python3 test_collector.py`.
To keep it running, your computer has to stay on and awake.
