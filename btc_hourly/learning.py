"""Shadow calibration for the paper bot. Stdlib only. No orders.

Records prediction-time features, attaches Kalshi finalized yes/no labels, and
fits an event-grouped chronological calibration. It never places trades, never
reads the paper ledger as labels, and never loosens risk limits.

A full refit from the verified set runs when that set changes. The same inputs
produce the same artifact. Cold start abstains.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import config

PT = ZoneInfo("America/Los_Angeles")
MODEL_VERSION = "hourly-bs-ewma-v1"
LEARNER_VERSION = "shadow-isotonic-v1"
DISCLAIMER = (
    "Shadow calibration only. This is not a prediction of profit, not a "
    "validated trading edge, and it does not change paper risk limits."
)
OFFICIAL_SOURCE = "official_kalshi"
FINAL_STATUS = "finalized"
VERIFIED = "verified"
PENDING = "pending"
PATTERN_FAMILIES = ("vol", "tod", "price", "horizon", "spread")


@dataclass(frozen=True)
class LearningConfig:
    """Pre-registered gates. Defaults are deliberately above a tiny paper book."""

    min_train_events: int = 40
    min_holdout_events: int = 16
    holdout_fraction: float = 0.25
    min_slice_events: int = 20
    min_slice_holdout: int = 12
    brier_margin: float = 0.02
    bootstrap_draws: int = 400
    bootstrap_seed: int = 0
    promotion_min_events: int = 80
    promotion_min_holdout: int = 20


def learning_db_path() -> Path:
    override = os.environ.get("BTC_HOURLY_LEARNING_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "learning.db"


def artifact_json_path() -> Path:
    override = os.environ.get("BTC_HOURLY_LEARNING_ARTIFACT")
    if override:
        return Path(override)
    return learning_db_path().with_name("learning_artifact.json")


SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
  decision_key TEXT PRIMARY KEY,
  ticker TEXT NOT NULL,
  event_ticker TEXT NOT NULL,
  close_time TEXT,
  recorded_at TEXT NOT NULL,
  minute_bucket TEXT NOT NULL,
  strike REAL,
  minutes_left REAL,
  spot REAL,
  sigma REAL,
  dvol REAL,
  dvol_prev REAL,
  rv_short REAL,
  rv_long REAL,
  model_p_yes REAL,
  yes_bid REAL,
  yes_ask REAL,
  spread REAL,
  market_p REAL,
  market_p_kind TEXT,
  side TEXT,
  disposition TEXT NOT NULL,
  gate_reason TEXT,
  model_version TEXT NOT NULL,
  features_json TEXT NOT NULL,
  lookahead_excluded INTEGER NOT NULL DEFAULT 0,
  label_yes INTEGER,
  label_result TEXT,
  label_status TEXT NOT NULL DEFAULT 'pending',
  label_source TEXT,
  settlement_value TEXT,
  settlement_ts TEXT,
  market_status TEXT,
  labeled_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  input_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS label_conflicts (
  decision_key TEXT NOT NULL,
  existing_result TEXT NOT NULL,
  rejected_result TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  UNIQUE(decision_key, rejected_result)
);
CREATE TABLE IF NOT EXISTS learner_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pred_ticker ON predictions(ticker);
CREATE INDEX IF NOT EXISTS idx_pred_event ON predictions(event_ticker);
CREATE INDEX IF NOT EXISTS idx_pred_label ON predictions(label_status);
"""


def _refuse_if_live() -> None:
    if config.LIVE_TRADING:
        raise RuntimeError("Refusing learning run because LIVE_TRADING is True. Paper only.")


def connect() -> sqlite3.Connection:
    path = learning_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _r6(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 6)


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _minute_bucket(recorded_at: str) -> str:
    parsed = _parse_dt(recorded_at)
    if parsed is None:
        return recorded_at[:16]
    return parsed.astimezone(PT).strftime("%Y-%m-%dT%H:%M")


def _event_ticker(row: dict) -> str:
    event = str(row.get("event_ticker") or "").strip()
    if event:
        return event
    ticker = str(row.get("ticker") or "")
    if "-" in ticker:
        return ticker.rsplit("-", 1)[0]
    return ticker


def _lookahead(recorded_at: str, close_time: Optional[str]) -> bool:
    recorded = _parse_dt(recorded_at)
    close = _parse_dt(close_time)
    if recorded is None or close is None:
        return True
    return recorded >= close


def decision_key(ticker: str, close_time: Optional[str], model_version: str, recorded_at: str) -> str:
    raw = "|".join([ticker, close_time or "", model_version, _minute_bucket(recorded_at)])
    return hashlib.sha256(raw.encode()).hexdigest()


def _market_probability(yes_bid: Optional[float], yes_ask: Optional[float]) -> tuple[Optional[float], str]:
    bid, ask = _r6(yes_bid), _r6(yes_ask)
    if bid is not None and ask is not None and 0 < bid <= ask < 1:
        return _r6((bid + ask) / 2.0), "mid"
    if ask is not None and 0 < ask < 1:
        return ask, "ask"
    return None, "missing"


