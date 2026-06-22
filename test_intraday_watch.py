#!/usr/bin/env python3
"""
盘中轻量盯盘测试

验证早盘无交易机会时，自动盘仍会持续观察、生成踏空事件，
并且只有观察池二次确认后才允许救援扫描。
"""
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduler.auto_trader import AutoTraderState, run_auto_cycle
from scheduler.intraday_watch import filter_trade_plan_for_rescue, run_watch_cycle


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _market(limit_up_count):
    return {"limit_up": [{"code": str(i)} for i in range(limit_up_count)], "limit_down": []}


def _account(cash=800000, total=1000000, positions=0):
    return {"cash": cash, "total_assets": total, "position_count": positions, "positions": {}}


def test_watch_cycle_keeps_working_without_trade():
    watch_file = tempfile.NamedTemporaryFile(suffix="_watchlist.json", delete=False)
    watch_path = watch_file.name
    watch_file.close()
    os.unlink(watch_path)
    try:
        state = AutoTraderState(date="2026-06-09")
        first = run_watch_cycle(
            state=state,
            now=datetime(2026, 6, 9, 10, 0),
            now_ts=1000,
            watchlist_path=watch_path,
            services={
                "watch_market_snapshot": lambda: _market(10),
                "watch_candidate_pool": lambda: [{"code": "600519", "name": "贵州茅台", "score": 54, "source": ["接近门槛"]}],
                "watch_account_snapshot": lambda: _account(),
            },
        )
        second = run_watch_cycle(
            state=state,
            now=datetime(2026, 6, 9, 10, 5),
            now_ts=1300,
            watchlist_path=watch_path,
            services={
                "watch_market_snapshot": lambda: _market(35),
                "watch_candidate_pool": lambda: [{"code": "600519", "name": "贵州茅台", "score": 56, "source": ["接近门槛"]}],
                "watch_account_snapshot": lambda: _account(),
            },
        )

        assert_true(any("轻量看盘" in a for a in first["actions"]), "无交易时仍输出轻量看盘动作")
        assert_true(first["rescue_requested"] is False, "首次观察不触发救援扫描")
        assert_true(second["missed_opportunity"] is True, "市场转强且高现金生成疑似踏空")
        assert_true(second["rescue_requested"] is True, "二次确认后允许救援扫描")
        assert_true(second["eligible_codes"] == ["600519"], "只有二次确认代码进入救援白名单")
    finally:
        if os.path.exists(watch_path):
            os.unlink(watch_path)


def test_rescue_filter_allows_only_confirmed_watchlist():
    plan = {
        "orders": [
            {"code": "600519", "action": "BUY", "target_weight": 0.1, "reason": "正常买入"},
            {"code": "000001", "action": "BUY", "target_weight": 0.1, "reason": "未确认"},
            {"code": "300750", "action": "SELL", "target_weight": 0, "reason": "卖出"},
        ],
        "hold_reasons": {},
    }
    filtered = filter_trade_plan_for_rescue(plan, ["600519"])
    codes = [o["code"] for o in filtered["orders"]]
    assert_true(codes == ["600519", "300750"], "救援计划只保留确认BUY和SELL")
    buy = filtered["orders"][0]
    assert_true(abs(buy["target_weight"] - 0.04) < 1e-9, "救援买入仓位按小仓缩放")
    assert_true(filtered["hold_reasons"]["000001"] == "HOLD_RESCUE_NOT_CONFIRMED", "未确认BUY写入拒绝原因")


