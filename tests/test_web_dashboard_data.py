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


def test_trades_backfill_missing_sell_pnl_from_prior_buy():
    db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
    db_path = db_file.name
    db_file.close()
    os.unlink(db_path)

    old_get_db = database_router._get_db
    database_router._get_db = lambda: Database(db_path=db_path)
    try:
        with Database(db_path=db_path) as db:
            db.insert_trade({
                "code": "600228",
                "name": "返利科技",
                "action": "BUY",
                "price": 10.79,
                "shares": 3000,
                "amount": 32370,
                "commission": 9.711,
                "reason": "测试买入",
                "pnl": 0.0,
                "pnl_pct": 0.0,
                "created_at": "2026-06-26T10:00:00",
            })
            db.insert_trade({
                "code": "600228",
                "name": "返利科技",
                "action": "SELL",
                "price": 11.87,
                "shares": 3000,
                "amount": 35610,
                "commission": 10.683,
                "reason": "测试卖出",
                "created_at": "2026-06-29T09:30:03",
            })

        data = database_router.get_trades(limit=10, page=1)
        sell_trade = data["trades"][0]
        assert_true(data["success"], "交易接口返回成功")
        assert_true(sell_trade["action"] == "SELL", "读取到缺失盈亏的卖出交易")
        assert_true(sell_trade["pnl"] is not None and sell_trade["pnl"] > 3000, "卖出盈亏按买入价回填")
        assert_true(abs(sell_trade["pnl_pct"] - ((11.87 - 10.79) / 10.79)) < 0.000001, "卖出盈亏比按买入价回填")

        with Database(db_path=db_path) as db:
            stored = db.get_trades(code="600228", limit=1)[0]
        assert_true(stored["pnl"] is not None and stored["pnl_pct"] is not None, "回填结果写回SQLite")
    finally:
        database_router._get_db = old_get_db
        for candidate in [db_path, f"{db_path}-wal", f"{db_path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)


def test_trades_keep_unknown_sell_pnl_null_without_prior_buy():
    db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
    db_path = db_file.name
    db_file.close()
    os.unlink(db_path)

    old_get_db = database_router._get_db
    database_router._get_db = lambda: Database(db_path=db_path)
    try:
        with Database(db_path=db_path) as db:
            db.insert_trade({
                "code": "000001",
                "name": "平安银行",
                "action": "SELL",
                "price": 12.0,
                "shares": 100,
                "amount": 1200,
                "commission": 5,
                "reason": "测试卖出",
                "created_at": "2026-07-01T10:00:00",
            })

        data = database_router.get_trades(limit=10, page=1)
        trade = data["trades"][0]
        assert_true(data["success"], "交易接口返回成功")
        assert_true(trade["pnl"] is None, "无法匹配买入时保留未知盈亏")
        assert_true(trade["pnl_pct"] is None, "无法匹配买入时保留未知盈亏比")
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


def test_performance_daily_pnl_matches_adjacent_assets():
    rows = [
        {"date": "2026-07-01", "total_assets": 1000.0, "daily_pnl": -999.0},
        {"date": "2026-07-02", "total_assets": 1030.0, "daily_pnl": -999.0},
        {"date": "2026-07-03", "total_assets": 1010.0, "daily_pnl": 999.0},
    ]

    normalized = database_router._normalize_performance_daily_pnl(rows)

    assert_true(normalized[0]["daily_pnl"] == -999.0, "首条业绩记录保留原始日盈亏")
    assert_true(normalized[1]["daily_pnl"] == 30.0, "日盈亏按相邻总资产增加额校准")
    assert_true(normalized[2]["daily_pnl"] == -20.0, "日盈亏按相邻总资产减少额校准")


def test_dashboard_frontend_separates_decision_confidence_and_unknown_pnl():
    js_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web", "static", "js", "modules", "dashboard.js"
    )
    with open(js_path, "r", encoding="utf-8") as f:
        source = f.read()

    assert_true("${this.actionText(action)}${confidence}" not in source, "持仓卡片不再把持有和置信度拼成一个标签")
    assert_true("决策置信度" in source, "持仓卡片单独展示决策置信度")
    assert_true("未知盈亏" in source, "交易记录对缺失盈亏显示未知而不是0")
    assert_true("Number(t.pnl || 0)" not in source, "前端统计不再把空盈亏当作0")


if __name__ == "__main__":
    test_decisions_hide_no_response_and_include_name()
    test_decisions_resolve_name_from_llm_prompt()
    test_trades_replace_generic_tradeplan_reason()
    test_trades_backfill_missing_sell_pnl_from_prior_buy()
    test_trades_keep_unknown_sell_pnl_null_without_prior_buy()
    test_performance_appends_today_when_review_is_stale()
    test_performance_daily_pnl_matches_adjacent_assets()
    test_dashboard_frontend_separates_decision_confidence_and_unknown_pnl()
    print("仪表盘数据展示契约测试通过")
