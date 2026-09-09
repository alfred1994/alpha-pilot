"""可解释形态的合成行情回归；不请求网络、不下单。"""
import sys
from pathlib import Path
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy.strategies import get_strategy


def bars(closes, volumes=None):
    c = np.array(closes, dtype=float)
    return pd.DataFrame({"date": pd.bdate_range("2025-01-01", periods=len(c)),
                         "open": c * .995, "high": c * 1.005, "low": c * .99,
                         "close": c, "volume": np.array(volumes, dtype=float) if volumes is not None else np.ones(len(c)) * 1000})


class PatternTests(unittest.TestCase):
    def flag(self):
        return bars(list(np.linspace(10, 17, 30)) + [16.5] * 10 + [18],
                    [1000] * 30 + [500] * 10 + [1500])

    def rps_data(self):
        df = bars(np.linspace(10, 20, 61))
        panel = pd.DataFrame({"target": df.close.to_numpy(), **{
            f"peer{i}": np.linspace(10, 10 + i / 10, 61) for i in range(19)}}, index=df.date)
        return df, panel

    def test_flag_chronological_breakout(self):
        signal = get_strategy("high_tight_flag").generate_signals("x", self.flag())
        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.metadata["breakout_level"], 17 * 1.005)

    def test_flag_downtrend_not_pole(self):
        df = self.flag()
        df.iloc[:30, df.columns.get_indexer(["open", "high", "low", "close"])] = bars(np.linspace(17, 10, 30))[["open", "high", "low", "close"]].to_numpy()
        self.assertEqual(get_strategy("high_tight_flag").generate_signals("x", df).action, "HOLD")

    def test_flag_requires_contraction(self):
        df = self.flag()
        df.loc[30:39, "volume"] = 2000
        self.assertEqual(get_strategy("high_tight_flag").generate_signals("x", df).action, "HOLD")

    def test_flag_requires_breakout(self):
        df = self.flag()
        df.loc[40, ["open", "high", "low", "close"]] = [16.4, 16.7, 16.3, 16.5]
        self.assertEqual(get_strategy("high_tight_flag").generate_signals("x", df).action, "HOLD")

    def test_future_bars_excluded(self):
        df = self.flag()
        cutoff = df.date.iloc[-1]
        future = bars([1])
        future["date"] = cutoff + pd.Timedelta(days=1)
        strategy = get_strategy("high_tight_flag")
        expected = strategy.generate_signals("x", df)
        actual = strategy.generate_signals("x", pd.concat([df, future]), as_of=cutoff)
        self.assertEqual(actual, expected)

    def test_missing_invalid_suspended_insufficient(self):
        for strategy in [get_strategy("high_tight_flag"), get_strategy("rps_breakout")]:
            for value in [None, pd.DataFrame(), self.flag().iloc[:5]]:
                self.assertEqual(strategy.generate_signals("x", value).action, "HOLD")
        for value in [0, float("nan"), float("inf"), -1]:
            df = self.flag()
            df.loc[35, "volume"] = value
            self.assertEqual(get_strategy("high_tight_flag").generate_signals("x", df).action, "HOLD")

    def test_rps_relative_ranking_and_shifted_breakout(self):
        df, panel = self.rps_data()
        signal = get_strategy("rps_breakout").generate_signals("target", df, universe_closes=panel)
        self.assertEqual(signal.action, "BUY")
        self.assertEqual(signal.metadata["rps"], 100)
        self.assertAlmostEqual(signal.metadata["breakout_level"], df.high.iloc[-2])

    def test_rps_absolute_gain_not_relative_strength(self):
        df, panel = self.rps_data()
        for c in panel.columns[1:]:
            panel[c] = np.linspace(10, 30, 61)
        signal = get_strategy("rps_breakout").generate_signals("target", df, universe_closes=panel)
        self.assertEqual(signal.action, "HOLD")
        self.assertLess(signal.metadata["rps"], 10)

    def test_rps_missing_universe(self):
        df, _ = self.rps_data()
        self.assertEqual(get_strategy("rps_breakout").generate_signals("target", df).action, "HOLD")

    def test_rps_missing_middle_day_reduces_sample(self):
        df, panel = self.rps_data()
        panel.iloc[30, 1] = np.nan
        signal = get_strategy("rps_breakout").generate_signals("target", df, universe_closes=panel)
        self.assertEqual(signal.action, "HOLD")
        self.assertEqual(signal.metadata["universe_size"], 19)

    def test_rps_adjustment_mismatch(self):
        df, panel = self.rps_data()
        panel["target"] *= 2
        self.assertIn("复权", get_strategy("rps_breakout").generate_signals("target", df, universe_closes=panel).reason)

    def test_rps_future_data_excluded(self):
        df, panel = self.rps_data()
        strategy = get_strategy("rps_breakout")
        expected = strategy.generate_signals("target", df, universe_closes=panel)
        panel.loc[panel.index[-1] + pd.Timedelta(days=1)] = 9999
        actual = strategy.generate_signals("target", df, universe_closes=panel, as_of=df.date.iloc[-1])
        self.assertEqual(expected, actual)

    def test_duplicate_dates_rejected(self):
        df, panel = self.rps_data()
        panel = pd.concat([panel, panel.iloc[-1:]])
        self.assertEqual(get_strategy("rps_breakout").generate_signals("target", df, universe_closes=panel).action, "HOLD")

    def test_instance_params_are_isolated(self):
        a = get_strategy("rps_breakout")
        a.set_params({"period": 10})
        self.assertEqual(get_strategy("rps_breakout").params["period"], 60)


if __name__ == "__main__":
    unittest.main()
