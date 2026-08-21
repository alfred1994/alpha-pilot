#!/usr/bin/env python3
"""候选反事实学习测试。"""
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.counterfactual import (
    evaluate_candidate_outcomes,
    record_trade_plan_candidates,
    summarize_candidate_outcomes,
)
from scheduler.trader_brief import build_daily_facts


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = handle.name
    handle.close()
    os.unlink(db_path)
    try:
        plan = SimpleNamespace(
            date="2026-06-01",
            raw_scores=[{
                "code": "600519", "name": "测试候选", "composite": 66,
                "latest_price": 10.0, "llm_action": "HOLD",
                "llm_confidence": 0.7, "llm_reason": "等待确认",
                "dimensions": {"technical": {"score": 66, "confidence": 0.7}},
            }],
            orders=[], hold_reasons={"600519": "HOLD_LLM(等待确认)"},
            strategy_version="test-v1", regime="sideways", scan_id="scan-1",
        )
        assert_true(record_trade_plan_candidates(plan, db_path=db_path) == 1, "候选首次观察写入")
        assert_true(record_trade_plan_candidates(plan, db_path=db_path) == 0, "同一计划重试不重复取样")
        plan.raw_scores[0]["composite"] = 72
        plan.raw_scores[0]["latest_price"] = 10.2
        plan.raw_scores[0]["llm_action"] = "BUY"
        plan.orders = [SimpleNamespace(code="600519", action="BUY")]
        plan.hold_reasons = {}
        plan.scan_id = "scan-2"
        assert_true(record_trade_plan_candidates(plan, db_path=db_path) == 1, "第二次扫描写入独立观察")

        rows = []
        for index in range(1, 5):
            price = 10.0 + index * 0.12
            rows.append({
                "code": "600519", "date": f"2026-06-{index + 1:02d}",
                "open": price, "high": price + 0.08, "low": price - 0.05,
                "close": price, "volume": 1000, "amount": 10000,
                "turn": 1, "pctChg": 1,
            })
        with Database(db_path=db_path) as db:
            db.insert_k_daily(rows, source="test")
            observations = db.get_candidate_outcomes()
            assert_true(len(observations) == 2, "不同扫描时点保留独立样本")
            assert_true(observations[0]["entry_price"] == 10.0 and observations[0]["action"] == "HOLD", "首次价格和动作保持一致")
            assert_true(observations[1]["entry_price"] == 10.2 and observations[1]["action"] == "BUY", "后续价格和动作不污染首次样本")
            assert_true(observations[0]["denial_layer"] == "llm", "拒绝层按扫描时事实持久化")

        result = evaluate_candidate_outcomes(db_path=db_path)
        assert_true(result["updated"] == 2 and result["matured"] == 0, "未满五日只回填已成熟的短周期")
        with Database(db_path=db_path) as db:
            observation = db.get_candidate_outcomes()[0]
            assert_true(observation["return_1d"] is not None, "回填1日收益")
            assert_true(observation["return_3d"] is not None and observation["return_5d"] is None, "T+3与T+5按交易日成熟")
            assert_true(observation["mfe_5d"] is None and observation["mae_5d"] is None, "不足五日不写入MFE/MAE")

        with Database(db_path=db_path) as db:
            db.insert_k_daily([{
                "code": "600519", "date": "2026-06-06", "open": 10.6,
                "high": 10.68, "low": 10.55, "close": 10.6, "volume": 1000,
                "amount": 10000, "turn": 1, "pctChg": 1,
            }], source="test")
        result = evaluate_candidate_outcomes(db_path=db_path)
        assert_true(result["updated"] == 2 and result["matured"] == 2, "T+5成熟后补充标签和五日极值")
        with Database(db_path=db_path) as db:
            observation = db.get_candidate_outcomes()[0]
            assert_true(observation["return_5d"] > 0.05 and observation["mfe_5d"] is not None, "回填5日收益与MFE")
            assert_true(observation["net_return_5d"] < observation["return_5d"], "反事实收益扣除费用和滑点")
            assert_true(observation["outcome_label"] == "missed_opportunity", "识别错误HOLD踏空")
            assert_true(db.get_candidate_outcomes()[1]["outcome_label"] == "validated_buy", "识别正确BUY")

        rows = []
        for index in range(6, 11):
            price = 10.0 + index * 0.12
            rows.append({
                "code": "600519", "date": f"2026-06-{index + 1:02d}",
                "open": price, "high": price + 0.08, "low": price - 0.05,
                "close": price, "volume": 1000, "amount": 10000,
                "turn": 1, "pctChg": 1,
            })
        with Database(db_path=db_path) as db:
            db.insert_k_daily(rows, source="test")
        result = evaluate_candidate_outcomes(db_path=db_path)
        assert_true(result["updated"] == 2 and result["matured"] == 0, "T+10继续回填且不重复统计成熟")
        with Database(db_path=db_path) as db:
            assert_true(all(item["return_10d"] is not None for item in db.get_candidate_outcomes()), "T+10成熟后结束等待")
        assert_true(evaluate_candidate_outcomes(db_path=db_path)["checked"] == 0, "完整T+10记录不再重复评估")
        summary = summarize_candidate_outcomes(days=3650, db_path=db_path)
        assert_true(summary.get("missed_opportunity") == 1, "反事实摘要可统计踏空")
        facts = build_daily_facts(
            date="2026-06-12", db_path=db_path, market_status="闭市", trading_day=True,
        )
        assert_true(facts["counterfactual"].get("missed_opportunity") == 1, "每日事实消费稳定的反事实摘要")
        print("候选反事实学习测试通过")
    finally:
        for path in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
            if os.path.exists(path):
                os.unlink(path)

    legacy = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    legacy_path = legacy.name
    legacy.close()
    try:
        conn = sqlite3.connect(legacy_path)
        conn.execute("""
            CREATE TABLE candidate_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_date TEXT NOT NULL, observed_at TEXT NOT NULL,
                code TEXT NOT NULL, name TEXT, strategy_version TEXT NOT NULL DEFAULT '',
                regime TEXT, action TEXT, llm_action TEXT, llm_confidence REAL,
                score REAL, entry_price REAL, hold_reason TEXT, dimensions TEXT,
                source TEXT, return_1d REAL, return_3d REAL, return_5d REAL,
                return_10d REAL, mfe_5d REAL, mae_5d REAL, outcome_label TEXT,
                evaluated_at TEXT, created_at TEXT, updated_at TEXT,
                UNIQUE(observation_date, code, strategy_version)
            )
        """)
        conn.execute("""
            INSERT INTO candidate_outcomes
            (observation_date, observed_at, code, strategy_version, action, entry_price)
            VALUES ('2026-06-01', '2026-06-01T10:00:00', '600519', 'old-v1', 'HOLD', 10)
        """)
        conn.commit()
        conn.close()
        with Database(db_path=legacy_path) as db:
            migrated = db.get_candidate_outcomes()
            columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(candidate_outcomes)")}
            assert_true("observation_key" in columns and "net_return_5d" in columns and "denial_layer" in columns, "旧反事实表自动迁移到逐扫描结构")
            assert_true(len(migrated) == 1 and migrated[0]["id"] == 1 and migrated[0]["observation_key"].startswith("legacy-"), "迁移保留旧反事实记录及主键")
        with Database(db_path=legacy_path) as db:
            assert_true(len(db.get_candidate_outcomes()) == 1, "重复初始化迁移保持幂等且不丢数据")
    finally:
        for path in (legacy_path, f"{legacy_path}-wal", f"{legacy_path}-shm"):
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    main()
