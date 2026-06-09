# Quant Pilot 🤖

A股量化AI交易员 —— 基于LLM推理决策的全自动量化交易系统。

## 系统架构

```
数据获取层 → 分析决策层 → 执行监控层
  │              │              │
  ├─ 华泰选股    ├─ 5维打分     ├─ 模拟盘交易
  ├─ 东方财富    ├─ LLM决策     ├─ 止损巡检
  ├─ 腾讯行情    ├─ 市场环境    ├─ 系统风控
  ├─ 长桥API     ├─ 可转债T+0   ├─ 复盘报告
  └─ AkShare     └─ 舆情分析    └─ 预警推送
```

## 核心特性

- **LLM决策引擎**：MiMo v2.5-pro 推理模型替代规则评分，自适应市场环境（牛市/熊市/震荡/反弹）
- **5维信号体系**：技术面40% + 舆情面25% + 情绪面15% + 资金面10% + 基本面10%
- **多策略支持**：A股选股 + 可转债T+0日内套利
- **系统级风控**：回撤熔断、单日亏损禁仓、连亏降仓、紧急停机
- **全自动调度**：支持 Ubuntu/Hermes systemd 用户任务和 Windows 计划任务，覆盖自动盯盘、Watchdog、盘后报告和运维状态

## 快速开始

```bash
# 克隆
git clone https://github.com/alfred1994/quant-pilot.git
cd quant-pilot

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp env.example .env
# 编辑 .env 填入API密钥

# 运行
python3 main.py --scan        # 扫描信号
python3 main.py --execute     # 执行交易
python3 main.py --review      # 每日复盘
python3 main.py --stop-check  # 止损巡检
python3 main.py --health      # 系统健康检查
python3 main.py --watchdog    # 自动盯盘运行态检查
python3 main.py --doctor      # 巡检并尝试触发一轮自愈
python3 main.py --linux-tasks # 生成Ubuntu/Hermes systemd用户任务脚本
python3 main.py --linux-unattended-status # 检查Ubuntu/Hermes无人值守状态
python3 main.py --windows-tasks # 生成Windows计划任务脚本
python3 main.py --unattended-status # 检查Windows无人值守脚本/任务/日志
python3 main.py --paper-ready --unattended-platform linux # Ubuntu上线前就绪检查
python3 main.py --paper-bootstrap --unattended-platform linux --python-cmd python3 # Ubuntu脚本+演练+上线门禁
python3 main.py --auto-pause --reason "人工观察" # 暂停盘中交易动作
python3 main.py --auto-resume # 恢复盘中交易动作
python3 main.py --auto-once   # 自动盯盘单轮测试
python3 main.py --auto        # 自动盯盘循环（模拟盘）
python3 main.py --auto-rehearse --rehearsal-days 5 # 连续多日闭环演练
python3 main.py --ai-report   # 最近5天AI交易员试运行报告
python3 main.py --ops-status  # 健康/Watchdog/报告一页汇总
```

## 自动盯盘模式

`python3 main.py --auto` 会启动模拟盘自动驾驶循环：

- 盘前/集合竞价：数据预热、市场环境识别
- 盘中：定时止损巡检、扫描信号、生成TradePlan并执行模拟盘交易
- 盘后：每日复盘、LLM教训提取、prompt进化、自适应参数更新

建议先运行：

```bash
python3 main.py --health
python3 test_auto_trader.py
python3 test_memory_feedback.py
python3 test_intraday_replay.py
python3 test_multi_day_replay.py
python3 test_adaptive_evolution.py
python3 test_ai_trader_report.py
python3 test_auto_rehearsal.py
python3 test_auto_watchdog.py
python3 test_auto_doctor.py
python3 test_auto_lock.py
python3 test_auto_control.py
python3 test_health_control.py
python3 test_ops_status.py
python3 test_paper_readiness.py
python3 test_linux_tasks.py
python3 test_windows_tasks.py
python3 test_paper_bootstrap.py
python3 main.py --auto-once
python3 main.py --auto-rehearse --rehearsal-days 5
python3 main.py --ai-report
python3 main.py --ops-status
python3 main.py --watchdog
python3 main.py --doctor
python3 main.py --linux-tasks
python3 main.py --linux-unattended-status
python3 main.py --windows-tasks
python3 main.py --unattended-status
python3 main.py --paper-ready --unattended-platform linux
python3 main.py --paper-bootstrap --unattended-platform linux --python-cmd python3
```

其中 `test_auto_trader.py` 会用固定时间和替身服务演练盘前、盘中、盘后三条路径，
不依赖真实行情、真实LLM或当前市场时段。
`test_memory_feedback.py` 会验证复盘教训能进入下一次 LLM 决策 prompt。
`test_intraday_replay.py` 会用固定 TradePlan 和固定行情验证盘中模拟成交闭环。
`test_multi_day_replay.py` 会验证买入、卖出、复盘、教训沉淀、记忆检索的连续闭环。
`test_order_audit_lessons.py` 会验证未成交/阻断订单也能沉淀为复盘教训。
`test_adaptive_evolution.py` 会验证低胜率复盘样本能自动收紧下一轮交易门槛。
`test_ai_trader_report.py` 会验证最近N天自动循环、模拟成交、LLM决策、复盘教训和自适应状态
能被聚合成可审计的AI交易员试运行报告。
`test_auto_rehearsal.py` 会验证连续多日演练能跑出盘前、盘中、盘后、成交、复盘、教训和自适应纠偏。

