#!/usr/bin/env python3
"""逐股 ML 标签、样本外切分与 pooled 批量信号的无依赖回归测试。"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy.qlib_signal as qlib_signal
from strategy.qlib_signal import (
    VALIDATION_EMBARGO_DAYS,
    VALIDATION_PURGE_DAYS,
    _time_split_indices,
    build_target,
)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


class _StubPredictor:
    """逐股路径替身：记录调用并返回固定信号。"""

    calls = []

    def __init__(self, train_days=None):
        pass

    def predict(self, code, date=None, lookback_days=None):
        _StubPredictor.calls.append(code)
        return 42.0, 0.7


def _with_stub_predictor():
    original = qlib_signal.QlibPredictor
    qlib_signal.QlibPredictor = _StubPredictor
    _StubPredictor.calls = []
    return original


def main():
    target = build_target(pd.DataFrame({"close": [10.0, 11.0, 10.5]}))
    assert_true(target.iloc[0] == 1 and target.iloc[1] == 0, "次日方向标签正确")
    assert_true(np.isnan(target.iloc[-1]), "最后一行无未来收盘价时标签为空")

    train_end, validation_start = _time_split_indices(120)
    assert_true(train_end == 89 and validation_start == 91, "时间切分保留purge和embargo间隔")
    assert_true(
        validation_start - train_end == VALIDATION_PURGE_DAYS + VALIDATION_EMBARGO_DAYS,
        "训练与验证之间不存在相邻标签泄漏",
    )

    # ── pooled 批量信号 ──
    original_predictor = _with_stub_predictor()
    try:
        # 命中：pooled 可用时直接用批量结果，不进入逐股训练路径
        def _fake_pooled_ok(codes, **kwargs):
            return {
                "600519": {
                    "status": "ok", "score": 66.6, "confidence": 0.58,
                    "model_version": "pooled-lgbm-shadow-v1", "data_cutoff": "2026-08-20",
                },
            }

        import strategy.pooled_ml as pooled_ml
        real_predict_pooled = pooled_ml.predict_pooled
        pooled_ml.predict_pooled = _fake_pooled_ok
        try:
            prefetched = qlib_signal.prefetch_pooled_signals(["600519"])
            assert_true("600519" in prefetched, "pooled可用时预取返回该代码")
            signal = qlib_signal.get_ml_signal("600519")
            assert_true(signal.score == 66.6, "命中缓存时使用pooled分数")
            assert_true(abs(signal.confidence - 0.58) < 1e-9, "置信度取样本外AUC")
            assert_true("pooled-lgbm-shadow-v1" in signal.detail, "详情标注模型版本")
            assert_true(_StubPredictor.calls == [], "命中缓存不触发逐股训练")
        finally:
            pooled_ml.predict_pooled = real_predict_pooled

        # 混合覆盖：不可用的代码回退逐股路径
        def _fake_pooled_mixed(codes, **kwargs):
            return {
                "600519": {"status": "ok", "score": 66.6, "confidence": 0.58,
                           "model_version": "v1", "data_cutoff": "2026-08-20"},
                "000001": {"status": "stale_data", "reason": "candidate_data_stale"},
            }

        pooled_ml.predict_pooled = _fake_pooled_mixed
        try:
            qlib_signal.prefetch_pooled_signals(["600519", "000001"])
            ok_signal = qlib_signal.get_ml_signal("600519")
            fallback_signal = qlib_signal.get_ml_signal("000001")
            assert_true(ok_signal.score == 66.6, "覆盖代码仍走pooled")
            assert_true(fallback_signal.score == 42.0 and fallback_signal.confidence == 0.7,
                        "未覆盖代码回退逐股路径")
            assert_true(_StubPredictor.calls == ["000001"], "只有未覆盖代码触发逐股预测")
        finally:
            pooled_ml.predict_pooled = real_predict_pooled

        # 异常安全：批量预测抛错时不影响逐股回退
        def _boom(codes, **kwargs):
            raise RuntimeError("db closed")

        pooled_ml.predict_pooled = _boom
        try:
            result = qlib_signal.prefetch_pooled_signals(["600519"])
            assert_true(result == {}, "批量预测异常时预取结果为空")
            signal = qlib_signal.get_ml_signal("600519")
            assert_true(signal.score == 42.0, "异常后回退逐股路径")
        finally:
            pooled_ml.predict_pooled = real_predict_pooled

        # 缓存刷新：新一轮预取替换上一轮结果
        def _fake_pooled_v2(codes, **kwargs):
            return {
                "600519": {"status": "ok", "score": 55.5, "confidence": 0.6,
                           "model_version": "v2", "data_cutoff": "2026-08-21"},
            }

        pooled_ml.predict_pooled = _fake_pooled_v2
        try:
            qlib_signal.prefetch_pooled_signals(["600519"])
            qlib_signal.prefetch_pooled_signals(["000002"])
            signal = qlib_signal.get_ml_signal("600519")
            assert_true(signal.score == 42.0, "新扫描未覆盖的旧缓存不残留")
        finally:
            pooled_ml.predict_pooled = real_predict_pooled
    finally:
        qlib_signal.QlibPredictor = original_predictor
        qlib_signal._pooled_signal_cache.clear()

    print("逐股 ML 标签与 pooled 批量信号测试通过")


if __name__ == "__main__":
    main()
