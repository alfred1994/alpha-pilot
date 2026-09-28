#!/usr/bin/env python3
"""风险状态与账户持久化的离线回归。"""
import json
import multiprocessing as mp
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
import pandas as pd
from data.quote_validation import BEIJING_TZ

from config import MAX_SINGLE_PCT, PAPER_SLIPPAGE_RATE
from data.database import AccountStateConflict, Database
from data.history import daily_history_usable
from execution.paper_account import PaperAccount
from risk.drawdown import DrawdownController
from risk.system_risk import SystemRiskController


def _open_database(db_path, ready):
    with Database(db_path=db_path) as db:
        ready.put(db.conn.execute("PRAGMA user_version").fetchone()[0])


def _update_system_state(path, day, assets, ready):
    SystemRiskController(state_file=path).update(assets, date=day)
    ready.put(day)


def _update_drawdown_state(path, assets, ready):
    DrawdownController(state_file=path).update(assets, date="2026-09-25")
    ready.put(assets)


def _run_pair(target, args_a, args_b):
    context = mp.get_context("spawn")
    ready = context.Queue()
    first = context.Process(target=target, args=(*args_a, ready))
    second = context.Process(target=target, args=(*args_b, ready))
    first.start()
    second.start()
    try:
        first.join(15)
        second.join(15)
        assert first.exitcode == 0 and second.exitcode == 0
        return ready.get(timeout=2), ready.get(timeout=2)
    finally:
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(2)
        ready.close()


def test_stale_account_cannot_overwrite_trade():
    with tempfile.TemporaryDirectory() as directory:
        account_path = os.path.join(directory, "account.json")
        db_path = os.path.join(directory, "account.db")
        first = PaperAccount(filepath=account_path, db_path=db_path)
        stale = PaperAccount(filepath=account_path, db_path=db_path)
        assert first.buy("600519", "测试", 10, shares=100,
                         execution_context="replay")
        assert stale.buy("000001", "过期账户", 10, shares=100,
                         execution_context="replay") is None
        with Database(db_path=db_path, readonly=True) as db:
            state = db.get_account_state()
            assert set(state["positions"]) == {"600519"}
            assert len(db.get_trades()) == 1


def test_paper_quote_and_slippage_limits():
    with tempfile.TemporaryDirectory() as directory:
        account = PaperAccount(
            filepath=os.path.join(directory, "account.json"),
            db_path=os.path.join(directory, "account.db"),
        )
        assert account.buy("600519", "测试", 10, shares=100) is None
        quote = {"code": "600519", "price": 10.0, "close_prev": 10.0,
                 "volume": 1000, "up_limit": 11.0, "down_limit": 9.0,
                 "timestamp": datetime.now(BEIJING_TZ).isoformat()}
        trade = account.buy("600519", "测试", 10, shares=30000, quote=quote)
        assert trade is not None
        assert abs(trade["price"] - 10 * (1 + PAPER_SLIPPAGE_RATE)) < 1e-8
        assert trade["amount"] <= account.initial_capital * MAX_SINGLE_PCT
        limit_quote = {**quote, "code": "000001", "price": 11.0,
                       "timestamp": datetime.now(BEIJING_TZ).isoformat()}
        assert account.buy("000001", "涨停", 11, shares=100,
                           quote=limit_quote, prices={"600519": 10}) is None


def test_bond_stop_sell_keeps_fresh_quote_path_without_board_price():
    with tempfile.TemporaryDirectory() as directory:
        account = PaperAccount(
            filepath=os.path.join(directory, "account.json"),
            db_path=os.path.join(directory, "account.db"),
        )
        assert account.buy("113000", "测试转债", 100, shares=100,
                           allow_t0=True, trade_unit=10,
                           execution_context="replay")
        quote = {"code": "113000", "price": 93.0, "volume": 1000,
                 "timestamp": datetime.now(BEIJING_TZ).isoformat()}
        trade = account.sell("113000", 93.0, shares=100, quote=quote)
        assert trade and trade["price"] < 93.0


def test_explicit_down_limit_blocks_simulated_sell():
    with tempfile.TemporaryDirectory() as directory:
        account = PaperAccount(
            filepath=os.path.join(directory, "account.json"),
            db_path=os.path.join(directory, "account.db"),
        )
        assert account.buy("600519", "测试股票", 10, shares=100,
                           trade_date="2026-09-24", execution_context="replay")
        quote = {"code": "600519", "price": 9.0, "volume": 1000,
                 "down_limit": 9.0, "timestamp": datetime.now(BEIJING_TZ).isoformat()}
        assert account.sell("600519", 9.0, shares=100, quote=quote) is None
        assert account.has_position("600519")