def test_watchlist_after_cutoff_and_cooling_no_buy():
    watch_file = tempfile.NamedTemporaryFile(suffix="_watchlist.json", delete=False)
    watch_path = watch_file.name
    watch_file.close()
    os.unlink(watch_path)
    try:
        state = AutoTraderState(date="2026-06-09")
        after_cutoff = run_watch_cycle(
            state=state,
            now=datetime(2026, 6, 9, 14, 35),
            now_ts=1000,
            watchlist_path=watch_path,
            services={
                "watch_market_snapshot": lambda: _market(35),
                "watch_candidate_pool": lambda: [{"code": "600519", "name": "贵州茅台", "score": 70}],
                "watch_account_snapshot": lambda: _account(),
            },
        )
        assert_true(len(after_cutoff["watchlist"]["items"]) == 0, "14:30后不新增观察池追入机会")
        assert_true(after_cutoff["rescue_requested"] is False, "14:30后不触发救援买入")

        state = AutoTraderState(date="2026-06-09")
        run_watch_cycle(
            state=state,
            now=datetime(2026, 6, 9, 10, 0),
            now_ts=2000,
            watchlist_path=watch_path,
            services={
                "watch_market_snapshot": lambda: _market(10),
                "watch_candidate_pool": lambda: [{"code": "600519", "name": "贵州茅台", "score": 60}],
                "watch_account_snapshot": lambda: _account(),
            },
        )
        confirmed = run_watch_cycle(
            state=state,
            now=datetime(2026, 6, 9, 10, 5),
            now_ts=2300,
            watchlist_path=watch_path,
            services={
                "watch_market_snapshot": lambda: _market(35),
                "watch_candidate_pool": lambda: [{"code": "600519", "name": "贵州茅台", "score": 62}],
                "watch_account_snapshot": lambda: _account(),
            },
        )
        cooling = run_watch_cycle(
            state=state,
            now=datetime(2026, 6, 9, 10, 10),
            now_ts=2600,
            watchlist_path=watch_path,
            services={
                "watch_market_snapshot": lambda: _market(36),
                "watch_candidate_pool": lambda: [],
                "watch_account_snapshot": lambda: _account(),
            },
        )
        assert_true(confirmed["rescue_requested"] is True, "连续两次确认才允许救援")
        assert_true(cooling["rescue_requested"] is False, "确认标的本轮消失后降级冷却不救援")
        assert_true(cooling["eligible_codes"] == [], "冷却标的不进入救援白名单")
    finally:
        if os.path.exists(watch_path):
            os.unlink(watch_path)


def test_rescue_scan_whitelist_and_llm_hold_no_buy():
    import data.snapshot as snapshot
    import execution.paper_account as paper_account
    import scheduler.pipeline as pipeline
    import strategy.llm_trader as llm_trader
    import strategy.regime_config as regime_config

    old_env = os.environ.get("USE_LLM_IN_FAST")
    originals = {
        "get_market_snapshot": snapshot.get_market_snapshot,
        "get_sentiment_snapshot": snapshot.get_sentiment_snapshot,
        "get_candidate_pool": snapshot.get_candidate_pool,
        "parallel_score": pipeline._parallel_score,
        "save_trade_plan": pipeline._save_trade_plan,
        "llm_timeout_count": pipeline._llm_timeout_count,
        "get_trade_params": regime_config.get_trade_params,
        "make_decision": llm_trader.make_decision,
        "PaperAccount": paper_account.PaperAccount,
    }

    class FakeAccount:
        cash = 1000000
        positions = {}
        position_count = 0

        def total_assets(self):
            return 1000000

    def fake_score(candidates, sentiment_scores, timeout=30):
        rows = []
        for c in candidates:
            rows.append({
                "code": c.code,
                "name": c.name,
                "composite": 68,
                "dimensions": {
                    "technical": {"score": 68, "confidence": 0.8, "detail": "测试"},
                    "sentiment": {"score": 65, "confidence": 0.7, "detail": "测试"},
                },
                "top_signal": "technical=68",
                "avg_confidence": 0.75,
                "latest_price": 10,
            })
        return rows

    try:
        os.environ["USE_LLM_IN_FAST"] = "1"
        snapshot.get_market_snapshot = lambda max_age=1800: {"limit_up": [], "limit_down": []}
        snapshot.get_sentiment_snapshot = lambda max_age=3600: {"scores": {}}
        snapshot.get_candidate_pool = lambda max_age=14400: {"candidates": []}
        pipeline._parallel_score = fake_score
        pipeline._save_trade_plan = lambda plan: None
        pipeline._llm_timeout_count = 0
        regime_config.get_trade_params = lambda regime: {"top_k": 5, "min_score": 50, "max_weight": 0.2}
        llm_trader.make_decision = lambda **kwargs: SimpleNamespace(
            action="HOLD",
            confidence=0.9,
            reason="等待二次确认",
            composite_score=68,
        )
        paper_account.PaperAccount = FakeAccount

        result = pipeline.run_scan(
            budget_seconds=120,
            candidate_codes=["600519"],
            candidate_items=[
                {"code": "600519", "name": "贵州茅台", "score": 70, "source": ["盘中观察池"]},
                {"code": "000001", "name": "平安银行", "score": 80, "source": ["不应进入"]},
            ],
        )
        assert_true([c["code"] for c in result.candidates] == ["600519"], "救援扫描只评分白名单候选")
        assert_true(result.trade_plan["orders"] == [], "LLM HOLD不会被兜底转换成BUY")
        assert_true("600519" in result.trade_plan["hold_reasons"], "LLM HOLD写入拒绝原因")
    finally:
        if old_env is None:
            os.environ.pop("USE_LLM_IN_FAST", None)
        else:
            os.environ["USE_LLM_IN_FAST"] = old_env
        snapshot.get_market_snapshot = originals["get_market_snapshot"]
        snapshot.get_sentiment_snapshot = originals["get_sentiment_snapshot"]
        snapshot.get_candidate_pool = originals["get_candidate_pool"]
        pipeline._parallel_score = originals["parallel_score"]
        pipeline._save_trade_plan = originals["save_trade_plan"]
        pipeline._llm_timeout_count = originals["llm_timeout_count"]
        regime_config.get_trade_params = originals["get_trade_params"]
        llm_trader.make_decision = originals["make_decision"]
        paper_account.PaperAccount = originals["PaperAccount"]


