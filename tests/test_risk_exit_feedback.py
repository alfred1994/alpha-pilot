"""系统风控退出不得被记忆系统误判为选股失败。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.memory import TradeMemory


def main():
    with tempfile.TemporaryDirectory(prefix="quant_feedback_") as d:
        db_path = os.path.join(d, "quant.db")
        with Database(db_path=db_path) as db:
            buy_id = db.insert_trade({
                "code": "600519", "name": "贵州茅台", "action": "BUY",
                "price": 100, "shares": 100, "created_at": "2026-08-06T10:00:00",
            })
            decision_id = db.insert_llm_decision({
                "code": "600519", "date": "2026-08-06", "action": "BUY",
                "trade_id": buy_id, "created_at": "2026-08-06T10:00:01",
            })
            db.insert_trade({
                "code": "600519", "name": "贵州茅台", "action": "SELL",
                "price": 97, "shares": 100, "reason": "移动止损触发",
                "created_at": "2026-08-07T10:00:00",
            })
        with TradeMemory(db_path=db_path) as memory:
            memory.update_pending_decisions()
        with Database(db_path=db_path) as db:
            row = db.conn.execute("SELECT outcome, outcome_pct FROM llm_decisions WHERE id=?", (decision_id,)).fetchone()
            assert row["outcome"] == "risk_exit" and row["outcome_pct"] == -3.0
    print("风控退出反馈分层测试通过")


if __name__ == "__main__":
    main()
