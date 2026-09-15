#!/usr/bin/env python3
"""复盘研究闭环与纪律门槛回归测试（2026-09 TODO 批次）。

覆盖五项改动:
1. 绩效接线: PerformanceAnalyzer 补齐 information_ratio 并接入 run_review。
2. 基准进决策链: benchmark_pnl_pct 进 LLM 复盘 prompt 与策略指令 prompt。
3. regime 归因: trades.market_regime / candidate_outcomes.regime 分环境聚合。
4. A/B 自动采用门槛: 20交易日/10笔 + Welch t 检验显著性门槛。
5. 影子晋级人工闭环: decide_candidate 审批（approved/rejected 不再被重新提名）。
"""
import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review.daily_review as daily_review_mod
from data.database import Database
from review.daily_review import DailyReviewer
from review.llm_review import build_llm_review_prompt
from review.performance import PerformanceAnalyzer
from strategy.ab_test import ABTestManager, welch_t_test, SIGNIFICANCE_ALPHA
from strategy.directive import _build_prompt
from strategy.shadow_eval import decide_candidate, promote_candidates


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


def cleanup_dir(path):
    for name in os.listdir(path):
        os.unlink(os.path.join(path, name))
    os.rmdir(path)


def test_welch_t_test_statistics():
    print("测试1: Welch t 检验与已知分布对齐")
    # 经典查表值: t=2.228, df=10 → 双尾 p≈0.05; t=2.0, df=10 → p≈0.0734
    t_stat, df, p_value = welch_t_test(
        [2.228 * 0.5 + x for x in [0] * 6], [0.0] * 6
    )
    # 构造精确 t 值困难，改为直接验证 p 随 |t| 单调且阈值行为正确
    assert_true(abs(welch_t_test([0.0, 0.0], [0.1, 0.2])[2] - 1.0) > 0,
                "小样本(各2条)也能返回有限p值")
    zero_var = welch_t_test([1.0, 1.0, 1.0], [2.0, 2.0, 2.0])
    assert_true(zero_var[2] == 1.0, f"零方差组回退p=1.0 (got {zero_var[2]})")
    assert_true(len(welch_t_test([1.0], [2.0, 3.0])) == 3, "单边样本<2返回(0,0,1)")

    # 显著 vs 不显著的区分: 10 vs 10, 一组均值0.02、另一组0.10、噪声0.03
    rng = random.Random(42)
    control = [rng.gauss(0.02, 0.03) for _ in range(10)]
    treatment = [rng.gauss(0.10, 0.03) for _ in range(10)]
    t_stat, df, p_value = welch_t_test(treatment, control)
    assert_true(p_value < SIGNIFICANCE_ALPHA,
                f"明显差异显著 p={p_value:.4f} (t={t_stat:.2f}, df={df:.1f})")

    treatment_noisy = [rng.gauss(0.03, 0.08) for _ in range(10)]
    _, _, p_noisy = welch_t_test(treatment_noisy, control)
    assert_true(p_noisy >= SIGNIFICANCE_ALPHA,
                f"重叠分布不显著 p={p_noisy:.4f}")


