#!/usr/bin/env python3
"""盘后研究股票池增量同步测试。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.history as history
import data.snapshot as snapshot
import strategy.stock_picker as stock_picker
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
        result = sync_research_universe(batch_size=2, workers=2, history_days=730, path=path)
        assert_true(result["ok"] == 2, "研究任务同步一批K线")
        assert_true(all(kwargs.get("require_full_range") for _, kwargs in calls), "研究同步要求完整历史覆盖")
        result2 = sync_research_universe(batch_size=2, workers=1, history_days=730, path=path)
        assert_true(result2["requested"] == 2, "研究股票池按游标持续轮转")

        with open(f"{path}.lock", "w", encoding="utf-8") as file:
            file.write("busy")
        assert_true(sync_research_universe(path=path)["status"] == "locked", "研究同步拒绝并发重复运行")
        os.unlink(f"{path}.lock")

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
