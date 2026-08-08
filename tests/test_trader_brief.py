#!/usr/bin/env python3
"""AI 交易员每日事实和能力状态契约测试。"""
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from scheduler.trader_brief import build_daily_facts
from strategy.decision import DimensionScore
from strategy.llm_trader import _build_decision_prompt


def ok(message):
    print(f"  OK {message}")


def _directive(version, review_date, effective_date, min_score, intent):
    return {
        "version": version,
        "review_date": review_date,
        "effective_date": effective_date,
        "created_at": f"{review_date}T15:05:00",
        "regime": "sideways",
        "intent": intent,
        "summary": "根据决策漏斗调整观察范围",
        "diagnosis": "候选存在，但交易信号不足",
        "rationale": "依据当日扫描和LLM判断事实",
        "hypothesis": "扩大观察后仍无信号则说明市场机会不足",
        "evaluation": {
            "previous_version": "",
            "verdict": "inconclusive",
            "evidence": "尚无次日事实",
        },
        "params": {"top_k": 3, "min_score": min_score, "max_weight": 0.1},
    }


def main():
    handle = tempfile.NamedTemporaryFile(suffix="_trader_brief.db", delete=False)
    db_path = handle.name
    handle.close()
    os.unlink(db_path)
    try:
        with Database(db_path=db_path) as db:
            current = _directive("directive-current", "2026-07-30", "2026-07-31", 60, "平衡巡航")
            pending = _directive("directive-next", "2026-07-31", "2026-08-03", 58, "谨慎探索")
            db.save_strategy_directive(current)
            db.save_strategy_directive(pending)
            db.insert_auto_event({
                "date": "2026-07-31",
                "event_type": "auto_cycle",
                "status": "盘中",
                "actions": ["盘中扫描完成"],
                "details": {
                    "scan_journey": {
                        "candidate_count": 8,
                        "scored_count": 6,
                        "llm_evaluated": 2,
                        "observations": 1,
                        "buy_signals": 1,
                        "sell_signals": 0,
                        "planned_orders": 1,
                    },
                    "order_audit": [{
                        "code": "600519",
                        "name": "贵州茅台",
                        "action": "BUY",
                        "status": "blocked",
                        "reason": "超过硬限价",
                    }],
                },
                "created_at": "2026-07-31T10:00:00",
            })
            for code, action in (("600519", "BUY"), ("000001", "HOLD")):
                db.insert_llm_decision({
                    "code": code,
                    "date": "2026-07-31",
                    "action": action,
                    "llm_response": json.dumps({"action": action}),
                    "reasoning": "依据完整策略进行判断",
                    "confidence": 0.7,
                })

        facts = build_daily_facts(
            date="2026-07-31",
            db_path=db_path,
            now=datetime(2026, 7, 31, 10, 5),
            market_status="盘中",
            trading_day=True,
        )
        assert facts["funnel"]["candidates"] == 8
        assert facts["funnel"]["llm_evaluated"] == 2
        assert facts["funnel"]["buy_signals"] == 1
        assert facts["funnel"]["observations"] == 1
        assert facts["funnel"]["blocked"] == 1
        assert facts["state"] == "signal_pending"
        assert facts["strategy"]["diff"][0]["key"] == "intent"
        assert any(item["key"] == "min_score" for item in facts["strategy"]["diff"])
        ok("每日事实统一聚合候选、判断、信号、计划和执行结果")

        prompt = _build_decision_prompt(
            "600519",
            "贵州茅台",
            {"technical": DimensionScore("technical", 70, 0.8, "趋势向上")},
            strategy_directive=current,
        )
        assert "directive-current" in prompt
        assert "扩大观察后仍无信号" in prompt
        assert "当综合分>=60" not in prompt
        ok("个股 LLM 收到完整生效策略且不再使用固定买入阈值提示")

        with Database(db_path=db_path) as db:
            replacement = dict(pending)
            replacement["version"] = "directive-next-revised"
            replacement["params"] = {"top_k": 3, "min_score": 57, "max_weight": 0.1}
            db.save_strategy_directive(replacement)
            same_day = [
                item for item in db.get_strategy_directive_history()
                if item.get("review_date") == "2026-07-31"
            ]
        assert len(same_day) == 1
        assert same_day[0]["version"] == "directive-next-revised"
        ok("同一复盘日重复运行只保留一个权威策略版本")

        # 日终事实不能被最近500条事件截断；模拟501个扫描事件，
        # 验证候选与扫描周期仍能完整聚合。
        with Database(db_path=db_path) as db:
            for index in range(501):
                db.insert_auto_event({
                    "date": "2026-08-01",
                    "event_type": "auto_cycle",
                    "status": "盘中",
                    "details": {
                        "scan_journey": {
                            "candidate_count": 1,
                            "scored_count": 1,
                        },
                    },
                    "created_at": f"2026-08-01T09:{index // 60:02d}:{index % 60:02d}",
                })
        complete_facts = build_daily_facts(
            date="2026-08-01",
            db_path=db_path,
            now=datetime(2026, 8, 1, 15, 10),
            market_status="盘后",
            trading_day=True,
        )
        assert complete_facts["funnel"]["scan_cycles"] == 501
        assert complete_facts["funnel"]["candidates"] == 501
        assert complete_facts["funnel"]["scored"] == 501
        ok("日终事实按日期完整读取事件，不受500条窗口影响")
    finally:
        for candidate in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
            if os.path.exists(candidate):
                os.unlink(candidate)


if __name__ == "__main__":
    main()