def vol_bucket(dvol: Optional[float], dvol_prev: Optional[float], rv_short: Optional[float], rv_long: Optional[float]) -> str:
    known = False
    shock = False
    if dvol is not None and dvol_prev is not None:
        known = True
        if abs(dvol - dvol_prev) >= 3.0:
            shock = True
    if rv_short is not None and rv_long is not None and rv_long > 0:
        known = True
        if rv_short >= 2.0 * rv_long:
            shock = True
    if not known:
        return "unknown"
    return "shock" if shock else "calm"


def tod_bucket(recorded_at: str) -> str:
    parsed = _parse_dt(recorded_at)
    if parsed is None:
        return "unknown"
    hour = parsed.astimezone(PT).hour
    if hour < 6:
        return "late_night"
    if hour < 12:
        return "morning"
    if hour < 16:
        return "afternoon"
    return "evening"


def price_bucket(yes_ask: Optional[float]) -> str:
    if yes_ask is None:
        return "unknown"
    edges = (
        (0.0, 0.10, "below_10c"),
        (0.10, 0.30, "10_30c"),
        (0.30, 0.50, "30_50c"),
        (0.50, 0.70, "50_70c"),
        (0.70, 0.90, "70_90c"),
        (0.90, 1.01, "90c_plus"),
    )
    for lo, hi, name in edges:
        if lo <= yes_ask < hi:
            return name
    return "unknown"


def horizon_bucket(minutes_left: Optional[float]) -> str:
    if minutes_left is None:
        return "unknown"
    if minutes_left < 15:
        return "under_15m"
    if minutes_left < 60:
        return "15_60m"
    if minutes_left < 120:
        return "60_120m"
    if minutes_left <= 240:
        return "120_240m"
    return "over_240m"


def spread_bucket(spread: Optional[float]) -> str:
    if spread is None:
        return "unknown"
    if spread < 0.02:
        return "tight"
    if spread < 0.05:
        return "moderate"
    return "wide"


def _features(row: dict) -> dict[str, Any]:
    yes_bid = _r6(row.get("yes_bid"))
    yes_ask = _r6(row.get("yes_ask"))
    spread = _r6(row.get("spread"))
    if spread is None and yes_bid is not None and yes_ask is not None:
        spread = _r6(yes_ask - yes_bid)
    return {
        "strike": _r6(row.get("strike")),
        "minutes_left": _r6(row.get("minutes_left")),
        "spot": _r6(row.get("spot")),
        "sigma": _r6(row.get("sigma")),
        "dvol": _r6(row.get("dvol")),
        "dvol_prev": _r6(row.get("dvol_prev")),
        "rv_short": _r6(row.get("rv_short")),
        "rv_long": _r6(row.get("rv_long")),
        "model_p_yes": _r6(row.get("model_p_yes")),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "spread": spread,
    }


def _risk_adjustment() -> dict[str, Any]:
    return {
        "edge_buffer_delta": 0.0,
        "min_price_delta": 0.0,
        "max_stake_multiplier": 1.0,
        "new_trades": 0,
        "reason": "shadow mode; learning cannot raise risk or loosen gates",
    }


def _base_report(cfg: LearningConfig, sample: dict, abstentions: list[str], now: Optional[datetime] = None) -> dict[str, Any]:
    patterns = [
        {
            "family": family,
            "bucket": None,
            "name": family,
            "status": "insufficient_sample",
            "validated": False,
            "n_events": 0,
            "n_holdout_events": 0,
            "note": "no verified official outcomes",
            "trading_use": "none",
        }
        for family in PATTERN_FAMILIES
    ]
    return {
        "abstentions": abstentions,
        "artifact_stale": False,
        "calibration": {
            "status": "insufficient_sample",
            "validated": False,
            "note": "no out-of-sample calibration is available",
            "train_events": 0,
            "holdout_events": 0,
            "brier_model": None,
            "brier_market": None,
            "brier_calibrated": None,
            "holdout_gap_vs_market": None,
            "holdout_gap_vs_model": None,
            "knots": [],
        },
        "claims_allowed": True,
        "config": asdict(cfg),
        "disclaimer": DISCLAIMER,
        "discovered_edges": [],
        "eval_protocol": (
            "Events are ordered by close time. The latest holdout block is out of sample "
            "and is never used to fit the calibration. Every contract in an event stays on "
            "one side of the split. Scores are event-equal means. Labels are Kalshi "
            "finalized yes/no only. Features are the values stored when the forecast was recorded."
        ),
        "input_hash": "",
        "last_error": None,
        "learned": False,
        "learner_version": LEARNER_VERSION,
        "mode": "shadow",
        "model_version": MODEL_VERSION,
        "not_learned_from_live_book": (
            "Paper-trade pnl is not a label. Historical spot settlements are not imported."
        ),
        "patterns": patterns,
        "promotion_eligible": False,
        "risk_adjustment": _risk_adjustment(),
        "sample": sample,
        "trading_impact": "none",
        "trained_at": now.astimezone(timezone.utc).isoformat() if now else None,
        "train_events": [],
        "holdout_events": [],
        "update_failed": False,
        "validated_for_trading": False,
        "what_changed_in_the_bot": "nothing; the raw model and risk limits are unchanged",
    }


