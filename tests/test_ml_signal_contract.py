#!/usr/bin/env python3
"""机器学习信号对象契约回归测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy.qlib_signal as qlib_signal
from strategy.decision import _compute_ml_dimension
from strategy.qlib_signal import MLPrediction


def main():
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


if __name__ == "__main__":
    main()
