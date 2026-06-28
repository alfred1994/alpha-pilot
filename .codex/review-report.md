# Quant Pilot Linux/Hermes 审核报告

- 日期：2026-06-28
- 执行者：Codex
- 工作区：`D:\workspace\quant-pilot`
- 审核目标：提交当前本地改动，拉取远端 `origin/main` 最新代码，并从产品体验和技术架构角度评估是否适合运行在 Linux 服务器的 Hermes agent 中。

## 0. 修复跟进（2026-06-29）

本报告初版列出的主要阻断项已做代码修复并纳入后续提交范围：

- 已将交易执行层自适应最低分与低位候选池 `PICKER_MIN_SCORE=20` 解耦，新增 `TRADE_ADAPTIVE_MIN_SCORE_BASE=55`，避免下一轮交易阈值被顶到 75。
- 已修正 `test_auto_rehearsal.py`，只统计 `event_type=auto_cycle` 的主循环事件，兼容新增 `watchlist_update` / `watch_cycle`。
- 已修正 `scheduler/linux_tasks.py`，生成脚本时保留 Linux 目标路径、兼容 `venv`/`.venv`，并恢复自动盘残留锁清理逻辑。
- 已重新生成 `data/linux_tasks/`，使已提交脚本与生成器保持一致。
- 已修正 `start_auto.sh`、`scripts/run_quant.sh`、`scripts/realtime_trader.sh`、`scripts/quant-realtime.service` 中的旧项目路径和环境加载方式。

修复后的验证结果以本次最终提交说明为准。

## 1. Git 更新结果

- 已提交本地改动：`f4c4171 chore: add OCI migration script state ignore`
  - `.gitignore` 新增 `.migration/`
  - `scripts/migrate-oci-phx.ps1` 新增 OCI PHX 迁移脚本
- 已执行 `git pull --rebase origin main`，无冲突。
- 当前 HEAD：`b70e1d6 chore: add OCI migration script state ignore`
- 当前远端：`origin/main = 748f1d1 Integrate a-stock-data v3.3 market data fixes`
- 当前状态：本地 `main` 相对 `origin/main` ahead 1，工作区在生成本报告前为干净状态。

## 2. 验证结果

| 命令 | 结果 | 备注 |
| --- | --- | --- |
| `python --version` | 通过 | 本机为 Python 3.14.5；项目文档目标为 Python 3.12 |
| `python -m compileall -q .` | 超时 | 全仓编译 124s 超时，可能遍历了非源码目录 |
| `python -m compileall -q main.py config.py data execution portfolio realtime review risk scheduler signals strategy` | 通过 | 源码目录级编译通过 |
| `python test_linux_tasks.py` | 通过 | Linux/Hermes systemd 脚本生成测试通过 |
| `python test_paper_readiness.py` | 通过 | 就绪门禁测试通过 |
| `python test_paper_bootstrap.py` | 通过 | 启动引导测试通过 |
| `python test_auto_watchdog.py` | 通过 | Watchdog 运行态测试通过 |
| `python test_auto_doctor.py` | 通过 | Doctor 自愈测试通过 |
| `python test_auto_trader.py` | 通过 | 自动盘主循环测试通过；日志里有测试注入异常 |
| `python test_ops_status.py` | 通过 | 运维状态汇总测试通过 |
| `python test_paper_observer.py` | 通过 | 正式模拟盘观察测试通过 |
| `python test_closure_check.py` | 通过 | 日内闭环缺口诊断测试通过 |
| `python test_closure_repair.py` | 通过 | 闭环自愈测试通过 |
| `python test_auto_rehearsal.py` | 失败 | 隔离库实际写入 15 条事件，不再是测试断言的 9 条；新增 `watchlist_update`/`watch_cycle` 事件后测试未更新 |
| `python test_ai_trader_report.py` | 失败 | `PICKER_MIN_SCORE=20` 与自适应公式叠加后把下一轮 `min_score` 推到 75，测试仍期望 60 |
| `python -m pytest -q` | 未执行 | 当前环境未安装 `pytest` |
| `python main.py --health` | 通过 | 基础依赖、SQLite、paper 交易通道正常；XIAOMI/HT/Telegram 未配置，将降级 |

## 3. Findings

### P0

未发现必须立即阻断部署的 P0 问题。

### P1 - 自适应分数公式与远端新阈值冲突，可能让自动盘过度保守

- 证据：
  - `config.py:53` 将 `PICKER_MIN_SCORE` 降为 `20`。
  - `strategy/regime_config.py:282-287` 使用 `result["min_score"] + adaptive_min_score - PICKER_MIN_SCORE` 叠加自适应最低分。
  - `test_ai_trader_report.py` 插入 `current_min_score=57` 后，`get_trade_params("sideways")` 实际输出日志为 `最低75`，触发测试失败。
