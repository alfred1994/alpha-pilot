#!/usr/bin/env python3
"""
盘中端到端模拟回放测试

用固定 TradePlan、固定行情、临时模拟账户执行一次 BUY，
验证执行链路能把计划转成模拟成交，并同步写入 SQLite。
"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from execution.broker import PaperBrokerAdapter
from risk.drawdown import DrawdownController
from risk.system_risk import SystemRiskController
from scheduler.pipeline import execute_trade_plan


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    temp_paths = []
    try:
        account_file = tempfile.NamedTemporaryFile(suffix="_paper_account.json", delete=False)
        account_path = account_file.name
        account_file.close()
        os.unlink(account_path)
        temp_paths.append(account_path)

        db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
        db_path = db_file.name
        db_file.close()
        os.unlink(db_path)
        temp_paths.append(db_path)

        drawdown_file = tempfile.NamedTemporaryFile(suffix="_drawdown.json", delete=False)
        drawdown_path = drawdown_file.name
        drawdown_file.close()
        os.unlink(drawdown_path)
        temp_paths.append(drawdown_path)

        system_risk_file = tempfile.NamedTemporaryFile(suffix="_system_risk.json", delete=False)
        system_risk_path = system_risk_file.name
        system_risk_file.close()
        os.unlink(system_risk_path)
        temp_paths.append(system_risk_path)

        broker = PaperBrokerAdapter(account_file=account_path, db_path=db_path)

        plan = {
            "date": "2026-06-09",
            "regime": "sideways",
            "regime_confidence": 0.6,
            "orders": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "action": "BUY",
                    "priority": 1,
                    "target_weight": 0.10,
                    "max_price": 11.0,
                    "reason": "端到端回放买入",
                    "score": 80,
                    "conviction": 0.8,
                }
            ],
        }

        def fake_realtime(codes):
            return [SimpleNamespace(price=10.0, close_prev=9.8) for _ in codes]

        result = execute_trade_plan(
            plan,
            broker=broker,
            realtime_func=fake_realtime,
            market_status="盘中",
            drawdown_controller=DrawdownController(state_file=drawdown_path),
            system_risk_controller=SystemRiskController(state_file=system_risk_path),
            update_memory=False,
        )

        assert_true(not result.errors, f"执行无错误: {result.errors}")
        assert_true(len(result.executed_orders) == 1, "TradePlan生成1笔模拟成交")
        assert_true(result.executed_orders[0]["status"] == "filled", "订单状态为filled")
        assert_true(len(result.order_audit) == 1, "TradePlan生成1条订单审计")
        assert_true(result.order_audit[0]["status"] == "filled", "订单审计记录成交状态")

        pos = broker.get_positions().get("600519")
        assert_true(pos is not None, "模拟账户生成持仓")
        assert_true(pos["shares"] > 0, f"持仓股数大于0: {pos['shares']}")
        assert_true(broker.get_cash() < 1_000_000, "模拟账户现金已扣减")

        with Database(db_path=db_path) as db:
            trades = db.get_trades(code="600519", limit=5)
            account_state = db.get_account_state()
        assert_true(len(trades) == 1 and trades[0]["action"] == "BUY", "SQLite写入BUY交易")
        assert_true(trades[0]["reason"] == "端到端回放买入", "BUY交易写入TradePlan具体理由")
        assert_true(
            account_state and "600519" in account_state["positions"],
            "SQLite账户状态同步持仓",
        )

        blocked_plan = {
            "date": "2026-06-09",
            "regime": "sideways",
            "orders": [
                {
                    "code": "000001",
                    "name": "平安银行",
                    "action": "BUY",
                    "priority": 1,
                    "target_weight": 0.10,
                    "max_price": 9.0,
                    "reason": "端到端回放限价阻断",
                    "score": 70,
                    "conviction": 0.7,
                }
            ],
        }
        blocked = execute_trade_plan(
            blocked_plan,
            broker=broker,
            realtime_func=fake_realtime,
            market_status="盘中",
            drawdown_controller=DrawdownController(state_file=drawdown_path),
            system_risk_controller=SystemRiskController(state_file=system_risk_path),
            update_memory=False,
        )
        assert_true(blocked.order_audit[0]["status"] == "blocked", "订单审计记录阻断状态")
        assert_true(blocked.order_audit[0]["reason"] == "超过硬限价", "订单审计记录阻断原因")

        print("盘中端到端模拟回放测试通过")

    finally:
        for path in temp_paths:
            for candidate in [path, f"{path}-wal", f"{path}-shm"]:
                if os.path.exists(candidate):
                    os.unlink(candidate)


if __name__ == "__main__":
    main()
