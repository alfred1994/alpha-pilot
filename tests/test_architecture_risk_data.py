#!/usr/bin/env python3
"""风险状态与账户持久化的离线回归。"""
import json
import multiprocessing as mp
import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from unittest.mock import patch
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


#: 共享 CI runner 只有 2 核且被节流，spawn 子进程要重新解释器启动并 import
#: pandas 等重依赖，启动耗时可达十几秒。给足余量：这个用例验证的是两个写入者
#: 之间的竞态，不是子进程启动速度。
PROCESS_WAIT_SECONDS = 120


def _run_pair(target, args_a, args_b):
    context = mp.get_context("spawn")
    ready = context.Queue()
    collected = []
    drain_errors = []

    def _drain():
        try:
            for _ in range(2):
                collected.append(ready.get(timeout=PROCESS_WAIT_SECONDS))
        except Exception as exc:  # queue.Empty 等回收失败留给断言统一报告
            drain_errors.append(exc)

    first = context.Process(target=target, args=(*args_a, ready))
    second = context.Process(target=target, args=(*args_b, ready))
    first.start()
    second.start()
    # 队列必须从子进程启动那一刻起就并发排空：子进程退出并不代表其 feeder
    # 线程已把数据刷进管道，先 join 再 get(timeout=2) 在慢机器上会偶发 Empty，
    # 那正是这个用例本身要制造并正确处理的竞态。
    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()
    try:
        first.join(PROCESS_WAIT_SECONDS)
        second.join(PROCESS_WAIT_SECONDS)
        assert not first.is_alive(), f"{target.__name__} 首个进程 {PROCESS_WAIT_SECONDS}s 未退出"
        assert not second.is_alive(), f"{target.__name__} 第二个进程 {PROCESS_WAIT_SECONDS}s 未退出"
        assert first.exitcode == 0 and second.exitcode == 0, (
            f"{target.__name__} 子进程退出码异常: {first.exitcode}/{second.exitcode}")
    finally:
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(5)
        ready.close()
        ready.join_thread()
        drainer.join(10)
    assert not drain_errors, f"{target.__name__} 子进程结果回收失败: {drain_errors!r}"
    assert len(collected) == 2, f"{target.__name__} 期望 2 条子进程结果，实收 {len(collected)} 条"
    return collected[0], collected[1]


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


def test_replay_trades_excluded_from_reports_by_default():
    """回放成交必须默认排除在胜率/复盘统计之外。

    生产账户 2026-06-25 凌晨被写入 7 笔盘外成交，虚增 156,491.13 元盈利。
    那些 +81%~+117% 的往返若混进逐笔统计，会把系统表现讲成完全相反的故事。
    """
    with tempfile.TemporaryDirectory() as directory:
        account_path = os.path.join(directory, "account.json")
        db_path = os.path.join(directory, "account.db")
        account = PaperAccount(filepath=account_path, db_path=db_path)
        assert account.buy("600519", "真实成交", 10, shares=100,
                           execution_context="replay")
        assert account.buy("000001", "回放成交", 20, shares=100,
                           execution_context="replay")
        with Database(db_path=db_path) as db:
            db.conn.execute("UPDATE trades SET is_replay = 1 WHERE code = '000001'")
            db.conn.commit()
        with Database(db_path=db_path, readonly=True) as db:
            default_codes = {t["code"] for t in db.get_trades(limit=50)}
            all_codes = {t["code"] for t in db.get_trades(limit=50, include_replay=True)}
        assert default_codes == {"600519"}, f"默认查询应排除回放成交，实际 {default_codes}"
        assert all_codes == {"600519", "000001"}, f"显式包含时应返回全部，实际 {all_codes}"


