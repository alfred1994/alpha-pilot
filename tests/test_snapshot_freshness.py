#!/usr/bin/env python3
"""市场快照新鲜度与刷新降级回归测试。"""
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import snapshot


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _market(tag):
    return {
        "date": "2026-08-08",
        "timestamp": time.time(),
        "limit_up": [{"code": tag}],
        "limit_down": [],
    }


def test_fresh_cache_is_reported_without_refresh():
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(snapshot, "SNAPSHOT_DIR", directory):
            snapshot._save_snapshot("market", _market("fresh"))
            with patch.object(snapshot, "prefetch_market_data", side_effect=AssertionError("不应刷新")):
                result = snapshot.get_market_snapshot_status(max_age=60, refresh=True)
        assert_true(result["source"] == "fresh_cache", "新鲜缓存标记为fresh_cache")
        assert_true(result["fresh"] is True, "新鲜缓存fresh=true")
        assert_true(result["snapshot"]["limit_up"][0]["code"] == "fresh", "返回新鲜快照内容")


def test_stale_cache_is_refreshed_and_marked():
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(snapshot, "SNAPSHOT_DIR", directory):
            snapshot._save_snapshot("market", _market("stale"))
            path = snapshot._snapshot_path("market")
            old = time.time() - 120
            os.utime(path, (old, old))
            with patch.object(snapshot, "prefetch_market_data", return_value=_market("refreshed")):
                result = snapshot.get_market_snapshot_status(
                    max_age=10, refresh=True, refresh_timeout=1
                )
        assert_true(result["source"] == "refreshed", "过期缓存触发刷新并标记为refreshed")
        assert_true(result["fresh"] is True, "刷新成功后fresh=true")
        assert_true(result["snapshot"]["limit_up"][0]["code"] == "refreshed", "返回刷新后的快照")


def test_failed_refresh_keeps_explicit_stale_cache():
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(snapshot, "SNAPSHOT_DIR", directory):
            snapshot._save_snapshot("market", _market("stale"))
            path = snapshot._snapshot_path("market")
            old = time.time() - 120
            os.utime(path, (old, old))
            with patch.object(snapshot, "prefetch_market_data", side_effect=RuntimeError("upstream")):
                result = snapshot.get_market_snapshot_status(
                    max_age=10, refresh=True, refresh_timeout=1
                )
        assert_true(result["source"] == "stale_cache", "刷新失败后明确标记stale_cache")
        assert_true(result["fresh"] is False, "过期缓存fresh=false")
        assert_true(result["snapshot"]["limit_up"][0]["code"] == "stale", "保留可追溯的过期快照")
        assert_true(bool(result["refresh_error"]), "记录刷新失败原因")


def test_missing_snapshot_is_unavailable_not_empty_market():
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(snapshot, "SNAPSHOT_DIR", directory), patch.object(
            snapshot, "prefetch_market_data", return_value=None
        ):
            result = snapshot.get_market_snapshot_status(
                max_age=10, refresh=True, refresh_timeout=1
            )
        assert_true(result["source"] == "unavailable", "无缓存且刷新失败标记为unavailable")
        assert_true(result["snapshot"] is None, "不可用时不伪造空市场快照")


def test_candidate_pool_has_stable_version_and_generation_metadata():
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(snapshot, "SNAPSHOT_DIR", directory):
            saved = snapshot.save_candidate_pool([
                {"code": "600519", "name": "贵州茅台", "score": 70, "source": ["测试"]},
            ])
            result = snapshot.get_candidate_pool_status(max_age=60)
        assert_true(len(saved["version"]) == 16, "候选池生成稳定短版本号")
        assert_true(saved["candidate_count"] == 1, "候选池记录候选数量")
        assert_true(bool(saved["generated_at"]), "候选池记录生成时间")
        assert_true(result["version"] == saved["version"], "读取状态复用候选池版本")
        assert_true(result["source"] == "fresh_cache", "新候选池标记为fresh_cache")


def main():
    test_fresh_cache_is_reported_without_refresh()
    test_stale_cache_is_refreshed_and_marked()
    test_failed_refresh_keeps_explicit_stale_cache()
    test_missing_snapshot_is_unavailable_not_empty_market()
    test_candidate_pool_has_stable_version_and_generation_metadata()
    print("市场快照新鲜度测试通过")


if __name__ == "__main__":
    main()
