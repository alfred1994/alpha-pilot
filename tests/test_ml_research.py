"""ML 研究的时间隔离、可执行标签、数据拒绝和独立产物契约。"""
import contextlib
import copy
from dataclasses import replace
import hashlib
import io
import gzip
import json
from pathlib import Path
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeRegressor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio.factor_backtest import ExecutionConfig
from research.audit import audit_panel
from research.store import read_experiments
from scripts.run_ml_research import main, read_input
from scripts.export_ml_research_panel import export_panel
from strategy.ml_research import FEATURE_COLS, MLResearchConfig, build_features, build_samples, run_ml_research


def fixture(n=185, stocks=8):
    dates = pd.bdate_range("2022-01-03", periods=n)
    rows = []
    for i, code in enumerate([f"600{i + 1:03d}" for i in range(stocks)] + ["510300", "510500"]):
        rng = np.random.default_rng(i + 41)
        close = (10 + i) * np.exp(np.cumsum(rng.normal(0.0003, 0.006, n)))
        opening = close * (1 + rng.normal(0, 0.002, n))
        for j, day in enumerate(dates):
            previous = close[max(j - 1, 0)]
            rows.append(dict(code=code, date=day, open=opening[j], close=close[j],
                             high=max(opening[j], close[j]) * 1.005, low=min(opening[j], close[j]) * .995,
                             volume=float(100000 + rng.integers(1, 100000)), raw_open=opening[j], raw_close=close[j],
                             tradable=True, eligible=True, limit_up=previous * 1.1, limit_down=previous * .9,
                             source="fixture", corporate_action=False))
    panel = pd.DataFrame(rows).sort_values(["date", "code"]).reset_index(drop=True)
    return panel[panel.code.str.startswith("6")].copy(), panel[panel.code.str.startswith("5")].copy()


def model_factory():
    return DecisionTreeRegressor(max_depth=3, min_samples_leaf=15, random_state=42)


class MLResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stocks, cls.etfs = fixture()
        cls.config = MLResearchConfig(train_days=45, valid_days=25, horizon=5, top_k=3, min_stocks=5, threads=1)
        cls.execution = ExecutionConfig(max_positions=3, max_weight=0.2, mode="research")
        cls.report = run_ml_research(cls.stocks, cls.etfs, cls.config, cls.execution, model_factory=model_factory)

    def run_study(self, stocks=None, etfs=None, **kwargs):
        return run_ml_research(self.stocks if stocks is None else stocks, self.etfs if etfs is None else etfs,
                               self.config, self.execution, model_factory=model_factory, **kwargs)

    def test_sixty_features_are_dimensionless_and_causal(self):
        bars = self.stocks[self.stocks.code == "600001"].set_index("date")
        features = build_features(bars)
        self.assertEqual(len(FEATURE_COLS), 60)
        self.assertEqual(len(set(FEATURE_COLS)), 60)
        scaled = bars.copy()
        scaled[["open", "high", "low", "close"]] *= 17
        scaled["volume"] *= 100
        np.testing.assert_allclose(features, build_features(scaled), equal_nan=True, rtol=1e-6, atol=1e-9)
        changed = bars.copy()
        changed.iloc[120:, changed.columns.get_indexer(["open", "high", "low", "close", "volume"])] *= 2
        pd.testing.assert_frame_equal(features.iloc[:120], build_features(changed).iloc[:120])

    def test_labels_use_next_open_horizon_costs_and_shared_calendar(self):
        dates = sorted(self.stocks.date.unique())
        samples = build_samples(self.stocks, dates, 5, self.execution)
        day = dates[80]
        sample = samples[(samples.code == "600001") & (samples.date == day)].iloc[0]
        bars = self.stocks[self.stocks.code == "600001"].set_index("date")
        ratio = bars.loc[dates[86], "open"] / bars.loc[dates[81], "open"]
        e = self.execution
        expected = ratio * (1-e.slippage)*(1-e.commission-e.stamp_tax)/((1+e.slippage)*(1+e.commission))-1
        self.assertAlmostEqual(sample.net_forward_return, expected)
        self.assertEqual(sample.label_end, dates[86])
        missing = self.stocks[~((self.stocks.code == "600001") & (self.stocks.date == dates[81]))]
        altered = build_samples(missing, dates, 5, self.execution)
        current = altered[(altered.code == "600001") & (altered.date == day)].iloc[0]
        self.assertTrue(pd.isna(current.net_forward_return))
        pd.testing.assert_series_equal(sample[FEATURE_COLS], current[FEATURE_COLS], check_names=False)

    def test_training_labels_are_mature_before_each_validation(self):
        self.assertGreater(len(self.report["folds"]), 1)
        for fold in self.report["folds"]:
            self.assertLess(fold["train_label_end"], fold["validation_start"])
            self.assertGreater(fold["purged_or_unlabelled_rows"], 0)
            self.assertEqual(set(fold["feature_importance"]), set(FEATURE_COLS))

    def test_future_changes_and_missing_future_bars_preserve_past_predictions(self):
        changed = self.stocks.copy()
        cutoff = sorted(changed.date.unique())[135]
        future = changed.date > cutoff
        cols = ["open", "high", "low", "close", "raw_open", "raw_close", "limit_up", "limit_down"]
        changed.loc[future, cols] *= 1.04
        changed.loc[future, "volume"] *= 2
        changed = changed[~(future & changed.code.eq("600008"))]
        result = self.run_study(changed)
        for key in ("lightgbm", "momentum20"):
            first = [r for r in self.report["selections"][key] if r["date"] <= str(cutoff.date())]
            second = [r for r in result["selections"][key] if r["date"] <= str(cutoff.date())]
            self.assertEqual(first, second)
        cols = ["date", "code", "score", "return_20", "fold"]
        old, new = pd.DataFrame(self.report["predictions"]), pd.DataFrame(result["predictions"])
        pd.testing.assert_frame_equal(old[old.date <= str(cutoff.date())][cols].reset_index(drop=True),
                                      new[new.date <= str(cutoff.date())][cols].reset_index(drop=True))

    def test_shared_account_dates_fees_and_compounded_folds(self):
        accounts = self.report["accounts"]
        axis = [d["date"] for d in accounts["lightgbm"]["curve"]]
        for name, book in accounts.items():
            self.assertEqual([d["date"] for d in book["curve"]], axis)
            compounded = np.prod([1 + f["accounts"][name]["total_return"] for f in self.report["folds"]])-1
            self.assertAlmostEqual(compounded, book["metrics"]["total_return"])
            cash = self.execution.capital
            for day in book["curve"]:
                cash += sum(t["cash_change"] for t in book["trades"] if t["date"] == day["date"])
                self.assertAlmostEqual(cash, day["cash"], places=7)
            for trade in book["trades"]:
                self.assertGreater(trade["date"], trade["signal_date"])
                self.assertEqual(trade["shares"] % 100, 0)
                self.assertGreaterEqual(trade["commission"], 5)
        self.assertEqual(accounts["510300"]["metrics"]["stamp_tax"], 0)
        self.assertEqual(self.report["cash_return"], 0)
        self.assertFalse(self.report["promotion_allowed"])
        json.dumps(self.report, allow_nan=False)

    def test_invalid_weekend_duplicates_and_missing_security_stop_before_fit(self):
        changed = self.stocks.copy()
        changed.loc[changed.index[0], "date"] = pd.Timestamp("2022-01-08")
        result = self.run_study(changed)
        self.assertEqual(result["status"], "waiting_data")
        self.assertIn("weekend_bars", result["reasons"])
        self.assertNotIn("metrics", result)
        duplicate = pd.concat([self.stocks, self.stocks.iloc[:1]])
        self.assertIn("duplicate_bars", self.run_study(duplicate)["reasons"])
        result = self.run_study(expected_codes=["600001", "600999"])
        self.assertIn("missing_requested_securities", result["reasons"])

    def test_missing_benchmark_is_not_silently_removed(self):
        result = self.run_study(etfs=self.etfs[self.etfs.code == "510300"])
        self.assertIn("missing_requested_securities", result["reasons"])
        missing = self.etfs.drop(self.etfs.index[-1])
        result = self.run_study(etfs=missing)
        self.assertEqual(result["status"], "waiting_data")
        self.assertTrue(any("benchmark_missing_evaluation_sessions" in reason for reason in result["reasons"]))

    def test_strict_mode_requires_evidence_even_with_ohlc(self):
        result = run_ml_research(self.stocks, self.etfs, self.config, replace(self.execution, mode="strict"), model_factory=model_factory)
        self.assertEqual(result["status"], "waiting_data")
        self.assertNotIn("metrics", result)
        self.assertIn("strict_data_not_ready", result["reasons"])

    def test_independent_calendar_and_missing_days_are_audited(self):
        dates = sorted(self.stocks.date.dt.strftime("%Y-%m-%d").unique())
        removed = self.stocks[self.stocks.date != pd.Timestamp(dates[90])]
        result = audit_panel(removed, dates, {"calendar_source": "fixture"})
        self.assertIn(dates[90], result["missing_dates"])
        self.assertEqual(len(result["coverage"]), self.stocks.code.nunique())
        intraday = self.stocks.copy()
        intraday.loc[intraday.index[0], "date"] += pd.Timedelta(hours=1)
        self.assertIn("non_daily_dates", audit_panel(intraday)["errors"])

    def test_current_eligibility_and_zero_volume_are_respected(self):
        changed = self.stocks.copy()
        day = sorted(changed.date.unique())[120]
        changed.loc[changed.code.eq("600001") & changed.date.eq(day), "eligible"] = False
        changed.loc[changed.code.eq("600002") & changed.date.eq(day), "volume"] = 0
        samples = build_samples(changed, sorted(changed.date.unique()), 5, self.execution)
        self.assertFalse((samples.date.eq(day) & samples.code.isin(["600001", "600002"])).any())

    def test_configuration_and_missing_dependency_fail_explicitly(self):
        with self.assertRaises(ValueError):
            replace(self.config, threads=4).validate()
        with patch.dict(sys.modules, {"lightgbm": None}):
            with self.assertRaisesRegex(RuntimeError, "lightgbm"):
                run_ml_research(self.stocks, self.etfs, self.config, self.execution)

    def test_real_lightgbm_can_run_when_installed(self):
        try:
            import lightgbm  # noqa: F401
        except ImportError:
            self.skipTest("当前解释器未安装可选研究运行依赖 lightgbm")
        result = run_ml_research(self.stocks, self.etfs, self.config, self.execution)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any(sum(fold["feature_importance"].values()) > 0 for fold in result["folds"]))

    def test_cli_freezes_manifest_registers_and_does_not_overwrite_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stocks, etfs = root / "stocks.json", root / "etfs.json"
            for path, data in ((stocks, self.stocks), (etfs, self.etfs)):
                data.assign(date=data.date.dt.strftime("%Y-%m-%d")).to_json(path, orient="records")
            before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (stocks, etfs)]
            output = root / "run"
            args = ["--input", str(stocks), "--benchmark-input", str(etfs), "--output", str(output),
                    "--start-date", "2022-01-03", "--end-date", "2022-12-30", "--execution", "research"]

            def study(*args, **kwargs):
                self.assertTrue((output / "manifest.json").exists())
                return copy.deepcopy(self.report)

            with patch("scripts.run_ml_research.run_ml_research", side_effect=study), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in (stocks, etfs)])
            self.assertTrue((output / "predictions.csv.gz").exists())
            stored = read_experiments(output / "evidence.sqlite3")
            self.assertEqual(stored[0]["verdict"], "inconclusive")
            self.assertEqual(stored[0]["manifest"]["interval_role"], "review")
            self.assertFalse(stored[0]["latest"]["promotion_allowed"])
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)

    def test_cli_preserves_failure_and_does_not_claim_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path = root / "input.json"
            input_path.write_text("[]", encoding="utf-8")
            args = ["--input", str(input_path), "--benchmark-input", str(input_path), "--output", str(root / "failed"),
                    "--start-date", "2022-01-03", "--end-date", "2022-12-30"]
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 1)
            self.assertTrue((root / "failed/failure.json").exists())
            self.assertEqual(read_experiments(root / "failed/evidence.sqlite3")[0]["status"], "waiting_data")

    def test_snapshot_export_is_readonly_frozen_and_keeps_missing_codes_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path, universe, output = root / "bars.db", root / "universe.json", root / "snapshot.json.gz"
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.execute("CREATE TABLE k_daily(code,date,open,high,low,close,volume,source)")
                db.execute("INSERT INTO k_daily VALUES('600001','2022-01-03',10,11,9,10,100,'fixture')")
                db.commit()
            universe.write_text(json.dumps({"codes": [{"code": code} for code in ["600001", "600999", "300001"]]}), encoding="utf-8")
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()
            result = export_panel(db_path, universe, output, "2022-01-01", "2022-12-31")
            self.assertEqual(result["rows"], 1)
            self.assertEqual(hashlib.sha256(db_path.read_bytes()).hexdigest(), before)
            with gzip.open(output, "rt", encoding="utf-8") as stream:
                payload = json.load(stream)
            self.assertEqual(payload["expected_codes"], ["600001", "600999"])
            self.assertEqual(payload["metadata"]["excluded_unsupported_board_codes"], ["300001"])
            self.assertIn("missing_requested_securities", audit_panel(payload["bars"], expected_codes=payload["expected_codes"])["errors"])
            with self.assertRaises(FileExistsError):
                export_panel(db_path, universe, output, "2022-01-01", "2022-12-31")

    def test_snapshot_row_budget_fails_without_repairing_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path, universe = root / "bars.db", root / "universe.json"
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.execute("CREATE TABLE k_daily(code,date,open,high,low,close,volume,source)")
                db.executemany("INSERT INTO k_daily VALUES('600001',?,10,11,9,10,100,'fixture')", [("2022-01-03",), ("2022-01-04",)])
                db.commit()
            universe.write_text('{"codes":[{"code":"600001"}]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "行数上限"):
                export_panel(db_path, universe, root / "partial.gz", "2022-01-01", "2022-12-31", max_rows=1)

    def test_input_window_does_not_hide_intraday_bar_at_end_date(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            row = self.stocks.iloc[0].to_dict()
            row["date"] = "2022-01-03T15:00:00"
            path.write_text(json.dumps([row]), encoding="utf-8")
            panel, _, _, _ = read_input(path, "2022-01-03", "2022-01-03")
            self.assertEqual(len(panel), 1)
            self.assertIn("non_daily_dates", audit_panel(panel)["errors"])

    def test_cli_rejects_input_change_during_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "input.json"
            self.stocks.assign(date=self.stocks.date.dt.strftime("%Y-%m-%d")).to_json(path, orient="records")
            args = ["--input", str(path), "--benchmark-input", str(path), "--output", str(root / "run"),
                    "--start-date", "2022-01-03", "--end-date", "2022-12-30"]

            def study(*args, **kwargs):
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(" ")
                return copy.deepcopy(self.report)

            with patch("scripts.run_ml_research.run_ml_research", side_effect=study), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 1)
            failure = json.loads((root / "run/failure.json").read_text(encoding="utf-8"))
            self.assertIn("输入文件发生变化", failure["reason"])
            self.assertFalse((root / "run/report.json").exists())


if __name__ == "__main__":
    unittest.main()
