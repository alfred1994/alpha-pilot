#!/usr/bin/env python3
"""
自动盯盘交易员测试

验证盘前、盘中、盘后三条自动循环路径可以在不依赖真实市场时间、
真实行情API和真实LLM的情况下稳定演练。
"""
import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduler.auto_trader import AutoTraderState, run_auto_cycle


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def test_premarket_cycle():
    calls = []

    def fake_prefetch():
        calls.append("prefetch")
        return {"ok": True}

    def fake_detect_regime():
        calls.append("detect_regime")
        return SimpleNamespace(regime="bull", confidence=0.72)

    today = "2026-06-09"
    state = AutoTraderState(date=today)
    result = run_auto_cycle(
        state=state,
        status_override="盘前",
        trading_day_override=True,
        today_override=today,
        now_override=datetime(2026, 6, 9, 8, 50),
        now_ts_override=1000,
        services={
            "prefetch": fake_prefetch,
            "detect_regime": fake_detect_regime,
        },
        persist_state=False,
        record_event=False,
    )

    assert_true(calls == ["prefetch", "detect_regime"], "盘前会执行数据预热和市场环境识别")
    assert_true(result["state"]["last_prefetch_date"] == today, "盘前预热日期会写入状态")
    assert_true(result["state"]["last_regime_date"] == today, "市场环境识别日期会写入状态")


def test_intraday_cycle():
    calls = []
    notifications = []

    def fake_check_stops_once():
        calls.append("check_stops_once")
        return {"checked": 2, "sold": 1, "trades": [{"code": "600519"}]}

    def fake_run_scan():
        calls.append("run_scan")
        return SimpleNamespace(candidates=[1, 2, 3], decisions=["BUY", "HOLD"])

    def fake_run_watch_cycle(**kwargs):
        calls.append("run_watch_cycle")
        return {
            "actions": ["轻量看盘: 候选2只 观察1只 确认0只 现金50% 涨停数 10->12"],
            "details": {"top_watch": []},
            "watchlist": {"items": {"600519": {"code": "600519"}}},
            "missed_opportunity": False,
            "rescue_requested": False,
            "eligible_codes": [],
        }

    def fake_execute_trades():
        calls.append("execute_trades")
        return SimpleNamespace(executed_orders=[{"code": "600519"}], risk_triggered=[], errors=[])

    def fake_notify(**kwargs):
        notifications.append(kwargs)
        return True

    today = "2026-06-09"
    state = AutoTraderState(date=today)
    result = run_auto_cycle(
        state=state,
        force_scan=True,
        status_override="盘中",
        trading_day_override=True,
        today_override=today,
        now_override=datetime(2026, 6, 9, 10, 0),
        now_ts_override=2000,
        services={
            "check_stops_once": fake_check_stops_once,
            "run_watch_cycle": fake_run_watch_cycle,
            "run_scan": fake_run_scan,
            "execute_trades": fake_execute_trades,
            "send_auto_cycle_report": fake_notify,
        },
        persist_state=False,
        record_event=False,
    )

    assert_true(calls == ["check_stops_once", "run_watch_cycle", "run_scan", "execute_trades"], "盘中会巡检止损、轻量看盘、扫描并执行模拟交易")
    assert_true(any("止损巡检" in action for action in result["actions"]), "盘中报告包含止损巡检结果")
    assert_true(any("轻量看盘" in action for action in result["actions"]), "盘中报告包含轻量看盘结果")
    assert_true(any("模拟执行" in action for action in result["actions"]), "盘中报告包含模拟执行结果")
    assert_true(result["state"]["last_scan_at"] == 2000, "盘中扫描时间会写入状态")
    assert_true(result["state"]["last_watch_at"] == 2000, "盘中看盘时间会写入状态")
    assert_true(result["state"]["last_execute_at"] == 2000, "模拟执行时间会写入状态")
    assert_true(result["notified"] is True and len(notifications) == 1, "盘中关键动作会触发通知")


def test_after_market_cycle():
    calls = []

    def fake_run_review():
        calls.append("run_review")
        return SimpleNamespace(steps=["base", "adaptive", "llm"], errors=[])

    today = "2026-06-09"
    state = AutoTraderState(date=today)
    result = run_auto_cycle(
        state=state,
        status_override="盘后",
        trading_day_override=True,
        today_override=today,
        now_override=datetime(2026, 6, 9, 15, 10),
        now_ts_override=3000,
        after_review_time_override=True,
        services={"run_review": fake_run_review},
        persist_state=False,
        record_event=False,
    )

    assert_true(calls == ["run_review"], "盘后到复盘窗口会执行复盘进化")
    assert_true(any("盘后复盘进化" in action for action in result["actions"]), "盘后报告包含复盘进化结果")
    assert_true(result["state"]["last_review_date"] == today, "盘后复盘日期会写入状态")


def test_notification_filter():
    from scheduler.notifier import should_notify_auto_cycle

    assert_true(
        should_notify_auto_cycle(["模拟执行: 成交1笔 风控0项 错误0项"]),
        "模拟成交会触发通知",
    )
    assert_true(
        should_notify_auto_cycle(["止损巡检: 持仓2只 卖出1笔"]),
        "止损卖出会触发通知",
    )
    assert_true(
        not should_notify_auto_cycle(["盘后: 等待复盘窗口或今日已复盘"]),
        "空闲盘后不会触发通知",
    )


def main():
    print("自动盯盘交易员测试")
    print("=" * 60)
    test_premarket_cycle()
    test_intraday_cycle()
    test_after_market_cycle()
    test_notification_filter()
    print("=" * 60)
    print("全部通过")


if __name__ == "__main__":
    main()