- 影响：
  - 震荡市基础 `min_score=58`，自适应 `current_min_score=57`，在新 `PICKER_MIN_SCORE=20` 下会被推到 `58 + 57 - 20 = 95`，再被上限钳到 `75`。
  - 对 Hermes 无人值守模拟盘而言，这会显著减少可交易标的，用户看到“系统在跑但长期无交易”的概率上升。
- 建议：
  - 明确 `current_min_score` 的语义：如果它是绝对阈值，应直接覆盖或与环境阈值取 max；如果是相对偏移，应命名为 delta。
  - 调整 `test_ai_trader_report.py` 期望，并补一个覆盖 `PICKER_MIN_SCORE=20` 的参数回归测试。

### P1 - Linux 脚本生成器与已提交脚本行为漂移

- 证据：
  - `scheduler/linux_tasks.py:84-124` 的 `_runner_script()` 只 `cd`、加载 `~/.hermes/.env`、设置 `BROKER_MODE=paper`，没有激活 venv，也没有残留锁清理。
  - `data/linux_tasks/run_auto.sh:12-15` 已提交脚本会激活 `venv/bin/activate`。
  - `data/linux_tasks/run_auto.sh:27-35` 已提交脚本会按 pid 清理残留锁。
  - 生成一致性检查中，归一化行尾后仍有 12 个 `data/linux_tasks` 文件与当前生成器产物不同。
- 影响：
  - README 推荐 `python3 main.py --linux-tasks --python-cmd "$(pwd)/.venv/bin/python"`，这能绕过 venv 激活问题；但如果用户按默认 `python3` 生成，systemd 可能使用系统 Python，服务器上依赖缺失时服务启动失败。
  - 仓库内脚本和生成器不一致，后续在 Linux 上重新生成会覆盖掉已提交脚本里的 venv/锁清理增强，运维行为不可预测。
- 建议：
  - 让生成器成为唯一来源：把 venv 激活策略、残留锁清理策略纳入 `scheduler/linux_tasks.py`，然后重新生成并提交 `data/linux_tasks`。
  - README 保留 `--python-cmd "$(pwd)/.venv/bin/python"`，同时在 `--linux-unattended-status` 检查脚本里实际 Python 路径和依赖可用性。

### P1 - 旧入口仍指向旧项目名或旧环境文件，容易误导 Hermes 部署

- 证据：
  - `scripts/run_quant.sh:5` 仍 `cd /home/ubuntu/projects/a-stock-quant`。
  - `scripts/realtime_trader.sh:12` 仍使用 `$HOME/projects/a-stock-quant`。
  - `scripts/quant-realtime.service:8` 仍以 `%h/projects/a-stock-quant` 为 WorkingDirectory。
  - `start_auto.sh:5` 仍 `source .env`，而 Linux/Hermes 主路径使用 `~/.hermes/.env`。
- 影响：
  - 新 README 主流程是 `quant-pilot` + systemd user；旧脚本仍存在会让用户或 Hermes agent 选错入口。
  - 如果服务器上没有旧目录，realtime/cron 入口会直接失败；如果旧目录残留，则可能跑旧代码。
- 建议：
  - 明确废弃或删除旧入口；保留时必须改成 `quant-pilot`、`~/.hermes/.env` 和 `.venv/bin/python`。
  - 把 README 中推荐入口标为唯一生产入口：`data/linux_tasks/install_systemd_user.sh` 和 `systemctl --user ...`。

### P2 - 多个 LLM 调用未复用 Linux 硬超时封装

- 证据：
  - `strategy/mimo_client.py` 已实现 POSIX 子进程硬超时。
  - `review/llm_review.py:49-52`、`review/llm_review.py:491-494` 仍直接 `requests.post(..., timeout=120/30)`。
  - `strategy/market_regime.py:312-350` 仍直接 `requests.post(..., timeout=30)`。
  - `strategy/prompt_evolution.py:22-40` 仍直接 `requests.post(..., timeout=30)`。
- 影响：
  - 自动盘主进程的关键 LLM 决策已较安全，但盘后复盘、市场环境识别、prompt 进化仍可能被底层网络卡住。
  - Hermes systemd 的 Doctor/报告任务有外层 timeout，但直接 requests 的模块级不一致会增加定位成本。
- 建议：
  - 所有 OpenAI-compatible LLM 调用统一走 `post_chat_completion()`。
  - 将 `MIMO_BASE_URL`、`LLM_MODEL`、DeepSeek 相关配置集中到 `config.py` 或单一 client 模块。

### P2 - `llm_trader.py` 配置重复且部分硬编码

