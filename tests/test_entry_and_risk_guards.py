#!/usr/bin/env python3
"""入场信号与风控状态守卫回归测试。

覆盖 2026-09 的一组生产缺陷（均由实盘数据定位）：

1. 风控状态污染：execute_trade_plan 在未显式传入控制器时会把临时账户的
   INITIAL_CAPITAL 写进生产 data/circuit_breaker.json，把最大回撤伪造成
   -15%（实测净值恰为 1000000.0），使熔断阈值实际失效。
2. cmd_risk 是"查看风控状态"的只读命令，却调用 dc.update() 改写峰值与回撤。
3. 连亏统计没有死区，收盘估值的取整噪声（-0.003% ≈ -30 元）被计为亏损日，
   累积成虚假连亏并误触发半仓。
4. adaptive 维度分析拿 dict 与阈值比较，2026-07-10 起每天崩溃，
   自适应参数（更严的 70/75 门槛）再未生效。
5. ML 特征读取盘中半成品当日 bar，同一只票单日分数摆动可达 42 分。
6. 买入只看加权综合分：技术面（唯一价量维度）不达标时，舆情/情绪单独
   把分数推过线即可开仓（09-24 沪电股份技术分全天锁死 54.3，
   靠舆情 65→78 触发买入）。
7. 盘中对同一标的重复扫描 13~15 次并取当日最高分成交，等价于在噪声上取最大值。
8. 限价只挡上行不挡下行，且决策价与成交价脱节（09-22 漫步者按 10.02 打分、
   10.26 成交，白付 2.4%）。
9. 移动止损 -6% / 可转债止损 -3% 都落在噪声带内（58-62 分入场带 5 日
   MFE +5.08% / MAE -4.94%），实测 17 次止损触发仅 3 次盈利、合计 -58,531。
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from risk.drawdown import DrawdownController
from risk.system_risk import SystemRiskController
from strategy.signal_stability import SignalStabilityTracker, technical_gate_score


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


# ── 1. 风控状态与账户同源，绝不写入生产文件 ────────────────────────────
def test_risk_state_follows_account():
    from execution.broker import PaperBrokerAdapter
    from scheduler.pipeline import _risk_state_dir, _risk_state_file

    # 回归运行器会全局设置隔离变量，这里先清掉以验证"按账户推导"这条默认路径
    saved = os.environ.pop("ALPHAPILOT_RISK_STATE_DIR", None)
    try:
        with tempfile.TemporaryDirectory() as d:
            account_path = os.path.join(d, "paper_account.json")
            broker = PaperBrokerAdapter(
                account_file=account_path, db_path=os.path.join(d, "q.db"))
            state_dir = _risk_state_dir(broker)
            assert_true(state_dir == d, f"临时账户的风控目录跟随临时目录（实际{state_dir}）")
            cb = _risk_state_file(state_dir, "circuit_breaker.json")
            sr = _risk_state_file(state_dir, "system_risk.json")
            assert_true(cb == os.path.join(d, "circuit_breaker.json"), "回撤状态落在临时目录")
            assert_true(sr == os.path.join(d, "system_risk.json"), "系统风控状态落在临时目录")
            assert_true(
                os.path.abspath(cb) != os.path.abspath(
                    os.path.join(config.DATA_DIR, "circuit_breaker.json")),
                "临时账户绝不指向生产 circuit_breaker.json",
            )

        class _ProdBroker:
            account = SimpleNamespace(
                filepath=os.path.join(config.DATA_DIR, "paper_account.json"))

        assert_true(_risk_state_dir(_ProdBroker()) is None,
                    "生产账户沿用生产默认风控路径")

        # 显式隔离变量优先（回归运行器依赖它）
        with tempfile.TemporaryDirectory() as iso:
            os.environ["ALPHAPILOT_RISK_STATE_DIR"] = iso
            broker = PaperBrokerAdapter(
                account_file=os.path.join(config.DATA_DIR, "paper_account.json"),
                db_path=os.path.join(iso, "q.db"))
            assert_true(_risk_state_dir(broker) == iso, "隔离变量优先生效")
    finally:
        os.environ.pop("ALPHAPILOT_RISK_STATE_DIR", None)
        if saved is not None:
            os.environ["ALPHAPILOT_RISK_STATE_DIR"] = saved


def test_execute_trade_plan_does_not_touch_production_risk_state():
    """临时账户执行交易计划后，生产风控状态文件的 mtime/内容不变。"""
    from execution.broker import PaperBrokerAdapter
    from scheduler.pipeline import execute_trade_plan

    prod_cb = os.path.join(config.DATA_DIR, "circuit_breaker.json")
    prod_sr = os.path.join(config.DATA_DIR, "system_risk.json")
    before = {}
    for path in (prod_cb, prod_sr):
        before[path] = (
            open(path, "rb").read() if os.path.exists(path) else None,
            os.path.getmtime(path) if os.path.exists(path) else None,
        )

    saved = os.environ.get("ALPHAPILOT_RISK_STATE_DIR")
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBrokerAdapter(
            account_file=os.path.join(d, "paper_account.json"),
            db_path=os.path.join(d, "q.db"),
        )
        os.environ["ALPHAPILOT_RISK_STATE_DIR"] = d

        def quote(codes):
            return [SimpleNamespace(
                code=codes[0], price=100.0, close_prev=99.0,
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )]

        plan = {
            "date": "2026-08-08", "deadline": "23:59", "regime": "sideways",
            "orders": [{
                "code": "600000", "name": "测试", "action": "BUY", "priority": 1,
                "target_weight": 0.10, "max_price": 102.0, "ref_price": 100.0,
                "reason": "守卫测试", "score": 70, "conviction": 0.8,
                "allow_t0": True, "trade_unit": 100, "market_regime": "sideways",
                "signal_detail": "t", "dimensions": {},
            }],
        }
        try:
            execute_trade_plan(
                plan, broker=broker, realtime_func=quote, market_status="盘中",
                update_memory=False, allow_historical_plan=True,
            )
        finally:
            if saved is None:
                os.environ.pop("ALPHAPILOT_RISK_STATE_DIR", None)
            else:
                os.environ["ALPHAPILOT_RISK_STATE_DIR"] = saved

        # 临时目录里应当有自己的回撤状态
        assert_true(
            os.path.exists(os.path.join(d, "circuit_breaker.json")),
            "临时账户在临时目录写出了自己的回撤状态",
        )
        payload = json.load(open(os.path.join(d, "circuit_breaker.json"), encoding="utf-8"))
        assert_true(payload["peak_value"] == config.INITIAL_CAPITAL,
                    f"临时回撤状态记录的是临时账户净值（{payload['peak_value']}）")

    for path, (content, mtime) in before.items():
        now_content = open(path, "rb").read() if os.path.exists(path) else None
        now_mtime = os.path.getmtime(path) if os.path.exists(path) else None
        assert_true(now_content == content and now_mtime == mtime,
                    f"生产风控状态未被污染: {os.path.basename(path)}")


# ── 2. 只读查看风控状态不得改写状态 ─────────────────────────────────────
def test_preview_does_not_persist():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cb.json")
        dc = DrawdownController(state_file=path)
        dc.update(1_200_000.0, date="2026-09-01")
        assert_true(os.path.exists(path), "update() 会落盘")
        before = open(path, encoding="utf-8").read()

        # 净值腰斩：真 update 会把最大回撤推到 -50% 并触发熔断
        preview = dc.preview(600_000.0, date="2026-09-26")
        assert_true(abs(preview["current_drawdown"] + 0.5) < 1e-9,
                    f"preview 正确推演回撤（{preview['current_drawdown']:.2%}）")
        assert_true(open(path, encoding="utf-8").read() == before,
                    "preview 不改写状态文件")
        assert_true(dc.state.max_drawdown == 0.0, "preview 不污染已记录的最大回撤")
        assert_true(dc.state.is_circuit_breaker is False, "preview 不触发熔断")


# ── 3. 连亏死区：取整噪声不算亏损日 ───────────────────────────────────
def test_consecutive_loss_deadzone():
    with tempfile.TemporaryDirectory() as d:
        sr = SystemRiskController(state_file=os.path.join(d, "sr.json"))
        base = 1_000_000.0
        # 三个"亏损日"其实都是 -0.003% 量级的取整噪声
        sr.update(base, date="2026-09-21")
        for i, day in enumerate(["2026-09-22", "2026-09-23", "2026-09-24"]):
            assets = base * (1 - 0.00003 * (i + 1))
            res = sr.update(assets, date=day)
        assert_true(res["daily_pnl"] < 0, "日收益率确实为负")
        assert_true(res["consecutive_loss"] == 0,
                    f"死区内的噪声不计入连亏（实际{res['consecutive_loss']}）")
        assert_true(res["reduce_position"] is False, "噪声不触发降仓")

        # 真实亏损超过死区才计数
        res = sr.update(base * 0.99, date="2026-09-25")
        assert_true(res["consecutive_loss"] == 1, "超过死区的亏损正常计数")


def test_consecutive_loss_counts_distinct_days_only():
    with tempfile.TemporaryDirectory() as d:
        sr = SystemRiskController(state_file=os.path.join(d, "sr.json"))
        sr.update(1_000_000.0, date="2026-09-21")
        # 同一天多条盘中记录，只能算一天
        sr.update(990_000.0, date="2026-09-22")
        res = sr.update(985_000.0, date="2026-09-22")
        assert_true(res["consecutive_loss"] == 1,
                    f"同一交易日的多条记录只计一天（实际{res['consecutive_loss']}）")


# ── 4. adaptive 维度分数的类型守卫 ────────────────────────────────────
def test_adaptive_dim_score_coercion():
    from strategy.adaptive import _coerce_dim_score

    assert_true(_coerce_dim_score({"score": 41.2, "confidence": 0.5}) == 41.2,
                "完整 dimensions 形态取 score")
    assert_true(_coerce_dim_score(72.0) == 72.0, "裸数字形态直接返回")
    assert_true(_coerce_dim_score({"confidence": 0.5}) is None, "缺 score 返回 None")
    assert_true(_coerce_dim_score(None) is None, "None 返回 None")
    assert_true(_coerce_dim_score("high") is None, "字符串返回 None")
    assert_true(_coerce_dim_score(True) is None, "布尔值不被当作数字")


def test_adaptive_dimension_analysis_survives_nested_dims():
    """回归：dim_score 是 dict 时不得抛 TypeError。"""
    from strategy.adaptive import AdaptiveEngine

    trades = [
        {"action": "SELL", "result_pct": 5.0, "dimensions": {
            "technical": {"score": 70.0, "confidence": 0.5, "detail": "均线"},
            "ml": {"score": 20.0, "confidence": 0.4, "detail": "x"},
        }},
        {"action": "SELL", "result_pct": -3.0, "dimensions": {
            "technical": {"score": 30.0, "confidence": 0.5, "detail": "均线"},
            "ml": {"score": 80.0, "confidence": 0.4, "detail": "x"},
        }},
    ]
    result = AdaptiveEngine()._analyze_signal_accuracy(trades)
    assert_true("technical" in result, "技术维度被统计")
    assert_true(result["technical"]["total"] == 2, "两条样本都计入")
    # technical 70+盈利、30+亏损 两次判断都成立；ml 20 遇盈利、80 遇亏损都不成立
    assert_true(result["technical"]["accuracy"] == 1.0, "准确率按数值分数判定")
    assert_true(result["ml"]["accuracy"] == 0.0, "ML 维度同样不崩溃")
    assert_true(result["technical"]["avg_score"] == 50.0, "均分取自数值分数")


# ── 5. ML 不得读取盘中半成品当日 bar ──────────────────────────────────
def test_drop_in_progress_bar():
    import pandas as pd
    from strategy.pooled_ml import drop_in_progress_bar

    today = "2026-09-24"
    frame = pd.DataFrame([
        {"date": "2026-09-22", "close": 10.0, "volume": 1000},
        {"date": "2026-09-23", "close": 10.5, "volume": 1200},
        {"date": today, "close": 10.6, "volume": 130},
    ])
    intraday = datetime(2026, 9, 24, 10, 30)
    trimmed = drop_in_progress_bar(frame, now=intraday)
    assert_true(len(trimmed) == 2, f"盘中剔除当日半成品 bar（剩{len(trimmed)}行）")
    assert_true(str(trimmed["date"].iloc[-1]) == "2026-09-23",
                "最后一行为已收盘的交易日")

    after_close = drop_in_progress_bar(frame, now=datetime(2026, 9, 24, 15, 30))
    assert_true(len(after_close) == 3, "收盘后当日 bar 已完整，不裁剪")

    # 只有当日一根时不做裁剪，交由数据陈旧度检查处理
    only_today = pd.DataFrame([{"date": today, "close": 1.0}])
    assert_true(len(drop_in_progress_bar(only_today, now=intraday)) == 1,
                "仅当日数据时不裁剪")
    assert_true(len(drop_in_progress_bar(pd.DataFrame())) == 0, "空表安全返回")
    assert_true(drop_in_progress_bar(None) is None, "None 安全返回")


# ── 6. 买入的技术面硬门槛 ────────────────────────────────────────────
def test_technical_gate_blocks_buy():
    import strategy.decision as decision_mod
    from strategy.decision import DimensionScore, make_decision

    def dims(technical):
        out = {
            "capital": DimensionScore("capital", 80.0, 0.5, "c"),
            "sentiment": DimensionScore("sentiment", 90.0, 0.7, "s"),
            "emotion": DimensionScore("emotion", 85.0, 0.6, "e"),
            "fundamental": DimensionScore("fundamental", 50.0, 0.4, "f"),
        }
        if technical is not None:
            out["technical"] = DimensionScore("technical", technical, 0.8, "t")
        return out

    original = decision_mod.compute_dimension_scores
    try:
        decision_mod.compute_dimension_scores = lambda *a, **k: dims(75.0)
        good = make_decision("600001", "达标", buy_threshold=58)
        assert_true(good.action == "BUY", f"技术分达标时正常买入（{good.composite_score}）")

        # 复现 2026-09-22~24 的三笔：技术分 41.9 / 52.1 / 54.3，综合分靠舆情情绪过线
        for tech in (41.9, 52.1, 54.3):
            decision_mod.compute_dimension_scores = (
                lambda *a, _t=tech, **k: dims(_t))
            weak = make_decision("600002", "不达标", buy_threshold=58)
            assert_true(weak.composite_score >= 58,
                        f"技术分{tech}时综合分确实过线（{weak.composite_score}）")
            assert_true(weak.action == "HOLD",
                        f"技术分{tech}低于门槛时不得买入（实际{weak.action}）")

        decision_mod.compute_dimension_scores = lambda *a, **k: dims(None)
        no_tech = make_decision("600003", "缺技术面", buy_threshold=58)
        assert_true(no_tech.action == "HOLD", "缺失技术面时按不通过处理")
    finally:
        decision_mod.compute_dimension_scores = original


# ── 7. 信号稳定性：单次尖峰不得开仓 ──────────────────────────────────
def test_signal_stability_requires_consecutive_rounds():
    with tempfile.TemporaryDirectory() as d:
        tracker = SignalStabilityTracker(
            required_rounds=2, state_file=os.path.join(d, "st.json"))
        assert_true(not tracker.observe("2026-09-24", "600001", 65.0, 58.0),
                    "首次过线不具备开仓资格")
        assert_true(tracker.streak("600001") == 1, "记录当前轮数")
        assert_true(tracker.observe("2026-09-24", "600001", 64.0, 58.0),
                    "连续第二轮过线后具备资格")

        # 掉线一次即清零
        tracker.observe("2026-09-24", "600002", 70.0, 58.0)
        tracker.observe("2026-09-24", "600002", 50.0, 58.0)
        assert_true(tracker.streak("600002") == 0, "跌破门槛清零")
        assert_true(not tracker.observe("2026-09-24", "600002", 70.0, 58.0),
                    "清零后需重新连续两轮")

        # 换交易日重置
        tracker.observe("2026-09-24", "600001", 70.0, 58.0)
        assert_true(not tracker.observe("2026-09-25", "600001", 70.0, 58.0),
                    "交易日切换后重新累计")

        # 状态可持久化
        reloaded = SignalStabilityTracker(
            required_rounds=2, state_file=os.path.join(d, "st.json"))
        assert_true(reloaded.streak("600001") == 1, "状态可从磁盘恢复")


def test_select_llm_candidates_gates():
    from scheduler.pipeline import select_llm_candidates

    def cand(code, composite, technical):
        return {
            "code": code, "composite": composite,
            "dimensions": {"technical": {"score": technical, "confidence": 0.5}},
        }

    with tempfile.TemporaryDirectory() as d:
        tracker = SignalStabilityTracker(
            required_rounds=2, state_file=os.path.join(d, "st.json"))
        scored = [
            cand("600001", 70.0, 80.0),   # 技术达标
            cand("600002", 68.0, 45.0),   # 技术不达标
            cand("600003", 66.0, 75.0),   # 技术达标但未确认
            {"code": "600004", "composite": 72.0, "dimensions": {}},  # 缺技术面
        ]
        selected, reasons = select_llm_candidates(
            scored, top_k=3, min_score=58, max_llm_candidates=10,
            trading_date="2026-09-24", stability_tracker=tracker)
        codes = [s["code"] for s in selected]
        assert_true("600002" not in codes, "技术不达标不进 LLM")
        assert_true(reasons["600002"].startswith("HOLD_TECH_LOW"),
                    f"记录技术面拒绝原因（{reasons['600002']}）")
        assert_true("600004" not in codes, "缺技术面不进 LLM")
        assert_true(reasons["600004"].startswith("HOLD_NO_TECHNICAL"),
                    "记录缺技术面拒绝原因")
        assert_true(reasons["600003"].startswith("HOLD_UNCONFIRMED"),
                    f"未确认信号被拦下（{reasons['600003']}）")

        # 第二轮：600001/600003 连续过线后才放行
        selected2, _ = select_llm_candidates(
            scored, top_k=3, min_score=58, max_llm_candidates=10,
            trading_date="2026-09-24", stability_tracker=tracker)
        codes2 = [s["code"] for s in selected2]
        assert_true("600001" in codes2 and "600003" in codes2,
                    f"连续两轮后放行（{codes2}）")
        assert_true("600002" not in codes2, "技术不达标始终被拦")


def test_technical_gate_score_helper():
    assert_true(technical_gate_score({"technical": {"score": 55}}) == 55.0,
                "完整形态取值")
    assert_true(technical_gate_score({"technical": 61}) == 61.0, "裸数字取值")
    assert_true(technical_gate_score({"capital": 80}) is None, "缺技术分返回 None")
    assert_true(technical_gate_score(None) is None, "None 安全")
    assert_true(technical_gate_score({"technical": {"confidence": 0.5}}) is None,
                "结构异常返回 None")


# ── 8. 止损阈值必须落在噪声带之外 ────────────────────────────────────
def test_stop_thresholds_outside_noise_band():
    # 实测 58-62 分入场带 5 日 MAE 为 -4.94%、MFE 为 +5.08%
    assert_true(config.TRAILING_STOP <= -0.08,
                f"移动止损不窄于固定止损（{config.TRAILING_STOP:.0%}）")
    assert_true(abs(config.TRAILING_STOP) > 0.05,
                "移动止损宽于 5% 噪声带")
    assert_true(config.CB_STOP_LOSS <= -0.05,
                f"可转债止损不落在噪声带内（{config.CB_STOP_LOSS:.0%}）")

    from risk.stop_loss import StopLossManager
    slm = StopLossManager()
    # 最高价回撤 7%（仍在旧 -6% 触发区间内）不应被新阈值砍掉
    assert_true(slm.calc_trailing_stop_price(100.0) < 93.0,
                f"回撤7%不触发移动止损（止损价{slm.calc_trailing_stop_price(100.0):.2f}）")


def test_signal_price_drift_guard_configured():
    assert_true(0 < config.MAX_SIGNAL_PRICE_DRIFT <= 0.05,
                f"价格漂移保护已启用（{config.MAX_SIGNAL_PRICE_DRIFT:.0%}）")
    assert_true(config.MAX_SIGNAL_PRICE_DRIFT < 0.07,
                "漂移保护严于原上行硬限价的 7%")


def main():
    tests = [
        test_risk_state_follows_account,
        test_execute_trade_plan_does_not_touch_production_risk_state,
        test_preview_does_not_persist,
        test_consecutive_loss_deadzone,
        test_consecutive_loss_counts_distinct_days_only,
        test_adaptive_dim_score_coercion,
        test_adaptive_dimension_analysis_survives_nested_dims,
        test_drop_in_progress_bar,
        test_technical_gate_blocks_buy,
        test_signal_stability_requires_consecutive_rounds,
        test_select_llm_candidates_gates,
        test_technical_gate_score_helper,
        test_stop_thresholds_outside_noise_band,
        test_signal_price_drift_guard_configured,
    ]
    for test in tests:
        print(f"\n▶ {test.__name__}")
        test()
    print("\n✅ 入场信号与风控状态守卫测试全部通过")


if __name__ == "__main__":
    main()