def test_corrupt_risk_state_blocks_loading():
    with tempfile.TemporaryDirectory() as directory:
        for controller, filename in (
            (SystemRiskController, "system.json"),
            (DrawdownController, "drawdown.json"),
        ):
            path = os.path.join(directory, filename)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{broken")
            try:
                controller(state_file=path)
            except RuntimeError:
                pass
            else:
                raise AssertionError("损坏的风险状态必须阻止控制器加载")


def test_drawdown_trading_day_expiry_and_recovery():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "drawdown.json")
        controller = DrawdownController(state_file=path, circuit_breaker_days=1)
        controller.update(1_000_000, date="2026-09-25")  # Friday
        triggered = controller.update(840_000, date="2026-09-25")
        assert triggered["is_circuit_breaker"]
        assert controller.state.circuit_breaker_until == "2026-09-29"  # Tuesday
        assert not controller.is_trading_allowed(today="2026-09-28")["allowed"]
        assert not controller.is_trading_allowed(today="2026-09-29")["allowed"]
        controller.update(900_000, date="2026-09-29")
        assert controller.is_trading_allowed(today="2026-09-29")["allowed"]


def test_readonly_account_does_not_initialize():
    with tempfile.TemporaryDirectory() as directory:
        db_path = os.path.join(directory, "absent.db")
        try:
            PaperAccount(filepath=os.path.join(directory, "account.json"),
                         db_path=db_path, read_only=True)
        except (FileNotFoundError, OSError):
            pass
        else:
            raise AssertionError("只读账户缺失时不得伪造默认资金")
        assert not os.path.exists(db_path)


def test_daily_cache_preserves_complete_row():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "cache.db")
        row = {"code": "600519", "date": "2026-09-25", "open": 10,
               "high": 11, "low": 9, "close": 10, "volume": 100,
               "amount": 1000, "turn": 2}
        with Database(db_path=path) as db:
            db.insert_k_daily([row], source="baostock")
            db.insert_k_daily([{**row, "close": 20, "turn": float("nan")}], source="kt")
            cached = db.get_k_daily("600519", "2026-09-25", "2026-09-25")[0]
            assert cached["close"] == 10 and cached["turn"] == 2
            assert cached["source"] == "baostock"


def test_review_claim_and_stale_plan_visibility():
    with tempfile.TemporaryDirectory() as directory:
        with Database(db_path=os.path.join(directory, "plans.db")) as db:
            assert db.claim_review_execution("2026-09-25")["claimed"]
            assert not db.claim_review_execution("2026-09-25")["claimed"]
            assert db.claim_trade_plan_execution("plan-1", "2026-09-25", "hash")["claimed"]
            pending = db.list_execution_reviews()
            assert len(pending["plans"]) == 1 and len(pending["reviews"]) == 1
            marked = db.mark_stale_executions_needs_review(1)
            assert marked == {"plans": 0, "reviews": 0}
            db.mark_trade_plan_needs_review("plan-1", "执行中断")
            assert db.get_trade_plan_execution("plan-1")["status"] == "needs_review"


def test_history_consumer_rejects_stale_but_allows_new_listing():
    frame = pd.DataFrame({"date": ["2026-09-25"], "close": [10.0]})
    frame.attrs.update(coverage_status="stale", coverage_end_gap_days=20)
    assert not daily_history_usable(frame)
    frame.attrs.update(coverage_status="incomplete", coverage_end_gap_days=0,
                       missing_start_days=40, coverage_max_internal_gap_trading_days=0)
    assert daily_history_usable(frame)
    frame.attrs["coverage_max_internal_gap_trading_days"] = 20
    assert not daily_history_usable(frame)


def test_concurrent_first_open_and_risk_updates():
    with tempfile.TemporaryDirectory() as directory:
        db_path = os.path.join(directory, "first.db")
        assert _run_pair(_open_database, (db_path,), (db_path,)) == (Database.SCHEMA_VERSION,) * 2
        with closing(sqlite3.connect(db_path)) as conn:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"

        system_path = os.path.join(directory, "system.json")
        assert set(_run_pair(
            _update_system_state,
            (system_path, "2026-09-25", 1_000_000),
            (system_path, "2026-09-25", 990_000),
        )) == {"2026-09-25"}
        with open(system_path, encoding="utf-8") as handle:
            records = json.load(handle)["daily_records"]
        assert len(records) == 1 and records[0]["date"] == "2026-09-25"

        drawdown_path = os.path.join(directory, "drawdown.json")
        _run_pair(
            _update_drawdown_state,
            (drawdown_path, 1_200_000),
            (drawdown_path, 1_100_000),
        )
        with open(drawdown_path, encoding="utf-8") as handle:
            assert json.load(handle)["peak_value"] == 1_200_000


def main():
    test_stale_account_cannot_overwrite_trade()
    test_paper_quote_and_slippage_limits()
    test_bond_stop_sell_keeps_fresh_quote_path_without_board_price()
    test_explicit_down_limit_blocks_simulated_sell()
    test_corrupt_risk_state_blocks_loading()
    test_drawdown_trading_day_expiry_and_recovery()
    test_readonly_account_does_not_initialize()
    test_daily_cache_preserves_complete_row()
    test_review_claim_and_stale_plan_visibility()
    test_history_consumer_rejects_stale_but_allows_new_listing()
    test_concurrent_first_open_and_risk_updates()


if __name__ == "__main__":
    main()