def _empty_sample(cfg: LearningConfig) -> dict[str, Any]:
    return {
        "quality": "none",
        "n_predictions": 0,
        "n_pending": 0,
        "n_verified": 0,
        "n_events_verified": 0,
        "n_scorable": 0,
        "n_events_scorable": 0,
        "n_excluded_lookahead": 0,
        "n_conflicts": 0,
        "min_train_events": cfg.min_train_events,
        "min_holdout_events": cfg.min_holdout_events,
        "trustworthy_labels": "official_kalshi_finalized_yes_no_only",
    }


def cold_start_report(cfg: Optional[LearningConfig] = None) -> dict[str, Any]:
    cfg = cfg or LearningConfig()
    sample = _empty_sample(cfg)
    report = _base_report(cfg, sample, ["no verified official settlements; cold start abstains"])
    report["input_hash"] = _hash_rows([], cfg)
    report["claims_allowed"] = True
    return report


def _hash_rows(rows: list[dict], cfg: LearningConfig) -> str:
    canon = []
    fields = (
        "decision_key",
        "event_ticker",
        "close_time",
        "recorded_at",
        "model_p_yes",
        "market_p",
        "yes_ask",
        "spread",
        "minutes_left",
        "dvol",
        "dvol_prev",
        "rv_short",
        "rv_long",
        "label_yes",
    )
    for row in sorted(rows, key=lambda item: item["decision_key"]):
        canon.append({field: row.get(field) for field in fields})
    blob = json.dumps(
        {"cfg": asdict(cfg), "learner": LEARNER_VERSION, "model": MODEL_VERSION, "rows": canon},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def record_observations(rows: list[dict], now: Optional[datetime] = None) -> dict[str, int]:
    """Insert prediction-time rows. A second insert of the same key does not change features."""
    _refuse_if_live()
    now = now or datetime.now(timezone.utc)
    created = now.astimezone(timezone.utc).isoformat()
    inserted = 0
    upgraded = 0
    skipped = 0
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for raw in rows:
            ticker = str(raw.get("ticker") or "").strip()
            recorded_at = str(raw.get("recorded_at") or "")
            if not ticker or not recorded_at:
                skipped += 1
                continue
            features = _features(raw)
            market_p, market_kind = _market_probability(features["yes_bid"], features["yes_ask"])
            disposition = str(raw.get("disposition") or "rejected")
            if disposition not in ("rejected", "shadow", "paper_filled"):
                disposition = "rejected"
            model_version = str(raw.get("model_version") or MODEL_VERSION)
            close_time = raw.get("close_time")
            key = decision_key(ticker, close_time, model_version, recorded_at)
            lookahead = 1 if _lookahead(recorded_at, close_time) else 0
            before = conn.execute("SELECT disposition FROM predictions WHERE decision_key=?", (key,)).fetchone()
            conn.execute(
                """
                INSERT INTO predictions (
                  decision_key, ticker, event_ticker, close_time, recorded_at, minute_bucket,
                  strike, minutes_left, spot, sigma, dvol, dvol_prev, rv_short, rv_long,
                  model_p_yes, yes_bid, yes_ask, spread, market_p, market_p_kind, side,
                  disposition, gate_reason, model_version, features_json, lookahead_excluded,
                  label_status, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(decision_key) DO UPDATE SET
                  disposition = CASE
                    WHEN predictions.disposition = 'paper_filled' THEN predictions.disposition
                    WHEN excluded.disposition = 'paper_filled' THEN excluded.disposition
                    ELSE predictions.disposition
                  END
                """,
                (
                    key,
                    ticker,
                    _event_ticker(raw),
                    close_time,
                    recorded_at,
                    _minute_bucket(recorded_at),
                    features["strike"],
                    features["minutes_left"],
                    features["spot"],
                    features["sigma"],
                    features["dvol"],
                    features["dvol_prev"],
                    features["rv_short"],
                    features["rv_long"],
                    features["model_p_yes"],
                    features["yes_bid"],
                    features["yes_ask"],
                    features["spread"],
                    market_p,
                    market_kind,
                    raw.get("side"),
                    disposition,
                    raw.get("gate_reason"),
                    model_version,
                    json.dumps(features, sort_keys=True),
                    lookahead,
                    PENDING,
                    created,
                ),
            )
            if before is None:
                inserted += 1
            elif before["disposition"] != "paper_filled" and disposition == "paper_filled":
                upgraded += 1
    return {"inserted": inserted, "upgraded": upgraded, "skipped": skipped}


def _market_obj(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("market")
    if isinstance(inner, dict):
        return inner
    return payload


def official_binary_result(payload: Any) -> Optional[str]:
    """Return yes/no only for a finalized binary market. Never infer from spot or expiration."""
    market = _market_obj(payload)
    status = str(market.get("status") or "").strip().lower()
    result = str(market.get("result") or "").strip().lower()
    if status != FINAL_STATUS:
        return None
    if result not in ("yes", "no"):
        return None
    return result


def _default_fetch_json(ticker: str) -> dict:
    url = config.KALSHI_MARKETS_URL.rstrip("/") + "/" + quote(ticker, safe="")
    req = Request(url, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def fetch_market_results(tickers: list[str], fetch_json=None) -> dict[str, dict]:
    """Public GET of market payloads. Missing or failed lookups are omitted, not invented."""
    _refuse_if_live()
    fetch_json = fetch_json or _default_fetch_json
    found: dict[str, dict] = {}
    for ticker in sorted({str(item).strip() for item in tickers if str(item).strip()}):
        try:
            payload = fetch_json(ticker)
        except Exception:
            continue
        if isinstance(payload, dict):
            found[ticker] = payload
    return found


def verified_outcomes(results: dict[str, Any]) -> dict[str, str]:
    """Reduce public payloads to finalized yes/no. Bare strings and other statuses are dropped."""
    outcomes: dict[str, str] = {}
    for ticker, payload in results.items():
        result = official_binary_result(payload)
        if result:
            outcomes[str(ticker)] = result
    return outcomes


def apply_official_results(results: dict[str, Any], now: Optional[datetime] = None) -> dict[str, int]:
    """Attach finalized yes/no labels. Pending stays pending. Conflicts are not overwritten."""
    _refuse_if_live()
    now = now or datetime.now(timezone.utc)
    labeled_at = now.astimezone(timezone.utc).isoformat()
    applied = 0
    unchanged = 0
    pending = 0
    conflicts = 0
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for ticker, payload in results.items():
            result = official_binary_result(payload) if not isinstance(payload, str) else None
            if isinstance(payload, str):
                pending += 1
                continue
            rows = conn.execute(
                "SELECT decision_key, label_status, label_result FROM predictions WHERE ticker=?",
                (ticker,),
            ).fetchall()
            if result is None:
                pending += len(rows)
                continue
            market = _market_obj(payload)
            for row in rows:
                if row["label_status"] == VERIFIED:
                    if row["label_result"] == result:
                        unchanged += 1
                        continue
                    conn.execute(
                        """
                        INSERT INTO label_conflicts (decision_key, existing_result, rejected_result, seen_at)
                        VALUES (?,?,?,?)
                        ON CONFLICT(decision_key, rejected_result) DO NOTHING
                        """,
                        (row["decision_key"], row["label_result"], result, labeled_at),
                    )
                    conflicts += 1
                    continue
                label_yes = 1 if result == "yes" else 0
                conn.execute(
                    """
                    UPDATE predictions
                    SET label_yes=?, label_result=?, label_status=?, label_source=?,
                        settlement_value=?, settlement_ts=?, market_status=?, labeled_at=?
                    WHERE decision_key=? AND label_status!=?
                    """,
                    (
                        label_yes,
                        result,
                        VERIFIED,
                        OFFICIAL_SOURCE,
                        None if market.get("expiration_value") in (None, "") else str(market.get("expiration_value")),
                        None if not market.get("settlement_ts") else str(market.get("settlement_ts")),
                        FINAL_STATUS,
                        labeled_at,
                        row["decision_key"],
                        VERIFIED,
                    ),
                )
                applied += 1
    return {"applied": applied, "unchanged": unchanged, "pending": pending, "conflicts": conflicts}


def _counts(conn: sqlite3.Connection, cfg: LearningConfig) -> dict[str, Any]:
    sample = _empty_sample(cfg)
    sample["n_predictions"] = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    sample["n_pending"] = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE label_status=?",
        (PENDING,),
    ).fetchone()[0]
    sample["n_verified"] = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE label_status=? AND label_source=?",
        (VERIFIED, OFFICIAL_SOURCE),
    ).fetchone()[0]
    sample["n_excluded_lookahead"] = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE lookahead_excluded=1 AND label_status=?",
        (VERIFIED,),
    ).fetchone()[0]
    sample["n_conflicts"] = conn.execute("SELECT COUNT(*) FROM label_conflicts").fetchone()[0]
    sample["n_events_verified"] = conn.execute(
        """
        SELECT COUNT(DISTINCT event_ticker) FROM predictions
        WHERE label_status=? AND label_source=?
        """,
        (VERIFIED, OFFICIAL_SOURCE),
    ).fetchone()[0]
    return sample


