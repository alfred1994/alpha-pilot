#!/usr/bin/env python3
"""机器学习信号对象契约回归测试。"""
import os
import sys
from types import SimpleNamespace
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy.qlib_signal as qlib_signal
import strategy.decision as decision
import data.history as history
from scheduler.pipeline import _parallel_score
from strategy.decision import DimensionScore, _compute_ml_dimension, get_effective_signal_weights
from strategy.qlib_signal import MLPrediction, build_target


def test_ml_prediction_contract():
    original = qlib_signal.get_ml_signal
    try:
        qlib_signal.get_ml_signal = lambda code: MLPrediction(
            score=67.5,
            confidence=0.72,
            detail="ML预测分数=67.5, 置信度=72%",
        )
        dimension = _compute_ml_dimension("600519")
        assert dimension.score == 67.5
        assert dimension.confidence == 0.72
        assert "67.5" in dimension.detail
        print("  OK MLPrediction 对象被正确转换为决策维度")
    finally:
        qlib_signal.get_ml_signal = original


def test_unavailable_ml_is_removed_and_remaining_weights_are_normalized():
    weights = get_effective_signal_weights({
        "technical": DimensionScore("technical", 80, 0.8, "可用"),
        "ml": DimensionScore("ml", 50, 0.0, "ML信号异常: model missing"),
    }, {"technical": 0.35, "ml": 0.17})
    assert weights == {"technical": 0.35}
    print("  OK 不可用ML维度从综合评分权重中剔除")


def test_last_bar_is_not_a_fake_down_label():
    target = build_target(pd.DataFrame({"close": [10, 11, 10]}))
    assert target.iloc[0] == 1
    assert target.iloc[1] == 0
    assert pd.isna(target.iloc[-1])
    print("  OK 最后一根K线不会被伪标为下跌样本")


def test_pipeline_records_ml_degradation_and_effective_weights():
    original_compute = decision.compute_dimension_scores
    original_daily = history.get_daily
    try:
        decision.compute_dimension_scores = lambda code, df=None: {
            "technical": DimensionScore("technical", 80, 0.8, "可用"),
            "ml": DimensionScore("ml", 50, 0.0, "ML信号异常: model missing"),
        }
        history.get_daily = lambda *args, **kwargs: None
        rows = _parallel_score(
            [SimpleNamespace(code="600519", name="贵州茅台")],
            {},
            timeout=2,
        )
        assert len(rows) == 1
        assert rows[0]["composite"] == 80.0
        assert rows[0]["signal_coverage"]["degraded_dimensions"] == ["ml"]
        assert rows[0]["signal_coverage"]["effective_weights"]["technical"] == 1.0
        print("  OK 快链路剔除ML伪中性分并记录归一化权重")
    finally:
        decision.compute_dimension_scores = original_compute
        history.get_daily = original_daily


def main():
    test_ml_prediction_contract()
    test_unavailable_ml_is_removed_and_remaining_weights_are_normalized()
    test_last_bar_is_not_a_fake_down_label()
    test_pipeline_records_ml_degradation_and_effective_weights()


if __name__ == "__main__":
    main()