def test_auto_cycle_records_watch_and_rescue():
    calls = []

    def fake_stops():
        calls.append("stops")
        return {"checked": 0, "sold": 0, "trades": []}

    def fake_watch(**kwargs):
        calls.append("watch")
        return {
            "actions": ["轻量看盘: 候选1只 观察1只 确认1只 现金80% 涨停数 10->35", "疑似踏空: 市场转强/高现金/观察池确认，等待救援确认"],
            "details": {"top_watch": [{"code": "600519"}]},
            "watchlist": {"items": {"600519": {"code": "600519", "confirmations": 2}}},
            "missed_opportunity": True,
            "rescue_requested": True,
            "eligible_codes": ["600519"],
        }

    def fake_rescue(watch_result):
        calls.append("rescue")
        return {
            "scan_result": type("Scan", (), {"candidates": [1]})(),
            "exec_result": type("Exec", (), {"executed_orders": [], "errors": []})(),
            "filtered_orders": 0,
        }

    state = AutoTraderState(date="2026-06-09")
    result = run_auto_cycle(
        state=state,
        status_override="盘中",
        trading_day_override=True,
        today_override="2026-06-09",
        now_override=datetime(2026, 6, 9, 10, 30),
        now_ts_override=2000,
        scan_interval=999999,
        services={
            "check_stops_once": fake_stops,
            "run_watch_cycle": fake_watch,
            "run_rescue_scan": fake_rescue,
        },
        persist_state=False,
        record_event=False,
        notify=False,
    )
    assert_true(calls == ["stops", "watch", "rescue"], "自动循环会止损、看盘并触发救援")
    assert_true(result["state"]["watchlist_count"] == 1, "自动状态记录观察池数量")
    assert_true(result["state"]["missed_opportunity_count"] == 1, "自动状态记录踏空次数")
    assert_true(result["state"]["rescue_scan_count"] == 1, "自动状态记录救援扫描次数")
    assert_true(result["state"]["last_scan_at"] == 2000, "救援扫描会刷新扫描时间")
    assert_true(result["state"]["last_execute_at"] == 2000, "救援执行会刷新执行时间")
    assert_true(any("救援扫描" in a for a in result["actions"]), "自动循环报告包含救援扫描")


def main():
    print("盘中轻量盯盘测试")
    print("=" * 60)
    test_watch_cycle_keeps_working_without_trade()
    test_rescue_filter_allows_only_confirmed_watchlist()
    test_watchlist_after_cutoff_and_cooling_no_buy()
    test_rescue_scan_whitelist_and_llm_hold_no_buy()
    test_auto_cycle_records_watch_and_rescue()
    print("=" * 60)
    print("全部通过")


if __name__ == "__main__":
    main()