- 证据：
  - `strategy/llm_trader.py:28-39` 中 `MIMO_API_KEY`、`MIMO_BASE_URL`、`LLM_MODEL` 定义重复。
  - `MIMO_BASE_URL` 在 `llm_trader.py`、`signals/sentiment.py`、`strategy/market_regime.py`、`strategy/prompt_evolution.py` 等处硬编码；`review/llm_review.py` 才读取 `XIAOMI_BASE_URL`。
- 影响：
  - Hermes 环境切换 API 网关、模型版本或代理时，需要改多处代码，容易漏改。
  - 用户以为 `~/.hermes/.env` 里的 `XIAOMI_BASE_URL` 全局生效，但并非所有模块都读取它。
- 建议：
  - 建立统一 LLM 配置入口，所有模块读取同一组 env/config。
  - 删除重复定义，保留 MiMo/DeepSeek 的显式 fallback 顺序。

### P2 - 模拟账户 JSON 仍是主存储，跨进程一致性不足

- 证据：
  - `execution/paper_account.py:52-53` 使用 `threading.RLock()`，只能保护同进程线程。
  - `execution/paper_account.py:266-286`、`execution/paper_account.py:357-383` 先写 JSON，再 best-effort 双写 SQLite。
  - `scheduler/auto_trader.py:66-704` 用单实例锁保护长驻 `--auto`，但 Doctor、观察、闭环修复等可能另起进程读写同一账户。
- 影响：
  - Hermes systemd 环境下多个 oneshot/手动诊断命令并行时，JSON 文件存在最后写覆盖和 SQLite/JSON 不一致风险。
  - 账户状态、交易记录和报告统计可能短时间不一致。
- 建议：
  - 将 SQLite 作为交易与账户主存储，JSON 仅作为导出快照；或给 JSON 写入增加跨进程文件锁和原子替换。
  - 所有会写账户的入口都复用同一个进程级/文件级锁策略。

### P2 - 就绪检查未验证 systemd lingering 和真实 Python 依赖路径

- 证据：
  - `scheduler/linux_tasks.py:431-435` 只在安装摘要中建议 `sudo loginctl enable-linger "$USER"`。
  - `run_linux_unattended_status()` 检查脚本、unit、`systemctl --user show` 和日志，但不检查 lingering，也不执行 service 使用的 Python 做 import smoke test。
- 影响：
  - Hermes 用户退出后，systemd user 服务可能停止；状态检查可能在当前会话内通过，但重启/登出后失效。
  - 如果脚本使用系统 `python3` 而依赖装在 `.venv`，状态检查可能直到服务触发后才暴露失败。
- 建议：
  - `--linux-unattended-status` 增加 `loginctl show-user "$USER" -p Linger` 检查。
  - 增加 “service Python import smoke”：用脚本中的 `PYTHON_CMD` 执行 `import pandas, requests, longport`。

### P3 - 测试依赖和测试断言需要同步

- 证据：
  - `python -m pytest -q` 失败：当前环境没有安装 `pytest`。
  - `test_auto_rehearsal.py:66-70` 断言 `get_auto_events(limit=20)` 总数为 9，但当前演练每个盘中还会写 `watchlist_update` 和 `watch_cycle`，实际为 15。
  - `test_ai_trader_report.py:206` 仍期望 `最低分60`，但当前远端参数实际到 `75`。
- 影响：
  - 本地自动验证不能一键跑宽回归。
  - 新增观察池事件后，测试会误报失败；参数测试则暴露真实策略阈值风险。
- 建议：
  - 将 `pytest` 加入开发依赖或提供 `python test_*.py` 的统一本地验证脚本。
  - `test_auto_rehearsal.py` 改为只统计 `event_type='auto_cycle'`。
  - `test_ai_trader_report.py` 在修正自适应分数公式后更新期望。

### P3 - 文档仍混用 Hermes Cron、systemd user、旧项目名

- 证据：
  - `docs/architecture.md` 仍描述 “Hermes Agent Cron 调度”，并出现 `a-stock-quant/` 和 `cd ~/projects/a-stock-quant`。
  - `README.md` 已有较新的 Ubuntu/Hermes systemd user 主流程。
- 影响：
  - 新用户会在 Cron 与 systemd user 两套模型之间摇摆。
  - Hermes agent 接手时可能按旧文档配置，绕过 Watchdog/Doctor/状态报告闭环。
- 建议：
  - 将 `README.md` 的 systemd user 流程作为唯一生产部署文档。
  - `docs/architecture.md` 增加“历史 Cron 已废弃或仅兼容”的说明，并修正项目目录。

### P3 - 行尾格式存在 Linux 脚本风险

