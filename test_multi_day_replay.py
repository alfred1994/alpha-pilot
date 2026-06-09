#!/usr/bin/env python3
"""
多日模拟回放测试

验证买入 -> 卖出 -> 每日复盘 -> 教训沉淀 -> 记忆检索的连续闭环。
所有账户、数据库、复盘文件均使用临时路径，不污染真实模拟盘。
"""
import os
import sys
import tempfile
from dataclasses import asdict
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.database import Database
from execution.broker import PaperBrokerAdapter
from review.daily_review import DailyReviewer
from review.llm_review import extract_and_save_lessons
from risk.drawdown import DrawdownController
from risk.system_risk import SystemRiskController
from scheduler.pipeline import execute_trade_plan
from strategy.memory import TradeMemory


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _temp_file(suffix: str) -> str:
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    path = f.name
    f.close()
    os.unlink(path)
    return path


def _cleanup(paths):
    for path in paths:
        for candidate in [path, f"{path}-wal", f"{path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)


def main():
    temp_paths = []
    review_dir = tempfile.mkdtemp(prefix="quant_review_")
    try:
        account_path = _temp_file("_paper_account.json")
        db_path = _temp_file("_quant.db")
        drawdown_path = _temp_file("_drawdown.json")
        system_risk_path = _temp_file("_system_risk.json")
        temp_paths.extend([account_path, db_path, drawdown_path, system_risk_path])

        broker = PaperBrokerAdapter(account_file=account_path, db_path=db_path)

        def fake_realtime_buy(codes):
            return [SimpleNamespace(price=10.0, close_prev=9.8) for _ in codes]

        buy_plan = {
            "date": "2026-06-09",
            "orders": [{
                "code": "600519",
                "name": "贵州茅台",
                "action": "BUY",
                "priority": 1,
                "target_weight": 0.10,
                "max_price": 11.0,
                "reason": "多日回放买入",
                "score": 80,
                "conviction": 0.8,
            }],
        }

        buy_result = execute_trade_plan(
            buy_plan,
            broker=broker,
            realtime_func=fake_realtime_buy,
            market_status="盘中",
            drawdown_controller=DrawdownController(state_file=drawdown_path),
            system_risk_controller=SystemRiskController(state_file=system_risk_path),
            update_memory=False,
        )
        assert_true(not buy_result.errors, f"买入执行无错误: {buy_result.errors}")
        assert_true(broker.has_position("600519"), "第一日买入后生成持仓")

        def fake_realtime_sell(codes):
            return [SimpleNamespace(price=10.8, close_prev=10.0) for _ in codes]

        sell_plan = {
            "date": "2026-06-10",
            "orders": [{
                "code": "600519",
                "name": "贵州茅台",
                "action": "SELL",
                "priority": 1,
                "target_weight": 0,
                "max_price": 0,
                "reason": "多日回放止盈卖出",
                "score": 70,
                "conviction": 0.7,
            }],
        }

        sell_result = execute_trade_plan(
            sell_plan,
            broker=broker,
            realtime_func=fake_realtime_sell,
            market_status="盘中",
            drawdown_controller=DrawdownController(state_file=drawdown_path),
            system_risk_controller=SystemRiskController(state_file=system_risk_path),
            update_memory=False,
        )
        assert_true(not sell_result.errors, f"卖出执行无错误: {sell_result.errors}")
        assert_true(not broker.has_position("600519"), "第二日卖出后清仓")
        assert_true(len(sell_result.executed_orders) == 1, "卖出产生1笔成交")
        sell_trade = sell_result.executed_orders[0]
        assert_true(sell_trade.get("profit_pct", 0) > 5, "卖出交易产生明确盈利")

        reviewer = DailyReviewer(review_dir=review_dir, db_path=db_path)
        review_result = reviewer.run_review(
            date="2026-06-10",
            positions=broker.get_positions(),
            trades=[sell_trade],
            prices={},
            prev_prices={},
            cash=broker.get_cash(),
            initial_capital=1_000_000,
        )
        review_data = asdict(review_result)
        assert_true(review_result.win_trades == 1, "复盘识别1笔盈利卖出")

        lesson_count = extract_and_save_lessons(
            review_data,
            llm_analysis="多日回放复盘: 低位买入后按计划止盈，继续记录该模式。",
            db_path=db_path,
        )
        assert_true(lesson_count >= 1, f"复盘提取教训: {lesson_count}条")

        with TradeMemory(db_path=db_path) as memory:
            context = memory.recall(stock_code="600519", regime="sideways")
        assert_true("盈利卖出" in context or "LLM复盘摘要" in context, "交易记忆可检索复盘教训")

        with Database(db_path=db_path) as db:
            trades = db.get_trades(code="600519", limit=10)
            lessons = db.get_lessons(limit=10)
            review_snapshot = db.conn.execute(
                "SELECT date FROM review_snapshots WHERE date = ?",
                ("2026-06-10",),
            ).fetchone()
        assert_true(len(trades) == 2, "SQLite记录买入和卖出两笔交易")
        assert_true(len(lessons) >= 1, "SQLite教训库已沉淀复盘教训")
        assert_true(review_snapshot is not None, "SQLite复盘快照已写入")

        print("多日模拟回放闭环测试通过")

    finally:
        _cleanup(temp_paths)
        if os.path.isdir(review_dir):
            for name in os.listdir(review_dir):
                os.unlink(os.path.join(review_dir, name))
            os.rmdir(review_dir)


if __name__ == "__main__":
    main()
