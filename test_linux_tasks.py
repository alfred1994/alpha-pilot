#!/usr/bin/env python3
"""
Ubuntu/Hermes无人值守任务脚本测试

验证Linux脚本生成器会输出自动盘、自动重启、Doctor、报告、状态和
systemd --user 安装/卸载脚本，且所有运行脚本默认强制 BROKER_MODE=paper。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduler.linux_tasks import generate_linux_task_scripts, run_linux_unattended_status


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
    temp_dir = tempfile.mkdtemp(prefix="quant_linux_tasks_")
    try:
        paths = generate_linux_task_scripts(
            project_dir=temp_dir,
            output_dir=temp_dir,
            python_cmd="python3",
            service_prefix="quant-pilot-test",
            report_days=7,
        )

        for key, path in paths.items():
            assert_true(os.path.exists(path), f"{key}已生成")

        for key in [
            "run_auto",
            "restart_auto",
            "run_doctor",
            "run_report",
            "run_status",
            "run_closure_repair",
            "install",
            "uninstall",
        ]:
            assert_true(os.access(paths[key], os.X_OK), f"{key}脚本可执行")

        run_auto = _read(paths["run_auto"])
        restart_auto = _read(paths["restart_auto"])
        run_doctor = _read(paths["run_doctor"])
        run_report = _read(paths["run_report"])
        run_status = _read(paths["run_status"])
        run_closure_repair = _read(paths["run_closure_repair"])
        install = _read(paths["install"])
        auto_service = _read(paths["auto_service"])
        auto_restart_timer = _read(paths["auto_restart_timer"])
        doctor_timer = _read(paths["doctor_timer"])

        assert_true('source "$HERMES_ENV_FILE"' in run_auto, "自动盘脚本加载Hermes环境")
        assert_true("export BROKER_MODE=paper" in run_auto, "自动盘脚本默认模拟盘")
        assert_true("main.py --auto" in run_auto, "自动盘脚本执行--auto")
        assert_true("export BROKER_MODE=paper" in restart_auto, "自动盘重启脚本默认模拟盘")
        assert_true("main.py --watchdog" in restart_auto, "自动盘重启脚本先执行Watchdog")
        assert_true("自动盘锁|自动循环新鲜度|自动盯盘状态" in restart_auto, "自动盘重启脚本只匹配运行态故障")
        assert_true('systemctl --user restart "$AUTO_UNIT"' in restart_auto, "自动盘重启脚本重启systemd服务")
        assert_true("main.py --doctor" in run_doctor, "Doctor脚本执行--doctor")
        assert_true("main.py --ai-report --report-days 7" in run_report, "报告脚本使用指定回看天数")
        assert_true("main.py --ops-status --report-days 7" in run_status, "状态脚本使用指定回看天数")
        assert_true("main.py --closure-repair" in run_closure_repair, "闭环修复脚本已生成")
        assert_true("systemctl --user enable --now quant-pilot-test-auto.service" in install, "安装脚本启用Auto服务")
        assert_true("systemctl --user enable --now quant-pilot-test-auto-restart.timer" in install, "安装脚本启用Auto-Restart timer")
        assert_true("Restart=always" in auto_service, "Auto服务带常驻重启保护")
        assert_true("OnUnitActiveSec=10min" in auto_restart_timer, "Auto-Restart timer每10分钟检查")
        assert_true("OnUnitActiveSec=5min" in doctor_timer, "Doctor timer每5分钟检查")

        log_dir = os.path.join(temp_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        for log_name, args in [
            ("auto_restart.log", "restart-auto"),
            ("doctor.log", "--doctor"),
            ("ai_report.log", "--ai-report --report-days 7"),
            ("ops_status.log", "--ops-status --report-days 7"),
        ]:
            with open(os.path.join(log_dir, log_name), "w", encoding="utf-8") as f:
                f.write(f"===== 2026-06-09 09:00:00 START {args} =====\n")
                f.write("运行成功\n")
                f.write("===== 2026-06-09 09:00:01 END exit=0 =====\n")
        with open(os.path.join(log_dir, "auto.log"), "w", encoding="utf-8") as f:
            f.write("===== 2026-06-09 08:45:00 START --auto =====\n")
            f.write("自动盯盘循环运行中\n")

        def fake_systemd_query(prefix):
            return {
                f"{prefix}-auto.service": {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "running",
                    "UnitFileState": "enabled",
                    "Result": "success",
                },
                f"{prefix}-auto-restart.timer": {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "waiting",
                    "UnitFileState": "enabled",
                    "Result": "success",
                },
                f"{prefix}-doctor.timer": {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "waiting",
                    "UnitFileState": "enabled",
                    "Result": "success",
                },
                f"{prefix}-report.timer": {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "waiting",
                    "UnitFileState": "enabled",
                    "Result": "success",
                },
                f"{prefix}-status.timer": {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "waiting",
                    "UnitFileState": "enabled",
                    "Result": "success",
                },
            }

        status_items = run_linux_unattended_status(
            project_dir=temp_dir,
            output_dir=temp_dir,
            service_prefix="quant-pilot-test",
            systemd_query_func=fake_systemd_query,
        )
        assert_true(not any(i.severity == "critical" for i in status_items), "Linux无人值守巡检无critical")
        assert_true(any(i.name == "systemd状态:quant-pilot-test-auto.service" and i.ok for i in status_items), "巡检识别Auto systemd服务")
        assert_true(any(i.name == "systemd状态:quant-pilot-test-auto-restart.timer" and i.ok for i in status_items), "巡检识别Auto-Restart timer")
        assert_true(
            any(
                i.name == "日志:Auto"
                and i.ok
                and "长驻--auto日志形态" in i.detail
                for i in status_items
            ),
            "巡检把无END的Auto日志识别为长驻运行中",
        )

        print("Ubuntu/Hermes无人值守任务脚本测试通过")
    finally:
        _remove_tree(temp_dir)


if __name__ == "__main__":
    main()
