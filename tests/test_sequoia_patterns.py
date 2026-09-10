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

    def turtle_breakout_frame(self):
        # 20日横盘后放量阳线突破，成交额 > 1亿
        closes = [10.0] * 21 + [11.2]
        volumes = [2_000_000] * 21 + [12_000_000]
        df = bars(closes, volumes)
        df["amount"] = df["close"] * df["volume"]
        # 最后一日显式写入超额成交额，确保过1亿门槛
        df.loc[len(df) - 1, "amount"] = 150_000_000
        # 让最后一天明显为阳线
        df.loc[len(df) - 1, "open"] = 10.05
        df.loc[len(df) - 1, "high"] = 11.3
        df.loc[len(df) - 1, "low"] = 10.0
        df.loc[len(df) - 1, "close"] = 11.2
        return df

    def test_turtle_entry_breakout(self):
        signal = get_strategy("turtle_trade").generate_signals(
            "x", self.turtle_breakout_frame(), total_assets=1_000_000,
        )
        self.assertEqual(signal.action, "BUY")
        self.assertTrue(signal.metadata["conditions"]["breakout"])
        self.assertTrue(signal.metadata["conditions"]["amount_filter"])
        self.assertGreater(signal.metadata["atr"], 0)
        self.assertGreater(signal.metadata["unit_shares"], 0)

    def test_turtle_amount_filter_blocks(self):
        df = self.turtle_breakout_frame()
        df["amount"] = df["close"] * df["volume"] * 0.001  # 远低于1亿
        signal = get_strategy("turtle_trade").generate_signals("x", df)
        self.assertEqual(signal.action, "HOLD")
        self.assertFalse(signal.metadata["conditions"]["amount_filter"])

    def test_turtle_exit_below_window_low(self):
        closes = [12.0] * 25
        closes[-1] = 8.0
        volumes = [3_000_000] * 25
        df = bars(closes, volumes)
        df["amount"] = df["close"] * df["volume"] * 1_000
        signal = get_strategy("turtle_trade").generate_signals("x", df)
        self.assertEqual(signal.action, "SELL")
        self.assertTrue(signal.metadata["conditions"]["exit_break"])

    def test_uptrend_limit_down_reversal(self):
        n = 70
        closes = list(np.linspace(10, 16, n - 2))
        closes.append(closes[-1] * 0.905)  # 跌停
        closes.append(closes[-2] * 1.02)   # 反包阳线回到跌停前上方
        volumes = [1_000_000] * n
        df = bars(closes, volumes)
        # 跌停日：低开收在近跌停
        df.loc[n - 2, ["open", "high", "low", "close"]] = [15.0, 15.1, 13.9, 13.95]
        # 反包日：低开高走吞没
        df.loc[n - 1, ["open", "high", "low", "close"]] = [13.8, 15.6, 13.7, 15.5]
        df.loc[n - 1, "volume"] = 1_500_000
        signal = get_strategy("uptrend_limit_down").generate_signals("x", df)
        self.assertEqual(signal.action, "BUY")
        self.assertTrue(signal.metadata["conditions"]["limit_down_shakeout"])
        self.assertTrue(signal.metadata["conditions"]["bullish_engulfing"])

    def test_uptrend_limit_down_requires_trend(self):
        n = 70
        closes = list(np.linspace(16, 10, n - 2))
        closes.append(closes[-1] * 0.905)
        closes.append(closes[-2] * 1.02)
        df = bars(closes, [1_000_000] * n)
        df.loc[n - 2, ["open", "high", "low", "close"]] = [11.0, 11.1, 10.0, 10.05]
        df.loc[n - 1, ["open", "high", "low", "close"]] = [10.0, 11.3, 9.95, 11.2]
        signal = get_strategy("uptrend_limit_down").generate_signals("x", df)
        self.assertEqual(signal.action, "HOLD")
        self.assertFalse(signal.metadata["conditions"]["prior_uptrend"])

    def test_registry_contains_full_sequoia_set(self):
        from strategy.strategies import STRATEGY_REGISTRY
        for name in ("high_tight_flag", "rps_breakout", "turtle_trade", "uptrend_limit_down"):
            self.assertIn(name, STRATEGY_REGISTRY)

    def test_technical_screen_includes_new_patterns(self):
        from strategy.technical_screen import STRATEGIES
        self.assertIn("turtle_trade", STRATEGIES)
        self.assertIn("uptrend_limit_down", STRATEGIES)


if __name__ == "__main__":
    unittest.main()
