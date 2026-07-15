#!/usr/bin/env python3
"""模拟账户输入边界和账本原子性回归测试。"""
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from execution.paper_account import PaperAccount


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def temp_path(suffix):
    item = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    path = item.name
    item.close()
    os.unlink(path)
    return path


def cleanup(*paths):
    for path in paths:
        for candidate in (path, f"{path}-wal", f"{path}-shm"):
            if os.path.exists(candidate):
                os.unlink(candidate)


def main():
    account_path = temp_path("_paper_account.json")
    db_path = temp_path("_quant.db")
    try:
        account = PaperAccount(filepath=account_path, db_path=db_path)
        initial_cash = account.cash
        invalid_prices = [0, -1, math.nan, math.inf]
        for price in invalid_prices:
            assert_true(account.buy("600519", "贵州茅台", price, shares=100) is None, f"拒绝非法买入价格 {price!r}")
        assert_true(account.buy("600519", "贵州茅台", 10, shares=-100) is None, "拒绝负数买入股数")
        assert_true(account.buy("600519", "贵州茅台", 10, shares=50) is None, "拒绝非整手买入")
        assert_true(account.cash == initial_cash and not account.positions, "非法买入不改变账户状态")

        buy = account.buy("600519", "贵州茅台", 10, shares=100)
        assert_true(buy is not None, "合法整手买入成功")
        cash_after_buy = account.cash
        shares_after_buy = account.positions["600519"]["shares"]

        assert_true(account.sell("600519", 10, shares=-100) is None, "拒绝负数卖出股数")
        assert_true(account.sell("600519", 0, shares=100) is None, "拒绝零价格卖出")
        assert_true(account.sell("600519", 10, shares=50) is None, "拒绝非整手部分卖出")
        assert_true(account.cash == cash_after_buy, "非法卖出不改变现金")
        assert_true(account.positions["600519"]["shares"] == shares_after_buy, "非法卖出不改变持仓")

        with Database(db_path=db_path) as db:
            state = db.get_account_state()
            positions = db.get_all_positions()
            trades = db.get_trades(code="600519")
        assert_true(state["cash"] == cash_after_buy, "账户快照与内存现金一致")
        assert_true(len(positions) == 1 and positions[0]["shares"] == 100, "持仓投影与账户快照同时提交")
        assert_true(len(trades) == 1 and trades[0]["action"] == "BUY", "成交记录与账户变更同时提交")
    finally:
        cleanup(account_path, db_path)


if __name__ == "__main__":
    main()