def test_readonly_query_survives_unmigrated_schema():
    """只读连接不跑迁移，查询不能硬引用尚未 ALTER 出来的新列。

    Web 服务可能先于任何读写连接打开数据库（research/看板路径就是只读的）。
    若 get_trades 无条件引用 is_replay，部署到未迁移的库上会整条查询报
    no such column，看板直接 500。
    """
    with tempfile.TemporaryDirectory() as directory:
        db_path = os.path.join(directory, "legacy.db")
        legacy = sqlite3.connect(db_path)
        legacy.executescript("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, name TEXT,
                action TEXT NOT NULL, price REAL, shares INTEGER, amount REAL,
                commission REAL DEFAULT 0, reason TEXT, signal_score REAL,
                signal_detail TEXT, market_regime TEXT, dimensions TEXT,
                pnl REAL, pnl_pct REAL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO trades (code, name, action, price, shares)
            VALUES ('600519', '旧库', 'BUY', 10.0, 100);
        """)
        legacy.commit()
        legacy.close()

        with Database(db_path=db_path, readonly=True) as db:
            assert not db._column_exists("trades", "is_replay"), "旧库不应有该列"
            trades = db.get_trades(limit=10)
            assert len(trades) == 1, f"未迁移库上只读查询应照常返回，得到 {len(trades)} 条"
        # 迁移后再由只读连接读取，行为一致
        with Database(db_path=db_path) as db:
            assert db._column_exists("trades", "is_replay")
        with Database(db_path=db_path, readonly=True) as db:
            assert len(db.get_trades(limit=10)) == 1
            assert len(db.get_trades(limit=10, include_replay=True)) == 1


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


def test_paper_fill_requires_trading_session():
    """模拟成交只能发生在真实交易时段内。

    生产账户 2026-06-25 凌晨 05:12 被写入 7 笔成交（2 分钟内往返 +81%~+117%，
    当时 A 股尚未开盘），凭空抬高净值约 15.7 万，之后所有绩效与熔断基准都被
    污染。闸门在账户层，回放上下文必须能绕过。
    """
    with tempfile.TemporaryDirectory() as directory:
        account = PaperAccount(
            filepath=os.path.join(directory, "account.json"),
            db_path=os.path.join(directory, "account.db"),
        )
        previous = os.environ.pop("ALPHAPILOT_ALLOW_OFFSESSION_TRADE", None)
        quote = {"code": "600519", "price": 10.0, "close_prev": 10.0,
                 "volume": 1000, "up_limit": 11.0, "down_limit": 9.0,
                 "timestamp": datetime.now(BEIJING_TZ).isoformat()}
        try:
            for off_session in ("盘前", "午休", "盘后", "休市"):
                with patch("scheduler.market_calendar.get_market_status", return_value=off_session):
                    assert account.buy("600519", f"{off_session}买入", 10,
                                       shares=100, quote=quote) is None, \
                        f"{off_session} 不应产生 paper 成交"
            with patch("scheduler.market_calendar.get_market_status", return_value="盘中"):
                assert account.buy("600519", "盘中买入", 10,
                                   shares=100, quote=quote) is not None, \
                    "盘中应正常成交"
            # 回放上下文用于历史重演，不受时段限制。
            with patch("scheduler.market_calendar.get_market_status", return_value="休市"):
                assert account.buy("000001", "回放买入", 10, shares=100,
                                   execution_context="replay") is not None, \
                    "replay 上下文应绕过时段闸门"
        finally:
            if previous is None:
                os.environ.pop("ALPHAPILOT_ALLOW_OFFSESSION_TRADE", None)
            else:
                os.environ["ALPHAPILOT_ALLOW_OFFSESSION_TRADE"] = previous
        with Database(db_path=account.db_path, readonly=True) as db:
            codes = {t["code"] for t in db.get_trades()}
        assert codes == {"600519", "000001"}, f"仅应留下盘中与回放两笔，实际 {codes}"


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
    test_replay_trades_excluded_from_reports_by_default()
    test_readonly_query_survives_unmigrated_schema()
    test_paper_quote_and_slippage_limits()
    test_paper_fill_requires_trading_session()
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
