#!/usr/bin/env python3
"""
AI交易员试运行报告测试

使用临时SQLite构造自动盯盘、模拟成交、LLM决策、复盘教训和
自适应状态，验证报告能聚合出完整闭环证据。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from review.ai_trader_report import generate_ai_trader_report
from scheduler.control import pause_auto_trader


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    temp_paths = []
    output_dir = tempfile.mkdtemp(prefix="quant_ai_report_")
    try:
        db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
        db_path = db_file.name
        db_file.close()
        os.unlink(db_path)
        temp_paths.append(db_path)

        control_file = tempfile.NamedTemporaryFile(suffix="_auto_control.json", delete=False)
        control_path = control_file.name
        control_file.close()
        os.unlink(control_path)
        temp_paths.append(control_path)
        pause_auto_trader("报告测试暂停", control_file=control_path)

        with Database(db_path=db_path) as db:
            db.save_account_state({
                "initial_capital": 1000000,
                "cash": 880000,
                "total_assets": 1012000,
                "position_count": 1,
                "positions": {"600519": {"shares": 1000}},
                "updated_at": "2026-06-05T15:05:00",
            })
            db.insert_market_regime({
                "date": "2026-06-05",
                "regime": "sideways",
                "confidence": 0.7,
                "indicators": "{}",
                "llm_reasoning": "报告测试市场环境",
            })
            db.conn.execute("""
                CREATE TABLE IF NOT EXISTS active_prompt_hints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hint TEXT NOT NULL,
                    source TEXT DEFAULT 'evolution',
                    active INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL
                )
            """)
            db.conn.execute(
                "INSERT INTO active_prompt_hints (hint, source, active, created_at) VALUES (?, 'test', 1, ?)",
                ("报告测试prompt建议: 低胜率阶段避免追高。", "2026-06-05T15:14:00"),
            )
            db.conn.commit()
            db.insert_auto_event({
                "date": "2026-06-04",
                "event_type": "auto_cycle",
                "status": "盘中",
                "actions": [
                    "自动盯盘已暂停: 报告测试暂停",
                    "盘中交易动作跳过: 止损巡检/扫描/模拟执行",
                ],
                "details": {"paused": True, "pause_reason": "报告测试暂停"},
                "created_at": "2026-06-04T10:00:00",
            })
            db.insert_auto_event({
                "date": "2026-06-05",
                "event_type": "auto_cycle",
                "status": "盘中",
                "actions": [
                    "止损巡检: 持仓1只 卖出0笔",
                    "盘中扫描: 候选3只 决策2条",
                    "模拟执行: 成交1笔 风控0项 错误0项",
                    "执行审计: 计划2笔 阻断/跳过1笔 失败0笔",
                ],
                "details": {
                    "order_audit": [
                        {
                            "code": "600519",
                            "name": "贵州茅台",
                            "action": "BUY",
                            "status": "filled",
                            "reason": "模拟买入成交",
                        },
                        {
                            "code": "000001",
                            "name": "平安银行",
                            "action": "BUY",
                            "status": "blocked",
                            "reason": "超过硬限价",
                        },
                    ],
                },
                "created_at": "2026-06-05T10:00:00",
            })
            db.insert_auto_event({
                "date": "2026-06-05",
                "event_type": "auto_cycle",
                "status": "盘后",
                "actions": ["盘后复盘进化: 步骤3项 错误0项"],
                "created_at": "2026-06-05T15:10:00",
            })
            db.insert_auto_event({
                "date": "2026-06-06",
                "event_type": "auto_cycle",
                "status": "休市",
                "actions": ["休市: 跳过交易动作"],
                "created_at": "2026-06-06T09:30:00",
            })
            db.insert_trade({
                "code": "600519",
                "name": "贵州茅台",
                "action": "BUY",
                "price": 10,
                "shares": 1000,
                "amount": 10000,
                "reason": "报告测试买入",
                "created_at": "2026-06-05T10:01:00",
            })
            db.insert_llm_decision({
                "code": "600519",
                "date": "2026-06-05",
                "action": "BUY",
                "reasoning": "报告测试决策",
                "confidence": 0.82,
                "outcome": "win",
                "outcome_pct": 3.2,
                "created_at": "2026-06-05T10:00:30",
            })
            db.insert_lesson({
                "date": "2026-06-05",
                "category": "buy",
                "content": "报告测试教训: 高置信度买入后继续观察成交质量。",
                "importance": 4,
                "market_regime": "sideways",
                "created_at": "2026-06-05T15:12:00",
            })
            db.save_review_snapshot("2026-06-05", {
                "date": "2026-06-05",
                "trade_reviews": [{"code": "600519", "result_pct": 3.2}],
            })
            db.save_adaptive_state({
                "last_update": "2026-06-05T15:13:00",
                "current_buy_threshold": 62,
                "current_min_score": 57,
                "current_top_k_delta": -1,
                "current_position_scale": 0.8,
                "adjustments": [{
                    "date": "2026-06-05",
                    "adjustments": [{"type": "position_scale", "reason": "胜率低，降低下一轮单笔仓位"}],
                }],
            })

        report = generate_ai_trader_report(
            days=3,
            end_date="2026-06-06",
            db_path=db_path,
            output_dir=output_dir,
            control_file=control_path,
            save=True,
        )

        metrics = report["metrics"]
        assert_true(metrics["auto_cycle_count"] == 4, "报告统计自动循环事件")
        assert_true(metrics["trade_count"] == 1, "报告统计模拟成交")
        assert_true(metrics["order_audit_count"] == 2, "报告统计订单审计")
        assert_true(metrics["order_filled_count"] == 1, "报告统计审计成交")
        assert_true(metrics["order_blocked_count"] == 1, "报告统计审计阻断")
        assert_true(metrics["order_failed_count"] == 0, "报告统计审计失败")
        assert_true(metrics["llm_decision_count"] == 1, "报告统计LLM决策")
        assert_true(metrics["llm_win_rate"] == 1.0, "报告统计已验证LLM胜率")
        assert_true(metrics["lesson_count"] == 1, "报告统计复盘教训")
        assert_true(metrics["closed_loop_days"] == 1, "报告识别完整闭环日")
        assert_true(metrics["paused_days"] == 1, "报告识别暂停日")
        assert_true(metrics["holiday_days"] == 1, "报告识别休市日")
        assert_true(report["adaptive"]["current_top_k_delta"] == -1, "报告包含TopK自适应偏移")
        assert_true(report["adaptive"]["current_position_scale"] == 0.8, "报告包含仓位自适应缩放")
        assert_true(report["latest_regime"]["regime"] == "sideways", "报告包含最新市场环境")
        assert_true(report["next_trade_params"]["current"]["top_k"] == 2, "报告计算下一轮TopK")
        assert_true(
            abs(report["next_trade_params"]["current"]["max_weight"] - 0.096) < 0.000001,
            "报告计算下一轮仓位上限",
        )
        assert_true(len(report["prompt_hints"]) == 1, "报告包含生效prompt建议")
        assert_true(report["control"]["paused"], "报告包含自动交易控制状态")
        assert_true("自动交易控制: 暂停" in report["markdown"], "Markdown包含暂停状态")
        assert_true("## 纠偏应用" in report["markdown"], "Markdown包含纠偏应用段")
        assert_true("执行审计: 计划2笔，成交1笔，阻断/跳过1笔，失败0笔" in report["markdown"], "Markdown包含执行审计摘要")
        assert_true("下一轮参数: Top2，最低分60，单票仓位上限9.6%" in report["markdown"], "Markdown包含下一轮参数影响")
        assert_true("Prompt进化建议: 1条生效" in report["markdown"], "Markdown包含prompt进化建议数量")
        assert_true("状态归因: 无循环0天，仅复盘0天，暂停1天，休市1天" in report["markdown"], "Markdown包含状态归因")
        assert_true("| 2026-06-04 | 盘中 | 暂停 |" in report["markdown"], "每日明细包含暂停诊断")
        assert_true("| 2026-06-05 | 盘中,盘后 | 闭环 | 2 | 1 | 1 | 2 | 1 | 0 |" in report["markdown"], "每日明细包含执行审计列")
        assert_true("| 2026-06-06 | 休市 | 休市 |" in report["markdown"], "每日明细包含休市诊断")
        assert_true("完整闭环日: 1/3天" in report["markdown"], "Markdown包含闭环摘要")
        assert_true(os.path.exists(report["paths"]["markdown"]), "Markdown报告已落盘")
        assert_true(os.path.exists(report["paths"]["json"]), "JSON报告已落盘")

        with Database(db_path=db_path) as db:
            for i in range(1005):
                db.insert_auto_event({
                    "date": "2026-06-07",
                    "event_type": "auto_cycle",
                    "status": "盘中",
                    "actions": ["止损巡检: 持仓0只 卖出0笔"],
                    "created_at": f"2026-06-07T10:{i // 60:02d}:{i % 60:02d}",
                })
            db.insert_auto_event({
                "date": "2026-06-07",
                "event_type": "watch_cycle",
                "status": "盘中",
                "actions": ["轻量看盘: 候选10只 观察10只 确认10只 现金100% 涨停数 0->0"],
                "created_at": "2026-06-07T13:45:00",
            })

        large_report = generate_ai_trader_report(
            days=1,
            end_date="2026-06-07",
            db_path=db_path,
            output_dir=output_dir,
            control_file=control_path,
            save=False,
        )
        assert_true(large_report["metrics"]["auto_cycle_count"] == 1005, "报告不会截断超过1000条自动循环")
        assert_true(large_report["metrics"]["watch_count"] == 1, "报告不会漏掉高事件量后的轻量看盘")

        print("AI交易员试运行报告测试通过")

    finally:
        for path in temp_paths:
            for candidate in [path, f"{path}-wal", f"{path}-shm"]:
                if os.path.exists(candidate):
                    os.unlink(candidate)
        if os.path.isdir(output_dir):
            for name in os.listdir(output_dir):
                os.unlink(os.path.join(output_dir, name))
            os.rmdir(output_dir)


if __name__ == "__main__":
    main()
