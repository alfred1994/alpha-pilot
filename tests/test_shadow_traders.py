#!/usr/bin/env python3
"""影子策略变体决策、指标计算与晋级门禁的无依赖回归测试。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.shadow_traders import (
    SHADOW_VARIANT_IDS,
    compute_variant_metrics,
    decide_variant_actions,
    record_daily_decisions,
)
from strategy.shadow_eval import (
    evaluate_variants,
    expire_stale_ab_tests,
    promote_candidates,
)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _dims(**scores):
    base = {"technical": 50, "capital": 50, "sentiment": 50,
            "emotion": 50, "fundamental": 50, "ml": 50}
    base.update(scores)
    return {k: {"score": float(v)} for k, v in base.items()}


def _entry(code, composite, dims, price=10.0):
    return {
        "code": code, "name": f"股票{code}", "composite": composite,
        "latest_price": price, "dimensions": dims,
    }


def _sample_scored():
    return [
        _entry("600000", 80.0, _dims(technical=90, ml=90)),
        _entry("600001", 70.0, _dims(technical=70, ml=70)),
        _entry("600002", 63.0, _dims(technical=63, ml=63)),
    ]


BASE_PARAMS = {"top_k": 2, "min_score": 65.0, "max_weight": 0.2}


def test_variant_decisions():
    decisions = decide_variant_actions(_sample_scored(), BASE_PARAMS, "baseline")
    assert_true(decisions["600000"]["action"] == "BUY", "baseline买入第一名")
    assert_true(decisions["600001"]["action"] == "BUY", "baseline买入第二名")
    assert_true(decisions["600002"]["action"] == "HOLD", "baseline持有第三名")
    assert_true("HOLD_NOT_TOP2" in decisions["600002"]["hold_reason"], "未进TopK标记原因")

    loose = decide_variant_actions(_sample_scored(), BASE_PARAMS, "loose_top")
    assert_true(
        all(loose[c]["action"] == "BUY" for c in ("600000", "600001", "600002")),
        "放宽门槛后三只全部买入",
    )

    strict = decide_variant_actions(_sample_scored(), BASE_PARAMS, "strict_top")
    assert_true(strict["600000"]["action"] == "BUY", "收紧门槛只买入第一名")
    assert_true(
        strict["600001"]["action"] == "HOLD" and strict["600002"]["action"] == "HOLD",
        "收紧门槛其余持有",
    )


def test_ml_weight_variants():
    # A 靠 ml 拿高 composite；B 靠 technical。去掉 ml 后 B 应反超 A。
    scored = [
        _entry("600000", 70.8, _dims(technical=90, ml=90)),
        _entry("600001", 58.9, _dims(technical=95, ml=10)),
    ]
    params = {"top_k": 1, "min_score": 50.0}

    baseline = decide_variant_actions(scored, params, "baseline")
    assert_true(baseline["600000"]["action"] == "BUY", "baseline按综合分选A")

    ml_none = decide_variant_actions(scored, params, "ml_none")
    assert_true(ml_none["600001"]["action"] == "BUY", "去ML权重后technical强的B反超")

    ml_only = decide_variant_actions(scored, params, "ml_only")
    assert_true(ml_only["600000"]["action"] == "BUY", "纯ML模式按ml分数选A")
    assert_true(ml_only["600000"]["adjusted_score"] == 90.0, "纯ML模式分数即ml分数")

    ml_heavy = decide_variant_actions(scored, params, "ml_heavy")
    assert_true(ml_heavy["600000"]["adjusted_score"] > ml_heavy["600001"]["adjusted_score"],
                "ML加倍后A仍领先")
    assert_true(abs(ml_heavy["600000"]["adjusted_score"] - 73.6) < 0.2,
                "ML加倍重算分数符合加权公式")


def test_record_and_metrics():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        with Database(db_path=db_path) as db:
            scored = _sample_scored()
            summary = record_daily_decisions(db, "2026-08-20", "sideways", "v1", scored, BASE_PARAMS)
            assert_true(summary["baseline"]["buys"] == 2, "baseline记录两笔买入")
            # 同日重复记录不重复插行
            record_daily_decisions(db, "2026-08-20", "sideways", "v1", scored, BASE_PARAMS)
            count = db.conn.execute(
                "SELECT COUNT(*) FROM shadow_decisions WHERE variant_id='baseline'"
            ).fetchone()[0]
            assert_true(count == 3, "同日重复记录只保留一行")

            # 反事实结果：600000 +5%，600001 -2%，600002(HOLD) +8%
            for code, ret in (("600000", 0.05), ("600001", -0.02), ("600002", 0.08)):
                db.conn.execute(
                    "INSERT INTO candidate_outcomes (observation_key, scan_id, observation_date,"
                    " observed_at, code, strategy_version, action, net_return_5d)"
                    " VALUES (?, 'scan1', '2026-08-20', '2026-08-20T15:00:00', ?, 'v1', 'BUY', ?)",
                    (f"legacy-{code}", code, ret),
                )
            db.conn.commit()

            metrics = compute_variant_metrics(db, "baseline")
            assert_true(metrics["buys"] == 2 and metrics["holds"] == 1, "指标统计买卖笔数")
            assert_true(abs(metrics["avg_net_5d"] - 0.015) < 1e-9, "买入平均净收益=(5-2)/2=1.5%")
            assert_true(abs(metrics["win_rate"] - 0.5) < 1e-9, "买入胜率50%")
            assert_true(abs(metrics["error_hold_rate"] - 1.0) < 1e-9, "错误HOLD率=1（持有的600002后续+8%）")
            assert_true(metrics["trading_days"] == 1, "交易日数=1")


def test_promotion_gate():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        with Database(db_path=db_path) as db:
            # 未成熟：只有3天样本 → 不产生晋级候选
            for day in range(3):
                record_daily_decisions(db, f"2026-08-{10+day:02d}", "sideways", "v1",
                                       _sample_scored(), BASE_PARAMS)
            result = promote_candidates(db, min_days=20, min_buys=10)
            assert_true(result["candidates"] == [], "未成熟不产生晋级候选")

            # 成熟且优于 baseline → 产生候选；重复评估不重复插行
            for day in range(25):
                date = f"2026-07-{1+day:02d}"
                scored = _sample_scored()
                record_daily_decisions(db, date, "sideways", "v1", scored, BASE_PARAMS)
                # 给 loose_top 的买入标的挂正收益，baseline 的挂负收益
                for code, ret in (("600000", -0.01), ("600001", -0.01), ("600002", 0.04)):
                    db.conn.execute(
                        "INSERT INTO candidate_outcomes (observation_key, scan_id, observation_date,"
                        " observed_at, code, strategy_version, action, net_return_5d)"
                        " VALUES (?, 's', ?, 'x', ?, 'v1', 'BUY', ?)",
                        (f"legacy-{date}-{code}", date, code, ret),
                    )
            db.conn.commit()
            result = promote_candidates(db, min_days=20, min_buys=10)
            assert_true("loose_top" in result["candidates"], "成熟且优于baseline的变体产生晋级候选")
            again = promote_candidates(db, min_days=20, min_buys=10)
            assert_true(again["candidates"] == ["loose_top"], "重复评估不产生重复候选")

            rows = db.conn.execute(
                "SELECT variant_id, status FROM shadow_promotions"
            ).fetchall()
            assert_true(len(rows) == 1 and rows[0][1] == "candidate", "晋级候选待人工门禁")

            leaderboard = evaluate_variants(db, min_days=20, min_buys=10)
            loose_row = next(r for r in leaderboard if r["variant_id"] == "loose_top")
            assert_true(loose_row["mature"] is True, "排行榜标注成熟状态")
            assert_true("avg_net_5d_delta" in loose_row, "排行榜含与baseline的差值")


def test_expire_stale_ab_tests():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        with Database(db_path=db_path) as db:
            db.conn.execute("""
                CREATE TABLE IF NOT EXISTS ab_tests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    test_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    status TEXT DEFAULT 'running',
                    control_params TEXT NOT NULL,
                    treatment_params TEXT NOT NULL,
                    control_regime TEXT,
                    treatment_regime TEXT,
                    min_trades INTEGER DEFAULT 5,
                    min_days INTEGER DEFAULT 3,
                    result TEXT,
                    winner TEXT,
                    finished_at TEXT
                )
            """)
            db.conn.execute(
                "INSERT INTO ab_tests (test_id, created_at, control_params, treatment_params)"
                " VALUES ('ab_old', '2026-07-01T10:00:00', '{}', '{}')"
            )
            db.conn.execute(
                "INSERT INTO ab_tests (test_id, created_at, control_params, treatment_params)"
                " VALUES ('ab_new', '2026-08-21T10:00:00', '{}', '{}')"
            )
            db.conn.commit()
            expired = expire_stale_ab_tests(db, max_age_days=14)
            assert_true(expired == 1, "过期实验清理1个")
            status_old = db.conn.execute(
                "SELECT status FROM ab_tests WHERE test_id='ab_old'"
            ).fetchone()[0]
            status_new = db.conn.execute(
                "SELECT status FROM ab_tests WHERE test_id='ab_new'"
            ).fetchone()[0]
            assert_true(status_old == "expired", "旧实验标记expired")
            assert_true(status_new == "running", "新实验保持running")


def main():
    assert_true(len(SHADOW_VARIANT_IDS) == 6, "六个影子变体已定义")
    test_variant_decisions()
    test_ml_weight_variants()
    test_record_and_metrics()
    test_promotion_gate()
    test_expire_stale_ab_tests()
    print("影子策略变体与晋级门禁测试通过")


if __name__ == "__main__":
    main()
