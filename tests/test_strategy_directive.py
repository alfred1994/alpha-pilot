#!/usr/bin/env python3
"""AI 日终策略指令契约测试。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.directive import (
    _next_trading_date,
    generate_and_save_strategy_directive,
    get_effective_trade_policy,
)
from scheduler.pipeline import TradePlan


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def directive_response(params=None):
    return json.dumps({
        "intent": "谨慎探索",
        "regime": "sideways",
        "summary": "评分门槛压制了LLM判断，明日扩大评估范围。",
        "diagnosis": "候选存在，但最高分未进入LLM评估链路。",
        "rationale": "在空仓和风险可控前提下，先扩大LLM评估样本，执行仍保持低仓位。",
        "hypothesis": "若LLM评估恢复且仍多数HOLD，说明市场确实缺少机会。",
        "params": params or {"top_k": 3, "min_score": 60, "max_weight": 0.06},
    }, ensure_ascii=False)


def main():
    db_file = tempfile.NamedTemporaryFile(suffix="_directive.db", delete=False)
    db_path = db_file.name
    db_file.close()
    os.unlink(db_path)
    try:
        import strategy.directive as directive_module
        original_next_trading_day = directive_module.next_trading_day
        directive_module.next_trading_day = lambda date: "20260730"
        try:
            assert_true(
                _next_trading_date("2026-07-29") == "2026-07-30",
                "日终指令会把复盘日期转换为下一交易日",
            )
        finally:
            directive_module.next_trading_day = original_next_trading_day

        directive = generate_and_save_strategy_directive(
            review_date="2026-07-29",
            effective_date="2026-07-30",
            review_data={"total_assets": 1000000, "daily_pnl": 0, "trade_reviews": []},
            llm_review="当日无成交，需判断是市场观望还是评分门槛压制。",
            regime="sideways",
            current_params={"top_k": 1, "min_score": 75, "max_weight": 0.03},
            db_path=db_path,
            llm_call=lambda prompt: directive_response(),
        )
        assert_true(directive is not None, "LLM JSON 会生成策略指令")
        assert_true(directive["effective_date"] == "2026-07-30", "策略指令标记下一交易日生效")

        assert_true(
            get_effective_trade_policy("2026-07-29", "sideways", db_path=db_path) is None,
            "策略指令不会在复盘当日提前生效",
        )
        policy = get_effective_trade_policy("2026-07-30", "sideways", db_path=db_path)
        assert_true(policy is not None, "下一交易日能读取AI策略指令")
        assert_true(policy["version"] == directive["version"], "读取到同一策略版本")
        assert_true(policy["params"] == {"top_k": 3, "min_score": 60.0, "max_weight": 0.06}, "指令参数可被交易链路直接消费")

        with Database(db_path=db_path) as db:
            pending = db.get_next_strategy_directive("2026-07-29")
        assert_true(pending["version"] == directive["version"], "复盘当日可读取待生效策略指令供看板展示")

        plan = TradePlan(date="2026-07-30", strategy_version=policy["version"], strategy_intent=policy["intent"])
        plan_data = plan.to_dict()
        assert_true(plan_data["strategy_version"] == directive["version"], "TradePlan 记录实际执行的策略版本")
        assert_true(plan_data["strategy_intent"] == "谨慎探索", "TradePlan 记录AI策略意图")

        rejected = generate_and_save_strategy_directive(
            review_date="2026-07-30",
            effective_date="2026-07-31",
            review_data={},
            llm_review="异常范围测试",
            regime="sideways",
            current_params=policy["params"],
            db_path=db_path,
            llm_call=lambda prompt: directive_response({"top_k": 8, "min_score": 60, "max_weight": 0.06}),
        )
        assert_true(rejected is None, "越界策略指令不会覆盖有效版本")
        retained = get_effective_trade_policy("2026-07-31", "sideways", db_path=db_path)
        assert_true(retained["version"] == directive["version"], "无效指令时沿用上一有效版本")

        with Database(db_path=db_path) as db:
            history = db.get_strategy_directive_history()
        assert_true(len(history) == 1, "只保存通过结构校验的AI策略版本")
        print("AI 日终策略指令测试通过")
    finally:
        for path in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    main()
