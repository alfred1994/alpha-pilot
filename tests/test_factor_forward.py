"""全部模型前向冻结、迟到结算、不可补造和并发幂等。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_ml_research import fixture
from research.ml_forward import BJ, frame_records
from strategy.factor_discovery import MLResearchConfig, ResourceBudget, SearchConfig
from strategy.factor_forward import MODEL_NAMES, advance, initialize, read_status, reconcile
from scripts.run_factor_forward import daily, main


class FactorForwardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "forward.db"
        self.stocks, self.etfs = fixture(n=200)
        self.dates = sorted(self.stocks.date.unique())
        self.calendar = {"error_code": "0", "data": [[day.strftime("%Y-%m-%d"), "1" if day.weekday() < 5 else "0"]
                         for day in pd.date_range(self.dates[0], self.dates[-1])]}
        self.config = MLResearchConfig(train_days=70, valid_days=20, horizon=3, top_k=3, min_stocks=5, threads=1)
        self.search = SearchConfig(max_candidates=6, max_selected=2, min_ic_days=5)

    def now(self, i, hour=16):
        return pd.Timestamp(self.dates[i]).to_pydatetime().replace(hour=hour, tzinfo=BJ)

    def data(self, i, full=False):
        return tuple(f[f.date <= self.dates[i]].copy() if full else f[f.date == self.dates[i]].copy()
                     for f in (self.stocks, self.etfs))

    def init(self, **kwargs):
        return initialize(self.path, *self.data(150, True), self.calendar, config=self.config,
                          search=self.search, now=self.now(150), **kwargs)

    def step(self, i, **kwargs):
        return advance(self.path, *self.data(i), self.calendar, now=self.now(i), **kwargs)

    def execution(self, i):
        rows = pd.concat([self.stocks, self.etfs])
        rows = rows[(rows.date >= self.dates[150]) & (rows.date <= self.dates[i])].copy()
        return {"bars": frame_records(rows), "metadata": {
            "source": "fixture", "calendar_source": "fixture", "adjustment": "raw",
            "universe": "forward_observed", "corporate_actions_verified": True,
            "start": self.dates[150].strftime("%Y-%m-%d"), "end": self.dates[i].strftime("%Y-%m-%d")}}

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(f"SELECT * FROM {table}").fetchall()

    def test_init_freezes_all_models_and_records_no_historical_profit(self):
        result = self.init()
        self.assertEqual(result["observed_days"], 0)
        self.assertEqual(result["model_count"], 6)
        self.assertIsNone(result["execution"]["accounts"])
        self.assertFalse(result["promotion_allowed"])
        manifest = read_status(self.path)["manifest"]
        self.assertEqual(set(manifest["model_features"]), set(MODEL_NAMES))
        self.assertLess(manifest["train_label_end"], manifest["initial_date"])
        self.assertLess(manifest["search"]["discovery_label_end"], manifest["search"]["selection_start"])
        before = self.rows("models")
        with self.assertRaisesRegex(ValueError, "覆盖"):
            self.init()
        self.assertEqual(before, self.rows("models"))

    def test_no_retraining_new_day_and_same_day_idempotence(self):
        self.init()
        original = self.rows("models")
        with patch("strategy.factor_forward._make_model", side_effect=AssertionError("no refit")):
            first = self.step(151)
            again = self.step(151)
        self.assertEqual(first["observed_days"], 1)
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(original, self.rows("models"))
        self.assertEqual(len(self.rows("days")), 2)
        self.assertEqual(len(self.rows("assessments")), 2)
        self.assertIsNone(first["execution"]["accounts"])

    def test_missing_day_future_intraday_and_wrong_coverage_rejected(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "遗漏"):
            self.step(152)
        with self.assertRaisesRegex(ValueError, "未来|未收盘"):
            advance(self.path, *self.data(151), self.calendar, now=self.now(151, 12))
        stocks, etfs = self.data(151)
        with self.assertRaisesRegex(ValueError, "覆盖"):
            advance(self.path, stocks[stocks.code != "600001"], etfs, self.calendar, now=self.now(151))
        with self.assertRaisesRegex(ValueError, "覆盖"):
            initialize(Path(self.temp.name) / "stale.db", *self.data(150, True), self.calendar,
                       config=self.config, search=self.search, now=self.now(151))
        self.assertEqual(len(self.rows("days")), 1)

    def test_source_and_model_corruption_stop_before_idempotent_return(self):
        self.init()
        with patch("strategy.factor_forward.implementation", return_value={}):
            with self.assertRaisesRegex(ValueError, "指纹"):
                self.step(150)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE models SET model=model||'tampered' WHERE name='baseline60'")
        with self.assertRaisesRegex(ValueError, "校验"):
            self.step(150)

    def test_same_day_checks_overlapping_history_and_current_revisions(self):
        self.init()
        stocks, etfs = self.data(150, True)
        stocks.loc[stocks.index[0], "volume"] += 100
        with self.assertRaisesRegex(ValueError, "修订"):
            advance(self.path, stocks, etfs, self.calendar, now=self.now(150))
        stocks, etfs = self.data(150)
        stocks.loc[stocks.index[0], "volume"] += 100
        with self.assertRaisesRegex(ValueError, "修订"):
            advance(self.path, stocks, etfs, self.calendar, now=self.now(150))

    def test_real_recording_time_rechecked_after_prediction(self):
        self.init()
        with patch("strategy.factor_forward.clock", side_effect=[self.now(151), self.now(152, 10)]):
            with self.assertRaisesRegex(ValueError, "晚于"):
                advance(self.path, *self.data(151), self.calendar)
        self.assertEqual(len(self.rows("days")), 1)

    def test_late_execution_data_reconciles_frozen_predictions_and_is_idempotent(self):
        self.init()
        self.step(151)
        signals = self.rows("days")
        result = reconcile(self.path, self.execution(151), now=self.now(152))
        self.assertEqual(result["execution"]["status"], "strict_simulation")
        self.assertEqual(len(result["execution"]["accounts"]), 9)
        self.assertEqual(signals, self.rows("days"))
        for book in result["execution"]["accounts"].values():
            self.assertTrue(book["trades"])
            self.assertTrue(all(t["signal_date"] < t["date"] for t in book["trades"]))
        before = self.rows("assessments")
        self.assertEqual(reconcile(self.path, self.execution(151), now=self.now(152))["status"], "unchanged")
        self.assertEqual(before, self.rows("assessments"))
        incomplete = self.execution(151)
        incomplete["metadata"]["corporate_actions_verified"] = False
        self.assertEqual(reconcile(self.path, incomplete, now=self.now(152))["execution"], result["execution"])

    def test_settled_trades_are_immutable_and_later_curve_extends(self):
        self.init()
        self.step(151)
        first = reconcile(self.path, self.execution(151), now=self.now(151))
        old = self.execution(151)
        old["bars"][0]["raw_close"] += .01
        with self.assertRaisesRegex(ValueError, "修订"):
            reconcile(self.path, old, now=self.now(151))
        second = self.step(152, execution_payload=self.execution(152))
        for name in first["execution"]["accounts"]:
            self.assertEqual(first["execution"]["accounts"][name]["curve"], second["execution"]["accounts"][name]["curve"][:1])

    def test_incomplete_or_company_action_data_never_becomes_profit(self):
        self.init()
        self.step(151)
        incomplete = self.execution(151)
        for row in incomplete["bars"]:
            row.pop("limit_up")
        waiting = reconcile(self.path, incomplete, now=self.now(151))
        self.assertIsNone(waiting["execution"]["accounts"])
        bad = self.execution(151)
        bad["bars"][0]["corporate_action"] = True
        with self.assertRaisesRegex(ValueError, "corporate_action"):
            reconcile(self.path, bad, now=self.now(151))
        self.assertEqual(self.rows("execution_rows"), [])

    def test_concurrent_step_appends_one_signal_day(self):
        self.init()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.step(151), range(2)))
        self.assertEqual(sorted(r["status"] for r in results), ["advanced", "unchanged"])
        self.assertEqual(len(self.rows("days")), 2)

    def test_horizon_labels_mature_and_terminal_run_cannot_extend(self):
        self.init(evaluation_days=5)
        for i in range(151, 156):
            result = self.step(i)
        self.assertGreater(result["mature_label_rows"], 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["assessment"], "waiting_execution_data")
        before = self.rows("days")
        self.assertEqual(self.step(156)["status"], "completed")
        self.assertEqual(before, self.rows("days"))
        result = reconcile(self.path, self.execution(155), now=self.now(156))
        self.assertEqual(result["assessment"], "review_required_no_automatic_promotion")
        self.assertFalse(result["promotion_allowed"])

    def test_calendar_revision_rejected_and_extension_allowed(self):
        short = dict(self.calendar, data=[r for r in self.calendar["data"] if r[0] <= self.dates[155].strftime("%Y-%m-%d")])
        initialize(self.path, *self.data(150, True), short, config=self.config, search=self.search, now=self.now(150))
        self.step(151)
        self.assertEqual(len(self.rows("calendar_versions")), 2)
        with self.assertRaisesRegex(ValueError, "日历"):
            advance(self.path, *self.data(152), short, now=self.now(152))

    def test_resource_limits_and_readonly_status(self):
        with self.assertRaisesRegex(ValueError, "行数预算"):
            self.init(resources=ResourceBudget(max_panel_rows=10))
        self.assertFalse(self.path.exists())
        self.init()
        before = self.path.read_bytes()
        self.assertTrue(read_status(self.path)["implementation_matches"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["status", "--state", str(self.path)]), 0)
        self.assertEqual(before, self.path.read_bytes())
        missing = Path(self.temp.name) / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            read_status(missing)
        self.assertFalse(missing.exists())

    def test_daily_idle_and_missed_day_never_collect_or_refit(self):
        self.init()
        with patch("scripts.run_factor_forward.collect", side_effect=AssertionError("must not collect")):
            with patch("scripts.run_factor_forward.clock", return_value=self.now(150)):
                self.assertEqual(daily(self.path, self.calendar, Path(self.temp.name) / "daily")["status"], "unchanged")
            with patch("scripts.run_factor_forward.clock", return_value=self.now(152)):
                with self.assertRaisesRegex(ValueError, "遗漏"):
                    daily(self.path, self.calendar, Path(self.temp.name) / "daily")

    def test_daily_success_advances_only_after_ready_snapshot(self):
        self.init()
        def snapshot(seed, calendar, directory, workers=2):
            import gzip
            directory.mkdir(parents=True)
            self.assertEqual(seed["expected_codes"], sorted(self.stocks.code.unique()))
            for name, frame in zip(("stocks", "benchmarks"), self.data(151, True)):
                with gzip.open(directory / (name + ".json.gz"), "wt", encoding="utf-8") as stream:
                    json.dump({"bars": frame_records(frame)}, stream)
            return {"status": "ready"}
        with patch("scripts.run_factor_forward.clock", return_value=self.now(151)), patch("strategy.factor_forward.clock", return_value=self.now(151)), patch("scripts.run_factor_forward.collect", side_effect=snapshot):
            result = daily(self.path, self.calendar, Path(self.temp.name) / "daily")
        self.assertEqual(result["observed_days"], 1)
        self.assertEqual(len(self.rows("days")), 2)


if __name__ == "__main__":
    unittest.main()
