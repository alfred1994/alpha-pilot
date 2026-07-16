#!/usr/bin/env python3
"""账户估值和每日盈亏口径一致性测试。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from execution.paper_account import PaperAccount
import execution.paper_account as paper_account_module
import data.realtime as realtime_module
from review.daily_review import DailyReviewer
from web.routers import status as status_router


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _temp_path(suffix):
    item = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    path = item.name
    item.close()
    os.unlink(path)
    return path


def _cleanup(paths):
    for path in paths:
        for candidate in [path, f"{path}-wal", f"{path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)


def test_account_uses_current_price_by_default():
    account_path = _temp_path("_paper_account.json")
    db_path = _temp_path("_quant.db")
    try:
        account = PaperAccount(filepath=account_path, db_path=db_path)
        account.cash = 900_000
        account.positions = {
            "600519": {
                "code": "600519",
                "name": "贵州茅台",
                "shares": 1000,
                "buy_price": 100.0,
                "current_price": 103.0,
                "cost": 100_000,
                "highest_price": 103.0,
            }
        }

        assert_true(account.market_value() == 103_000, "默认持仓市值使用current_price")
        assert_true(account.total_assets() == 1_003_000, "默认总资产使用current_price计价")
    finally:
        _cleanup([account_path, db_path])


def test_daily_pnl_prefers_previous_asset_snapshot():
    db_path = _temp_path("_quant.db")
    review_dir = tempfile.mkdtemp(prefix="review_pnl_")
    try:
        with Database(db_path=db_path) as db:
            db.insert_snapshot({
                "date": "2026-07-01",
                "cash": 900_000,
                "market_value": 100_000,
                "total_assets": 1_000_000,
                "position_count": 1,
            })

        reviewer = DailyReviewer(review_dir=review_dir, db_path=db_path)
        result = reviewer.run_review(
            date="2026-07-02",
            positions={
                "600519": {
                    "code": "600519",
                    "name": "贵州茅台",
                    "shares": 1000,
                    "buy_price": 100.0,
                    "buy_date": "2026-07-01",
                }
            },
            trades=[],
            prices={"600519": 101.0},
            prev_prices={"600519": 100.0},
            cash=900_000,
            initial_capital=1_000_000,
        )

        assert_true(result.total_assets == 1_001_000, "复盘总资产按现金加当前持仓市值计算")
        assert_true(result.daily_pnl == 1_000, "每日盈亏优先使用上一快照总资产差额")
        assert_true(abs(result.daily_pnl_pct - 0.001) < 0.000001, "每日盈亏率使用上一快照总资产作分母")
    finally:
        _cleanup([db_path])
        if os.path.isdir(review_dir):
            for name in os.listdir(review_dir):
                os.unlink(os.path.join(review_dir, name))
            os.rmdir(review_dir)


def test_daily_pnl_fallback_includes_realized_trade_pnl_and_buy_fee():
    db_path = _temp_path("_quant.db")
    review_dir = tempfile.mkdtemp(prefix="review_pnl_")
    try:
        reviewer = DailyReviewer(review_dir=review_dir, db_path=db_path)
        result = reviewer.run_review(
            date="2026-07-02",
            positions={
                "600519": {
                    "code": "600519",
                    "name": "贵州茅台",
                    "shares": 1000,
                    "buy_price": 100.0,
                    "buy_date": "2026-07-01",
                }
            },
            trades=[
                {"action": "SELL", "code": "000001", "name": "平安银行", "pnl": 500, "profit_pct": 5},
                {"action": "BUY", "code": "300750", "name": "宁德时代", "commission": 5},
            ],
            prices={"600519": 101.0},
            prev_prices={"600519": 100.0},
            cash=900_000,
            initial_capital=1_000_000,
        )

        assert_true(result.daily_pnl == 1_495, "无上一快照时兜底日盈亏包含浮盈、已实现盈亏和买入手续费")
    finally:
        _cleanup([db_path])
        if os.path.isdir(review_dir):
            for name in os.listdir(review_dir):
                os.unlink(os.path.join(review_dir, name))
            os.rmdir(review_dir)


def test_positions_api_is_read_only_for_realtime_valuation():
    db_path = _temp_path("_quant.db")
    try:
        with Database(db_path=db_path) as db:
            db.upsert_position({
                "code": "601058",
                "name": "赛轮轮胎",
                "shares": 5300,
                "buy_price": 11.39,
                "buy_date": "2026-07-01",
                "cost": 60367,
                "highest_price": 12.38,
                "current_price": 0,
            })
            db.save_account_state({
                "initial_capital": 1_000_000,
                "cash": 1_109_559.7338,
                "positions": {
                    "601058": {
                        "code": "601058",
                        "name": "赛轮轮胎",
                        "shares": 5300,
                        "buy_price": 11.39,
                        "buy_date": "2026-07-01",
                        "cost": 60367,
                        "highest_price": 12.38,
                        "current_price": 0,
                    }
                },
            })

        class FakeAccount:
            def __init__(self):
                self.db_path = db_path
                self.initial_capital = 1_000_000
                self.cash = 1_109_559.7338
                self.positions = {
                    "601058": {
                        "code": "601058",
                        "name": "赛轮轮胎",
                        "shares": 5300,
                        "buy_price": 11.39,
                        "buy_date": "2026-07-01",
                        "cost": 60367,
                        "highest_price": 12.38,
                        "current_price": 0,
                    }
                }

            def total_assets(self, prices=None):
                price = (prices or {}).get("601058", self.positions["601058"]["current_price"])
                return self.cash + price * self.positions["601058"]["shares"]

        original_account = paper_account_module.PaperAccount
        original_realtime = realtime_module.get_realtime
        paper_account_module.PaperAccount = FakeAccount
        realtime_module.get_realtime = lambda codes: [
            type("Quote", (), {"code": "601058", "price": 12.28, "name": "赛轮轮胎"})()
        ]
        try:
            result = status_router.get_detailed_positions()
        finally:
            paper_account_module.PaperAccount = original_account
            realtime_module.get_realtime = original_realtime

        with Database(db_path=db_path) as db:
            pos = db.get_position("601058")
            state = db.get_account_state()

        assert_true(result["success"], "持仓接口返回成功")
        assert_true(result["positions"][0]["current_price"] == 12.28, "响应使用实时估值")
        assert_true(pos["current_price"] == 0, "GET请求不回写持仓实时价格")
        assert_true(abs(state["total_assets"] - 1_169_926.7338) < 0.0001, "GET请求不回写账户总资产")
    finally:
        _cleanup([db_path])


if __name__ == "__main__":
    test_account_uses_current_price_by_default()
    test_daily_pnl_prefers_previous_asset_snapshot()
    test_daily_pnl_fallback_includes_realized_trade_pnl_and_buy_fee()
    test_positions_api_is_read_only_for_realtime_valuation()
    print("账户估值和每日盈亏一致性测试通过")