`python3 main.py --ai-report --report-days 5` 会在 `data/reports/` 下生成 Markdown 和 JSON 报告，
用于观察模拟盘是否跑出了“盯盘 → 决策 → 交易 → 复盘 → 参数进化”的闭环；
每日明细会标记“闭环/暂停/休市/缺扫描/缺执行/缺复盘/无可交易决策”等状态归因，
并展示执行审计（计划/成交/阻断/失败）以及最新自适应纠偏对下一轮 TopK、最低分和单票仓位上限的实际影响。
`python3 main.py --ops-status --report-days 5` 会把健康检查、Watchdog、控制开关和最近试运行报告
聚合成一页式运维状态，并给出下一步动作命令；如果出现 Watchdog critical 或必需健康检查失败，
命令会返回失败退出码。
`python3 main.py --auto-rehearse --rehearsal-days 5` 会写入隔离的 `data/rehearsal/rehearsal.db`，
并生成演练报告，不污染正式模拟盘数据库。
`python3 main.py --paper-ready --unattended-platform linux` 会把基础健康、模拟盘通道、连续演练闭环、无人值守脚本/systemd用户任务/日志、
正式模拟盘证据合成一页式上线前门禁。critical 代表不建议启动；warn 代表可以手动启动观察，
但还不算完全无人值守。
`python3 main.py --paper-bootstrap --unattended-platform linux --python-cmd python3` 会先生成默认模拟盘保护的Ubuntu/Hermes脚本，再跑隔离连续演练，
最后执行 `--paper-ready` 门禁。它不会安装计划任务，也不会连接实盘；推荐作为第一次启动模拟盘前的
总入口。
`python3 main.py --watchdog` 会检查自动循环是否停滞、盘中是否漏扫/漏执行/漏止损巡检、
`--auto` 长驻锁是否新鲜、盘后是否漏复盘，以及今日 `auto_events` 是否出现异常。critical 项会让命令返回失败退出码，
适合接入系统计划任务或外部监控。
`python3 main.py --doctor` 会先执行 Watchdog；如果发现可自愈的 critical 且交易通道仍是
`paper`，会触发一轮自动盯盘循环，然后再次复查并记录 `auto_doctor` 事件。
如果自愈后仍有 critical，Doctor 默认会自动暂停盘中交易动作，可用
`--no-auto-pause-on-failure` 关闭这个保护。

## Ubuntu/Hermes无人值守部署

目标服务器推荐 Ubuntu 22.04/24.04，使用 Hermes 用户自己的 systemd --user 任务运行。全流程默认模拟盘，
生成的运行脚本都会强制 `export BROKER_MODE=paper`。

```bash
cd /path/to/quant-pilot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

mkdir -p ~/.hermes
cp env.example ~/.hermes/.env  # 如已有Hermes环境文件，可直接编辑已有文件
chmod 600 ~/.hermes/.env

python3 main.py --linux-tasks --python-cmd "$(pwd)/.venv/bin/python"
bash data/linux_tasks/install_systemd_user.sh
sudo loginctl enable-linger "$USER"

python3 main.py --linux-unattended-status
python3 main.py --paper-ready --unattended-platform linux
```

`~/.hermes/.env` 至少应包含长桥行情凭证：

```bash
LONGPORT_APP_KEY=...
LONGPORT_APP_SECRET=...
LONGPORT_ACCESS_TOKEN=...
BROKER_MODE=paper
```

生成目录默认是 `data/linux_tasks/`，包含：

- `run_auto.sh`: 启动长驻 `python3 main.py --auto`
- `restart_auto.sh`: 每10分钟用 `--watchdog` 判断是否需要重启 Auto service
- `run_doctor.sh`: 每5分钟运行 `python3 main.py --doctor`
- `run_report.sh`: 工作日15:40生成 `python3 main.py --ai-report`
- `run_status.sh`: 工作日15:50生成 `python3 main.py --ops-status`
- `run_closure_repair.sh`: 手动闭环修复脚本
- `install_systemd_user.sh` / `uninstall_systemd_user.sh`: 安装或卸载 systemd --user 单元
- `systemd_user/`: `quant-pilot-auto.service`、`quant-pilot-auto-restart.timer`、`quant-pilot-doctor.timer` 等单元文件

常用运维命令：

```bash
systemctl --user status quant-pilot-auto.service
systemctl --user list-timers 'quant-pilot-*'
journalctl --user -u quant-pilot-auto.service -n 100 --no-pager
tail -n 100 logs/auto.log
python3 main.py --linux-unattended-status
python3 main.py --ops-status
```

