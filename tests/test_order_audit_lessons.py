#!/usr/bin/env python3
"""
订单执行审计教训测试

验证盘中计划订单即使没有成交，只要被风控、价格、资金或执行异常阻断，
也能在盘后复盘中沉淀为交易记忆，反哺下一轮LLM决策。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from review.llm_review import extract_and_save_lessons
from data.database import Database
from scheduler.pipeline import _load_today_order_audit
from strategy.memory import TradeMemory


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    db_file = tempfile.NamedTemporaryFile(suffix="_audit_lessons.db", delete=False)
    db_path = db_file.name
    db_file.close()
    os.unlink(db_path)

    try:
        review_data = {
            "date": "2026-06-09",
            "trade_reviews": [],
            "position_pnls": [],
            "order_audit": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "action": "BUY",
                    "status": "blocked",
                    "reason": "超过硬限价",
                    "score": 78,
                    "target_weight": 0.1,
                },
                {
                    "code": "000001",
                    "name": "平安银行",
                    "action": "BUY",
                    "status": "failed",
                    "reason": "下单异常: 模拟账户写入失败",
                    "score": 66,
                    "target_weight": 0.08,
                },
            ],
        }

        with TradeMemory(db_path=db_path) as memory:
            saved = extract_and_save_lessons(review_data, memory=memory)
            lessons = memory.recall_lessons(limit=10)

        contents = [lesson.content for lesson in lessons]
        categories = [lesson.category for lesson in lessons]
        assert_true(saved == 2, "订单审计沉淀2条教训")
        assert_true(any("贵州茅台(600519)" in text and "超过硬限价" in text for text in contents), "限价阻断教训已保存")
        assert_true(any("平安银行(000001)" in text and "下单异常" in text for text in contents), "执行异常教训已保存")
        assert_true("entry" in categories, "价格类阻断归为entry")
        assert_true("execution" in categories, "失败类阻断归为execution")

        with Database(db_path=db_path) as db:
            db.insert_auto_event({
                "date": "2026-06-09",
                "event_type": "auto_cycle",
                "status": "盘中",
                "actions": ["执行审计: 计划2笔 阻断/跳过1笔 失败1笔"],
                "details": {"order_audit": review_data["order_audit"]},
                "created_at": "2026-06-09T10:00:00",
            })
        loaded = _load_today_order_audit("2026-06-09", db_path=db_path)
        assert_true(len(loaded) == 2, "盘后复盘可读取当天订单审计")

        print("订单执行审计教训测试通过")
    finally:
        for candidate in [db_path, f"{db_path}-wal", f"{db_path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)


if __name__ == "__main__":
    main()