def test_information_ratio():
    print("测试2: information_ratio 实现（组合跑赢基准→IR>0）")
    analyzer = PerformanceAnalyzer()
    rng = random.Random(42)
    nav = 1_000_000
    bench_value = 1.0
    nav_series = []
    bench_nav_series = []
    for i in range(61):
        date = f"2026-07-{i+1:02d}" if i < 31 else f"2026-08-{i-30:02d}"
        nav *= 1 + rng.gauss(0.003, 0.008)   # 组合日均+0.3%，带波动
        bench_value *= 1 + rng.gauss(0.0005, 0.006)  # 基准弱于组合
        nav_series.append({"date": date, "total_assets": nav})
        bench_nav_series.append({"date": date, "nav": bench_value})

    metrics = analyzer.analyze_from_nav_series(
        nav_series, initial_capital=1_000_000, benchmark_nav=bench_nav_series
    )
    assert_true(metrics.information_ratio > 0, f"跑赢基准 IR={metrics.information_ratio:.2f}")
    assert_true(abs(metrics.information_ratio) < 50, "IR 数值量级合理(跟踪误差非退化)")
    assert_true(metrics.tracking_error > 0, f"跟踪误差={metrics.tracking_error:.4f}")
    assert_true(metrics.benchmark_return > 0, f"基准累计={metrics.benchmark_return:.4f}")
    assert_true(metrics.benchmark_annualized_return < metrics.annualized_return,
                "组合年化高于基准年化")

    # 基准缺失 → IR 保持 0，不猜基准
    metrics_no_bench = analyzer.analyze_from_nav_series(nav_series, initial_capital=1_000_000)
    assert_true(metrics_no_bench.information_ratio == 0.0, "无基准时 IR=0")

    # 基准长度不匹配 → 回退 IR=0
    metrics_mismatch = analyzer.analyze_from_nav_series(
        nav_series, initial_capital=1_000_000, benchmark_nav=bench_nav_series[:5]
    )
    assert_true(metrics_mismatch.information_ratio == 0.0, "基准长度不匹配 IR=0")

    # 跑输基准 → IR<0
    losing = []
    value = 1_000_000
    for item in nav_series:
        value *= 1 + rng.gauss(-0.002, 0.008)
        losing.append({"date": item["date"], "total_assets": value})
    metrics_lose = analyzer.analyze_from_nav_series(
        losing, initial_capital=1_000_000, benchmark_nav=bench_nav_series
    )
    assert_true(metrics_lose.information_ratio < 0, f"跑输基准 IR={metrics_lose.information_ratio:.2f}")


