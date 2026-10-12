#!/usr/bin/env python3
"""离线 RF 基线的泄漏、成交时序、成本与只读入口回归。"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.ml_baseline import (
    FEATURE_COLS, _performance, build_baseline_features, build_samples,
    load_local_bars, run_baseline,
)


def make_bars(n=260):
    rng = np.random.default_rng(42)
    close = 20 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    opening = close * np.exp(rng.normal(0, 0.01, n))
    return pd.DataFrame({
        "date": pd.bdate_range("2021-01-04", periods=n),
        "open": opening, "close": close,
        "high": np.maximum(opening, close) * 1.02,
        "low": np.minimum(opening, close) * 0.98,
        "volume": rng.integers(1000, 10000, n).astype(float),
    })


class BaselineTests(unittest.TestCase):
    def test_features_are_causal_and_scale_independent(self):
        bars = make_bars()
        features = build_baseline_features(bars)
        self.assertEqual(list(features), FEATURE_COLS)
        self.assertEqual(features.shape[1], 14)
        changed = bars.copy()
        changed.loc[160:, ["open", "high", "low", "close", "volume"]] *= 2
        pd.testing.assert_frame_equal(features.iloc[:160], build_baseline_features(changed).iloc[:160])
        scaled = bars.copy()
        scaled[["open", "high", "low", "close"]] *= 10
        scaled["volume"] *= 3
        np.testing.assert_allclose(features, build_baseline_features(scaled), atol=1e-12, equal_nan=True)

    def test_targets_use_next_open_and_t_plus_one_exit(self):
        bars = make_bars()
        samples = build_samples(bars)
        row = samples.iloc[0]
        self.assertEqual(row["date"], bars.iloc[59]["date"])
        self.assertEqual(row["entry_date"], bars.iloc[60]["date"])
        self.assertEqual(row["label_end"], bars.iloc[61]["date"])
        self.assertAlmostEqual(row["forward_return"], bars.iloc[61]["open"] / bars.iloc[60]["open"] - 1)
        self.assertEqual(samples.iloc[-1]["date"], bars.iloc[-3]["date"])

    def test_walk_forward_purges_labels_and_covers_oos_once(self):
        report = run_baseline(make_bars(), train_days=80, valid_days=30)
        self.assertEqual(len(report["folds"]), 4)
        self.assertEqual(report["oos_rows"], 119)
        dates = [row["date"] for row in report["predictions"]]
        self.assertEqual(dates, sorted(set(dates)))
        for fold in report["folds"]:
            self.assertLess(fold["train_label_end"], fold["validation_start"])
            self.assertEqual(fold["purged_rows"], 2)
            self.assertEqual(fold["train_rows"], 78)
        self.assertEqual(report["folds"][-1]["validation_rows"], 29)
        self.assertLessEqual(report["strategy_net"]["total_return"], report["strategy_gross"]["total_return"])
        json.dumps(report, allow_nan=False)

    def test_changing_future_cannot_change_earlier_prediction(self):
        bars = make_bars()
        original = run_baseline(bars, train_days=80, valid_days=30)
        changed = bars.copy()
        changed.loc[150:, ["open", "high", "low", "close"]] *= 1.5
        updated = run_baseline(changed, train_days=80, valid_days=30)
        prefix = bars.loc[149, "date"].strftime("%Y-%m-%d")
        for before, after in zip(original["predictions"], updated["predictions"]):
            if before["date"] <= prefix:
                self.assertEqual(before["probability"], after["probability"])
        self.assertEqual(original["folds"][0]["feature_importance"], updated["folds"][0]["feature_importance"])
        self.assertNotEqual(original["input_sha256"], updated["input_sha256"])

    def test_holdout_fits_once_and_baseline_uses_training_only(self):
        bars = make_bars()
        report = run_baseline(bars, train_days=80, valid_days=30, validation="holdout")
        self.assertEqual(len(report["folds"]), 1)
        train = build_samples(bars).iloc[:78]
        expected_prior = float(train["target"].mean())
        self.assertTrue(all(row["prior_probability"] == expected_prior for row in report["predictions"]))
        self.assertEqual(report["oos_rows"], 119)

    def test_cost_ledger_holds_across_intervals_and_settles_last_exit(self):
        c, s, tax = 0.001, 0.002, 0.003
        buy = 1 / ((1 + s) * (1 + c))
        sell = (1 - s) * (1 - c - tax)
        report = _performance([0.1, 0.2, -0.8, 0.05], [1, 1, 0, 1], c, s, tax)
        expected = (buy * 1.1 * 1.2 * sell) * (buy * 1.05 * sell) - 1
        self.assertAlmostEqual(report["total_return"], expected)
        self.assertEqual(report["entries"], 2)
        self.assertEqual(report["exits"], 2)
        held = _performance([0.1, 0.2, 0.05], [1, 1, 1], c, s, tax)
        self.assertAlmostEqual(held["total_return"], buy * 1.1 * 1.2 * 1.05 * sell - 1)
        self.assertEqual(held["entries"], 1)
        cash = _performance([0.1, -0.2], [0, 0], c, s, tax)
        self.assertEqual(cash["total_return"], 0)
        self.assertEqual(cash["max_drawdown"], 0)
        loss = _performance([-0.1, -0.1], [1, 1], 0, 0, 0)
        self.assertAlmostEqual(loss["max_drawdown"], -0.19)

    def test_flat_and_one_class_data_do_not_fabricate_auc(self):
        for trend in (False, True):
            bars = make_bars(190)
            prices = np.arange(190) + 100.0 if trend else np.full(190, 100.0)
            bars["open"] = bars["close"] = prices
            bars["high"], bars["low"] = prices + 1, prices - 1
            report = run_baseline(bars, train_days=60, valid_days=30)
            self.assertIsNone(report["classification"]["auc"])
            self.assertIsNone(report["classification"]["balanced_accuracy"])
            self.assertTrue(all(row["probability"] == float(trend) for row in report["predictions"]))
            self.assertEqual(report["strategy_net"]["entries"], int(trend))
            json.dumps(report, allow_nan=False)

    def test_bad_data_and_parameters_fail_closed(self):
        bars = make_bars()
        bad_frames = [bars.iloc[::-1], bars.iloc[:61], pd.concat([bars, bars.iloc[-1:]])]
        for col, value in (("close", np.nan), ("volume", 0), ("open", np.inf), ("low", 9999)):
            bad = bars.copy()
            bad.loc[100, col] = value
            bad_frames.append(bad)
        for bad in bad_frames:
            with self.assertRaises(ValueError):
                run_baseline(bad, train_days=60, valid_days=30)
        for args in ({"threshold": np.nan}, {"slippage": -1}, {"valid_days": 0},
                     {"train_days": 59}, {"validation": "random"}, {"stamp_tax": np.inf}):
            with self.assertRaises(ValueError):
                run_baseline(bars, **args)

    def test_local_loader_is_read_only_and_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.db"
            with self.assertRaises(sqlite3.OperationalError):
                load_local_bars("600519", "2021-01-01", "2022-01-01", path)
            self.assertFalse(path.exists())
            bars = make_bars(100)
            bars["date"] = bars["date"].dt.strftime("%Y-%m-%d")
            bars["code"] = "600519"
            with contextlib.closing(sqlite3.connect(path)) as connection:
                bars.to_sql("k_daily", connection, index=False)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            loaded = load_local_bars("600519", "2021-01-01", "2022-01-01", path)
            self.assertEqual(len(loaded), 100)
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertTrue(load_local_bars("x' OR 1=1 --", "2021-01-01", "2022-01-01", path).empty)

    def test_cli_routes_to_research_and_returns_parseable_json(self):
        import main

        argv = ["main.py", "--ml-baseline", "--stocks", "600519", "--wf-train-days", "60",
                "--wf-valid-days", "30", "--start-date", "2021-01-01", "--end-date", "2022-01-01"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch("strategy.ml_baseline.load_local_bars", return_value=make_bars(190)), \
                mock.patch.object(main, "cmd_full", side_effect=AssertionError("不能启动交易")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main.main()
        report = json.loads(output.getvalue())
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["reports"]["600519"]["status"], "research_only")
        with mock.patch.object(sys, "argv", argv), \
                mock.patch("strategy.ml_baseline.load_local_bars", return_value=make_bars(70)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue())["errors"], ["600519"])


if __name__ == "__main__":
    unittest.main()
