#!/usr/bin/env python3
"""
分层记忆沉淀测试

验证复盘教训、自动盯盘事件、已验证LLM决策和prompt进化建议
会沉淀为短期/中期/长期记忆。
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.memory import TradeMemory


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db_path = tmp_db.name
    tmp_db.close()
    os.unlink(tmp_db_path)

    target_date = "2026-07-03"
    expired_at = (datetime.now() - timedelta(days=1)).isoformat()

    try:
        with Database(db_path=tmp_db_path) as db:
            db.insert_lesson({
                "date": target_date,
                "category": "buy",
                "content": "600519放量长上影后不要追高，先等待回踩确认。",
                "importance": 5,
                "market_regime": "sideways",
                "related_trades": '["600519"]',
            })
            db.insert_auto_event({
                "date": target_date,
                "event_type": "order_audit",
                "status": "blocked",
                "actions": ["贵州茅台 BUY 被硬限价阻断"],
                "details": {},
                "error": "",
            })
            for idx, pct in enumerate([-2.5, -3.0, -1.8], start=1):
                db.insert_llm_decision({
                    "code": "600519",
                    "date": target_date,
                    "action": "BUY",
                    "llm_prompt": "test",
                    "llm_response": "{}",
                    "reasoning": f"第{idx}次追高买入",
                    "confidence": 0.7,
                    "outcome": "lose",
                    "outcome_pct": pct,
                    "created_at": f"{target_date}T10:0{idx}:00",
                })
            db.conn.execute("""
                CREATE TABLE IF NOT EXISTS active_prompt_hints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hint TEXT NOT NULL,
                    source TEXT DEFAULT 'evolution',
                    active INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL
                )
            """)
            db.conn.execute(
                "INSERT INTO active_prompt_hints (hint, source, active, created_at) VALUES (?, 'test', 1, ?)",
                ("遇到高位放量长上影时，必须降低买入置信度。", f"{target_date}T15:40:00"),
            )
            db.insert_memory_item({
                "layer": "short",
                "scope": "global",
                "key": "",
                "category": "execution",
                "content": "已过期短期记忆",
                "evidence": {},
                "score": 50,
                "source": "test",
                "expires_at": expired_at,
            })

        with TradeMemory(db_path=tmp_db_path) as memory:
            report = memory.consolidate_layers(date=target_date, lookback_days=10)
            assert_true(report["expired"] >= 1, "过期短期记忆会失效")
            assert_true(report["short"] >= 2, "当天教训和自动事件沉淀为短期记忆")
            assert_true(report["medium"] >= 1, "已验证决策沉淀为中期模式")
            assert_true(report["long"] >= 2, "高重要度教训和稳定亏损模式沉淀为长期记忆")

            context = memory.recall(stock_code="600519", regime="sideways")
            assert_true("【短期记忆】" in context, "召回包含短期记忆")
            assert_true("【中期记忆】" in context, "召回包含中期记忆")
            assert_true("【长期记忆】" in context, "召回包含长期记忆")
            assert_true("胜率仅0%" in context, "亏损模式进入记忆上下文")
            assert_true("放量长上影" in context, "复盘教训进入记忆上下文")

        with Database(db_path=tmp_db_path) as db:
            expired = db.get_memory_items(active=False, limit=10)
            stats = db.get_memory_stats()
            assert_true(any(item["content"] == "已过期短期记忆" for item in expired), "可查询已失效记忆")
            assert_true(stats["layers"].get("long", 0) >= 2, "长期记忆统计正确")

        print("分层记忆沉淀测试通过")

    finally:
        for candidate in [tmp_db_path, f"{tmp_db_path}-wal", f"{tmp_db_path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)


if __name__ == "__main__":
    main()
