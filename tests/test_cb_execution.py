"""可转债T+0订单必须真正进入执行层的回归测试。"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from execution.broker import PaperBrokerAdapter
from scheduler.pipeline import execute_trade_plan


def main():
    with tempfile.TemporaryDirectory(prefix="quant_cb_") as d:
        account_path = os.path.join(d, "paper.json")
        db_path = os.path.join(d, "quant.db")
        broker = PaperBrokerAdapter(account_file=account_path, db_path=db_path)

        def quote(codes):
            return [SimpleNamespace(code=codes[0], price=100.0, close_prev=99.0)]

        plan = {
            "date": "2026-08-08",
            "deadline": "23:59",
            "regime": "sideways",
            "orders": [{
                "code": "113000", "name": "测试转债", "action": "BUY",
                "priority": 1, "target_weight": 0.10, "max_price": 105,
                "reason": "CB测试信号", "score": 88, "conviction": 0.88,
                "allow_t0": True, "trade_unit": 10,
                "market_regime": "sideways",
                "signal_detail": "premium=2%",
                "dimensions": {"convertible_bond": {"premium": 98}},
            }],
        }
        result = execute_trade_plan(plan, broker=broker, realtime_func=quote)
        assert not result.errors, result.errors
        assert broker.get_positions()["113000"]["shares"] % 10 == 0
        assert broker.get_sellable_shares("113000", trade_date="2026-08-08") > 0

        with Database(db_path=db_path) as db:
            row = db.conn.execute(
                "SELECT signal_score, market_regime, signal_detail, dimensions FROM trades WHERE code='113000' AND action='BUY'"
            ).fetchone()
            assert row and row[0] == 88 and row[1] == "sideways"
            assert "premium" in (row[2] or "") and "convertible_bond" in (row[3] or "")
    print("可转债T+0执行与成交上下文测试通过")


if __name__ == "__main__":
    main()
