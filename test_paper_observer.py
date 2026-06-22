#!/usr/bin/env python3
"""
正式模拟盘观察运行测试

使用临时SQLite、状态文件和假服务验证：
  - paper-observe 只跑有限轮
  - 自动事件写入正式观察库
  - 状态文件和观察日志落盘
  - 交易执行链路可通过服务注入隔离真实API
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.database import Database
from scheduler.paper_observer import format_paper_observer, run_paper_observer
from scheduler.pipeline import PipelineResult, StepResult


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    temp_paths = []
    temp_dirs = []
    try:
        db_file = tempfile.NamedTemporaryFile(suffix="_paper_observe.db", delete=False)
        db_path = db_file.name
        db_file.close()
        os.unlink(db_path)
        temp_paths.append(db_path)

        state_file = tempfile.NamedTemporaryFile(suffix="_auto_state.json", delete=False)
        state_path = state_file.name
        state_file.close()
        os.unlink(state_path)
        temp_paths.append(state_path)

        lock_file = tempfile.NamedTemporaryFile(suffix="_auto.lock", delete=False)
        lock_path = lock_file.name
        lock_file.close()
        os.unlink(lock_path)
        temp_paths.append(lock_path)

        log_file = tempfile.NamedTemporaryFile(suffix="_paper_observe.log", delete=False)
        log_path = log_file.name
        log_file.close()
        os.unlink(log_path)
        temp_paths.append(log_path)

        report_dir = tempfile.mkdtemp(prefix="quant_paper_observe_report_")
        temp_dirs.append(report_dir)

        def fake_check_stops_once():
            return {"checked": 0, "sold": 0, "trades": []}

        def fake_run_scan():
            return PipelineResult(
                date="2026-06-09",
                market_status="盘中",
                steps=[StepResult(name="scan", success=True)],
                candidates=[{"code": "600519", "name": "贵州茅台"}],
                decisions=[SimpleNamespace(code="600519", action="BUY")],
            )

        def fake_execute_trades():
            return PipelineResult(
                date="2026-06-09",
                market_status="盘中",
                steps=[StepResult(name="execute", success=True)],
                executed_orders=[{"code": "600519", "action": "BUY"}],
                order_audit=[{"code": "600519", "action": "BUY", "status": "filled"}],
            )

        services = {
            "check_stops_once": fake_check_stops_once,
            "run_scan": fake_run_scan,
            "execute_trades": fake_execute_trades,
        }

        import config
        import execution.broker as broker
        os.environ["BROKER_MODE"] = "real"
        config.BROKER_MODE = "real"
        broker.BROKER_MODE = "real"

        result = run_paper_observer(
            cycles=2,
            interval=0,
            force_scan=True,
            log_file=log_path,
            lock_file=lock_path,
            state_file=state_path,
            db_path=db_path,
            services=services,
            status_sequence=["盘中", "盘中"],
            now_sequence=[
                datetime(2026, 6, 9, 10, 0, 0),
                datetime(2026, 6, 9, 10, 1, 0),
            ],
            trading_day_override=True,
            notify=False,
            save_report=True,
            report_days=1,
            report_end_date="2026-06-09",
            report_output_dir=report_dir,
        )
        text = format_paper_observer(result)

        assert_true(result["status"] == "ok", "观察运行正常结束")
        assert_true(config.BROKER_MODE == "paper", "观察运行强制保持模拟盘配置")
        assert_true(broker.BROKER_MODE == "paper", "观察运行强制保持交易适配器模拟盘")
        assert_true(result["cycles"] == 2, "观察运行只执行指定轮数")
        assert_true(os.path.exists(state_path), "观察运行写入状态文件")
        assert_true(os.path.exists(log_path), "观察运行写入日志")
        assert_true(not os.path.exists(lock_path), "观察运行结束后释放锁")

        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        assert_true(state["loop_count"] == 2, "状态文件记录循环次数")
        assert_true(state["last_status"] == "盘中", "状态文件记录最近市场阶段")

        with open(log_path, "r", encoding="utf-8") as f:
            log_text = f.read()
        assert_true("START paper-observe" in log_text, "日志记录观察开始")
        assert_true("END exit=0" in log_text, "日志记录观察结束")
        assert_true("模拟执行: 成交1笔" in log_text, "日志记录模拟执行动作")
        assert_true("observe_report" in log_text, "日志记录观察后证据摘要")

        with Database(db_path=db_path) as db:
            events = db.get_auto_events(date="2026-06-09", limit=10)
        auto_cycle_events = [e for e in events if e.get("event_type") == "auto_cycle"]
        watch_events = [e for e in events if e.get("event_type") == "watch_cycle"]
        assert_true(len(auto_cycle_events) == 2, "观察运行写入正式自动循环事件")
        assert_true(len(watch_events) >= 1, "观察运行写入盘中轻量看盘事件")
        assert_true(
            any((e["details"].get("order_audit") or [{}])[0].get("status") == "filled" for e in auto_cycle_events),
            "自动事件保留执行审计",
        )

        evidence = result["evidence_report"]
        assert_true(evidence["metrics"]["auto_cycle_count"] == 2, "观察运行生成正式报告循环统计")
        assert_true(evidence["metrics"]["scan_count"] == 2, "观察运行生成正式报告扫描统计")
        assert_true(evidence["metrics"]["execute_count"] == 2, "观察运行生成正式报告执行统计")
        assert_true(evidence["metrics"]["order_audit_count"] == 2, "观察运行生成正式报告审计统计")
        assert_true(os.path.exists(evidence["paths"]["markdown"]), "观察运行保存Markdown报告")
        assert_true("正式模拟盘证据" in text, "格式化输出包含证据摘要")
        assert_true(result["closure_check"]["date"] == "2026-06-09", "观察运行生成日内闭环缺口诊断")
        assert_true("日内闭环缺口" in text, "格式化输出包含闭环缺口")

        print("正式模拟盘观察运行测试通过")
    finally:
        for path in temp_paths:
            for candidate in [path, f"{path}-wal", f"{path}-shm"]:
                if os.path.exists(candidate):
                    os.unlink(candidate)
        for path in temp_dirs:
            if os.path.isdir(path):
                for name in os.listdir(path):
                    os.unlink(os.path.join(path, name))
                os.rmdir(path)


if __name__ == "__main__":
    main()