def _scorable_rows(conn: sqlite3.Connection) -> list[dict]:
    found = conn.execute(
        """
        SELECT * FROM predictions
        WHERE label_status=? AND label_source=? AND lookahead_excluded=0
          AND label_yes IN (0, 1)
          AND model_p_yes IS NOT NULL AND market_p IS NOT NULL
        ORDER BY decision_key
        """,
        (VERIFIED, OFFICIAL_SOURCE),
    ).fetchall()
    rows = []
    for item in found:
        model_p = _r6(item["model_p_yes"])
        market_p = _r6(item["market_p"])
        if model_p is None or market_p is None:
            continue
        if not (0.0 <= model_p <= 1.0 and 0.0 <= market_p <= 1.0):
            continue
        row = {key: item[key] for key in item.keys()}
        row["model_p_yes"] = model_p
        row["market_p"] = market_p
        row["yes_ask"] = _r6(item["yes_ask"])
        row["spread"] = _r6(item["spread"])
        row["minutes_left"] = _r6(item["minutes_left"])
        row["dvol"] = _r6(item["dvol"])
        row["dvol_prev"] = _r6(item["dvol_prev"])
        row["rv_short"] = _r6(item["rv_short"])
        row["rv_long"] = _r6(item["rv_long"])
        row["label_yes"] = int(item["label_yes"])
        rows.append(row)
    return rows


