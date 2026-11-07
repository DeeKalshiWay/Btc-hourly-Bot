"""Deterministic offline tests for shadow learning and official settlement.

No network. No live orders. Fixtures live under a temp directory.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(HERE))

import config
import db
import learning
from broker import Broker, PaperFill

PT = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 3, 1, 18, 0, tzinfo=PT)
CFG = learning.LearningConfig()


def _finalized(result: str, **extra) -> dict:
    market = {
        "status": "finalized",
        "result": result,
        "expiration_value": extra.pop("expiration_value", "100"),
        "settlement_ts": extra.pop("settlement_ts", "2026-01-02T00:00:00Z"),
    }
    market.update(extra)
    return {"market": market}


def _row(index: int, model_p: float, *, minute_offset: int = 0, event: str | None = None, ticker: str | None = None) -> dict:
    close = datetime(2026, 1, 5, 12, 0, tzinfo=PT) + timedelta(minutes=minute_offset)
    recorded = close - timedelta(minutes=20)
    return {
        "ticker": ticker or f"KXBTCD-26JAN05-T{index}",
        "event_ticker": event or f"KXBTCD-26JAN05-E{index}",
        "close_time": close.isoformat(),
        "recorded_at": recorded.isoformat(),
        "strike": 100000.0,
        "minutes_left": 30,
        "spot": 100100.0,
        "sigma": 0.50,
        "dvol": 50.0,
        "dvol_prev": 50.0,
        "rv_short": 0.40,
        "rv_long": 0.45,
        "model_p_yes": model_p,
        "yes_bid": 0.45,
        "yes_ask": 0.55,
        "side": "YES",
        "disposition": "shadow",
        "gate_reason": "fixture",
        "model_version": learning.MODEL_VERSION,
    }


class LearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.book = root / "book.db"
        self.learn_db = root / "learning.db"
        self.artifact = root / "learning_artifact.json"
        os.environ["BTC_HOURLY_LEARNING_DB"] = str(self.learn_db)
        os.environ["BTC_HOURLY_LEARNING_ARTIFACT"] = str(self.artifact)
        os.environ["BTC_HOURLY_OFFLINE"] = "1"
        config.DB_PATH = self.book
        config.LIVE_TRADING = False
        self._original_build = learning.build_report

    def tearDown(self) -> None:
        learning.build_report = self._original_build
        config.LIVE_TRADING = False
        os.environ.pop("BTC_HOURLY_LEARNING_DB", None)
        os.environ.pop("BTC_HOURLY_LEARNING_ARTIFACT", None)
        os.environ.pop("BTC_HOURLY_OFFLINE", None)
        self.tmp.cleanup()

    def _record_labeled(self, specs: list[tuple[float, str]]) -> None:
        rows = []
        payloads = {}
        for index, (model_p, result) in enumerate(specs):
            row = _row(index, model_p, minute_offset=index)
            rows.append(row)
            payloads[row["ticker"]] = _finalized(result)
        learning.record_observations(rows, now=NOW)
        learning.apply_official_results(payloads, now=NOW)

    def _artifact_count(self) -> int:
        with sqlite3.connect(self.learn_db) as conn:
            return conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    def _knots(self) -> list:
        with sqlite3.connect(self.learn_db) as conn:
            raw = conn.execute("SELECT payload_json FROM artifacts ORDER BY id DESC LIMIT 1").fetchone()[0]
        return json.loads(raw)["calibration"]["knots"]

    def test_official_parser_rejects_unsettled_and_scalar(self) -> None:
        self.assertEqual(learning.official_binary_result(_finalized("yes")), "yes")
        self.assertEqual(learning.official_binary_result(_finalized("no")), "no")
        self.assertIsNone(learning.official_binary_result({"market": {"status": "determined", "result": "yes"}}))
        self.assertIsNone(learning.official_binary_result({"market": {"status": "disputed", "result": "yes"}}))
        self.assertIsNone(learning.official_binary_result({"market": {"status": "amended", "result": "no"}}))
        self.assertIsNone(learning.official_binary_result({"market": {"status": "closed", "result": ""}}))
        self.assertIsNone(learning.official_binary_result({"market": {"status": "active", "result": "yes"}}))
        self.assertIsNone(learning.official_binary_result(_finalized("scalar")))
        self.assertIsNone(
            learning.official_binary_result({"market": {"status": "finalized", "expiration_value": "99999", "result": ""}})
        )
        self.assertEqual(learning.verified_outcomes({"A": _finalized("yes"), "B": {"market": {"status": "determined", "result": "yes"}}}), {"A": "yes"})
        fetched = learning.fetch_market_results(
            ["A", "B"],
            fetch_json=lambda ticker: _finalized("yes") if ticker == "A" else {"market": {"status": "closed", "result": ""}},
        )
        self.assertEqual(learning.verified_outcomes(fetched), {"A": "yes"})

    def test_cold_start_abstains_without_creating_claims(self) -> None:
        self.assertFalse(self.learn_db.exists())
        report = learning.status()
        self.assertFalse(report["learned"])
        self.assertFalse(report["promotion_eligible"])
        self.assertFalse(report["validated_for_trading"])
        self.assertEqual(report["trading_impact"], "none")
        self.assertEqual(report["mode"], "shadow")
        self.assertEqual(report["discovered_edges"], [])
        self.assertEqual(report["sample"]["quality"], "none")
        self.assertEqual(report["sample"]["n_events_verified"], 0)
        self.assertTrue(all(item["validated"] is False for item in report["patterns"]))
        self.assertTrue(any(item["status"] == "insufficient_sample" for item in report["patterns"]))
        self.assertEqual(report["risk_adjustment"]["edge_buffer_delta"], 0.0)
        self.assertEqual(report["risk_adjustment"]["min_price_delta"], 0.0)
        self.assertEqual(report["risk_adjustment"]["max_stake_multiplier"], 1.0)
        self.assertEqual(report["risk_adjustment"]["new_trades"], 0)
        self.assertFalse(self.learn_db.exists())

    def test_duplicate_ingestion_is_idempotent(self) -> None:
        row = _row(1, 0.62)
        first = learning.record_observations([row, row], now=NOW)
        self.assertEqual(first["inserted"], 1)
        changed = dict(row)
        changed["model_p_yes"] = 0.11
        changed["disposition"] = "shadow"
        second = learning.record_observations([changed], now=NOW)
        self.assertEqual(second["inserted"], 0)
        upgraded = dict(row)
        upgraded["disposition"] = "paper_filled"
        third = learning.record_observations([upgraded], now=NOW)
        self.assertEqual(third["upgraded"], 1)
        with sqlite3.connect(self.learn_db) as conn:
            conn.row_factory = sqlite3.Row
            stored = conn.execute("SELECT model_p_yes, disposition, COUNT(*) c FROM predictions").fetchone()
        self.assertEqual(stored["c"], 1)
        self.assertEqual(stored["model_p_yes"], 0.62)
        self.assertEqual(stored["disposition"], "paper_filled")
        learning.record_observations([upgraded], now=NOW)
        with sqlite3.connect(self.learn_db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_labels_are_official_idempotent_and_not_rewritten(self) -> None:
        row = _row(1, 0.7)
        learning.record_observations([row], now=NOW)
        pending = learning.apply_official_results({"KXBTCD-26JAN05-T1": {"market": {"status": "determined", "result": "yes"}}}, now=NOW)
        self.assertEqual(pending["applied"], 0)
        learning.apply_official_results({row["ticker"]: _finalized("scalar", expiration_value="100")}, now=NOW)
        learning.apply_official_results(
            {row["ticker"]: {"market": {"status": "finalized", "result": "", "expiration_value": "200000"}}},
            now=NOW,
        )
        with sqlite3.connect(self.learn_db) as conn:
            label = conn.execute("SELECT label_status, label_yes FROM predictions").fetchone()
        self.assertEqual(label[0], "pending")
        self.assertIsNone(label[1])
        first = learning.apply_official_results({row["ticker"]: _finalized("yes")}, now=NOW)
        second = learning.apply_official_results({row["ticker"]: _finalized("yes")}, now=NOW + timedelta(days=1))
        self.assertEqual(first["applied"], 1)
        self.assertEqual(second["unchanged"], 1)
        conflict = learning.apply_official_results({row["ticker"]: _finalized("no")}, now=NOW)
        again = learning.apply_official_results({row["ticker"]: _finalized("no")}, now=NOW)
        self.assertEqual(conflict["conflicts"], 1)
        self.assertEqual(again["conflicts"], 1)
        with sqlite3.connect(self.learn_db) as conn:
            stored = conn.execute("SELECT label_result, label_yes, label_source, labeled_at FROM predictions").fetchone()
            conflicts = conn.execute("SELECT COUNT(*) FROM label_conflicts").fetchone()[0]
        self.assertEqual(stored[0], "yes")
        self.assertEqual(stored[1], 1)
        self.assertEqual(stored[2], "official_kalshi")
        self.assertEqual(stored[3], NOW.astimezone(ZoneInfo("UTC")).isoformat())
        self.assertEqual(conflicts, 1)

    def test_tiny_book_and_one_event_cluster_do_not_learn(self) -> None:
        self._record_labeled([(0.8, "yes"), (0.2, "no"), (0.8, "yes"), (0.2, "no"), (0.8, "yes")])
        report = learning.refresh(now=NOW, cfg=CFG)
        self.assertFalse(report["learned"])
        self.assertFalse(report["promotion_eligible"])
        self.assertFalse(report["validated_for_trading"])
        self.assertEqual(report["discovered_edges"], [])
        self.assertEqual(report["sample"]["quality"], "insufficient")
        self.assertEqual(report["calibration"]["status"], "insufficient_sample")
        self.assertTrue(all(item["status"] != "supported" and item["validated"] is False for item in report["patterns"]))
        clustered = [_row(index, 0.8, event="KXBTCD-SAME", ticker=f"KXBTCD-SAME-T{index}") for index in range(10)]
        learning.record_observations(clustered, now=NOW)
        learning.apply_official_results({row["ticker"]: _finalized("yes") for row in clustered}, now=NOW)
        clustered_report = learning.refresh(now=NOW, cfg=CFG)
        self.assertEqual(clustered_report["sample"]["n_events_scorable"], 6)
        self.assertLess(clustered_report["sample"]["n_events_scorable"], clustered_report["sample"]["n_scorable"])
        self.assertFalse(clustered_report["learned"])
        self.assertFalse(self.book.exists())

    def test_holdout_can_support_a_shadow_fit_without_changing_risk(self) -> None:
        specs = []
        for index in range(56):
            model_p = 0.8 if index % 2 == 0 else 0.2
            specs.append((model_p, "yes" if model_p > 0.5 else "no"))
        self._record_labeled(specs)
        report = learning.refresh(now=NOW, cfg=CFG)
        other = tempfile.TemporaryDirectory()
        try:
            os.environ["BTC_HOURLY_LEARNING_DB"] = str(Path(other.name) / "learning.db")
            os.environ["BTC_HOURLY_LEARNING_ARTIFACT"] = str(Path(other.name) / "learning_artifact.json")
            self._record_labeled(specs)
            again = learning.refresh(now=NOW, cfg=CFG)
        finally:
            os.environ["BTC_HOURLY_LEARNING_DB"] = str(self.learn_db)
            os.environ["BTC_HOURLY_LEARNING_ARTIFACT"] = str(self.artifact)
            other.cleanup()
        self.assertEqual(report["input_hash"], again["input_hash"])
        self.assertEqual(report["calibration"]["knots"], again["calibration"]["knots"])
        self.assertEqual(report["train_events"], again["train_events"])
        self.assertEqual(report["holdout_events"], again["holdout_events"])
        self.assertEqual(report["calibration"]["status"], "supported")
        self.assertTrue(report["learned"])
        self.assertFalse(report["promotion_eligible"])
        self.assertFalse(report["validated_for_trading"])
        self.assertEqual(report["trading_impact"], "none")
        self.assertEqual(learning.risk_adjustment(report)["edge_buffer_delta"], 0.0)
        self.assertEqual(learning.risk_adjustment(report)["new_trades"], 0)
        self.assertGreaterEqual(len(report["holdout_events"]), CFG.min_holdout_events)
        self.assertGreaterEqual(len(report["train_events"]), CFG.min_train_events)
        self.assertEqual(set(report["train_events"]) & set(report["holdout_events"]), set())
        self.assertLess(report["calibration"]["brier_calibrated"], report["calibration"]["brier_market"])
        self.assertLess(report["calibration"]["brier_calibrated"], report["calibration"]["brier_model"])
        self.assertTrue(report["discovered_edges"])
        self.assertTrue(all(item["validated"] is False and item["trading_use"] == "none" for item in report["discovered_edges"]))
        second = learning.refresh(now=NOW, cfg=CFG)
        self.assertFalse(second["updated"])
        self.assertEqual(self._artifact_count(), 1)
        saved = json.loads(self.artifact.read_text())
        self.assertEqual(saved["input_hash"], report["input_hash"])
        self.assertFalse(self.artifact.with_suffix(self.artifact.suffix + ".tmp").exists())

    def test_reversed_holdout_is_not_called_supported(self) -> None:
        specs = []
        for index in range(40):
            model_p = 0.8 if index % 2 == 0 else 0.2
            specs.append((model_p, "yes" if model_p > 0.5 else "no"))
        for index in range(40, 56):
            specs.append((0.8, "no"))
        self._record_labeled(specs)
        report = learning.refresh(now=NOW, cfg=CFG)
        self.assertEqual(report["calibration"]["status"], "candidate")
        self.assertFalse(report["learned"])
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(report["discovered_edges"], [])
        self.assertTrue(all(item["status"] != "supported" and item["validated"] is False for item in report["patterns"]))

    def test_holdout_labels_do_not_change_the_fit(self) -> None:
        specs = []
        for index in range(56):
            model_p = 0.8 if index % 2 == 0 else 0.2
            specs.append((model_p, "yes" if model_p > 0.5 else "no"))
        self._record_labeled(specs)
        first = learning.refresh(now=NOW, cfg=CFG)
        knots = self._knots()
        holdout_event = first["holdout_events"][0]
        train_event = first["train_events"][0]
        self.assertLess(
            min(self._close(event) for event in first["train_events"]),
            min(self._close(event) for event in first["holdout_events"]),
        )
        with sqlite3.connect(self.learn_db) as conn:
            conn.execute(
                "UPDATE predictions SET label_yes=CASE WHEN label_yes=1 THEN 0 ELSE 1 END WHERE event_ticker=?",
                (holdout_event,),
            )
            conn.commit()
        leaked = learning.refresh(now=NOW, cfg=CFG)
        self.assertEqual(self._knots(), knots)
        self.assertEqual(leaked["train_events"], first["train_events"])
        self.assertNotEqual(leaked["input_hash"], first["input_hash"])
        with sqlite3.connect(self.learn_db) as conn:
            conn.execute(
                "UPDATE predictions SET label_yes=CASE WHEN label_yes=1 THEN 0 ELSE 1 END WHERE event_ticker=?",
                (train_event,),
            )
            conn.commit()
        changed = learning.refresh(now=NOW, cfg=CFG)
        self.assertNotEqual(self._knots(), knots)
        self.assertIn(train_event, changed["train_events"])
        self.assertNotIn(holdout_event, changed["train_events"])

    def test_lookahead_rows_are_excluded(self) -> None:
        close = datetime(2026, 1, 5, 12, 0, tzinfo=PT)
        late = _row(1, 0.8)
        late["recorded_at"] = (close + timedelta(minutes=5)).isoformat()
        late["close_time"] = close.isoformat()
        learning.record_observations([late], now=NOW)
        learning.apply_official_results({late["ticker"]: _finalized("yes")}, now=NOW)
        report = learning.refresh(now=NOW, cfg=CFG)
        self.assertEqual(report["sample"]["n_verified"], 1)
        self.assertEqual(report["sample"]["n_excluded_lookahead"], 1)
        self.assertEqual(report["sample"]["n_scorable"], 0)
        self.assertFalse(report["learned"])

    def test_failed_refit_suppresses_stale_claims(self) -> None:
        self._record_labeled([(0.8, "yes")])
        learning.refresh(now=NOW, cfg=CFG)
        before = self._artifact_count()
        extra = _row(99, 0.2, minute_offset=99)
        learning.record_observations([extra], now=NOW)
        learning.apply_official_results({extra["ticker"]: _finalized("no")}, now=NOW)

        def boom(*_args, **_kwargs):
            raise RuntimeError("forced fit failure")

        learning.build_report = boom
        failed = learning.refresh(now=NOW, cfg=CFG)
        learning.build_report = self._original_build
        self.assertTrue(failed["update_failed"])
        self.assertFalse(failed["learned"])
        self.assertFalse(failed["claims_allowed"])
        self.assertEqual(failed["risk_adjustment"]["max_stake_multiplier"], 1.0)
        self.assertEqual(self._artifact_count(), before)
        follow = learning.status(CFG)
        self.assertTrue(follow["artifact_stale"])
        self.assertFalse(follow["learned"])
        self.assertFalse(follow["promotion_eligible"])
        self.assertEqual(follow["discovered_edges"], [])

    def test_large_sample_may_be_promotion_eligible_and_still_cannot_add_risk(self) -> None:
        specs = []
        for index in range(80):
            model_p = 0.8 if index % 2 == 0 else 0.2
            specs.append((model_p, "yes" if model_p > 0.5 else "no"))
        self._record_labeled(specs)
        report = learning.refresh(now=NOW, cfg=CFG)
        self.assertTrue(report["promotion_eligible"])
        self.assertTrue(report["learned"])
        self.assertFalse(report["validated_for_trading"])
        self.assertEqual(report["trading_impact"], "none")
        adjustment = learning.risk_adjustment(report)
        self.assertEqual(adjustment["edge_buffer_delta"], 0.0)
        self.assertEqual(adjustment["min_price_delta"], 0.0)
        self.assertEqual(adjustment["max_stake_multiplier"], 1.0)
        self.assertEqual(adjustment["new_trades"], 0)

    def test_live_flag_refuses_learning(self) -> None:
        config.LIVE_TRADING = True
        with self.assertRaises(RuntimeError):
            learning.record_observations([_row(1, 0.5)], now=NOW)
        with self.assertRaises(RuntimeError):
            learning.refresh(now=NOW, cfg=CFG)
        config.LIVE_TRADING = False

    def _close(self, event: str) -> datetime:
        with sqlite3.connect(self.learn_db) as conn:
            raw = conn.execute("SELECT MIN(close_time) FROM predictions WHERE event_ticker=?", (event,)).fetchone()[0]
        return datetime.fromisoformat(raw)

    def test_settlement_uses_official_result_once_and_preserves_history(self) -> None:
        broker = Broker()
        close = (NOW - timedelta(hours=2)).isoformat()
        future = (NOW + timedelta(days=1)).isoformat()
        broker.buy_yes_paper(
            PaperFill("KX-OPEN", "YES", 2, 0.40, 100.0, "KX-E", close, 0.80, 0.6, 0.40, 0.05)
        )
        cash_after_buy = db.cash()
        self.assertAlmostEqual(cash_after_buy, 999.20, places=2)
        untouched = broker.settle_due(1_000_000.0, {}, now=NOW)
        self.assertEqual(untouched, [])
        self.assertAlmostEqual(db.cash(), cash_after_buy, places=6)
        with db.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM trades WHERE ticker='KX-OPEN'").fetchone()["status"], "open")
        raw_payload = broker.settle_due(1.0, {"KX-OPEN": {"status": "finalized", "result": "yes"}}, now=NOW)
        self.assertEqual(raw_payload, [])
        self.assertAlmostEqual(db.cash(), cash_after_buy, places=6)
        broker.buy_yes_paper(
            PaperFill("KX-FUTURE", "YES", 1, 0.50, 100.0, "KX-E2", future, 0.50, 0.6, 0.50, 0.05)
        )
        cash_with_future = db.cash()
        not_yet = broker.settle_due(0.0, {"KX-FUTURE": "yes"}, now=NOW)
        self.assertEqual(not_yet, [])
        self.assertAlmostEqual(db.cash(), cash_with_future, places=6)
        won = broker.settle_due(0.0, {"KX-OPEN": "yes"}, now=NOW)
        self.assertEqual(len(won), 1)
        self.assertTrue(won[0]["win"])
        self.assertEqual(won[0]["settlement_source"], "official_kalshi")
        paid = db.cash()
        self.assertAlmostEqual(paid, cash_with_future + 2.0, places=6)
        again = broker.settle_due(0.0, {"KX-OPEN": "yes"}, now=NOW)
        self.assertEqual(again, [])
        self.assertAlmostEqual(db.cash(), paid, places=6)
        meta, recon = db.ledger_check()
        self.assertAlmostEqual(meta, recon, places=6)
        with db.db() as conn:
            row = conn.execute("SELECT status, official_result, settlement_source, pnl FROM trades WHERE ticker='KX-OPEN'").fetchone()
        self.assertEqual(row["status"], "won")
        self.assertEqual(row["official_result"], "yes")
        self.assertEqual(row["settlement_source"], "official_kalshi")
        before_learn = self._book_rows()
        learning.refresh(now=NOW, cfg=CFG)
        self.assertEqual(self._book_rows(), before_learn)

    def test_historical_spot_settlement_is_not_relabeled_or_recashed(self) -> None:
        raw = sqlite3.connect(self.book)
        raw.execute(
            """
            CREATE TABLE trades (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              ticker TEXT NOT NULL,
              side TEXT NOT NULL,
              qty INTEGER NOT NULL,
              price REAL NOT NULL,
              strike REAL,
              event_ticker TEXT,
              close_time TEXT,
              cost REAL NOT NULL,
              model_p REAL,
              market_ask REAL,
              edge REAL,
              status TEXT NOT NULL DEFAULT 'open',
              pnl REAL,
              settled_ts TEXT
            )
            """
        )
        raw.execute(
            """
            CREATE TABLE meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )
        raw.execute(
            "INSERT INTO trades (ts,ticker,side,qty,price,strike,event_ticker,close_time,cost,model_p,market_ask,edge,status,pnl,settled_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-11-05T12:00:00-08:00",
                "KX-OLD",
                "YES",
                1,
                0.40,
                100.0,
                "KX-OLD-E",
                "2026-11-05T13:00:00-08:00",
                5.0,
                0.55,
                0.40,
                0.02,
                "won",
                10.0,
                "2026-11-05T13:05:00-08:00",
            ),
        )
        raw.execute("INSERT INTO meta(key,value) VALUES('cash','1105.000000')")
        raw.commit()
        raw.close()
        with db.db() as conn:
            before = dict(conn.execute("SELECT * FROM trades WHERE ticker='KX-OLD'").fetchone())
        self.assertIsNone(before["settlement_source"])
        self.assertIsNone(before["official_result"])
        broker = Broker()
        settled = broker.settle_due(1.0, {"KX-OLD": "no"}, now=NOW)
        self.assertEqual(settled, [])
        self.assertAlmostEqual(db.cash(), 1105.0, places=6)
        with db.db() as conn:
            after = dict(conn.execute("SELECT * FROM trades WHERE ticker='KX-OLD'").fetchone())
        self.assertEqual(after["status"], "won")
        self.assertEqual(after["pnl"], 10.0)
        self.assertEqual(after["settled_ts"], before["settled_ts"])
        self.assertIsNone(after["official_result"])
        self.assertIsNone(after["settlement_source"])

    def test_losing_official_result_pays_zero_once(self) -> None:
        broker = Broker()
        close = (NOW - timedelta(hours=1)).isoformat()
        broker.buy_yes_paper(PaperFill("KX-NO", "NO", 3, 0.30, 50.0, "KX-NO-E", close, 0.90, 0.4, 0.30, 0.04))
        cash_open = db.cash()
        lost = broker.settle_due(999.0, {"KX-NO": "yes"}, now=NOW)
        self.assertEqual(len(lost), 1)
        self.assertFalse(lost[0]["win"])
        self.assertAlmostEqual(db.cash(), cash_open, places=6)
        broker.settle_due(0.0, {"KX-NO": "yes"}, now=NOW)
        self.assertAlmostEqual(db.cash(), cash_open, places=6)
        meta, recon = db.ledger_check()
        self.assertAlmostEqual(meta, recon, places=6)

    def _book_rows(self) -> list:
        with db.db() as conn:
            trades = [tuple(row) for row in conn.execute("SELECT * FROM trades ORDER BY id")]
            cash = conn.execute("SELECT value FROM meta WHERE key='cash'").fetchone()["value"]
        return [cash, trades]

    def test_source_has_no_live_order_path_or_risk_loosening(self) -> None:
        for name in ("learning.py", "broker.py", "run.py", "db.py"):
            source = (HERE / name).read_text()
            ast.parse(source)
            self.assertNotIn("coinbase_orders_create", source)
            self.assertNotIn("/orders", source)
        run_src = (HERE / "run.py").read_text()
        broker_src = (HERE / "broker.py").read_text()
        learn_src = (HERE / "learning.py").read_text()
        self.assertIn("verified_outcomes", run_src)
        self.assertNotIn("risk_adjustment(", run_src)
        self.assertNotIn("config.EDGE_BUFFER =", run_src)
        self.assertNotIn("config.MIN_PRICE =", run_src)
        self.assertNotIn("FROM trades", learn_src)
        self.assertNotIn("set_cash", learn_src)
        self.assertNotIn("spot >", broker_src)
        self.assertIn("status='open'", broker_src)
        self.assertTrue(config.LIVE_TRADING is False)


if __name__ == "__main__":
    unittest.main()
