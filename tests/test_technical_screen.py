"""影子集成及回测横截面时点回归；不访问网络或运行账户。"""
import os
import sys
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategy.technical_screen import completed_daily_cutoff, evaluate_patterns, screen_cached_universe
from strategy.strategies.base import Signal


class ScreenTests(unittest.TestCase):
    def test_intraday_excludes_unfinished_bar(self):
        self.assertEqual(completed_daily_cutoff(datetime(2026, 9, 9, 14, 59)), "2026-09-08")
        self.assertEqual(completed_daily_cutoff(datetime(2026, 9, 9, 15, 1)), "2026-09-09")
        seen = []
        class Probe:
            version = "test"
            def generate_signals(self, code, df, **kwargs):
                seen.extend(df.date.tolist())
                return Signal("", code, "HOLD", 0, "probe")
        with patch("strategy.strategies.get_strategy", return_value=Probe()):
            result = evaluate_patterns("600000", pd.DataFrame({"date": ["2026-09-08", "2026-09-09"]}), as_of="2026-09-08")
        self.assertNotIn("2026-09-09", seen)
        self.assertFalse(result["affects_orders"])

    def test_cached_universe_is_readonly_and_excludes_future(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cache.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE k_daily(code,date,open,high,low,close,volume,amount)")
                conn.executemany("INSERT INTO k_daily VALUES(?,?,?,?,?,?,?,?)", [
                    ("600000", "2026-09-08", 10, 11, 9, 10, 100, 1000),
                    ("600000", "2026-09-09", 1000, 1100, 900, 1000, 100, 100000),
                    ("113042", "2026-09-08", 100, 110, 90, 100, 100, 10000),
                ])
                conn.commit()
            before = path.read_bytes()
            result = screen_cached_universe(db_path=path, as_of="2026-09-08")
            self.assertEqual(result["universe_size"], 1)
            self.assertEqual(result["data_as_of"], "2026-09-08")
            self.assertEqual(before, path.read_bytes())

    def test_backtest_supplies_signal_date_panel(self):
        from portfolio.backtest import SimpleBacktestEngine
        captured = []
        class Probe:
            requires_cross_section = True
            def generate_signals(self, code, df, **kwargs):
                captured.append(kwargs)
                return Signal("", code, "HOLD", 0, "probe")
        engine = object.__new__(SimpleBacktestEngine)
        engine.strategy = Probe()
        df = pd.DataFrame({"date": pd.bdate_range("2026-06-01", periods=70).strftime("%Y-%m-%d"),
                           "close": [10.] * 70, "volume": [100.] * 70})
        df.loc[2, "volume"] = 0
        cutoff = df.date.iloc[59]
        engine._run_strategy_decision(SimpleNamespace(positions={}), ["600000"], {"600000": 10}, {"600000": df}, cutoff, df.date.iloc[60])
        self.assertEqual(captured[0]["as_of"], cutoff)
        self.assertEqual(captured[0]["universe_closes"].index.max(), pd.Timestamp(cutoff))
        self.assertTrue(pd.isna(captured[0]["universe_closes"].iloc[2, 0]))

    def test_old_data_and_empty_database(self):
        result = evaluate_patterns("600000", pd.DataFrame({"date": ["2025-01-01"]}), as_of="2026-09-09")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["signals"], [])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "empty.db"
            path.touch()
            self.assertEqual(screen_cached_universe(db_path=path)["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
