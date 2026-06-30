#!/usr/bin/env python3
"""
模拟盘启动引导测试

验证启动引导会串联脚本生成、隔离演练和就绪门禁：
  - 只有warn时允许手动启动观察
  - 出现critical时返回错误
  - 输出报告包含计划任务脚本路径和演练摘要
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler.bootstrap import format_paper_bootstrap, run_paper_bootstrap


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _paths(output_dir):
    return {
        "run_auto": os.path.join(output_dir, "run_auto.ps1"),
        "run_doctor": os.path.join(output_dir, "run_doctor.ps1"),
        "run_report": os.path.join(output_dir, "run_report.ps1"),
        "run_status": os.path.join(output_dir, "run_status.ps1"),
        "install": os.path.join(output_dir, "install_tasks.ps1"),
        "uninstall": os.path.join(output_dir, "uninstall_tasks.ps1"),
    }


def _rehearsal_report(days=5):
    return {
        "metrics": {
            "days": days,
            "auto_cycle_count": days * 3,
            "closed_loop_days": days,
            "trade_count": days * 2,
            "lesson_count": days,
        },
        "adaptive": {"available": True},
    }


def main():
    temp_dir = tempfile.mkdtemp(prefix="quant_bootstrap_")
    try:
        captured = {}

        def fake_generate_scripts(**kwargs):
            captured["scripts"] = kwargs
            return _paths(kwargs["output_dir"] or temp_dir)

        def fake_rehearsal(**kwargs):
            captured["rehearsal"] = kwargs
            return {
                "db_path": os.path.join(temp_dir, "rehearsal.db"),
                "report": _rehearsal_report(kwargs["days"]),
            }

        def fake_readiness(**kwargs):
            captured["readiness"] = kwargs
            return {
                "overall": {
                    "ready": True,
                    "fully_unattended_ready": False,
                    "critical": [],
                    "warn": ["无人值守配置"],
                },
                "items": [],
            }

        result = run_paper_bootstrap(
            project_dir=temp_dir,
            task_output_dir=temp_dir,
            python_cmd="python",
            task_prefix="AlphaPilotTest",
            report_days=7,
            rehearsal_days=5,
            generate_scripts_func=fake_generate_scripts,
            run_rehearsal_func=fake_rehearsal,
            run_readiness_func=fake_readiness,
        )
        text = format_paper_bootstrap(result)

        assert_true(result["overall"]["ready"], "只有warn时允许手动启动观察")
        assert_true(result["errors"] == [], "warn不会变成错误退出")
        assert_true(captured["scripts"]["task_prefix"] == "AlphaPilotTest", "脚本生成使用任务前缀")
        assert_true(captured["rehearsal"]["days"] == 5, "演练使用指定天数")
        assert_true(captured["readiness"]["rehearsal_report"]["metrics"]["closed_loop_days"] == 5, "门禁复用本次演练报告")
        assert_true("install_tasks.ps1" in text, "报告包含安装脚本路径")
        assert_true("演练摘要: 闭环5/5天" in text, "报告包含演练摘要")

        def critical_readiness(**kwargs):
            return {
                "overall": {
                    "ready": False,
                    "fully_unattended_ready": False,
                    "critical": ["连续演练闭环"],
                    "warn": [],
                },
                "items": [],
            }

        blocked = run_paper_bootstrap(
            project_dir=temp_dir,
            task_output_dir=temp_dir,
            generate_scripts_func=fake_generate_scripts,
            run_rehearsal_func=fake_rehearsal,
            run_readiness_func=critical_readiness,
        )
        assert_true(not blocked["overall"]["ready"], "critical会阻止启动")
        assert_true(blocked["errors"] == ["连续演练闭环"], "critical进入错误列表")

        print("模拟盘启动引导测试通过")
    finally:
        for root, dirs, files in os.walk(temp_dir, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(temp_dir)


if __name__ == "__main__":
    main()
