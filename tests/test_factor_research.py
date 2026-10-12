#!/usr/bin/env python3
"""多因子研究闭环：时间因果、筛选隔离、共享资金和受限成交。"""
import contextlib
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portfolio.factor_backtest import ExecutionConfig, run_portfolio
from strategy.factor_research import (
    FactorConfig, build_factor_panel, factor_diagnostics, fit_fusion,
    load_local_panel, run_factor_research, validate_panel,
)
from strategy.ml_baseline import FEATURE_COLS


def make_panel(n=160, stocks=8):
    dates = pd.bdate_range("2022-01-03", periods=n)
    rows = []
    for i in range(stocks):
        drift = (i - stocks / 2) * 0.0008
        close = (10 + i) * np.exp(np.arange(n) * drift + np.sin(np.arange(n) / 9) * 0.002)
        for j, date in enumerate(dates):
            previous = close[max(0, j - 1)]
            rows.append({"date": date, "code": f"600{i + 1:03d}", "open": close[j],
                         "high": close[j] * 1.01, "low": close[j] * 0.99, "close": close[j],
                         "volume": float(100_000 + i * 1000 + j * (i + 1)),
                         "raw_open": close[j], "raw_close": close[j],
                         "limit_up": previous * 1.1, "limit_down": previous * 0.9,
                         "tradable": True, "eligible": True,
                         "market_cap": float((i + 1) * 1e9), "industry": str(i % 2)})
    return pd.DataFrame(rows).sort_values(["date", "code"]).reset_index(drop=True)


def small_config():
    return FactorConfig(train_days=40, valid_days=25, rebalance_days=5, top_k=2,
                        min_ic_days=12, min_abs_rank_ic=0.02, max_factors=3)


class FactorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = make_panel()
        cls.config = small_config()
        cls.execution = ExecutionConfig(max_positions=2, max_weight=0.5)
        cls.report = run_factor_research(cls.panel, cls.config, cls.execution)

    def test_complete_loop_has_selected_factors_and_reconciled_shared_account(self):
        report = self.report
        self.assertGreater(len(report["folds"]), 1)
        self.assertTrue(any(fold["fusion"]["weights"] for fold in report["folds"]))
        self.assertTrue(report["strategy"]["trades"])
        self.assertFalse(report["promotion_allowed"])
        self.assertEqual(report["execution_status"], "completed")
        cash = self.execution.capital
        for day in report["strategy"]["curve"]:
            cash += sum(t["cash_change"] for t in report["strategy"]["trades"] if t["date"] == day["date"])
            self.assertAlmostEqual(cash, day["cash"], places=7)
            self.assertGreaterEqual(day["cash"], -1e-8)
            self.assertLessEqual(len(day["holdings"]), 2)
            self.assertAlmostEqual(day["equity"], cash + sum(p["shares"] * p["mark"] for p in day["holdings"].values()))
        for trade in report["strategy"]["trades"]:
            self.assertGreater(trade["date"], trade["signal_date"])
            self.assertEqual(trade["shares"] % 100, 0)
            self.assertGreaterEqual(trade["commission"], 5)
        self.assertEqual([d["date"] for d in report["strategy"]["curve"]],
                         [d["date"] for d in report["equal_weight_benchmark"]["curve"]])
        compound = np.prod([1 + fold["strategy_net"]["total_return"] for fold in report["folds"]]) - 1
        self.assertAlmostEqual(compound, report["strategy"]["metrics"]["total_return"])
        json.dumps(report, allow_nan=False)

    def test_every_fold_purges_actual_label_end(self):
        for fold in self.report["folds"]:
            self.assertLess(fold["train_label_end"], fold["validation_start"])
            self.assertGreater(fold["purged_or_unlabelled_rows"], 0)
            self.assertAlmostEqual(sum(abs(w) for w in fold["fusion"]["weights"].values()), 1)

    def test_future_prices_and_missing_future_bars_do_not_change_past_choices(self):
        changed = self.panel.copy()
        cutoff = sorted(changed["date"].unique())[115]
        future = changed["date"] > cutoff
        cols = ["open", "close", "high", "low", "raw_open", "raw_close", "limit_up", "limit_down"]
        changed.loc[future, cols] *= 1.05
        # 验证股票次日缺数据不应从前一天候选池中被提前删除。
        changed = changed[~((changed["date"] > cutoff) & (changed["code"] == "600008"))]
        report = run_factor_research(changed, self.config, self.execution)
        before = [s for s in self.report["selections"] if s["date"] <= str(pd.Timestamp(cutoff).date())]
        after = [s for s in report["selections"] if s["date"] <= str(pd.Timestamp(cutoff).date())]
        self.assertEqual(before, after)
        self.assertEqual(self.report["folds"][0]["fusion"], report["folds"][0]["fusion"])
        self.assertNotEqual(self.report["input_sha256"], report["input_sha256"])

    def test_factors_do_not_read_future_and_eligibility_is_point_in_time(self):
        panel = validate_panel(self.panel)
        factors = build_factor_panel(panel)
        changed = panel.copy()
        cutoff = sorted(changed["date"].unique())[100]
        changed.loc[changed["date"] > cutoff, "volume"] *= 3
        changed.loc[(changed["date"] > cutoff) & (changed["code"] == "600001"), "eligible"] = False
        modified = build_factor_panel(changed)
        cols = ["date", "code"] + FEATURE_COLS
        pd.testing.assert_frame_equal(factors.loc[factors["date"] <= cutoff, cols].reset_index(drop=True),
                                      modified.loc[modified["date"] <= cutoff, cols].reset_index(drop=True))
        self.assertFalse(((modified["date"] > cutoff) & (modified["code"] == "600001")).any())

    def test_ic_sign_redundancy_and_constant_factor(self):
        rows = []
        for date in pd.bdate_range("2020-01-01", periods=20):
            for i in range(8):
                row = {col: 0.0 for col in FEATURE_COLS}
                row.update(date=date, code=str(i), forward_return=i / 100,
                           return_5=float(i), return_10=-float(i), return_20=float(i))
                rows.append(row)
        train = pd.DataFrame(rows)
        result = fit_fusion(train, small_config())
        self.assertEqual(len(result["weights"]), 1)
        selected = next(iter(result["weights"]))
        self.assertEqual(result["weights"][selected], -1.0 if selected == "return_10" else 1.0)
        self.assertEqual(result["diagnostics"]["volume_ratio"]["ic_days"], 0)
        self.assertIn("insufficient_ic_days", result["diagnostics"]["volume_ratio"]["exclusions"])
        self.assertTrue(any(any("correlated_with:" in reason for reason in item["exclusions"])
                            for item in result["diagnostics"].values()))
        positive = factor_diagnostics(train, ["return_5"])["return_5"]
        self.assertAlmostEqual(positive["rank_ic_mean"], 1)
        self.assertGreater(positive["top_minus_bottom"], 0)

    def test_no_valid_factors_stays_cash(self):
        config = replace(self.config, min_abs_rank_ic=1.0, min_ic_days=100)
        # validate 要求训练窗覆盖最低样本数，此处测试纯筛选及其下游空仓，不绕过该门禁。
        factors = build_factor_panel(validate_panel(self.panel))
        fitted = fit_fusion(factors, config)
        self.assertEqual(fitted["weights"], {})
        dates = sorted(self.panel["date"].unique())
        book = run_portfolio(self.panel, {dates[100]: {}}, self.execution)
        self.assertEqual(book["metrics"]["total_return"], 0)
        self.assertEqual(book["trades"], [])

    def test_neutralization_requires_historical_metadata(self):
        panel = validate_panel(self.panel)
        with self.assertRaisesRegex(ValueError, "中性化"):
            build_factor_panel(panel.drop(columns="market_cap"), neutralize=True)
        result = build_factor_panel(panel, neutralize=True)
        valid = result.dropna(subset=FEATURE_COLS)
        self.assertGreater(len(valid), 0)
        np.testing.assert_allclose(valid.groupby("date")[FEATURE_COLS].mean(), 0, atol=1e-10)

    def test_bad_data_and_insufficient_universe_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, "横截面"):
            run_factor_research(self.panel[self.panel["code"] == "600001"], self.config, self.execution)
        with self.assertRaisesRegex(ValueError, "重复"):
            validate_panel(pd.concat([self.panel, self.panel.iloc[:1]]))
        with self.assertRaisesRegex(ValueError, "递增"):
            validate_panel(self.panel.iloc[::-1])
        bad = self.panel.copy()
        bad.loc[0, "volume"] = -1
        with self.assertRaises(ValueError):
            validate_panel(bad)
        bad = self.panel.copy()
        bad.loc[0, "raw_open"] = np.nan
        with self.assertRaisesRegex(ValueError, "配对"):
            validate_panel(bad)

    def test_loader_is_read_only_and_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.db"
            with self.assertRaises(sqlite3.OperationalError):
                load_local_panel(["600001"], "2022-01-01", "2023-01-01", path)
            self.assertFalse(path.exists())
            data = self.panel.copy()
            data["date"] = data["date"].dt.strftime("%Y-%m-%d")
            with contextlib.closing(sqlite3.connect(path)) as connection:
                data.to_sql("k_daily", connection, index=False)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            loaded = load_local_panel(["600001"], "2022-01-01", "2023-01-01", path)
            self.assertEqual(set(loaded["code"]), {"600001"})
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_cli_dispatch_and_report_file_never_overwrites_input(self):
        import main
        codes = sorted(self.panel["code"].unique())
        argv = ["main.py", "--factor-research", "--stocks"] + codes + [
            "--start-date", "2022-01-01", "--end-date", "2022-12-31",
            "--wf-train-days", "40", "--wf-valid-days", "25", "--factor-top-k", "2"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with mock.patch.object(sys, "argv", argv + ["--factor-report", str(path)]), \
                    mock.patch("strategy.factor_research.load_local_panel", return_value=self.panel), \
                    mock.patch("strategy.factor_research.run_factor_research", return_value=self.report), \
                    mock.patch.object(main, "cmd_full", side_effect=AssertionError("不能执行交易")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                main.main()
            self.assertEqual(json.loads(output.getvalue())["errors"], [])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], "factor-loop-v1")
            original = path.read_bytes()
            with mock.patch.object(sys, "argv", argv + ["--factor-report", str(path)]), \
                    mock.patch("strategy.factor_research.load_local_panel", return_value=self.panel), \
                    mock.patch("strategy.factor_research.run_factor_research", return_value=self.report), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main.main()
            self.assertEqual(path.read_bytes(), original)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.panel = make_panel(n=5, stocks=2)
        self.panel[["open", "close", "raw_open", "raw_close"]] = 10.0
        self.panel["high"], self.panel["low"] = 10.1, 9.9
        self.panel["limit_up"], self.panel["limit_down"] = 11.0, 9.0
        self.dates = sorted(self.panel["date"].unique())
        self.config = ExecutionConfig(capital=10_000, max_positions=1, max_weight=1.0, slippage=0)

    def test_flat_price_round_trip_fee_cash_and_t_plus_one(self):
        targets = {self.dates[0]: {"600001": 1.0}, self.dates[1]: {}}
        book = run_portfolio(self.panel, targets, self.config)
        buy, sell = book["trades"]
        self.assertEqual((buy["shares"], sell["shares"]), (900, 900))
        self.assertEqual(buy["date"], str(pd.Timestamp(self.dates[1]).date()))
        self.assertEqual(sell["date"], str(pd.Timestamp(self.dates[2]).date()))
        self.assertAlmostEqual(book["metrics"]["ending_equity"], 10000 - 5 - 5 - 4.5)
        self.assertEqual(book["ending_positions"], {})

    def test_limit_down_locked_position_does_not_fund_replacement(self):
        date = self.dates[2]
        mask = (self.panel["code"] == "600001") & (self.panel["date"] >= date)
        self.panel.loc[mask, ["open", "raw_open", "close", "raw_close"]] = 9
        targets = {self.dates[0]: {"600001": 1.0}, self.dates[1]: {"600002": 1.0}}
        book = run_portfolio(self.panel, targets, self.config)
        self.assertEqual([t["side"] for t in book["trades"]], ["BUY"])
        self.assertIn("limit_down", book["blocked_counts"])
        self.assertIn("position_slots_locked", book["blocked_counts"])
        self.assertIn("600001", book["ending_positions"])

    def test_limit_up_missing_bar_and_suspension_block_then_retry(self):
        mask = (self.panel["code"] == "600001") & (self.panel["date"] == self.dates[1])
        self.panel.loc[mask, "raw_open"] = 11
        mask2 = (self.panel["code"] == "600001") & (self.panel["date"] == self.dates[2])
        self.panel.loc[mask2, "tradable"] = False
        mask3 = (self.panel["code"] == "600001") & (self.panel["date"] == self.dates[3])
        panel = self.panel.loc[~mask3]
        book = run_portfolio(panel, {self.dates[0]: {"600001": 1.0}}, self.config)
        self.assertIn("limit_up", book["blocked_counts"])
        self.assertIn("suspended", book["blocked_counts"])
        self.assertIn("missing_bar", book["blocked_counts"])
        self.assertEqual(len(book["trades"]), 1)
        self.assertEqual(book["trades"][0]["date"], str(pd.Timestamp(self.dates[4]).date()))

    def test_missing_held_quote_keeps_value_and_reports_staleness(self):
        panel = self.panel[~((self.panel["code"] == "600001") & (self.panel["date"] >= self.dates[2]))]
        book = run_portfolio(panel, {self.dates[0]: {"600001": 1.0}}, self.config)
        self.assertEqual(book["curve"][-1]["stale_marks"], ["600001"])
        self.assertAlmostEqual(book["metrics"]["ending_equity"], 9995)

    def test_strict_mode_never_silently_uses_adjusted_prices(self):
        panel = self.panel.drop(columns=["raw_open", "raw_close", "limit_up", "limit_down", "tradable"])
        targets = {self.dates[0]: {"600001": 1.0}}
        strict = run_portfolio(panel, targets, self.config)
        self.assertEqual(strict["trades"], [])
        self.assertIn("missing_signal_price", strict["blocked_counts"])
        approximate = run_portfolio(panel, targets, replace(self.config, mode="research"))
        self.assertEqual(len(approximate["trades"]), 1)

    def test_quantities_use_signal_close_not_future_close(self):
        target = {self.dates[0]: {"600001": 0.5}}
        before = run_portfolio(self.panel, target, self.config)
        modified = self.panel.copy()
        modified.loc[modified["date"] == self.dates[1], "raw_close"] = 10.5
        after = run_portfolio(modified, target, self.config)
        self.assertEqual(before["trades"][0], after["trades"][0])

    def test_partial_rebalance_preserves_remaining_lots_and_cash(self):
        targets = {self.dates[0]: {"600001": 0.8}, self.dates[1]: {"600001": 0.4}, self.dates[2]: {}}
        book = run_portfolio(self.panel, targets, self.config)
        self.assertEqual([t["shares"] for t in book["trades"]], [800, 500, 300])
        self.assertEqual([t["side"] for t in book["trades"]], ["BUY", "SELL", "SELL"])
        self.assertAlmostEqual(book["metrics"]["ending_equity"], 10000 - 15 - 4)

    def test_two_stocks_share_cash_and_unaffordable_lots_remain_cash(self):
        config = replace(self.config, max_positions=2, max_weight=0.5)
        book = run_portfolio(self.panel, {self.dates[0]: {"600001": 0.5, "600002": 0.5}}, config)
        self.assertEqual(sum(t["shares"] for t in book["trades"]), 900)
        self.assertAlmostEqual(book["curve"][0]["cash"], 990)
        self.assertEqual(len(book["ending_positions"]), 2)

    def test_small_target_budget_has_explicit_no_trade_reason(self):
        book = run_portfolio(self.panel, {self.dates[0]: {"600001": 0.01}}, self.config)
        self.assertEqual(book["trades"], [])
        self.assertEqual(book["blocked_counts"]["target_budget_below_one_lot"], 1)


if __name__ == "__main__":
    unittest.main()