def _event_order(rows: list[dict]) -> list[str]:
    grouped: dict[str, list[datetime]] = {}
    for row in rows:
        grouped.setdefault(row["event_ticker"], [])
        parsed = _parse_dt(row.get("close_time"))
        if parsed is not None:
            grouped[row["event_ticker"]].append(parsed)
    def sort_key(event: str) -> tuple:
        closes = grouped.get(event) or []
        close = min(closes) if closes else datetime.max.replace(tzinfo=timezone.utc)
        return (close, event)
    return sorted(grouped, key=sort_key)


def _split_events(events: list[str], cfg: LearningConfig) -> Optional[tuple[list[str], list[str]]]:
    needed = cfg.min_train_events + cfg.min_holdout_events
    if len(events) < needed:
        return None
    holdout_n = max(cfg.min_holdout_events, math.ceil(cfg.holdout_fraction * len(events)))
    holdout_n = min(holdout_n, len(events) - cfg.min_train_events)
    if holdout_n < cfg.min_holdout_events:
        return None
    return events[:-holdout_n], events[-holdout_n:]


def _by_event(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["event_ticker"], []).append(row)
    return grouped


def _isotonic_knots(train_rows: list[dict]) -> list[dict]:
    grouped = _by_event(train_rows)
    pairs = []
    for event in sorted(grouped):
        block = grouped[event]
        model_p = sum(row["model_p_yes"] for row in block) / len(block)
        outcome = sum(row["label_yes"] for row in block) / len(block)
        pairs.append((round(model_p, 6), round(outcome, 6), event))
    pairs.sort(key=lambda item: (item[0], item[2]))
    if not pairs:
        return []
    blocks = [{"sum": outcome, "w": 1.0, "lo": model_p, "hi": model_p} for model_p, outcome, _event in pairs]
    index = 0
    while index < len(blocks) - 1:
        left = blocks[index]["sum"] / blocks[index]["w"]
        right = blocks[index + 1]["sum"] / blocks[index + 1]["w"]
        if left > right + 1e-15:
            merged = {
                "sum": blocks[index]["sum"] + blocks[index + 1]["sum"],
                "w": blocks[index]["w"] + blocks[index + 1]["w"],
                "lo": blocks[index]["lo"],
                "hi": blocks[index + 1]["hi"],
            }
            blocks[index:index + 2] = [merged]
            if index > 0:
                index -= 1
        else:
            index += 1
    knots = []
    for block in blocks:
        knots.append(
            {
                "p_lo": round(block["lo"], 6),
                "p_hi": round(block["hi"], 6),
                "y": round(block["sum"] / block["w"], 6),
            }
        )
    return knots


def apply_knots(knots: list[dict], model_p: float) -> float:
    if not knots:
        return model_p
    if model_p <= knots[0]["p_lo"]:
        return knots[0]["y"]
    previous = knots[0]
    for knot in knots:
        if knot["p_lo"] - 1e-12 <= model_p <= knot["p_hi"] + 1e-12:
            return knot["y"]
        if model_p < knot["p_lo"]:
            return previous["y"]
        previous = knot
    return knots[-1]["y"]


def _event_brier(rows: list[dict], predict) -> dict[str, float]:
    grouped = _by_event(rows)
    scores = {}
    for event, block in grouped.items():
        total = 0.0
        for row in block:
            probability = min(1.0, max(0.0, float(predict(row))))
            total += (probability - row["label_yes"]) ** 2
        scores[event] = total / len(block)
    return scores


def _mean_events(events: list[str], scores: dict[str, float]) -> Optional[float]:
    if not events:
        return None
    return round(sum(scores[event] for event in events) / len(events), 6)