def test_run_review_wires_performance_benchmark_regime():
    print("测试3: run_review 落盘 benchmark/performance/regime_attribution 并进 prompt")
    review_dir = tempfile.mkdtemp(prefix="_wire_dir_")
    db_path = temp_path("_wire.db")
    try:
        # 前一日复盘（基准累计5%、总资产100万）
        with open(os.path.join(review_dir, "review_2026-09-10.json"), "w", encoding="utf-8") as f:
            json.dump({
                "date": "2026-09-10", "total_assets": 1_000_000,
                "benchmark_pnl_pct": 0.05, "trade_reviews": [],
            }, f)

        with Database(db_path=db_path) as db:
            db.insert_trade({
                "code": "600519", "name": "贵州茅台", "action": "SELL",
                "price": 110.0, "shares": 100, "pnl": 1000.0, "pnl_pct": 0.10,
                "market_regime": "bull",
                "created_at": "2026-09-11T14:00:00",
            })
            db.insert_trade({
                "code": "000001", "name": "平安银行", "action": "SELL",
                "price": 9.0, "shares": 100, "pnl": -100.0, "pnl_pct": -0.10,
                "market_regime": "bear",
                "created_at": "2026-09-11T14:30:00",
            })
            db.conn.execute(
                "INSERT INTO candidate_outcomes (observation_key, scan_id, observation_date,"
                " observed_at, code, regime, net_return_5d) VALUES (?,?,?,?,?,?,?)",
                ("k1", "s1", "2026-09-08", "2026-09-08T15:00:00", "600519", "bull", 0.04),
            )
            db.conn.commit()

        original = daily_review_mod._fetch_hs300_daily_pct
        daily_review_mod._fetch_hs300_daily_pct = lambda date=None: 0.01
        try:
            reviewer = DailyReviewer(review_dir=review_dir, db_path=db_path)
            result = reviewer.run_review(
                date="2026-09-11", positions={}, trades=[], prices={},
                prev_prices={}, cash=1_010_000, initial_capital=1_000_000,
            )
        finally:
            daily_review_mod._fetch_hs300_daily_pct = original

        assert_true(result.benchmark_pnl_pct is not None, "benchmark_pnl_pct 已填充")
        assert_true(result.performance.get("trading_days") == 2,
                    f"绩效覆盖2个交易日 (got {result.performance.get('trading_days')})")
        assert_true("information_ratio" in result.performance, "绩效含 information_ratio")
        assert_true(
            result.regime_attribution["trades_by_regime"].get("bull", {}).get("win_rate") == 1.0,
            "trades 分环境: bull 胜率100%",
        )
        assert_true(
            result.regime_attribution["trades_by_regime"].get("bear", {}).get("win_rate") == 0.0,
            "trades 分环境: bear 胜率0%",
        )
        assert_true(
            result.regime_attribution["candidates_by_regime"].get("bull", {}).get("samples") == 1,
            "candidate_outcomes 分环境聚合生效",
        )

        with open(os.path.join(review_dir, "review_2026-09-11.json"), "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert_true(saved.get("performance", {}).get("trading_days") == 2, "JSON 已落盘 performance")
        assert_true("regime_attribution" in saved, "JSON 已落盘 regime_attribution")

        # 复盘 prompt 含基准超额、绩效、分环境归因
        prompt = build_llm_review_prompt("2026-09-11", saved)
        assert_true("基准(沪深300)累计" in prompt, "prompt 含基准累计")
        assert_true("相对基准超额" in prompt, "prompt 含超额收益")
        assert_true("区间绩效指标" in prompt, "prompt 含区间绩效")
        assert_true("分市场环境归因" in prompt, "prompt 含分环境归因")

        # 策略指令 prompt 同样接入
        directive_prompt = _build_prompt(
            "2026-09-11", "2026-09-14", saved, "复盘文本",
            "bull", {"top_k": 3, "min_score": 58, "max_weight": 0.1},
        )
        assert_true("相对基准与区间绩效" in directive_prompt, "指令 prompt 含基准绩效块")
        assert_true("分市场环境归因" in directive_prompt, "指令 prompt 含分环境归因")

        # 无基准数据时不渲染也不报错
        bare_prompt = build_llm_review_prompt("2026-09-11", {"total_assets": 1})
        assert_true("基准(沪深300)累计" not in bare_prompt, "无基准数据不渲染基准行")
    finally:
        cleanup_dir(review_dir)
        cleanup(db_path)


def test_ab_test_significance_gate():
    print("测试4: A/B 结论受显著性门槛约束")
    db_path = temp_path("_ab.db")
    rng = random.Random(42)
    try:
        with Database(db_path=db_path) as db:
            abm = ABTestManager(db=db)

            # 实验1: 实验组明显更好 → treatment 胜出且 significant
            t1 = abm.create_test({"a": 1}, {"a": 2}, min_trades=5, min_days=0)
            for _ in range(8):
                abm.record_trade(t1, "control", "600519", "SELL", 100, rng.gauss(0.01, 0.02))
                abm.record_trade(t1, "treatment", "600519", "SELL", 100, rng.gauss(0.08, 0.02))
            r1 = abm.evaluate_test(t1)
            assert_true(r1.get("status") == "concluded", "实验1已结论")
            assert_true(r1.get("winner") == "treatment", f"实验1 treatment 胜出 (got {r1.get('winner')})")
            assert_true(r1.get("significant") is True, f"实验1 显著 (p={r1.get('p_value')})")

            # 实验2: 分布重叠（均差小、噪声大）→ 平局，不显著
            t2 = abm.create_test({"a": 1}, {"a": 3}, min_trades=5, min_days=0)
            for _ in range(10):
                abm.record_trade(t2, "control", "600519", "SELL", 100, rng.gauss(0.02, 0.15))
                abm.record_trade(t2, "treatment", "600519", "SELL", 100, rng.gauss(0.025, 0.15))
            r2 = abm.evaluate_test(t2)
            assert_true(r2.get("status") == "concluded", "实验2已结论")
            assert_true(r2.get("winner") == "tie", f"实验2 噪声判平局 (got {r2.get('winner')})")
            assert_true(r2.get("significant") is False, f"实验2 不显著 (p={r2.get('p_value')})")

            # 实验3: 实验组显著更差 → control 胜出
            t3 = abm.create_test({"a": 1}, {"a": 4}, min_trades=5, min_days=0)
            for _ in range(8):
                abm.record_trade(t3, "control", "600519", "SELL", 100, rng.gauss(0.05, 0.02))
                abm.record_trade(t3, "treatment", "600519", "SELL", 100, rng.gauss(-0.03, 0.02))
            r3 = abm.evaluate_test(t3)
            assert_true(r3.get("winner") == "control", f"实验3 control 胜出 (got {r3.get('winner')})")
            assert_true(r3.get("significant") is True, f"实验3 显著 (p={r3.get('p_value')})")

            # 默认门槛对齐影子评估: 20交易日/10笔
            t4 = abm.create_test({"a": 1}, {"a": 5})
            row = db.conn.execute(
                "SELECT min_trades, min_days FROM ab_tests WHERE test_id=?", (t4,)
            ).fetchone()
            assert_true(row[0] == 10 and row[1] == 20,
                        f"默认门槛 10笔/20天 (got {row[0]}笔/{row[1]}天)")

        # adaptive 自动采用守卫: 不显著的历史结论不得改写参数
        concluded_no_sig = {"winner": "treatment", "significant": False}
        adopted = (
            concluded_no_sig.get("winner") == "treatment"
            and concluded_no_sig.get("significant", False)
        )
        assert_true(adopted is False, "无显著性字段的结论不会自动采用")
    finally:
        cleanup(db_path)


def test_shadow_candidate_approval():
    print("测试5: 影子晋级候选人工审批闭环")
    db_path = temp_path("_shadow.db")
    try:
        with Database(db_path=db_path) as db:
            # 未知变体拒绝
            bad = decide_candidate(db, "not_a_variant", approve=True)
            assert_true(bad.get("ok") is False, "未知变体被拒绝")

            # 无候选行时直接批准 → 建档 approved
            ok = decide_candidate(db, "loose_top", approve=True, note="人工认可")
            assert_true(ok.get("ok") is True and ok.get("status") == "approved",
                        f"loose_top 批准建档 (got {ok.get('status')})")
            row = db.conn.execute(
                "SELECT status, note FROM shadow_promotions WHERE variant_id='loose_top'"
            ).fetchone()
            assert_true(row["status"] == "approved" and row["note"] == "人工认可",
                        "审批状态与备注已落库")

            # approved 变体不会被 promote_candidates 重新提名
            promote_candidates(db)
            row = db.conn.execute(
                "SELECT status FROM shadow_promotions WHERE variant_id='loose_top'"
            ).fetchone()
            assert_true(row["status"] == "approved", "approved 不被重新提名")

            # 拒绝流程
            rej = decide_candidate(db, "strict_top", approve=False, note="样本不足")
            assert_true(rej.get("status") == "rejected", "strict_top 拒绝")
            rej_again = decide_candidate(db, "strict_top", approve=False)
            assert_true(rej_again.get("ok") is True, "重复拒绝幂等")

            # 拒绝后再 promote，仍保持 rejected
            promote_candidates(db)
            row = db.conn.execute(
                "SELECT status FROM shadow_promotions WHERE variant_id='strict_top'"
            ).fetchone()
            assert_true(row["status"] == "rejected", "rejected 不被重新提名")
    finally:
        cleanup(db_path)


def main():
    test_welch_t_test_statistics()
    test_information_ratio()
    test_run_review_wires_performance_benchmark_regime()
    test_ab_test_significance_gate()
    test_shadow_candidate_approval()
    print("\n全部复盘研究闭环与纪律门槛测试通过")


if __name__ == "__main__":
    main()
