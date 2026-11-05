"""Paper-only BTC hourly Kalshi bot config. LIVE_TRADING stays False."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "btc_hourly.db"
MONITOR_LOG = ROOT / "monitor_log.md"

# HARD OFF. Broker raises if this is ever True. Never call coinbase_orders_create.
LIVE_TRADING = False

STARTING_CASH = 1000.0
KILL_EQUITY = 750.0  # stop new paper risk below this

# Fees (Kalshi taker): round_up(0.07 * C * P * (1-P)); we model per-contract
TAKER_FEE_RATE = 0.07
MAKER_FEE_RATE = 0.0175
EDGE_BUFFER = 0.03  # base EV buffer after fees
VOL_SHOCK_BUFFER_BUMP = 0.02  # temporary add-on when DVOL/RV shocks

# Sizing
KELLY_FRACTION = 0.15
MAX_STAKE_PER_TRADE = 25.0
MAX_STAKE_PER_EVENT = 75.0
TOP_N_CONTRACTS = 3

# Price gates (Nov 5 deep dive: longshots under 10c were 0/7)
MIN_PRICE = 0.10
MAX_PRICE = 0.97

# Timing
SKIP_LAST_MINUTES = 5  # no new risk in final 5 min before close
DVOL_BLEND = 0.30  # 30% Deribit DVOL + 70% realized

# Vol-shock thresholds
DVOL_SHOCK_PTS = 3.0
RV_SHOCK_MULT = 2.0

# Data
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
DERIBIT_DVOL_URL = (
    "https://www.deribit.com/api/v2/public/get_volatility_index_data"
    "?currency=BTC&resolution=3600"
)
# Kalshi public trade API (no auth for market reads)
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_SERIES = "KXBTCD"

USER_AGENT = "btc-hourly-paper-bot/1.0 (paper-only; contact=local)"
