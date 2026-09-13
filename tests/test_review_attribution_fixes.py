#!/usr/bin/env python3
"""复盘归因链路修复回归测试。

覆盖三类修复:
1. outcome 词表统一为小写(win/lose/breakeven)，与 memory/ai_trader_report 消费端一致。
2. 基准曲线缺失日延续上一日累计值，不再归零重算。
3. trade_reviews 序列化补齐 signal_score/market_regime/dimensions 归因字段。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review.daily_review as daily_review_mod
from data.database import Database
from review.daily_review import (
    DailyReviewResult,
    DailyReviewer,
    TradeReview,
    _compute_benchmark_pnl_pct,
    _parse_dimensions,
)


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


def test_outcome_lowercase_backfill():
    print("测试1: outcome 回填写入小写词表")
    db_path = temp_path("_review.db")
    try:
        with Database(db_path=db_path) as db:
            db.insert_llm_decision({
                "code": "600519", "date": "2026-09-10", "action": "BUY",
                "confidence": 0.7,
            })
            db.insert_llm_decision({
                "code": "000001", "date": "2026-09-10", "action": "SELL",
                "confidence": 0.6,
            })
            db.insert_trade({
                "code": "600519", "name": "贵州茅台", "action": "BUY",
                "price": 100.0, "shares": 100,
                "created_at": "2026-09-10T10:00:00",
            })
            db.insert_trade({
                "code": "600519", "name": "贵州茅台", "action": "SELL",
                "price": 110.0, "shares": 100, "pnl": 1000.0, "pnl_pct": 0.10,
                "created_at": "2026-09-10T14:00:00",
            })
            db.insert_trade({
                "code": "000001", "name": "平安银行", "action": "SELL",
                "price": 10.0, "shares": 100, "pnl": -50.0, "pnl_pct": -0.05,
                "created_at": "2026-09-10T14:30:00",
            })

        reviewer = DailyReviewer(
            review_dir=tempfile.mkdtemp(prefix="_review_dir_"), db_path=db_path
        )
        updated = reviewer.backfill_decision_outcomes("2026-09-10")
        assert_true(updated == 2, f"回填{updated}条决策")

        with Database(db_path=db_path) as db:
            decisions = db.get_llm_decisions(start_date="2026-09-10", end_date="2026-09-10")
            outcomes = {d["code"]: d["outcome"] for d in decisions}
            assert_true(outcomes.get("600519") == "win", f"盈利BUY决策 outcome={outcomes.get('600519')}")
            assert_true(outcomes.get("000001") == "lose", f"亏损SELL决策 outcome={outcomes.get('000001')}")
            assert_true(
                all(str(v).islower() for v in outcomes.values()),
                "全部 outcome 为小写(消费端可直接匹配)",
            )
        cleanup_dir(reviewer.review_dir)
    finally:
        cleanup(db_path)


def test_benchmark_chain_continuity():
    print("测试2: 基准取数失败时延续累计值")
    review_dir = tempfile.mkdtemp(prefix="_bm_dir_")
    try:
        prev = {"benchmark_pnl_pct": 0.05}
        with open(os.path.join(review_dir, "review_2026-09-10.json"), "w", encoding="utf-8") as f:
            json.dump(prev, f)

        original = daily_review_mod._fetch_hs300_daily_pct
        try:
            daily_review_mod._fetch_hs300_daily_pct = lambda date=None: None
            carried = _compute_benchmark_pnl_pct("2026-09-11", review_dir)
            assert_true(abs(carried - 0.05) < 1e-9, f"取数失败延续累计基准 {carried}")
            daily_review_mod._fetch_hs300_daily_pct = lambda date=None: 0.01
            chained = _compute_benchmark_pnl_pct("2026-09-11", review_dir)
            expected = (1.0 + 0.05) * (1.0 + 0.01) - 1.0
            assert_true(abs(chained - expected) < 1e-9, f"正常日按链式累计 {chained:.4f}")
        finally:
            daily_review_mod._fetch_hs300_daily_pct = original
    finally:
        cleanup_dir(review_dir)


def cleanup_dir(path):
    for name in os.listdir(path):
        os.unlink(os.path.join(path, name))
    os.rmdir(path)


def test_trade_review_serialization():
    print("测试3: trade_reviews 序列化包含归因字段")
    review_dir = tempfile.mkdtemp(prefix="_ser_dir_")
    db_path = temp_path("_ser.db")
    try:
        result = DailyReviewResult(
            date="2026-09-11",
            initial_capital=1_000_000, total_assets=1_000_000,
            daily_pnl=0.0, daily_pnl_pct=0.0,
            cumulative_pnl=0.0, cumulative_pnl_pct=0.0,
            cash=1_000_000, market_value=0.0, position_count=0,
        )
        result.trade_reviews.append(TradeReview(
            code="600519", name="贵州茅台", action="BUY",
            price=100.0, shares=100, reason="测试买入",
            signal_score=72.5,
            market_regime="bull",
            dimensions={"technical": 80, "ml": 60},
        ))
        reviewer = DailyReviewer(review_dir=review_dir, db_path=db_path)
        # 序列化会计算基准，mock掉网络取数（测试运行器禁网）
        original = daily_review_mod._fetch_hs300_daily_pct
        daily_review_mod._fetch_hs300_daily_pct = lambda date=None: 0.01
        try:
            reviewer._save_review(result)
        finally:
            daily_review_mod._fetch_hs300_daily_pct = original

        with open(os.path.join(review_dir, "review_2026-09-11.json"), "r", encoding="utf-8") as f:
            saved = json.load(f)
        trade = saved["trade_reviews"][0]
        assert_true(trade.get("signal_score") == 72.5, "signal_score 已序列化")
        assert_true(trade.get("market_regime") == "bull", "market_regime 已序列化")
        assert_true(trade.get("dimensions", {}).get("technical") == 80, "dimensions 已序列化")
    finally:
        cleanup_dir(review_dir)
        cleanup(db_path)


def test_parse_dimensions():
    print("测试4: dimensions 容错解析")
    assert_true(_parse_dimensions('{"technical": 80}') == {"technical": 80}, "JSON字符串解析")
    assert_true(_parse_dimensions({"a": 1}) == {"a": 1}, "dict直通")
    assert_true(_parse_dimensions(None) == {}, "None返回空dict")
    assert_true(_parse_dimensions("not-json") == {}, "非法字符串返回空dict")


def main():
    test_outcome_lowercase_backfill()
    test_benchmark_chain_continuity()
    test_trade_review_serialization()
    test_parse_dimensions()
    print("\n全部复盘归因修复测试通过")


if __name__ == "__main__":
    main()