def _bootstrap_diff(events: list[str], left: dict[str, float], right: dict[str, float], cfg: LearningConfig) -> Optional[dict]:
    if len(events) < 2:
        return None
    rng = random.Random(cfg.bootstrap_seed)
    draws = []
    width = len(events)
    for _ in range(cfg.bootstrap_draws):
        sample = [events[rng.randrange(width)] for _ in range(width)]
        diff = sum(left[event] - right[event] for event in sample) / width
        draws.append(diff)
    draws.sort()
    lo_i = max(0, math.floor(0.025 * (cfg.bootstrap_draws - 1)))
    hi_i = min(cfg.bootstrap_draws - 1, math.ceil(0.975 * (cfg.bootstrap_draws - 1)))
    point = sum(left[event] - right[event] for event in events) / width
    return {
        "point": round(point, 6),
        "ci95": [round(draws[lo_i], 6), round(draws[hi_i], 6)],
    }


def _beats(gap: Optional[dict], margin: float) -> bool:
    if not gap:
        return False
    hi = gap["ci95"][1]
    return gap["point"] <= -margin and hi < 0


def _slice_of(row: dict, family: str) -> str:
    if family == "vol":
        return vol_bucket(row.get("dvol"), row.get("dvol_prev"), row.get("rv_short"), row.get("rv_long"))
    if family == "tod":
        return tod_bucket(row.get("recorded_at") or "")
    if family == "price":
        return price_bucket(row.get("yes_ask"))
    if family == "horizon":
        return horizon_bucket(row.get("minutes_left"))
    return spread_bucket(row.get("spread"))


def _pattern_rows(rows: list[dict], events: list[str], family: str, bucket: str) -> list[dict]:
    chosen = set(events)
    return [row for row in rows if row["event_ticker"] in chosen and _slice_of(row, family) == bucket]


def _confirm_model_vs_market(rows: list[dict], events: list[str], cfg: LearningConfig) -> tuple[bool, Optional[dict], Optional[float], Optional[float]]:
    if len(events) < 2:
        return False, None, None, None
    model_scores = _event_brier(rows, lambda row: row["model_p_yes"])
    market_scores = _event_brier(rows, lambda row: row["market_p"])
    gap = _bootstrap_diff(events, model_scores, market_scores, cfg)
    return _beats(gap, cfg.brier_margin), gap, _mean_events(events, model_scores), _mean_events(events, market_scores)


def _patterns(train_rows: list[dict], hold_rows: list[dict], train_events: list[str], hold_events: list[str], cfg: LearningConfig) -> list[dict]:
    reports = []
    families: dict[str, set[str]] = {family: set() for family in PATTERN_FAMILIES}
    for row in train_rows + hold_rows:
        for family in PATTERN_FAMILIES:
            families[family].add(_slice_of(row, family))
    for family in PATTERN_FAMILIES:
        buckets = families[family] or {None}
        for bucket in sorted(buckets, key=lambda item: "" if item is None else item):
            if bucket is None:
                reports.append(
                    {
                        "family": family,
                        "bucket": None,
                        "name": family,
                        "status": "insufficient_sample",
                        "validated": False,
                        "n_events": 0,
                        "n_holdout_events": 0,
                        "note": "no verified rows in this family",
                        "trading_use": "none",
                    }
                )
                continue
            train_ids = {row["event_ticker"] for row in _pattern_rows(train_rows, train_events, family, bucket)}
            hold_ids = [event for event in hold_events if any(row["event_ticker"] == event for row in _pattern_rows(hold_rows, hold_events, family, bucket))]
            train_confirmed, _gap, _bm, _bk = _confirm_model_vs_market(
                _pattern_rows(train_rows, list(train_ids), family, bucket),
                sorted(train_ids),
                cfg,
            )
            hold_block = _pattern_rows(hold_rows, hold_ids, family, bucket)
            hold_confirmed, hold_gap, brier_model, brier_market = _confirm_model_vs_market(hold_block, hold_ids, cfg)
            total = len(train_ids) + len(hold_ids)
            if total < cfg.min_slice_events or len(hold_ids) < cfg.min_slice_holdout:
                if train_confirmed and len(train_ids) >= cfg.min_slice_events:
                    status = "candidate"
                    note = "in-sample difference only; holdout sample is below the minimum"
                else:
                    status = "insufficient_sample"
                    note = "not enough event clusters in this pre-registered bucket"
                hold_gap = None
                brier_model = None
                brier_market = None
            elif hold_confirmed:
                status = "supported"
                note = "holdout event-grouped Brier beat the market; not a trading validation"
            elif train_confirmed:
                status = "candidate"
                note = "train difference was not confirmed on the chronological holdout"
            else:
                status = "abstain"
                note = "no out-of-sample improvement over the market"
            reports.append(
                {
                    "family": family,
                    "bucket": bucket,
                    "name": f"{family}:{bucket}",
                    "status": status,
                    "validated": False,
                    "n_events": total,
                    "n_holdout_events": len(hold_ids),
                    "brier_model": brier_model,
                    "brier_market": brier_market,
                    "holdout_gap_vs_market": hold_gap,
                    "note": note,
                    "trading_use": "none",
                }
            )
    return reports


