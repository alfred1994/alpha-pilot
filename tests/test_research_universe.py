#!/usr/bin/env python3
"""盘后研究股票池增量同步测试。"""
import os
import json
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.history as history
import data.snapshot as snapshot
import strategy.stock_picker as stock_picker
import data.research_universe as research_universe
from data.research_universe import refresh_research_universe, sync_research_universe


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    handle = tempfile.NamedTemporaryFile(suffix="_universe.json", delete=False)
    path = handle.name
    handle.close()
    os.unlink(path)
    original_active = stock_picker._get_active_stocks
    original_daily = history.get_daily
    original_pool_status = snapshot.get_candidate_pool_status
    try:
        stock_picker._get_active_stocks = lambda min_amount, limit: {
            "600519": "贵州茅台", "000001": "平安银行", "300750": "宁德时代",
            "688001": "科创样本", "830001": "北交样本", "920001": "北交样本2",
        }
        calls = []
        import pandas as pd
        history.get_daily = lambda code, **kwargs: (
            calls.append((code, kwargs)) or pd.DataFrame({
                "date": ["2025-01-01", "2026-08-20"], "close": [10, 11],
            })
        )
        universe = refresh_research_universe(limit=3, path=path)
        assert_true(len(universe["codes"]) == 3, "按活跃股构建研究股票池")
        assert_true(research_universe.RESEARCH_UNIVERSE_SIZE == 800, "研究股票池默认覆盖800只普通A股")
        assert_true(
            all(research_universe._is_eligible_research_code(item["code"]) for item in universe["codes"]),
            "研究股票池显式排除北交所和科创板",
        )
        assert_true(not research_universe._is_eligible_research_code("510300"), "研究股票池排除ETF")
        assert_true(not research_universe._is_eligible_research_code("000300"), "研究股票池排除指数代码")
        result = sync_research_universe(batch_size=2, workers=2, history_days=730, path=path)
        assert_true(result["ok"] == 2, "研究任务同步一批K线")
        assert_true(all(kwargs.get("require_full_range") for _, kwargs in calls), "研究同步要求完整历史覆盖")
        result2 = sync_research_universe(batch_size=2, workers=1, history_days=730, path=path)
        assert_true(result2["requested"] == 2, "研究股票池按游标持续轮转")

        # 刷新股票池不能丢掉上一批失败标的，否则 retry_codes 永远不会生效。
        with open(path, "r", encoding="utf-8") as file:
            persisted = json.load(file)
        persisted["retry_codes"] = ["600519"]
        with open(path, "w", encoding="utf-8") as file:
            json.dump(persisted, file)
        refreshed = refresh_research_universe(limit=3, path=path)
        assert_true(refreshed["retry_codes"] == ["600519"], "刷新研究池保留失败重试队列")

        heartbeat_path = f"{path}.heartbeat.lock"
        assert_true(research_universe._acquire_lock(heartbeat_path), "研究锁可创建")
        try:
            old_time = 1
            os.utime(heartbeat_path, (old_time, old_time))
            research_universe._heartbeat_lock(heartbeat_path)
            assert_true(os.path.getmtime(heartbeat_path) > old_time, "研究锁心跳刷新文件时间")
        finally:
            research_universe._release_lock(heartbeat_path)

        with open(f"{path}.lock", "w", encoding="utf-8") as file:
            json.dump({"pid": os.getpid(), "started_at": "2026-01-01T00:00:00"}, file)
        old_time = 1
        os.utime(f"{path}.lock", (old_time, old_time))
        assert_true(sync_research_universe(path=path)["status"] == "locked", "研究同步拒绝并发重复运行")
        os.unlink(f"{path}.lock")

        # 仅无主且超过完整任务预算的锁才回收；混合结果也必须标成非成功。
        with open(f"{path}.lock", "w", encoding="utf-8") as file:
            json.dump({"pid": 99999999, "started_at": "2026-01-01T00:00:00"}, file)
        os.utime(f"{path}.lock", (1, 1))
        recovered = sync_research_universe(batch_size=1, path=path)
        assert_true(recovered["status"] == "success", "超时预算外的无主研究锁可回收")

        import main as app_main
        original_refresh = research_universe.refresh_research_universe
        original_sync = research_universe.sync_research_universe
        try:
            research_universe.refresh_research_universe = lambda **kwargs: {"codes": [{"code": "600519"}]}
            for status in ("partial", "failed", "locked", "no_universe"):
                research_universe.sync_research_universe = lambda status=status, **kwargs: {"status": status}
                command_result = app_main.cmd_research_sync(type("Args", (), {
                    "research_universe_size": 1,
                    "research_batch_size": 1,
                    "research_workers": 1,
                    "research_history_days": 365,
                })())
                assert_true(command_result["errors"] == ["research_sync"], f"研究状态{status}失败退出")
        finally:
            research_universe.refresh_research_universe = original_refresh
            research_universe.sync_research_universe = original_sync

        stock_picker._get_active_stocks = lambda min_amount, limit: {}
        snapshot.get_candidate_pool_status = lambda max_age: {
            "snapshot": {"candidates": [{"code": "000001", "name": "平安银行"}]},
        }
        fallback_path = f"{path}.fallback"
        fallback = refresh_research_universe(limit=3, path=fallback_path)
        assert_true(fallback["source"] == "candidate_pool_fallback", "活跃股接口失败时回退当天候选池")
        os.unlink(fallback_path)
        print("研究股票池同步测试通过")
    finally:
        stock_picker._get_active_stocks = original_active
        history.get_daily = original_daily
        snapshot.get_candidate_pool_status = original_pool_status
        if os.path.exists(path):
            os.unlink(path)
        if os.path.exists(f"{path}.lock"):
            os.unlink(f"{path}.lock")


if __name__ == "__main__":
    main()
