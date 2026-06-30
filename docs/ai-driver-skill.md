# AlphaPilot AI 驾驶员 Skill 使用指南

本文档面向想把自己的 AI Agent 接到 AlphaPilot 上的人。你可以把它理解成一份驾驶执照考试说明：
AlphaPilot 是交易赛车，AI Agent 是驾驶员，人类用户是车队老板。

> 仅用于研究、教育和模拟盘观察。默认保持 `BROKER_MODE=paper`，不要把未经验证的 Agent
> 直接接到真实交易执行链路上。

## 这辆车由谁负责什么

- **人类用户**：设定目标、配置密钥、决定是否部署、查看结果。
- **AI 驾驶员**：巡检状态、判断故障、执行自愈、跑测试、重启服务、汇报原因。
- **LLM Trader**：读取行情和信号上下文，生成模拟盘交易决策。
- **Watchdog**：像赛道监控一样判断自动循环是否停滞、漏扫、漏执行。
- **Doctor**：像维修技师一样尝试自愈，并在无法修复时暂停日内动作。
- **Web Cockpit**：公网只读仪表盘，只展示车辆状态，不提供公开控制面板。

## 给 AI Agent 的安装方式

推荐直接使用仓库内的 Skill 文件：

```text
.agents/skills/pilot_driver/SKILL.md
```

如果你的 Agent 支持“自定义 Skill / Instructions / System Prompt”，把该文件内容复制进去。
如果你的 Agent 支持读取仓库文件，让它在接管项目前先完整阅读该文件和本指南。

## 驾驶前检查

AI 驾驶员接管服务器后，先在项目根目录执行：

```bash
python3 main.py --agent-status
python3 main.py --paper-ready --unattended-platform linux
python3 main.py --linux-unattended-status
```

判断标准：

- `health.ok=true`：基础配置、依赖和数据源可用。
- `watchdog.ok=true`：自动驾驶循环没有明显停滞。
- `control.paused=false`：车辆没有处于人工或 Doctor 暂停状态。
- `BROKER_MODE=paper`：当前仍是模拟盘赛车，不是真实交易执行。

## 日常驾驶循环

建议 AI 驾驶员在交易时段每 3 到 5 分钟执行：

```bash
python3 main.py --agent-status
```

然后按以下逻辑处理：

1. 如果 `health.ok=true` 且 `watchdog.ok=true`，只记录状态并汇报“车辆正常巡航”。
2. 如果 `watchdog.ok=false`，读取 `watchdog.criticals`，执行 `python3 main.py --doctor`。
3. 如果 `control.paused=true`，读取暂停原因。Doctor 暂停可以在修复后恢复，人工暂停不得擅自恢复。
4. 如果账户、持仓、LLM 决策为空，先判断是否是非交易时段、缺少行情、缺少 LLM Key 或 Doctor 暂停。
5. 每次操作后汇报三个问题：现在是否在跑、为什么没交易、下一步怎么修。

## 进站维修流程

车辆崩溃时，AI 驾驶员按下面顺序处理：

```bash
python3 main.py --crash-info
```

1. 读取崩溃 JSON 中的 `error_type`、`message`、`file`、`line` 和 `traceback`。
2. 读取故障文件附近 30 行上下文，定位真实原因。
3. 只修复相关代码，不顺手重构、不删除无关逻辑。
4. 运行对应测试和基础编译：

```bash
python3 -m compileall -q web scheduler strategy execution data main.py config.py tests
python3 tests/test_web_public_dashboard.py
python3 tests/test_agent_driver_contract.py
```

5. 如果修改了自动驾驶循环，额外运行：

```bash
python3 tests/test_auto_watchdog.py
python3 tests/test_auto_doctor.py
python3 tests/test_auto_trader.py
```

6. 修复通过后复位崩溃状态并重启自动驾驶服务：

```bash
python3 main.py --resolve-crash
systemctl --user restart alpha-pilot-auto.service
systemctl --user status alpha-pilot-auto.service
```

## AI 驾驶员不得做的事

- 不要把 `BROKER_MODE` 从 `paper` 改成真实交易模式。
- 不要在公网暴露 `/api/control/*`、Token、Traceback、内部路径或 Shell 命令。
- 不要在未测试的情况下重启服务并宣称修复完成。
- 不要覆盖用户本地未提交改动。
- 不要把“LLM 无响应”当成唯一结论，要继续检查 API Key、Base URL、网络、Prompt 长度和降级逻辑。

## 汇报模板

```text
驾驶员报告：
- 当前状态：正常巡航 / Doctor 暂停 / Watchdog critical / 崩溃待修复
- 账户观察：总资产、现金、持仓数量、今日成交数量
- 决策观察：最近 LLM 决策是否有响应，若无响应说明怀疑原因
- 已执行动作：状态检查、Doctor、自愈补丁、测试、重启
- 下一步：继续观察 / 需要配置密钥 / 需要人工确认 / 已恢复自动驾驶
```

## 入口命令速查

```bash
python3 main.py --agent-status
python3 main.py --ops-status
python3 main.py --watchdog
python3 main.py --doctor
python3 main.py --crash-info
python3 main.py --resolve-crash
python3 main.py --auto-once
python3 main.py --auto
python3 main.py --paper-ready --unattended-platform linux
python3 main.py --linux-tasks
python3 main.py --linux-unattended-status
```
