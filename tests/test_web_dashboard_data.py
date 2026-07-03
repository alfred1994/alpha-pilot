#!/usr/bin/env python3
"""仪表盘数据展示契约测试。"""
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
import web.routers.database as database_router


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def test_decisions_hide_no_response_and_include_name():
    db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
    db_path = db_file.name
    db_file.close()
    os.unlink(db_path)

    old_get_db = database_router._get_db
    database_router._get_db = lambda: Database(db_path=db_path)
    try:
        with Database(db_path=db_path) as db:
            db.upsert_position({
                "code": "600519",
                "name": "贵州茅台",
                "shares": 100,
                "buy_price": 10,
                "buy_date": "2026-07-01",
                "cost": 1000,
                "highest_price": 10,
                "current_price": 10,
            })
            db.insert_llm_decision({
                "code": "000001",
                "date": "2026-07-01",
                "action": "HOLD",
                "llm_response": "",
                "reasoning": "LLM无响应",
                "confidence": 0,
            })
            db.insert_llm_decision({
                "code": "600519",
                "date": "2026-07-01",
                "action": "BUY",
                "llm_response": '{"action":"BUY"}',
                "reasoning": "趋势和资金信号共振",
                "confidence": 0.82,
            })

        data = database_router.get_decisions(limit=10)
        decisions = data["decisions"]
        assert_true(data["success"], "决策接口返回成功")
        assert_true(len(decisions) == 1, "过滤LLM无响应噪声记录")
        assert_true(decisions[0]["code"] == "600519", "保留有效LLM决策")
        assert_true(decisions[0]["name"] == "贵州茅台", "决策记录补齐股票名称")
    finally:
        database_router._get_db = old_get_db
        for candidate in [db_path, f"{db_path}-wal", f"{db_path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)


def test_decisions_resolve_name_from_llm_prompt():
    db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
    db_path = db_file.name
    db_file.close()
    os.unlink(db_path)

    old_get_db = database_router._get_db
    database_router._get_db = lambda: Database(db_path=db_path)
    try:
        with Database(db_path=db_path) as db:
            db.insert_llm_decision({
                "code": "601012",
                "date": "2026-07-01",
                "action": "SELL",
                "llm_prompt": "代码: 601012  名称: 隆基绿能\n其他字段",
                "llm_response": '{"action":"SELL"}',
                "reasoning": "光伏基本面偏弱，震荡市不追高",
                "confidence": 0.3,
            })

        data = database_router.get_decisions(limit=10)
        decisions = data["decisions"]
        assert_true(data["success"], "决策接口返回成功")
        assert_true(decisions[0]["name"] == "隆基绿能", "从LLM prompt补齐股票名称")
    finally:
        database_router._get_db = old_get_db
        for candidate in [db_path, f"{db_path}-wal", f"{db_path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)


def test_trades_replace_generic_tradeplan_reason():
    db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
    db_path = db_file.name
    db_file.close()
    os.unlink(db_path)

    old_get_db = database_router._get_db
    database_router._get_db = lambda: Database(db_path=db_path)
    try:
        with Database(db_path=db_path) as db:
            db.insert_trade({
                "code": "600519",
                "name": "贵州茅台",
                "action": "BUY",
                "price": 10,
                "shares": 100,
                "amount": 1000,
                "commission": 5,
                "reason": "TradePlan执行",
                "created_at": "2026-07-01T10:00:00",
            })
            db.insert_llm_decision({
                "code": "600519",
                "date": "2026-07-01",
                "action": "BUY",
                "llm_response": '{"action":"BUY"}',
                "reasoning": "趋势向上且资金净流入，适合小仓试错",
                "confidence": 0.8,
            })

        data = database_router.get_trades(limit=10, page=1)
        trades = data["trades"]
        assert_true(data["success"], "交易接口返回成功")
        assert_true(len(trades) == 1, "读取到测试交易")
        assert_true("趋势向上" in trades[0]["reason"], "泛化TradePlan理由替换为具体决策理由")
    finally:
        database_router._get_db = old_get_db
        for candidate in [db_path, f"{db_path}-wal", f"{db_path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)


def test_performance_appends_today_when_review_is_stale():
    data = database_router.get_performance(days=10)
    today = datetime.now().strftime("%Y-%m-%d")

    assert_true(data["success"], "业绩接口返回成功")
    assert_true(data["performance"], "业绩接口返回记录")
    assert_true(data["performance"][-1]["date"] == today, "缺少今日复盘时追加今日账户记录")
    assert_true("daily_pnl" in data["performance"][-1], "今日记录包含daily_pnl")


if __name__ == "__main__":
    test_decisions_hide_no_response_and_include_name()
    test_decisions_resolve_name_from_llm_prompt()
    test_trades_replace_generic_tradeplan_reason()
    test_performance_appends_today_when_review_is_stale()
    print("仪表盘数据展示契约测试通过")
