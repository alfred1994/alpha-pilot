#!/usr/bin/env python3
"""walk-forward 回测回归测试。

覆盖:
1. build_walk_forward_folds 折切分几何（纯逻辑，无需 vectorbt）。
2. run_walk_forward 端到端（合成价格注入，vectorbt 不可用时跳过引擎部分）。
"""
import os
import sys
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import portfolio.fast_backtest as fb


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _make_index(n):
    days = pd.bdate_range("2024-01-02", periods=n)
    return pd.DatetimeIndex(days)


try:
    import vectorbt  # noqa: F401
    HAVE_VECTORBT = True
except ImportError:
    HAVE_VECTORBT = False


def test_fold_geometry():
    print("测试1: 折切分几何")
    index = _make_index(250 + 3 * 60)  # 恰好3折
    folds = fb.build_walk_forward_folds(index, train_days=250, valid_days=60)
    assert_true(len(folds) == 3, f"430根/250/60 → 3折 (got {len(folds)})")
    t0, t1, v0, v1 = folds[0]
    assert_true((t0, t1, v0, v1) == (0, 249, 250, 309), f"首折边界 {folds[0]}")
    t0, t1, v0, v1 = folds[1]
    assert_true(t1 == v0 - 1, "验证窗紧随训练窗")
    assert_true(folds[-1][3] == len(index) - 1, "末折覆盖到序列末尾")

    # 数据不足
    assert_true(fb.build_walk_forward_folds(_make_index(100), 250, 60) == [],
                "数据不足返回空折")

    # 末折不满一个验证窗时截断
    index2 = _make_index(250 + 90)
    folds2 = fb.build_walk_forward_folds(index2, 250, 60)
    assert_true(len(folds2) == 2 and folds2[-1][3] == 339,
                f"不足整窗的末折截断 (got {folds2[-1]})")


def test_walk_forward_end_to_end():
    if not HAVE_VECTORBT:
        print("测试2: 跳过（vectorbt 未安装，仅验证折切分）")
        return
    print("测试2: walk-forward 端到端（合成价格）")
    rng = np.random.default_rng(7)
    n = 550
    # 震荡序列：保证 MA 交叉与 RSI 超卖同时出现的行情存在（纯趋势下
    # RSI 一直在超卖区上方，策略按定义不成交，无法检验引擎）
    phases = np.cumsum(rng.normal(0, 0.05, n))
    close = pd.Series(
        100 * (1 + 0.08 * np.sin(np.linspace(0, 12 * np.pi, n) + phases))
        + rng.normal(0, 0.8, n),
        index=_make_index(n),
    ).clip(lower=1.0)
    small_grid = {
        "short_ma": [5, 10],
        "long_ma": [20, 30],
        "rsi_period": [14],
        "rsi_oversold": [30.0, 50.0],
        "rsi_overbought": [70.0],
        "stop_loss": [-0.08],
        "take_profit": [0.10],
    }
    start = close.index[0].strftime("%Y-%m-%d")
    end = close.index[-1].strftime("%Y-%m-%d")

    with mock.patch.object(fb, "_load_close_series", return_value=close):
        result = fb.run_walk_forward(
            "600519", start, end,
            param_grid=small_grid, train_days=250, valid_days=60,
        )

    assert_true(result is not None, "端到端返回结果")
    assert_true(result.n_folds == 5, f"620根/250/60 → 5折 (got {result.n_folds})")
    assert_true(len(result.folds) == result.n_folds, "折记录齐全")
    for f in result.folds:
        assert_true(f.in_sample is not None, f"折{f.fold} 有样本内结果")
        assert_true(f.valid_start > f.train_end, f"折{f.fold} 验证窗在训练窗之后")
    assert_true(-1.0 <= result.oos_compound_return <= 10.0,
                f"样本外复利收益在合理范围 ({result.oos_compound_return:+.2%})")
    assert_true(result.oos_worst_drawdown <= 0, "样本外最差回撤非正")
    assert_true(np.isfinite(result.is_avg_sharpe) and np.isfinite(result.oos_avg_sharpe),
                "夏普聚合不含 inf/NaN")
    assert_true(result.sharpe_degradation == result.is_avg_sharpe - result.oos_avg_sharpe,
                "夏普衰减=样本内-样本外")

    report = fb.format_walk_forward_report(result)
    assert_true("样本外复利收益" in report and "Walk-Forward 报告" in report,
                "报告包含样本外口径")


def main():
    test_fold_geometry()
    test_walk_forward_end_to_end()
    print("\n全部 walk-forward 测试通过")


if __name__ == "__main__":
    main()