def build_report(rows: list[dict], sample: dict, cfg: LearningConfig, now: datetime, last_error: Optional[str]) -> dict[str, Any]:
    sample = dict(sample)
    sample["n_scorable"] = len(rows)
    events = _event_order(rows)
    sample["n_events_scorable"] = len(events)
    needed = cfg.min_train_events + cfg.min_holdout_events
    if len(events) < needed:
        sample["quality"] = "none" if sample["n_verified"] == 0 else "insufficient"
        abstentions = [
            f"need at least {needed} verified event clusters for a holdout; have {len(events)}"
        ]
        if sample["n_verified"] == 0:
            abstentions = ["no verified official settlements; cold start abstains"]
        report = _base_report(cfg, sample, abstentions, now)
        report["input_hash"] = _hash_rows(rows, cfg)
        report["last_error"] = last_error
        return report

    train_events, hold_events = _split_events(events, cfg)
    grouped = _by_event(rows)
    train_rows = [row for event in train_events for row in grouped[event]]
    hold_rows = [row for event in hold_events for row in grouped[event]]
    knots = _isotonic_knots(train_rows)
    sample["quality"] = "shadow_eval"
    model_scores = _event_brier(hold_rows, lambda row: row["model_p_yes"])
    market_scores = _event_brier(hold_rows, lambda row: row["market_p"])
    calibrated_scores = _event_brier(hold_rows, lambda row: apply_knots(knots, row["model_p_yes"]))
    gap_market = _bootstrap_diff(hold_events, calibrated_scores, market_scores, cfg)
    gap_model = _bootstrap_diff(hold_events, calibrated_scores, model_scores, cfg)
    train_cal = _event_brier(train_rows, lambda row: apply_knots(knots, row["model_p_yes"]))
    train_market = _event_brier(train_rows, lambda row: row["market_p"])
    train_model = _event_brier(train_rows, lambda row: row["model_p_yes"])
    train_gap_market = _bootstrap_diff(train_events, train_cal, train_market, cfg)
    train_gap_model = _bootstrap_diff(train_events, train_cal, train_model, cfg)
    hold_ok = _beats(gap_market, cfg.brier_margin) and _beats(gap_model, cfg.brier_margin)
    train_ok = _beats(train_gap_market, cfg.brier_margin) and _beats(train_gap_model, cfg.brier_margin)
    if hold_ok:
        cal_status = "supported"
        cal_note = "holdout calibration beat the raw model and the market; trading limits are unchanged"
    elif train_ok:
        cal_status = "candidate"
        cal_note = "in-sample fit was not confirmed on the chronological holdout"
    else:
        cal_status = "abstain"
        cal_note = "calibration did not beat both the raw model and the market out of sample"
    patterns = _patterns(train_rows, hold_rows, train_events, hold_events, cfg)
    learned = cal_status == "supported" or any(item["status"] == "supported" for item in patterns)
    promotion = (
        cal_status == "supported"
        and len(events) >= cfg.promotion_min_events
        and len(hold_events) >= cfg.promotion_min_holdout
    )
    abstentions = ["paper sizing, EDGE_BUFFER, and MIN_PRICE are unchanged"]
    if not learned:
        abstentions.append("no pre-registered pattern cleared the holdout gate")
    if not promotion:
        abstentions.append("promotion gate is closed")
    else:
        abstentions.append("promotion_eligible is informational only; this version does not apply it")
    report = _base_report(cfg, sample, abstentions, now)
    report.update(
        {
            "calibration": {
                "status": cal_status,
                "validated": False,
                "note": cal_note,
                "train_events": len(train_events),
                "holdout_events": len(hold_events),
                "brier_model": _mean_events(hold_events, model_scores),
                "brier_market": _mean_events(hold_events, market_scores),
                "brier_calibrated": _mean_events(hold_events, calibrated_scores),
                "holdout_gap_vs_market": gap_market,
                "holdout_gap_vs_model": gap_model,
                "knots": knots,
            },
            "discovered_edges": [item for item in patterns if item["status"] == "supported"],
            "holdout_events": list(hold_events),
            "input_hash": _hash_rows(rows, cfg),
            "last_error": last_error,
            "learned": learned,
            "patterns": patterns,
            "promotion_eligible": promotion,
            "train_events": list(train_events),
        }
    )
    for item in report["discovered_edges"]:
        item["validated"] = False
        item["trading_use"] = "none"
    report["validated_for_trading"] = False
    report["trading_impact"] = "none"
    report["risk_adjustment"] = _risk_adjustment()
    return report


