#!/usr/bin/env python3
"""
分层记忆检索测试

验证短期/中期/长期记忆会按固定层级注入决策上下文。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

    try:
        with TradeMemory(db_path=tmp_db_path) as memory:
            memory.save_memory_item(
                layer="short",
                scope="global",
                key="",
                category="execution",
                content="今日自动盯盘发现冲高回落，买入需等待二次确认。",
                evidence={"source": "test"},
                score=70,
            )
            memory.save_memory_item(
                layer="medium",
                scope="stock",
                key="600519",
                category="risk",
                content="近3次600519 BUY决策胜率仅33%，追高信号需要降权。",
                evidence={"decision_ids": [1, 2, 3]},
                score=82,
            )
            memory.save_memory_item(
                layer="long",
                scope="regime",
                key="sideways",
                category="entry",
                content="震荡市中高分候选要优先确认量能延续，再考虑买入。",
                evidence={"lesson_ids": [1]},
                score=88,
            )

            context = memory.recall_layered(
                stock_code="600519",
                regime="sideways",
                max_chars=1000,
            )
            assert_true("【短期记忆】" in context, "包含短期记忆")
            assert_true("【中期记忆】" in context, "包含中期记忆")
            assert_true("【长期记忆】" in context, "包含长期记忆")
            assert_true(
                context.index("【短期记忆】") < context.index("【中期记忆】") < context.index("【长期记忆】"),
                "记忆层级顺序固定",
            )
            assert_true("600519 BUY决策胜率仅33%" in context, "股票记忆按代码召回")
            assert_true("震荡市中高分候选" in context, "市场环境记忆按regime召回")

            clipped = memory.recall_layered(
                stock_code="600519",
                regime="sideways",
                max_chars=80,
            )
            assert_true("记忆已截断" in clipped, "超长记忆会被截断")

        print("分层记忆检索测试通过")

    finally:
        for candidate in [tmp_db_path, f"{tmp_db_path}-wal", f"{tmp_db_path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)


if __name__ == "__main__":
    main()