- 证据：
  - 对 `data/linux_tasks` 执行目录 diff 时，Git 提示多处 `CRLF will be replaced by LF`。
- 影响：
  - 当前 Git 规范化后通常可恢复 LF，但在 Windows 上直接复制脚本到 Linux 时可能出现 shebang 或 shell 解析问题。
- 建议：
  - 增加 `.gitattributes`：`*.sh text eol=lf`、`*.service text eol=lf`、`*.timer text eol=lf`。

## 4. 产品体验评估

正向点：

- README 已提供 Ubuntu/Hermes 主路径：创建 `.venv`、配置 `~/.hermes/.env`、运行 `--linux-tasks`、安装 systemd user、检查 `--linux-unattended-status` 和 `--paper-ready --unattended-platform linux`。
- Watchdog、Doctor、运维状态、闭环检查、自愈、试运行报告都已有 CLI 和测试覆盖。
- `python main.py --health` 在当前环境下能给出清楚的基础依赖、交易通道和环境变量降级状态。

主要体验问题：

- 用户仍能看到多个旧入口：`start_auto.sh`、`scripts/run_quant.sh`、`scripts/quant-realtime.service`，且这些入口和 README 推荐路径不一致。
- `--paper-bootstrap` / `--paper-ready` 默认平台仍是 Windows；Linux 用户必须记得传 `--unattended-platform linux`。
- 如果用户没有仔细按 README 传 `--python-cmd "$(pwd)/.venv/bin/python"`，生成脚本可能使用系统 Python，导致服务启动失败。

## 5. Public Interfaces 审核

适合 Hermes agent 直接调用：

- `python3 main.py --auto`：长驻自动盯盘；应由 `quant-pilot-auto.service` 托管。
- `python3 main.py --watchdog`：只读运行态诊断；适合作为自动重启前置检查。
- `python3 main.py --doctor`：自愈入口；适合作为 timer 周期任务。
- `python3 main.py --ops-status`：一页式运维状态；适合巡检/日报。
- `python3 main.py --paper-ready --unattended-platform linux`：上线门禁；适合部署后检查。

适合人工安装/诊断：

- `python3 main.py --linux-tasks --python-cmd "$(pwd)/.venv/bin/python"`：生成脚本，应在目标 Linux 服务器执行。
- `bash data/linux_tasks/install_systemd_user.sh`：安装/启用 systemd user，需要人工确认环境。
- `systemctl --user status quant-pilot-auto.service`：人工或运维查询。
- `python3 main.py --paper-bootstrap --unattended-platform linux --python-cmd "$(pwd)/.venv/bin/python"`：会跑演练和门禁，适合上线前人工执行，不建议作为长期周期任务。

不建议继续作为生产入口：

- `start_auto.sh`
- `scripts/run_quant.sh`
- `scripts/realtime_trader.sh`
- `scripts/quant-realtime.service`

## 6. 上线前门槛

建议满足以下条件再让 Hermes 长期无人值守：

1. 修正 `get_trade_params()` 自适应分数叠加公式，并让 `test_ai_trader_report.py` 通过。
2. 修正 `test_auto_rehearsal.py` 对新增观察池事件的断言，让连续演练测试通过。
3. 统一 `scheduler/linux_tasks.py` 与 `data/linux_tasks/`，确保生成器产物就是仓库脚本。
4. 在目标 Linux 服务器上使用 `.venv/bin/python` 重新生成并安装 systemd user 任务。
5. 在服务器上执行：
   - `.venv/bin/python -m compileall -q main.py config.py data execution review risk scheduler signals strategy`
   - `.venv/bin/python test_linux_tasks.py`
   - `.venv/bin/python test_paper_readiness.py`
   - `.venv/bin/python test_paper_bootstrap.py`
   - `.venv/bin/python test_auto_watchdog.py`
   - `.venv/bin/python test_auto_doctor.py`
   - `.venv/bin/python test_auto_trader.py`
   - `.venv/bin/python main.py --health`
   - `.venv/bin/python main.py --linux-unattended-status`
   - `.venv/bin/python main.py --paper-ready --unattended-platform linux`
6. 确认 `loginctl enable-linger "$USER"` 已生效，`systemctl --user list-timers 'quant-pilot-*'` 正常。
7. 连续至少 3 个交易日观察 `--ops-status` 和 AI 报告，确认“扫描、执行、复盘、教训、自适应”闭环稳定。

## 7. 工具降级说明

- 本环境未暴露 `sequential-thinking`、`shrimp-task-manager`、`code-index` MCP 工具。
- 实际使用 `rtk`、`git`、`rg`、Python 本地测试、源码阅读和临时目录生成检查完成审核。
- 未执行 CI，未推送远端，未安装 systemd 任务。
