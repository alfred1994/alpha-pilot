#!/usr/bin/env python3
"""老 10 策略 as_of 防穿越回归测试。

回测与研究调用方（portfolio/backtest.py、technical_screen.py）都会传
as_of；老 10 策略此前忽略该参数，指标在含未来K线的全量 df 上计算。
本测试验证：截断后 as_of 之后的数据不得影响信号与信号日期。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.strategies.base import truncate_as_of
from strategy.strategies.bollinger_squeeze import BollingerSqueezeStrategy
from strategy.strategies.ma_cross import MACrossStrategy
from strategy.strategies.technical_ensemble import TechnicalEnsembleStrategy


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _make_df(n_flat=65, n_future=3):
    """前段水平盘整，后段(as_of之后)强力拉升制造金叉/突破。"""
    rows = []
    for i in range(n_flat):
        rows.append({
            "date": pd.Timestamp("2026-01-05") + pd.Timedelta(days=i),
            "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0,
            "volume": 1000, "amount": 10000,
        })
    for j in range(n_future):
        price = 11.0 + j
        rows.append({
            "date": pd.Timestamp("2026-01-05") + pd.Timedelta(days=n_flat + j),
            "open": price, "high": price + 0.5, "low": price - 0.2,
            "close": price + 0.3, "volume": 8000, "amount": 88000,
        })
    return pd.DataFrame(rows)


def test_truncate_as_of_helper():
    print("测试1: truncate_as_of 截断行为")
    df = _make_df()
    cutoff = pd.Timestamp("2026-01-05") + pd.Timedelta(days=64)

    truncated = truncate_as_of(df, cutoff)
    assert_true(len(truncated) == 65, f"截断到 as_of 含当日 (got {len(truncated)})")
    assert_true(truncated["date"].max() <= cutoff, "不存在晚于 as_of 的行")
    assert_true(truncated["date"].is_monotonic_increasing, "截断后按日期升序")

    passthrough = truncate_as_of(df, None)
    assert_true(len(passthrough) == len(df), "无 as_of 时保留全部行")
    assert_true(passthrough["date"].is_monotonic_increasing, "无 as_of 时仍排序")

    shuffled = df.sample(frac=1.0, random_state=1)
    assert_true(truncate_as_of(shuffled, None)["date"].is_monotonic_increasing,
                "乱序输入被排序还原")


def test_ma_cross_no_lookahead():
    print("测试2: ma_cross 不使用 as_of 之后的数据")
    strategy = MACrossStrategy()
    df = _make_df()
    as_of = pd.Timestamp("2026-01-05") + pd.Timedelta(days=64)

    full = strategy.generate_signals("600519", df)
    assert_true(pd.Timestamp(full.date) > as_of, f"全量数据下信号在拉升段 ({full.date})")

    guarded = strategy.generate_signals("600519", df, as_of=as_of)
    assert_true(pd.Timestamp(guarded.date) <= as_of,
                f"as_of 截断后信号日期不晚于 as_of (got {guarded.date})")
    assert_true(guarded.action == "HOLD",
                f"拉升段数据被截断后不再触发金叉 (got {guarded.action}: {guarded.reason})")


def test_ensemble_and_bollinger_respect_as_of():
    print("测试3: 组合器与布林策略信号日期不泄露未来")
    df = _make_df(n_flat=95, n_future=3)
    as_of = pd.Timestamp("2026-01-05") + pd.Timedelta(days=94)

    ensemble = TechnicalEnsembleStrategy().generate_signals("600519", df, as_of=as_of)
    assert_true(pd.Timestamp(ensemble.date) <= as_of,
                f"组合器 last_date 不晚于 as_of (got {ensemble.date})")

    boll = BollingerSqueezeStrategy().generate_signals("600519", df, as_of=as_of)
    assert_true(pd.Timestamp(boll.date) <= as_of,
                f"布林挤压信号日期不晚于 as_of (got {boll.date})")

    # 无 as_of 时行为保持原样（能用到全部数据）
    boll_full = BollingerSqueezeStrategy().generate_signals("600519", df)
    assert_true(pd.Timestamp(boll_full.date) == df["date"].iloc[-1],
                "无 as_of 时信号日期为最后一根K线")


def main():
    test_truncate_as_of_helper()
    test_ma_cross_no_lookahead()
    test_ensemble_and_bollinger_respect_as_of()
    print("\n全部 as_of 防穿越测试通过")


if __name__ == "__main__":
    main()
