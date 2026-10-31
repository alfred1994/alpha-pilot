"""因子增量、受限搜索和嵌套时间隔离的离线回归。"""
import contextlib
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeRegressor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_ml_research import fixture
from portfolio.factor_backtest import ExecutionConfig
from research.audit import records
from research.store import read_experiments
from scripts.run_factor_discovery import main, render_report
from strategy.factor_discovery import (
    EXTRA_COLS, EXTRA_GROUPS, EXTENDED_COLS, MAX_PANEL_ROWS, VARIANTS,
    MLResearchConfig, ResourceBudget, SearchConfig, build_extra_features, discovery_samples,
    evaluate_formulas, formula_catalog, run_discovery, select_formulas,
)
from strategy.ml_research import build_samples, run_ml_research


def model_factory():
    return DecisionTreeRegressor(max_depth=3, min_samples_leaf=15, random_state=42)


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stocks, cls.etfs = fixture(n=215)
        cls.config = MLResearchConfig(train_days=90, valid_days=25, horizon=5, top_k=3, min_stocks=5, threads=1)
        cls.search = SearchConfig(max_candidates=9, max_selected=2, min_ic_days=5)
        cls.execution = ExecutionConfig(max_positions=3, max_weight=.2, mode="research")
        cls.report = cls.study()

    @classmethod
    def study(cls, stocks=None, etfs=None, **kwargs):
        return run_discovery(cls.stocks if stocks is None else stocks, cls.etfs if etfs is None else etfs,
                             cls.config, cls.execution, kwargs.pop("search", cls.search),
                             model_factory=model_factory, **kwargs)

    def test_extra_features_have_known_values_and_units(self):
        bars = pd.DataFrame({"open": np.full(45, 10.), "high": np.full(45, 12.),
                             "low": np.full(45, 9.), "close": np.full(45, 11.), "volume": np.full(45, 1000.)})
        values = build_extra_features(bars).iloc[-1]
        self.assertAlmostEqual(values.parkinson_20, np.log(12 / 9) / np.sqrt(4 * np.log(2)))
        self.assertAlmostEqual(values.cmf_20, 1 / 3)
        self.assertEqual(values.signed_volume_20, 0)
        self.assertEqual(values.efficiency_20, 0)
        self.assertEqual(values.downside_share_20, .5)
        scaled = bars.copy()
        scaled[["open", "high", "low", "close"]] *= 23
        scaled["volume"] *= 100
        np.testing.assert_allclose(build_extra_features(bars), build_extra_features(scaled), equal_nan=True, atol=1e-12)
        flat = bars.copy()
        flat[["open", "high", "low", "close"]] = 10.
        self.assertTrue(np.isfinite(build_extra_features(flat).iloc[21:]).all().all())
        self.assertEqual(build_extra_features(flat).iloc[-1].cmf_20, 0)

    def test_features_are_causal_and_do_not_bridge_missing_sessions(self):
        bars = self.stocks[self.stocks.code == "600001"].set_index("date")
        before = build_extra_features(bars)
        changed = bars.copy()
        changed.iloc[120:, changed.columns.get_indexer(["open", "high", "low", "close", "volume"])] *= 2
        pd.testing.assert_frame_equal(before.iloc[:120], build_extra_features(changed).iloc[:120])
        missing = bars.copy()
        missing.iloc[90, missing.columns.get_indexer(["open", "high", "low", "close", "volume"])] = np.nan
        self.assertTrue(build_extra_features(missing).iloc[90:110].isna().all().all())

    def test_extended_sample_pool_and_labels_match_baseline_exactly(self):
        sessions = sorted(self.stocks.date.unique())
        original = build_samples(self.stocks, sessions, 5, self.execution)
        extra = discovery_samples(self.stocks, sessions, 5, self.execution)
        pd.testing.assert_frame_equal(original, extra[original.columns])
        self.assertTrue(np.isfinite(extra[EXTRA_COLS]).all().all())
        day = sessions[80]
        without_future = self.stocks[~(self.stocks.code.eq("600001") & self.stocks.date.eq(sessions[81]))]
        altered = discovery_samples(without_future, sessions, 5, self.execution)
        old = extra[extra.date.eq(day) & extra.code.eq("600001")].iloc[0]
        new = altered[altered.date.eq(day) & altered.code.eq("600001")].iloc[0]
        self.assertTrue(pd.isna(new.target))
        pd.testing.assert_series_equal(old[EXTENDED_COLS], new[EXTENDED_COLS], check_names=False)

    def test_catalog_budget_complexity_and_operator_whitelist(self):
        catalog = formula_catalog(self.search)
        self.assertEqual(len(catalog), 9)
        self.assertEqual(catalog, formula_catalog(self.search))
        self.assertTrue(all(item["nodes"] == 3 for item in catalog))
        self.assertEqual(len({item["expression"] for item in formula_catalog(SearchConfig(max_candidates=96))}), 96)
        samples = discovery_samples(self.stocks, sorted(self.stocks.date.unique()), 5, self.execution)
        result = evaluate_formulas(samples, catalog)
        spec = catalog[0]
        product = samples[spec["left"]] * samples[spec["right"]]
        expected = (product.groupby(samples.date).rank(pct=True) - .5).astype("float32")
        pd.testing.assert_series_equal(result[spec["name"]], expected, check_names=False)
        cutoff = sorted(samples.date.unique())[30]
        prefix = evaluate_formulas(samples[samples.date <= cutoff], catalog)
        pd.testing.assert_frame_equal(result.loc[prefix.index], prefix)
        with self.assertRaisesRegex(ValueError, "白名单"):
            evaluate_formulas(samples, [{**catalog[0], "operator": "__import__"}])
        with self.assertRaisesRegex(ValueError, "白名单"):
            evaluate_formulas(samples, [{**catalog[0], "input_transform": "uncentered_rank"}])
        for kwargs in ({"max_candidates": 97}, {"max_selected": 5}, {"max_candidates": True}, {"max_correlation": float("nan")}):
            with self.assertRaises(ValueError):
                formula_catalog(SearchConfig(**kwargs))

    def test_inner_selection_purges_labels_rejects_duplicates_and_keeps_all_attempts(self):
        dates = list(pd.bdate_range("2020-01-02", periods=95))
        rng = np.random.default_rng(43)
        frame = pd.DataFrame(rng.normal(size=(80 * 12, len(EXTENDED_COLS))), columns=EXTENDED_COLS)
        frame["date"] = np.repeat(dates[:80], 12)
        frame["label_end"] = np.repeat(dates[5:85], 12)
        frame["code"] = [f"600{i:03d}" for _ in range(80) for i in range(12)]
        signal = np.tile(np.linspace(-.5, .5, 12), 80)
        frame["target"] = signal / 100
        catalog = formula_catalog(replace(self.search, max_candidates=3))
        frame[catalog[0]["name"]], frame[catalog[1]["name"]], frame[catalog[2]["name"]] = signal, -signal, 0.
        selected = select_formulas(frame, dates[:80], catalog, self.search, 5)
        self.assertEqual(selected["selected"], [catalog[0]["name"]])
        self.assertIn("redundant_with:" + catalog[0]["name"], selected["candidates"][catalog[1]["name"]]["reasons"])
        self.assertTrue(selected["candidates"][catalog[2]["name"]]["reasons"])
        self.assertEqual(len(selected["candidates"]), 3)
        self.assertLess(selected["discovery_label_end"], selected["selection_start"])
        self.assertGreater(selected["purged_inner_rows"], 0)
        changed = frame.copy()
        cut = pd.Timestamp(selected["selection_start"])
        changed.loc[(changed.date < cut) & (changed.label_end >= cut), "target"] *= -1000
        self.assertEqual(selected, select_formulas(changed, dates[:80], catalog, self.search, 5))

    def test_outer_labels_are_mature_and_model_features_are_traceable(self):
        self.assertGreater(len(self.report["folds"]), 1)
        for fold in self.report["folds"]:
            self.assertLess(fold["train_label_end"], fold["validation_start"])
            self.assertLess(fold["search"]["selection_label_end"], fold["validation_start"])
            self.assertLess(fold["search"]["discovery_label_end"], fold["search"]["selection_start"])
            self.assertGreater(fold["purged_outer_rows"], 0)
            self.assertLessEqual(len(fold["search"]["selected"]), self.search.max_selected)
            self.assertEqual(fold["model_features"]["formula_augmented"], EXTENDED_COLS + fold["search"]["selected"])

    def test_future_quotes_and_labels_do_not_change_earlier_search_or_predictions(self):
        cutoff = sorted(self.stocks.date.unique())[170]
        changed = self.stocks.copy()
        future = changed.date > cutoff
        columns = ["open", "high", "low", "close", "raw_open", "raw_close", "limit_up", "limit_down"]
        changed.loc[future, columns] *= 1.04
        changed = changed[~(future & changed.code.eq("600008"))]
        altered = self.study(changed)
        self.assertEqual(self.report["folds"][0]["search"], altered["folds"][0]["search"])
        for name in self.report["predictions"]:
            columns = ["date", "code", "score", "fold"]
            first, second = pd.DataFrame(self.report["predictions"][name]), pd.DataFrame(altered["predictions"][name])
            day = cutoff.strftime("%Y-%m-%d")
            pd.testing.assert_frame_equal(first[first.date <= day][columns].reset_index(drop=True),
                                          second[second.date <= day][columns].reset_index(drop=True))

    def test_baseline_results_match_existing_research_engine(self):
        previous = run_ml_research(self.stocks, self.etfs, self.config, self.execution, model_factory=model_factory)
        self.assertEqual(self.report["predictions"]["baseline60"], previous["predictions"])
        self.assertEqual(self.report["accounts"]["baseline60"], previous["accounts"]["lightgbm"])
        for key in ("momentum20", "510300", "510500"):
            self.assertEqual(self.report["accounts"][key], previous["accounts"][key])

    def test_common_dates_cash_fees_and_ablation_deltas(self):
        self.assertEqual(set(self.report["accounts"]), set(VARIANTS) | {"formula_augmented", "momentum20", "510300", "510500"})
        axis = [d["date"] for d in self.report["accounts"]["baseline60"]["curve"]]
        for name, book in self.report["accounts"].items():
            self.assertEqual([d["date"] for d in book["curve"]], axis)
            for day in book["curve"]:
                self.assertGreaterEqual(day["cash"], 0)
                self.assertAlmostEqual(day["equity"], day["cash"] + sum(p["value"] for p in day["holdings"].values()))
            for trade in book["trades"]:
                self.assertLess(trade["signal_date"], trade["date"])
                self.assertGreaterEqual(trade["commission"], 5)
                self.assertEqual(trade["shares"] % 100, 0)
                if name in ("510300", "510500"):
                    self.assertEqual(trade["stamp_tax"], 0)
        for group in EXTRA_GROUPS:
            expected = self.report["metrics"]["extended66"]["total_return"] - self.report["metrics"]["without_" + group]["total_return"]
            self.assertAlmostEqual(self.report["increments"][group]["total_return"], expected)
        self.assertFalse(self.report["promotion_allowed"])
        self.assertIn("不按本表挑选冠军", render_report(self.report))

    def test_no_formula_passing_gates_does_not_invent_an_enhancement(self):
        report = self.study(search=replace(self.search, min_abs_rank_ic=1.0))
        self.assertTrue(all(not fold["search"]["selected"] for fold in report["folds"]))
        self.assertEqual(report["predictions"]["formula_augmented"], report["predictions"]["extended66"])
        self.assertEqual(report["metrics"]["formula_augmented"], report["metrics"]["extended66"])

    def test_bad_data_strict_metadata_and_resource_budget_stop_before_training(self):
        with patch("strategy.factor_discovery._make_model", side_effect=AssertionError("must not fit")):
            result = run_discovery(self.stocks, self.etfs, self.config, replace(self.execution, mode="strict"), self.search)
            self.assertEqual(result["reasons"], ["strict_data_not_ready"])
            duplicate = pd.concat([self.stocks, self.stocks.iloc[:1]])
            self.assertIn("duplicate_bars", self.study(duplicate)["reasons"])
            self.assertIn("missing_requested_securities", self.study(expected_codes=["600999"])["reasons"])
            missing = self.etfs[~self.etfs.code.eq("510500")]
            self.assertIn("missing_requested_securities", self.study(etfs=missing)["reasons"])
        with patch("strategy.factor_discovery.MAX_PANEL_ROWS", 5):
            with self.assertRaisesRegex(ValueError, "资源上限"):
                self.study()
        with patch("strategy.factor_discovery.MAX_CANDIDATE_CELLS", 5):
            with self.assertRaisesRegex(ValueError, "内存预算"):
                self.study()
        self.assertGreater(MAX_PANEL_ROWS, len(self.stocks))
        for limits in (ResourceBudget(max_panel_rows=1_000_001), ResourceBudget(max_candidate_cells=48_000_001),
                       ResourceBudget(max_panel_rows=True)):
            with self.assertRaises(ValueError):
                self.study(resources=limits)
        allowed = self.study(resources=ResourceBudget(max_panel_rows=500_000, max_candidate_cells=20_000_000))
        self.assertEqual(allowed["resource_limits"]["max_panel_rows"], 500_000)
        self.assertEqual(allowed["metrics"], self.report["metrics"])

    def test_strict_data_uses_existing_execution_contract(self):
        sessions = sorted(self.stocks.date.dt.strftime("%Y-%m-%d").unique())
        metadata = dict(source="synthetic", adjustment="raw", calendar_source="synthetic",
                        universe="point_in_time", delisted_coverage_verified=True, corporate_actions_verified=True)
        report = run_discovery(self.stocks, self.etfs, self.config, replace(self.execution, mode="strict"), self.search,
                               calendar=sessions, metadata=metadata, benchmark_metadata=metadata, model_factory=model_factory)
        self.assertEqual(report["execution_status"], "completed")
        self.assertEqual(report["data_audit"]["status"], "strict_ready")
        self.assertFalse(report["promotion_allowed"])

    def _cli_inputs(self, directory, mode="research"):
        stocks, etfs, output = directory / "stocks.json", directory / "etfs.json", directory / "result"
        stocks.write_text(json.dumps(records(self.stocks)), encoding="utf-8")
        etfs.write_text(json.dumps(records(self.etfs)), encoding="utf-8")
        days = sorted(self.stocks.date.dt.strftime("%Y-%m-%d").unique())
        args = ["--input", str(stocks), "--benchmark-input", str(etfs), "--output", str(output),
                "--start-date", days[0], "--end-date", days[-1], "--execution", mode,
                "--train-days", "90", "--valid-days", "25", "--top-k", "3", "--min-stocks", "5",
                "--threads", "1", "--formula-budget", "9", "--keep-formulas", "2"]
        return stocks, etfs, output, args

    def test_cli_freezes_manifest_preserves_inputs_and_writes_independent_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            stocks, etfs, output, args = self._cli_inputs(Path(temp))
            before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (stocks, etfs)]

            def fit_factory(*unused):
                self.assertTrue((output / "manifest.json").exists())
                return model_factory()

            with patch("strategy.factor_discovery._make_model", side_effect=fit_factory), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in (stocks, etfs)])
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["formula_catalog"]), 9)
            self.assertFalse(manifest["promotion_allowed"])
            self.assertEqual(len(list(output.glob("account-*.json"))), 9)
            self.assertEqual(len(list(output.glob("predictions-*.csv.gz"))), 6)
            experiment = read_experiments(output / "evidence.sqlite3")[0]
            self.assertEqual(experiment["status"], "completed")
            self.assertEqual(experiment["verdict"], "inconclusive")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)

    def test_cli_strict_missing_metadata_keeps_diagnostic_report(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, output, args = self._cli_inputs(Path(temp), "strict")
            with contextlib.redirect_stdout(io.StringIO()), patch("strategy.factor_discovery._make_model", side_effect=AssertionError("must not fit")):
                self.assertEqual(main(args), 2)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "waiting_data")
            self.assertNotIn("metrics", report)
            self.assertFalse(list(output.glob("account-*.json")))

    def test_cli_rejects_changed_input_and_preserves_failure_record(self):
        with tempfile.TemporaryDirectory() as temp:
            stocks, _, output, args = self._cli_inputs(Path(temp))

            def changed(*unused, **kwargs):
                stocks.write_text("[]", encoding="utf-8")
                return {"status": "waiting_data"}

            with patch("scripts.run_factor_discovery.run_discovery", side_effect=changed), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 1)
            self.assertTrue((output / "manifest.json").exists())
            failure = json.loads((output / "failure.json").read_text(encoding="utf-8"))
            self.assertIn("输入文件发生变化", failure["reason"])
            self.assertFalse((output / "summary.json").exists())

    def test_cli_rejects_changed_source_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, output, args = self._cli_inputs(Path(temp), "strict")
            old = {"git_head": "synthetic", "source_sha256": "before"}
            new = {"git_head": "synthetic", "source_sha256": "after"}
            with patch("scripts.run_factor_discovery.source_version", side_effect=[old, new]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 1)
            failure = json.loads((output / "failure.json").read_text(encoding="utf-8"))
            self.assertIn("研究代码发生变化", failure["reason"])
            self.assertFalse((output / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
