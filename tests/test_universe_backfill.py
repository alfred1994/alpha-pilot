#!/usr/bin/env python3
"""全市场日线回填工具回归测试（离线，注入假数据源）。"""
import json
import os
import sys
import tempfile
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.universe_backfill as ub
from data.database import Database


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


def _fake_daily_df(code, latest="2026-09-11"):
    df = pd.DataFrame({
        "date": [latest],
        "open": [10.0], "high": [10.5], "low": [9.8],
        "close": [10.2], "volume": [1000], "amount": [10200],
    })
    df.attrs["coverage_status"] = "ok"
    df.attrs["coverage_latest"] = latest
    return df


def test_resource_detection():
    print("测试1: 资源自适应与环境变量覆盖")
    os.environ["UNIVERSE_BACKFILL_WORKERS"] = "3"
    try:
        res = ub._detect_resources()
        assert_true(res["workers"] == 3, f"环境变量生效 workers={res['workers']}")
        assert_true(res["worker_source"] == "env:UNIVERSE_BACKFILL_WORKERS", "来源标注")
    finally:
        del os.environ["UNIVERSE_BACKFILL_WORKERS"]
    res = ub._detect_resources(workers=5)
    assert_true(res["workers"] == 5, "显式参数生效")


def test_disk_guard():
    print("测试2: 磁盘水位保护")
    with mock.patch.object(ub.shutil, "disk_usage") as fake:
        fake.return_value = mock.Mock(free=0.5 * 1024 ** 3)  # 0.5GB
        try:
            ub._check_disk(5000, "20160101")
            assert_true(False, "磁盘不足应抛异常")
        except RuntimeError as exc:
            assert_true("磁盘空间不足" in str(exc), "磁盘不足拒绝运行")


def test_incremental_and_full_modes():
    print("测试3: 增量/全区间任务编排")
    db_path = temp_path("_backfill.db")
    try:
        with Database(db_path=db_path) as db:
            db.insert_k_daily([
                {"code": "600001", "date": "2026-09-11", "open": 10, "high": 10.5,
                 "low": 9.8, "close": 10.2, "volume": 1000, "amount": 10200},
            ], source="test")
            # 600002 无数据 → 增量模式也纳入任务

        with mock.patch.object(ub, "load_universe", return_value=["600001", "600002"]), \
             mock.patch("data.history.get_daily", return_value=_fake_daily_df("600001")):
            # 增量：600001 已新鲜应被跳过
            summary = ub.run_backfill(universe="pool", db_path=db_path, workers=2)
            assert_true(summary["skipped_fresh"] == 1, f"新鲜股票跳过({summary['skipped_fresh']})")
            assert_true(summary["tasks"] == 1, f"只回填1只({summary['tasks']})")
            assert_true(summary["ok"] == 1, "回填成功1只")
            assert_true(summary["status"] == "ok", "整体状态ok")

            # 全区间：两只都进入任务，覆盖中间缺口
            summary_full = ub.run_backfill(
                universe="pool", full=True, db_path=db_path, workers=2
            )
            assert_true(summary_full["skipped_fresh"] == 0, "全区间不跳过")
            assert_true(summary_full["tasks"] == 2, f"全区间任务2只({summary_full['tasks']})")
            assert_true(summary_full["ok"] == 2, "全区间成功2只")
    finally:
        cleanup(db_path)
        report = ub.BACKFILL_REPORT_FILE
        if os.path.exists(report):
            os.unlink(report)


def test_universe_all_filter():
    print("测试4: 全市场股票池板块过滤")
    fake = pd.DataFrame({
        "code": ["sh.600000", "sh.688001", "sz.000001", "sz.300750",
                 "sh.000001", "sh.510300", "bj.832000", "sh.601398"],
        "code_name": ["浦发银行", "科创板", "平安银行", "宁德时代",
                      "上证指数", "ETF", "北交所", "工商银行"],
        "type": ["1"] * 8,
        "status": ["1"] * 8,
    })
    with mock.patch("data.history.get_stock_list", return_value=fake):
        codes = ub._fetch_all_universe()
    assert_true("600000" in codes and "601398" in codes, "沪深主板保留")
    assert_true("000001" in codes and "300750" in codes, "深主板/创业板保留")
    assert_true("688001" not in codes, "科创板剔除")
    assert_true("832000" not in codes, "北交所剔除")
    # sh.000001 上证指数不能混入 sz.000001 平安银行
    assert_true(codes.count("000001") == 1, "指数与股票不混淆")


def test_universe_all_eastmoney_pagination():
    print("测试5: 东财分页全市场列表与板块过滤")
    calls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None):
        calls.append(params["pn"])
        pn = params["pn"]
        if pn == 1:
            diff = [
                {"f12": "600000", "f14": "浦发银行"},
                {"f12": "688001", "f14": "科创板"},
                {"f12": "300750", "f14": "宁德时代"},
                {"f12": "510300", "f14": "ETF"},
            ]
            total = 201  # 强制进入第2页
        else:
            diff = [{"f12": "000001", "f14": "平安银行"}]
            total = 201
        return _Resp({"data": {"total": total, "diff": diff}})

    import requests as requests_mod
    with mock.patch.object(requests_mod, "get", side_effect=fake_get):
        codes = ub._fetch_all_universe_eastmoney()
    assert_true(calls == [1, 2], f"分页到总数为止({calls})")
    assert_true("600000" in codes and "300750" in codes and "000001" in codes, "A股保留")
    assert_true("688001" not in codes, "科创板剔除")
    assert_true("510300" not in codes, "ETF剔除")


def main():
    test_resource_detection()
    test_disk_guard()
    test_incremental_and_full_modes()
    test_universe_all_filter()
    test_universe_all_eastmoney_pagination()
    print("\n全市场回填工具测试通过")


if __name__ == "__main__":
    main()
