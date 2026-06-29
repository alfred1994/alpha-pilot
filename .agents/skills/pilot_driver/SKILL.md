---
name: pilot_driver
description: Instructions for AI Agent to drive, monitor, and auto-heal the Quant Pilot trading system.
---

# A股量化AI交易员 驾驶员技能说明书 (AI Pilot Driver Skill)

本 Skill 指导 AI Agent (如 Hermes Agent) 如何作为“驾驶员”，托管、监测并自主修复量化系统“交易车” (Quant Pilot)。

---

## 1. 车辆状态日常监测 (Monitoring)

作为驾驶员，你应定时（如每 5 分钟在大盘交易时间内）执行以下命令，获取车辆状态：

```bash
python3 main.py --agent-status
```

### 状态研判逻辑：
*   **健康状态检查 (`health.ok`)**:
    *   若为 `true`，代表底层数据源、账户环境均就绪。
    *   若为 `false`，读取 `health.failed_required` 列表。如果缺失必需的依赖，需尝试修复或发出警告。
*   **看门狗状态检查 (`watchdog.ok`)**:
    *   若为 `false`，读取 `watchdog.criticals`。常见异常为“自动循环死锁”、“盘中漏扫”、“盘后漏复盘”。
    *   若出现 Watchdog Critical 异常，应尝试调用 `python3 main.py --doctor` 进行一轮自愈巡检。
*   **控制阀状态 (`control.paused`)**:
    *   若为 `true`，代表人工或 Doctor 暂停了车辆日内交易动作。可通过 Tg 向主人询问是否需要恢复驾驶。

---

## 2. 崩溃故障捕获与诊断 (Diagnosis)

一旦监听到车辆停止或者在日志/Tg 中收到 `ValueError` / `NameError` / `IndexError` 等致命异常：

1.  **拉取结构化崩溃快照**：
    ```bash
    python3 main.py --crash-info
    ```
    它会打印出如下崩溃 JSON 结构：
    ```json
    {
      "timestamp": "2026-06-29T20:15:04.567845",
      "error_type": "NameError",
      "message": "name 'api_client' is not defined",
      "file": "/path/to/quant-pilot/data/eastmoney.py",
      "line": 42,
      "traceback": "...完整堆栈..."
    }
    ```
2.  **获取上下文**：根据 `file` 和 `line`，读取出错源文件附近的代码上下文（建议前后各 30 行），锁定位故障逻辑。

---

## 3. 代码编辑与自动热修复 (Self-Repair)

1.  **LLM 分析**：利用你的代码理解和分析能力，针对 Traceback 报错设计出热修复补丁。
2.  **热补丁编辑**：直接修改 `{file}` 中发生崩溃的代码块。
3.  **注意规范**：
    *   保留原有的中文注释和 Docstring。
    *   不修改任何不相关的业务逻辑。

---

## 4. 门禁验证与热重启 (Verification & Relaunch)

修好代码后，必须通过门禁验证才能让赛车重新上路：

1.  **模块级测试**：
    *   寻找出错模块对应的测试脚本。例如，若修改了 `low_position_picker.py`，必须运行并通过：
        ```bash
        python3 test_low_position.py
        ```
2.  **就绪状态自检**：
    ```bash
    python3 main.py --health
    ```
    验证 `health.ok` 重新变为 `true`。
3.  **热重启服务**：
    测试全部通过后，重启无人值守常驻循环服务：
    ```bash
    systemctl --user restart quant-pilot-auto.service
    ```
4.  **向主人汇报**：
    在 Telegram 中向用户发送自愈喜报，描述错误原因，并附带本次热修复的 `git diff` 补丁。
