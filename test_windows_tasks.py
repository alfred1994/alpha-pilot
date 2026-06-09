#!/usr/bin/env python3
"""
Windows无人值守任务脚本测试

验证脚本生成器会输出自动盘、Doctor、报告、状态和安装/卸载脚本，
且运行脚本默认强制 BROKER_MODE=paper。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduler.windows_tasks import generate_windows_task_scripts, run_unattended_status


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _remove_tree(path: str):
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.unlink(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    os.rmdir(path)


def main():
    temp_dir = tempfile.mkdtemp(prefix="quant_tasks_")
    try:
        paths = generate_windows_task_scripts(
            project_dir=temp_dir,
            output_dir=temp_dir,
            python_cmd="python",
            task_prefix="QuantPilotTest",
            report_days=7,
        )

        for key, path in paths.items():
            assert_true(os.path.exists(path), f"{key}脚本已生成")

        run_auto = _read(paths["run_auto"])
        restart_auto = _read(paths["restart_auto"])
        run_doctor = _read(paths["run_doctor"])
        run_report = _read(paths["run_report"])
        run_status = _read(paths["run_status"])
        install = _read(paths["install"])
        uninstall = _read(paths["uninstall"])

        assert_true("$env:BROKER_MODE = 'paper'" in run_auto, "自动盘脚本默认模拟盘")
        assert_true("main.py --auto" in run_auto, "自动盘脚本执行--auto")
        assert_true("$env:BROKER_MODE = 'paper'" in restart_auto, "自动盘重启脚本默认模拟盘")
        assert_true("main.py --watchdog" in restart_auto, "自动盘重启脚本先执行Watchdog")
        assert_true("自动盘锁|自动循环新鲜度|自动盯盘状态" in restart_auto, "自动盘重启脚本只匹配运行态故障")
        assert_true("skip restart" in restart_auto, "自动盘重启脚本会跳过非运行态故障")
        assert_true("Start-ScheduledTask -TaskName $AutoTask" in restart_auto, "自动盘重启脚本会启动Auto任务")
        assert_true("$restartExit = 0" in restart_auto and "catch" in restart_auto, "自动盘重启脚本显式处理退出码")
        assert_true("main.py --doctor" in run_doctor, "Doctor脚本执行--doctor")
        assert_true("main.py --ai-report --report-days 7" in run_report, "报告脚本使用指定回看天数")
        assert_true("main.py --ops-status --report-days 7" in run_status, "状态脚本使用指定回看天数")
        assert_true("$TaskPrefix = 'QuantPilotTest'" in install, "安装脚本使用指定任务前缀")
        assert_true("-Name 'Auto'" in install, "安装脚本注册Auto任务")
        assert_true("-Name 'Auto-Restart'" in install, "安装脚本注册Auto-Restart任务")
        assert_true("-Name 'Doctor'" in install, "安装脚本注册Doctor任务")
        assert_true("-Name 'Report'" in install, "安装脚本注册Report任务")
        assert_true("-Name 'Status'" in install, "安装脚本注册Status任务")
        assert_true("Unregister-ScheduledTask" in uninstall, "卸载脚本删除计划任务")

        log_dir = os.path.join(temp_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        for log_name, args in [
            ("auto.log", "--auto"),
            ("auto_restart.log", "restart-auto"),
            ("doctor.log", "--doctor"),
            ("ai_report.log", "--ai-report --report-days 7"),
            ("ops_status.log", "--ops-status --report-days 7"),
        ]:
            with open(os.path.join(log_dir, log_name), "w", encoding="utf-8") as f:
                f.write(f"===== 2026-06-09 09:00:00 START {args} =====\n")
                f.write("运行成功\n")
                f.write("===== 2026-06-09 09:00:01 END exit=0 =====\n")

        def fake_task_query(prefix):
            return {
                f"{prefix}-Auto": {"State": "Ready", "LastRunTime": "2026-06-09T08:45:00", "LastTaskResult": 0},
                f"{prefix}-Auto-Restart": {"State": "Ready", "LastRunTime": "2026-06-09T09:05:00", "LastTaskResult": 0},
                f"{prefix}-Doctor": {"State": "Ready", "LastRunTime": "2026-06-09T10:00:00", "LastTaskResult": 0},
                f"{prefix}-Report": {"State": "Ready", "LastRunTime": "2026-06-09T15:40:00", "LastTaskResult": 0},
                f"{prefix}-Status": {"State": "Ready", "LastRunTime": "2026-06-09T15:50:00", "LastTaskResult": 0},
            }

        status_items = run_unattended_status(
            project_dir=temp_dir,
            output_dir=temp_dir,
            task_prefix="QuantPilotTest",
            task_query_func=fake_task_query,
        )
        assert_true(not any(i.severity == "critical" for i in status_items), "无人值守巡检无critical")
        assert_true(any(i.name == "计划任务:Auto" and i.ok for i in status_items), "巡检识别Auto计划任务")
        assert_true(any(i.name == "计划任务:Auto-Restart" and i.ok for i in status_items), "巡检识别Auto-Restart计划任务")
        assert_true(any(i.name == "日志:Auto-Restart" and i.ok for i in status_items), "巡检识别Auto-Restart日志成功")
        assert_true(any(i.name == "日志:Status" and i.ok for i in status_items), "巡检识别状态日志成功")

        with open(os.path.join(log_dir, "auto.log"), "w", encoding="utf-8") as f:
            f.write("===== 2026-06-09 08:45:00 START --auto =====\n")
            f.write("自动盯盘循环运行中\n")
        running_items = run_unattended_status(
            project_dir=temp_dir,
            output_dir=temp_dir,
            task_prefix="QuantPilotTest",
            task_query_func=fake_task_query,
        )
        assert_true(
            any(
                i.name == "日志:Auto"
                and i.ok
                and "长驻--auto日志形态" in i.detail
                for i in running_items
            ),
            "巡检把无END的Auto日志识别为长驻运行中",
        )

        print("Windows无人值守任务脚本测试通过")

    finally:
        _remove_tree(temp_dir)


if __name__ == "__main__":
    main()
