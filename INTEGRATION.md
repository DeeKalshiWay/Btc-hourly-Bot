# Integrate the shadow learner into an existing paper runtime

This bundle updates the paper bot's settlement and adds a shadow calibration layer. It does not claim a trading edge. On an empty learning database the report says `learned: false`, `promotion_eligible: false`, and `discovered_edges: []`.

The stated local book (29 settled trades, 9 wins / 20 losses, clustered on events, settled from the spot price rather than Kalshi's result) is **not** imported. Those rows are not trustworthy labels, and they are below the event-count gate anyway. Historical won/lost rows and cash are not rewritten.

`LIVE_TRADING` stays `False`. There is no live order path and no `coinbase_orders_create` call. Stake, `EDGE_BUFFER`, and `MIN_PRICE` are not loosened.

## Do not overwrite these local files

The running tree at `/workspace/btc_hourly` has local settings this bundle must not replace:

- `config.py` — local `EDGE_BUFFER` is `0.05` (this repo copy still has the older `0.03` default). Leave the local file.
- `edge_checks.py` — local `MACRO_EVENTS` runs through February 2027.
- `collector/collector.py` — same calendar, and the collector stays a non-AI snapshot writer. This change does not edit it.
- `btc_hourly.db`, `collector/market_data.db`, `learning.db`, logs, pid files, `.env`, and anything with a key or token.

Diff `broker.py`, `db.py`, and `run.py` against the local copies before replacing them. They are based on the ledger-fix versions in this repo (single-transaction cash updates, ledger check, exit 3). If the local copies have other edits, merge those by hand. Do not copy `config.py` over the local one.

## Files to copy

From this bundle into `/workspace/btc_hourly`:

| Bundle path | Action |
| --- | --- |
| `btc_hourly/learning.py` | new file |
| `btc_hourly/test_learning.py` | new file |
| `btc_hourly/broker.py` | replace, after a diff |
| `btc_hourly/db.py` | replace, after a diff |
| `btc_hourly/run.py` | replace, after a diff |

`learning.py` reads `LIVE_TRADING`, `KALSHI_MARKETS_URL`, and `USER_AGENT` from the local `config.py`. It does not need new config fields. No new package and no API key.

The archive in this repo is `dist/btc_hourly_learning_src.tar.gz`. Extracting it creates `btc_hourly_learning_src/`.

```bash
tar -xzf dist/btc_hourly_learning_src.tar.gz
cd btc_hourly_learning_src
diff -u /workspace/btc_hourly/broker.py btc_hourly/broker.py
diff -u /workspace/btc_hourly/db.py btc_hourly/db.py
diff -u /workspace/btc_hourly/run.py btc_hourly/run.py
cp btc_hourly/learning.py btc_hourly/test_learning.py /workspace/btc_hourly/
cp btc_hourly/broker.py btc_hourly/db.py btc_hourly/run.py /workspace/btc_hourly/
cd /workspace/btc_hourly
python3 test_learning.py
python3 collector/test_collector.py
BTC_HOURLY_OFFLINE=1 python3 run.py --status
```

`python3 run.py --once` was not run for this change. It fetches public data and can write new paper fills. Use it only when a paper cycle is intended.

## What starts running

- `python3 run.py --once` (the existing hourly paper cycle) records every considered contract: rejected, shadow, or paper-filled. Features and probabilities are the values at that minute. It then refits the shadow artifact. A learning failure does not change the paper bet that was already sized by the old rules.
- `python3 run.py --learn` settles open paper trades from official results and refits. It places no new bet. Schedule this beside the collector if you want a refresh without a new entry. The collector process itself is unchanged.
- `python3 run.py --status` prints the last report from local files. It does not fetch.

Runtime files, created next to the bot and gitignored:

- `learning.db` — predictions, official labels, artifact history
- `learning_artifact.json` — the latest report, written with a temp file and `os.replace`

Optional environment variables:

- `BTC_HOURLY_LEARNING_DB`
- `BTC_HOURLY_LEARNING_ARTIFACT`
- `BTC_HOURLY_OFFLINE=1` skips the public market GET. Labels stay pending and open trades stay open.

## Settlement change

For trades that are still `open`:

- Kalshi `GET /markets/{ticker}` on the existing public `KALSHI_MARKETS_URL` (no key).
- A label or a payout is applied only when `status` is `finalized` and `result` is `yes` or `no`.
- `determined` stays pending because that result can still be disputed. `disputed`, `amended`, `closed`, empty, and `scalar` also stay pending. The expiration value is stored for audit and is never turned into yes/no.
- The payout is added only if the row was still `open`. A second call does not pay again.
- A contradictory later result does not overwrite a verified learning label; it is counted as a conflict.
- Rows already `won` or `lost` are not updated, even if an official result disagrees. Cash for those rows is not moved.

Until Kalshi finalizes a market, its paper cost stays in the open book. That is slower than the old spot settlement and avoids paying from a price that is not the contract result.

## What the learner does

Each new verified event cluster triggers a full refit. The same verified rows produce the same artifact (`input_hash`). The fit is event-grouped isotonic calibration:

- Sort events by close time. The latest holdout block is never used to fit.
- Every contract of an event stays on one side of the split.
- Scores are means of per-event means, so ten strikes in one hour count as one event.
- A forecast recorded at or after its close time is stored but excluded.
- Pre-registered slices only: volatility (calm/shock), time of day, price, horizon, and spread. No search over new thresholds.

Production gates, in `learning.LearningConfig`:

- at least 40 train events and 16 holdout events before any out-of-sample number is treated as a result
- a slice needs 20 events and 12 of them in the holdout
- `supported` requires the holdout Brier to beat the named baseline by at least 0.02, with the event-bootstrap 95% interval entirely on the better side
- overall calibration must beat **both** the raw model and the market
- `promotion_eligible` also requires 80 events and 20 holdout events, and even then `trading_impact` is `none` and every risk delta is zero
- `validated` and `validated_for_trading` stay false

`status` is `insufficient_sample`, `abstain`, `candidate`, or `supported`. `candidate` means an in-sample difference that the holdout did not confirm. None of these statuses place or size a trade.

## Tests

`python3 test_learning.py` (16 tests, stdlib `unittest`, temp databases):

- cold start abstains and does not create a database just to say so
- duplicate forecasts do not move the stored probability; a paper fill only upgrades disposition
- determined, scalar, empty, and expiration-only payloads do not label
- a verified label is not overwritten; a second identical label is a no-op
- five events, and ten contracts in one event, stay below the gate
- 56 synthetic events can show a holdout `supported` calibration and still leave risk deltas at 0; a second refit does not write another artifact
- a reversed holdout stays `candidate` and is not `supported`
- flipping holdout labels does not change the fitted knots; flipping a train label does
- a forecast timestamp after close is excluded
- a forced fit error keeps the previous artifact and suppresses stale claims
- 80 synthetic events can set `promotion_eligible` without a stake or buffer change
- `LIVE_TRADING=True` refuses the learner
- spot does not settle an open trade; a raw payload dict does not either; a future close stays open; an official yes pays once; the ledger still balances
- a pre-existing won row keeps its pnl, timestamp, and cash when a different official result is offered
- an official loss pays zero and does not pay again
- source scan: no `coinbase_orders_create`, no `/orders`, learner does not read the `trades` table or set cash

The collector test is unchanged and still passes. `run.py --once` was not executed.

## Remaining risks

- Nothing in the live book has been learned. The first real report stays a cold start until new forecasts receive finalized results, and those results will still have to clear the event gates.
- Open paper risk now waits for `finalized`. A long `determined` window leaves cash reserved. That is intentional.
- If Kalshi ever marks a binary hourly market `finalized` with a wrong `yes`/`no`, this code will trust that field. It will not invent a correction from spot, and it will not rewrite an already settled paper row.
- `promotion_eligible` can become true on later data. This version still does not read it when sizing. Do not wire it to stake or to a looser buffer.
- A `supported` slice is an out-of-sample Brier result on a pre-registered bucket. It is not a validated trading rule and it is not a promise of profit.
- Event ids come from `event_ticker`. A bad ticker grouping would under- or over-count clusters.
- The public GET can fail or change shape. Failures leave the label pending and the previous artifact in place. A stale artifact is not allowed to keep a `supported` claim.
- Copying this repo's `config.py` or `edge_checks.py` onto the machine would drop the local 0.05 buffer and the February 2027 calendar. Don't.