def _latest_payload(conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute("SELECT payload_json FROM artifacts ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return json.loads(row["payload_json"])


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM learner_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def _suppress_stale(payload: dict) -> dict:
    report = json.loads(json.dumps(payload))
    report["artifact_stale"] = True
    report["learned"] = False
    report["claims_allowed"] = False
    report["promotion_eligible"] = False
    report["validated_for_trading"] = False
    report["trading_impact"] = "none"
    report["risk_adjustment"] = _risk_adjustment()
    report["discovered_edges"] = []
    calibration = report.get("calibration") or {}
    calibration["status"] = "abstain"
    calibration["validated"] = False
    calibration["note"] = "artifact does not match current verified labels; stale fit suppressed"
    report["calibration"] = calibration
    for pattern in report.get("patterns") or []:
        pattern["validated"] = False
        if pattern.get("status") == "supported":
            pattern["status"] = "abstain"
            pattern["note"] = "suppressed because the artifact is stale"
    abstentions = list(report.get("abstentions") or [])
    abstentions.append("stale artifact suppressed")
    report["abstentions"] = abstentions
    return report


def _overlay_live(payload: dict, sample: dict) -> dict:
    report = json.loads(json.dumps(payload))
    frozen = report.get("sample") or {}
    for key in (
        "n_predictions",
        "n_pending",
        "n_verified",
        "n_events_verified",
        "n_excluded_lookahead",
        "n_conflicts",
    ):
        frozen[key] = sample.get(key, frozen.get(key))
    report["sample"] = frozen
    report["risk_adjustment"] = _risk_adjustment()
    report["validated_for_trading"] = False
    report["trading_impact"] = "none"
    return report


def status(cfg: Optional[LearningConfig] = None) -> dict[str, Any]:
    """Read the latest artifact. Does not fetch and does not refit."""
    cfg = cfg or LearningConfig()
    path = learning_db_path()
    if not path.exists():
        return cold_start_report(cfg)
    with _db() as conn:
        sample = _counts(conn, cfg)
        rows = _scorable_rows(conn)
        current = _hash_rows(rows, cfg)
        payload = _latest_payload(conn)
        last_error = _meta(conn, "last_error")
    if payload is None:
        report = cold_start_report(cfg)
        report["sample"] = {**report["sample"], **{k: sample[k] for k in sample if k in (
            "n_predictions", "n_pending", "n_verified", "n_events_verified", "n_excluded_lookahead", "n_conflicts"
        )}}
        report["input_hash"] = current
        report["last_error"] = last_error
        return report
    if payload.get("input_hash") != current:
        report = _suppress_stale(payload)
        report = _overlay_live(report, sample)
        report["last_error"] = last_error
        return report
    report = _overlay_live(payload, sample)
    report["artifact_stale"] = False
    report["last_error"] = last_error
    return report


def _export_artifact(payload: dict) -> None:
    path = artifact_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def refresh(fetch_json=None, now: Optional[datetime] = None, cfg: Optional[LearningConfig] = None) -> dict[str, Any]:
    """Label pending tickers from official results, then refit if the verified set changed."""
    _refuse_if_live()
    cfg = cfg or LearningConfig()
    now = now or datetime.now(timezone.utc)
    offline = os.environ.get("BTC_HOURLY_OFFLINE") == "1" and fetch_json is None
    with _db() as conn:
        pending = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT ticker FROM predictions WHERE label_status=?",
                (PENDING,),
            ).fetchall()
        ]
    if pending and not offline:
        try:
            payloads = fetch_market_results(pending, fetch_json=fetch_json)
        except Exception as exc:
            with _db() as conn:
                conn.execute(
                    "INSERT INTO learner_meta(key,value) VALUES('last_error',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"official lookup failed: {exc}",),
                )
            payloads = {}
        if payloads:
            apply_official_results(payloads, now=now)
    try:
        with _db() as conn:
            sample = _counts(conn, cfg)
            rows = _scorable_rows(conn)
            last_error = _meta(conn, "last_error")
        report = build_report(rows, sample, cfg, now, last_error)
    except Exception as exc:
        with _db() as conn:
            conn.execute(
                "INSERT INTO learner_meta(key,value) VALUES('last_error',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(exc),),
            )
        failed = status(cfg)
        failed["update_failed"] = True
        failed["learned"] = False
        failed["claims_allowed"] = False
        failed["promotion_eligible"] = False
        failed["validated_for_trading"] = False
        failed["last_error"] = str(exc)
        failed["risk_adjustment"] = _risk_adjustment()
        return failed

    report["update_failed"] = False
    written = False
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = _latest_payload(conn)
        if previous is None or previous.get("input_hash") != report["input_hash"]:
            conn.execute(
                "INSERT INTO artifacts(input_hash, created_at, payload_json) VALUES (?,?,?)",
                (report["input_hash"], report["trained_at"], json.dumps(report, sort_keys=True)),
            )
            written = True
        conn.execute("DELETE FROM learner_meta WHERE key='last_error'")
    if written:
        try:
            _export_artifact(report)
        except Exception as exc:
            message = f"artifact export failed: {exc}"
            with _db() as conn:
                conn.execute(
                    "INSERT INTO learner_meta(key,value) VALUES('last_error',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (message,),
                )
            report["last_error"] = message
    else:
        report = status(cfg)
    report["updated"] = written
    report["risk_adjustment"] = _risk_adjustment()
    report["validated_for_trading"] = False
    report["trading_impact"] = "none"
    return report


def risk_adjustment(report: Optional[dict] = None) -> dict[str, Any]:
    """Always zero. Present so callers cannot mistake a report for permission to add risk."""
    del report
    return _risk_adjustment()
