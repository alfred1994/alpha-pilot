#!/usr/bin/env python3
"""
交易记忆反哺测试

验证复盘沉淀的教训会进入下一次 LLM 决策 prompt，
这是“自我复盘纠正进化”的最小闭环。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy.decision import DimensionScore
from strategy.llm_trader import make_decision
from strategy.memory import TradeMemory


def main():
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db_path = tmp_db.name
    tmp_db.close()
    os.unlink(tmp_db_path)

    try:
        with TradeMemory(db_path=tmp_db_path) as memory:
            lesson_text = "600519历史教训: 放量长上影后不要追高，先等待回踩确认。"
            lesson_id = memory.save_lesson(
                category="buy",
                content=lesson_text,
                importance=5,
                market_regime="sideways",
                related_trades=["600519"],
            )
            if lesson_id <= 0:
                raise AssertionError("教训保存失败")

            dims = {
                "technical": DimensionScore("technical", 55, 0.7, "震荡整理"),
                "capital": DimensionScore("capital", 52, 0.5, "资金中性"),
                "sentiment": DimensionScore("sentiment", 50, 0.5, "舆情中性"),
                "emotion": DimensionScore("emotion", 48, 0.5, "市场情绪一般"),
                "fundamental": DimensionScore("fundamental", 60, 0.5, "基本面稳定"),
            }

            decision = make_decision(
                code="600519",
                name="贵州茅台",
                dimensions=dims,
                regime="sideways",
                memory=memory,
            )
            if decision.code != "600519":
                raise AssertionError("决策对象异常")

            records = memory.recall_decisions(code="600519", limit=1)
            if not records:
                raise AssertionError("决策记录未保存")

            db = memory._get_db()
            row = db.conn.execute(
                "SELECT llm_prompt FROM llm_decisions WHERE code = ? ORDER BY id DESC LIMIT 1",
                ("600519",),
            ).fetchone()
            prompt = row["llm_prompt"] if row else ""
            if lesson_text not in prompt:
                raise AssertionError("历史教训没有注入LLM决策prompt")

        print("交易记忆反哺测试通过")

    finally:
        if os.path.exists(tmp_db_path):
            os.unlink(tmp_db_path)


if __name__ == "__main__":
    main()
