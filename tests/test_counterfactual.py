#!/usr/bin/env python3
"""候选反事实学习测试。"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.counterfactual import (
    evaluate_candidate_outcomes,
    record_trade_plan_candidates,
    summarize_candidate_outcomes,
)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = handle.name
    handle.close()
    os.unlink(db_path)
    try:
        plan = SimpleNamespace(
            date="2026-06-01",
            raw_scores=[{
                "code": "600519", "name": "测试候选", "composite": 66,
                "latest_price": 10.0, "llm_action": "HOLD",
                "llm_confidence": 0.7, "llm_reason": "等待确认",
                "dimensions": {"technical": {"score": 66, "confidence": 0.7}},
            }],
            orders=[], hold_reasons={"600519": "HOLD_LLM(等待确认)"},
            strategy_version="test-v1", regime="sideways",
        )
        assert_true(record_trade_plan_candidates(plan, db_path=db_path) == 1, "候选首次观察写入")
        assert_true(record_trade_plan_candidates(plan, db_path=db_path) == 1, "重复扫描更新同一观察")

        rows = []
        for index in range(1, 12):
            price = 10.0 + index * 0.12
            rows.append({
                "code": "600519", "date": f"2026-06-{index + 1:02d}",
                "open": price, "high": price + 0.08, "low": price - 0.05,
                "close": price, "volume": 1000, "amount": 10000,
                "turn": 1, "pctChg": 1,
            })
        with Database(db_path=db_path) as db:
            db.insert_k_daily(rows, source="test")
            assert_true(len(db.get_candidate_outcomes()) == 1, "重复观察不产生重复样本")

        result = evaluate_candidate_outcomes(db_path=db_path)
        assert_true(result["updated"] == 1 and result["matured"] == 1, "盘后回填候选后续结果")
        with Database(db_path=db_path) as db:
            observation = db.get_candidate_outcomes()[0]
            assert_true(observation["return_1d"] is not None, "回填1日收益")
            assert_true(observation["return_5d"] > 0.05, "回填5日收益")
            assert_true(observation["outcome_label"] == "missed_opportunity", "识别错误HOLD踏空")
        summary = summarize_candidate_outcomes(days=3650, db_path=db_path)
        assert_true(summary.get("missed_opportunity") == 1, "反事实摘要可统计踏空")
        print("候选反事实学习测试通过")
    finally:
        for path in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    main()
