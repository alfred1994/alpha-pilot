"""线上技术分历史检验脚本：重放口径与线上一致、无未来数据、统计结构完整。"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import eval_live_scoring as ev
from strategy.decision import _technical_dimension


def synthetic_panel(n_codes=40, n_days=420, seed=5):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2016-01-04", periods=n_days).strftime("%Y-%m-%d")
    codes = [f"{600000 + i:06d}" for i in range(n_codes)]
    rets = rng.normal(0.0003, 0.02, size=(n_days, n_codes))
    close = pd.DataFrame(20 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=codes)
    open_ = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.003, size=close.shape))
    high = np.maximum(close, open_) * 1.01
    low = np.minimum(close, open_) * 0.99
    volume = pd.DataFrame(rng.uniform(5e6, 2e7, size=close.shape), index=dates, columns=codes)
    return {"close": close, "open": open_, "high": high, "low": low, "raw_close": close,
            "raw_open": open_, "ref": close.shift(1), "amount": close * 0 + 2e8,
            "volume": volume, "flag": close * 0}


class EvalLiveScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = synthetic_panel()
        cls.frame = ev.collect(cls.panel, start="2016-01-01", step=40, per_day=12, horizons=(5,), log_every=10**6)

    def test_technical_score_matches_live_aggregation(self):
        close = self.panel["close"].iloc[:ev.WINDOW]
        code = close.columns[0]
        df = pd.DataFrame({name: self.panel[name][code].iloc[:ev.WINDOW].to_numpy()
                           for name in ("open", "high", "low", "close", "volume")})
        df.insert(0, "date", close.index)
        score, subs = ev.technical_score(df)
        live = _technical_dimension(df)
        self.assertAlmostEqual(score, live.score, places=9)
        self.assertEqual(len(subs), 9)

    def test_collect_uses_only_past_bars_and_labels_future(self):
        self.assertFalse(self.frame.empty)
        dates = list(self.panel["close"].index)
        for date in self.frame["date"].unique():
            self.assertGreaterEqual(dates.index(date), ev.WINDOW - 1)
        row = self.frame.dropna(subset=["net5"]).iloc[0]
        pos = dates.index(row["date"])
        opens = self.panel["open"][row["code"]]
        expected = opens.iloc[pos + 6] / opens.iloc[pos + 1] - 1 - ev.ROUND_TRIP_COST
        self.assertAlmostEqual(row["net5"], expected, places=9)

    def test_signal_unchanged_when_future_bars_change(self):
        dates = list(self.panel["close"].index)
        date = self.frame["date"].iloc[0]
        pos = dates.index(date)
        altered = {k: v.copy() for k, v in self.panel.items()}
        for name in ("open", "high", "low", "close"):
            altered[name].iloc[pos + 1:] *= 3.0
        again = ev.collect(altered, start="2016-01-01", step=40, per_day=12, horizons=(5,), log_every=10**6)
        left = self.frame[self.frame["date"] == date].set_index("code")["tech"]
        right = again[again["date"] == date].set_index("code")["tech"]
        pd.testing.assert_series_equal(left.sort_index(), right.sort_index())

    def test_analyze_report_structure(self):
        report = ev.analyze(self.frame, (5,), 58.0)
        block = report["horizons"]["5d"]
        self.assertIn("all", block["ic"])
        self.assertIn("tech", block["ic"]["all"])
        self.assertEqual(len(block["buckets"]), len(ev.TECH_BUCKETS) + 1)
        self.assertIn("passed", block["gate"]["all"])
        self.assertTrue(ev.format_report(report))


if __name__ == "__main__":
    unittest.main()