停止无人值守：

```bash
bash data/linux_tasks/uninstall_systemd_user.sh
```

`python3 main.py --windows-tasks` 只生成 PowerShell 脚本，不会直接修改系统计划任务。
脚本默认输出到 `data/task_scripts/`，包含：

- `run_auto.ps1`: 工作日盘前启动 `python main.py --auto`
- `restart_auto.ps1`: 工作日盘中定时用 Watchdog 判断是否需要重启 `QuantPilot-Auto`
- `run_doctor.ps1`: 工作日盘中每5分钟运行 `python main.py --doctor`
- `run_report.ps1`: 工作日盘后生成 `python main.py --ai-report`
- `run_status.ps1`: 工作日盘后生成 `python main.py --ops-status`
- `install_tasks.ps1` / `uninstall_tasks.ps1`: 注册或删除 Windows 计划任务

审阅脚本后，用管理员 PowerShell 进入输出目录执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install_tasks.ps1
```

安装或修改脚本后运行 `python3 main.py --unattended-status`，它会只读检查：
脚本是否齐全、run脚本是否强制 `BROKER_MODE=paper`、Windows计划任务是否存在、
以及 `logs/` 下 Auto/Doctor/Report/Status 最近一次运行是否成功。`run_auto.ps1`
是长驻自动盘，日志里最近一次只有 `START --auto` 且暂无 `END` 时，会按运行中处理。

默认交易通道是 `BROKER_MODE=paper`。真实交易适配器只是预留占位，不会触发实盘下单。
常驻 `python3 main.py --auto` 会使用 `data/auto_trader.lock` 单实例锁，防止重复启动多个自动盘循环。
`python3 main.py --auto-pause --reason "..."` 会暂停盘中止损巡检、扫描和模拟执行；
`python3 main.py --auto-resume` 会恢复。暂停期间盘前预热和盘后复盘仍可继续。
`python3 main.py --health` 和 `python3 main.py --ai-report` 都会显示当前自动交易控制状态，
便于区分“系统暂停”和“策略没有机会”。
关键动作通知由 `AUTO_NOTIFY_ENABLED=1` 控制；未配置 Telegram 时会静默跳过，不影响模拟盘循环。

## 项目结构

```
quant-pilot/
├── config.py                 # 全局配置
├── main.py                   # 主入口
├── data/                     # 数据层
│   ├── realtime.py           # 腾讯实时行情
│   ├── history.py            # Baostock历史数据
│   ├── eastmoney.py          # 东方财富数据
│   ├── convertible_bond.py   # 可转债数据(AkShare)
│   └── sentiment.py          # 舆情数据
├── signals/                  # 信号层
│   ├── technical.py          # 技术信号
│   ├── capital.py            # 资金面信号
│   └── sentiment.py          # 舆情信号(LLM)
├── strategy/                 # 策略层
│   ├── llm_trader.py         # LLM决策引擎
│   ├── cb_t0_strategy.py     # 可转债T+0策略
│   ├── market_regime.py      # 市场环境识别
│   └── memory.py             # 交易记忆系统
├── execution/                # 执行层
│   └── paper_account.py      # 模拟账户
├── risk/                     # 风控层
│   ├── stop_loss.py          # 止损管理
│   ├── drawdown.py           # 回撤控制
│   └── system_risk.py        # 系统级风控
├── scheduler/                # 调度层
│   ├── pipeline.py           # 全链路管道
│   └── notifier.py           # 通知推送
└── review/                   # 复盘层
    └── daily_review.py       # 每日复盘
```

## 配置

核心参数在 `config.py`：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `INITIAL_CAPITAL` | 1,000,000 | 初始资金 |
| `MAX_POSITIONS` | 5 | 最大持仓数 |
| `STOP_LOSS` | -8% | 固定止损 |
| `MAX_DRAWDOWN` | -15% | 回撤熔断阈值 |
| `DAILY_LOSS_LIMIT` | -5% | 单日亏损禁仓 |
| `CONSECUTIVE_LOSS_DAYS` | 3 | 连亏降仓天数 |
| `CB_T0_ENABLED` | True | 可转债T+0开关 |

## 数据源

| 数据 | 主源 | 备用 |
|------|------|------|
| A股行情 | 腾讯财经 | 长桥OpenAPI |
| 选股 | 华泰Skills | 东方财富 |
| 历史K线 | Baostock | 长桥OpenAPI |
| 可转债 | AkShare(集思录) | 东方财富 |
| 舆情 | 东方财富新闻 | 微博 |

## 风控体系

- **个股止损**：固定-8% / ATR动态 / 移动止损（最高点回撤-3%）
- **回撤熔断**：总资产回撤>15% → 清仓+暂停交易
- **单日亏损**：单日亏损>5% → 次日禁止开新仓
- **连亏降仓**：连续3日亏损 → 仓位降至50%
- **系统停机**：异常时紧急停止自动交易

## License

MIT

---
*仅供学习研究，不构成投资建议。*
