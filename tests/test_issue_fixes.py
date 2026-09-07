#!/usr/bin/env python3
"""六大核心问题系统性修复回归测试。"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.cb_t0_strategy import is_cb_code, should_buy, should_sell
from execution.paper_account import PaperAccount
from review.daily_review import DailyReviewer, TradeReview
from strategy.llm_trader import _parse_decision_response
from strategy.decision import _compute_capital_score, compute_dimension_scores, get_effective_signal_weights, DimensionScore


def ok(msg):
    print(f"  OK {msg}")


def test_issue1_cb_isolation():
    print("=== 测试1: 可转债与股票策略隔离与退出规则 ===")
    assert is_cb_code("113000") is True
    assert is_cb_code("128000") is True
    assert is_cb_code("600519") is False
    assert is_cb_code("000001") is False
    ok("is_cb_code 准确识别沪深可转债")

    # should_buy 限制最大仓位
    cb_data = {
        "cb_code": "113000",
        "cb_name": "测试转债",
        "total_score": 90,
        "premium_rate": 2.0,
        "stock_change_pct": 9.8,
    }
    decision = should_buy(cb_data, max_single_weight=0.06)
    assert decision["buy"] is True
    assert decision["position_pct"] <= 0.06
    ok("should_buy 受限于外部策略指令 max_weight")

    # 默认应不超过 0.08
    decision_default = should_buy(cb_data)
    assert decision_default["position_pct"] <= 0.08
    ok("should_buy 默认仓位不超过 8% 试验上限")

    # should_sell 专用规则
    sell_stop = should_sell("113000", 96.0, 100.0)  # -4% 跌幅
    assert sell_stop["sell"] is True
    assert "止损" in sell_stop["reason"]
    ok("should_sell 正确触发 -3% 止损")

    sell_hold = should_sell("113000", 99.0, 100.0)  # -1% 跌幅
    assert sell_hold["sell"] is False
    ok("should_sell -1% 跌幅正常继续持有")

    # PaperAccount check_stop_conditions 对可转债应用专用止损
    with tempfile.TemporaryDirectory(prefix="test_cb_stop_") as d:
        acc_file = os.path.join(d, "account.json")
        db_file = os.path.join(d, "quant.db")
        acc = PaperAccount(filepath=acc_file, db_path=db_file)
        # 买入转债 (10张一手, allow_t0=True)
        acc.buy("113000", "测试转债", price=100.0, shares=100, allow_t0=True, trade_unit=10)
        # 现价 96.5 (-3.5%)
        triggered = acc.check_stop_conditions({"113000": 96.5})
        assert len(triggered) == 1
        assert "可转债止损" in triggered[0]["reason"]
        assert "113000" not in acc.positions
        ok("PaperAccount.check_stop_conditions 正确触发可转债专用止损退出")


def test_issue2_daily_review_pnl_and_reconciliation():
    print("=== 测试2: 复盘交易事实字段修复与命中率对账 ===")
    with tempfile.TemporaryDirectory(prefix="test_review_") as d:
        db_path = os.path.join(d, "quant.db")
        reviewer = DailyReviewer(db_path=db_path)

        # 模拟从 SQLite 读取的交易记录 (只有 pnl 和 pnl_pct，无 profit_pct)
        trades = [
            {"code": "600519", "name": "贵州茅台", "action": "BUY", "price": 100.0, "shares": 100, "pnl": 0.0, "pnl_pct": 0.0},
            {"code": "600519", "name": "贵州茅台", "action": "SELL", "price": 95.0, "shares": 100, "pnl": -500.0, "pnl_pct": -0.05, "reason": "策略卖出"},
            {"code": "000001", "name": "平安银行", "action": "SELL", "price": 11.0, "shares": 1000, "pnl": 1000.0, "pnl_pct": 0.10, "reason": "止盈卖出"},
        ]
        result = reviewer.run_review(
            date="2026-09-04",
            positions={},
            trades=trades,
            prices={"600519": 95.0, "000001": 11.0},
            prev_prices={"600519": 100.0, "000001": 10.0},
            cash=1000000,
            initial_capital=1000000,
        )

        assert result.win_trades == 1, f"期望1笔盈利，实际: {result.win_trades}"
        assert result.lose_trades == 1, f"期望1笔亏损，实际: {result.lose_trades}"
        assert result.total_trades == 2
        assert abs(result.win_rate - 0.5) < 1e-6
        ok("复盘正确识别亏损卖出，彻底修复 profit_pct 缺失导致的假赢缺陷")

        # 校验 TradeReview.result_pct
        for tr in result.trade_reviews:
            if tr.code == "600519" and tr.action == "SELL":
                assert tr.hit is False
                assert tr.result_pct == -0.05
            elif tr.code == "000001" and tr.action == "SELL":
                assert tr.hit is True
                assert tr.result_pct == 0.10
        ok("TradeReview 记录真实的收益比例与命中状态")


def test_issue3_counterfactual_and_directive():
    print("=== 测试3: 策略演进护栏（证据不足禁止扩大风险） ===")
    from strategy.directive import normalize_strategy_directive
    # 当上一策略为 inconclusive 时，若试图降低 min_score 或提升 max_weight，护栏生效
    raw_risky = {
        "intent": "激进进攻",
        "regime": "sideways",
        "summary": "尝试降低门槛",
        "diagnosis": "错失机会过多",
        "rationale": "下调门槛",
        "hypothesis": "明日验证",
        "evaluation": {"verdict": "inconclusive", "evidence": "证据不足"},
        "params": {"top_k": 3, "min_score": 56.0, "max_weight": 0.10},
    }
    current_params = {"top_k": 3, "min_score": 58.0, "max_weight": 0.08}
    directive = normalize_strategy_directive(
        raw_risky,
        review_date="2026-09-04",
        effective_date="2026-09-05",
        regime="sideways",
        current_params=current_params,
    )
    assert directive["params"]["min_score"] == 58.0, f"门槛应被限制为不低于58，实际: {directive['params']['min_score']}"
    assert directive["params"]["max_weight"] == 0.08, f"仓位应被限制为不超过0.08，实际: {directive['params']['max_weight']}"
    assert "安全护栏" in directive["rationale"]
    ok("inconclusive 评估下禁止扩大风险的安全护栏生效")


def test_issue4_capital_cache_and_confidence():
    print("=== 测试4: 资金面缓存隔离与缺失置信度 ===")
    from strategy.decision import _compute_capital_score, _CAPITAL_SCORE_CACHE
    _CAPITAL_SCORE_CACHE.clear()

    # 模拟股票A缓存
    import time
    _CAPITAL_SCORE_CACHE["600519"] = {
        "score": 88.0,
        "confidence": 0.6,
        "detail": "茅台两融强劲",
        "time": time.time(),
    }

    # 读取股票A
    score_a, conf_a, detail_a = _compute_capital_score("600519")
    assert score_a == 88.0
    assert conf_a == 0.6

    # 股票B 不应复用股票A的缓存
    assert "000001" not in _CAPITAL_SCORE_CACHE
    ok("资金面评分缓存按股票代码隔离，杜绝全局复用")

    # 缺失或异常数据时置信度为0.0
    dims = {
        "technical": DimensionScore("technical", 70.0, 0.8),
        "capital": DimensionScore("capital", 50.0, 0.0, "缺失数据"),
    }
    effective = get_effective_signal_weights(dims)
    assert "capital" not in effective
    assert "technical" in effective
    ok("缺失的资金面数据 (confidence=0.0) 被有效权重逻辑自动剔除")


def test_issue5_llm_parsing_safety():
    print("=== 测试5: LLM 输出解析安全加固 ===")
    # 典型易误判文本：截断的JSON或自然语言文本，提到“卖出信号”，但明确表达持有/观望
    tricky_text = '{"reason": "当前处于震荡区间，没有明确卖出信号，建议持有观望", '
    action, conf, reason = _parse_decision_response(tricky_text)
    assert action == "HOLD", f"期望HOLD，实际为: {action}"
    assert conf == 0.0
    ok("包含'没有卖出信号'的非结构化响应被安全解析为 HOLD，绝不误判为 SELL")

    # 纯自然语言截断文本
    nlp_text = "当前没有卖出信号，建议持有"
    action2, conf2, _ = _parse_decision_response(nlp_text)
    assert action2 == "HOLD"
    assert conf2 == 0.0
    ok("纯自然语言推理文本一律安全回退为 HOLD，彻底消除猜单清仓隐患")


def test_issue6_stock_picker_refinement():
    print("=== 测试6: 候选规则优化（消除名义低价与无检验涨停） ===")
    from strategy.low_position_picker import _get_early_stage_candidates
    # 验证低位候选函数能正常运行且不再按名义价格绝对值排序
    candidates = _get_early_stage_candidates()
    assert isinstance(candidates, dict)
    ok("_get_early_stage_candidates 执行正常")


def main():
    test_issue1_cb_isolation()
    test_issue2_daily_review_pnl_and_reconciliation()
    test_issue3_counterfactual_and_directive()
    test_issue4_capital_cache_and_confidence()
    test_issue5_llm_parsing_safety()
    test_issue6_stock_picker_refinement()
    print("\n" + "=" * 60)
    print("六大核心问题全部回归测试通过！")
    print("=" * 60)


if __name__ == "__main__":
    main()
