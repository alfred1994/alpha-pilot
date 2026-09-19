# Vibe-Trading 研究层

AlphaPilot 可以把 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)
（MIT，港大开源的 AI 量化研究 Agent）作为**外置研究层**接入，用于因子研究
和回测交叉验证。它不参与交易决策链路，默认关闭。

## 它解决什么问题

- **因子库**：内置 GTJA191（国泰君安 A 股 191 因子）、Alpha101、academic
  等因子动物园，提供 A 股语境下已经过整理的实现。
- **因子 IC/IR 基准**：`alpha_bench` 输出每个因子的 IC 均值与 IR；面板数据
  默认由 AlphaPilot 本地日线（k_daily 缓存/同花顺，前复权口径）构建，**不使用
  Tushare**。
- **交叉验证**：为自研回测（`strategy/backtest.py`、fast_backtest）提供
  一个独立实现的对照，防止回测幻觉。

## 架构：进程隔离 + MCP 契约

```
AlphaPilot 主进程                 vibe venv（独立安装）
┌────────────────────┐   subprocess   ┌──────────────────────────┐
│ strategy/          │ ─────────────> │ scripts/vibe/            │
│   vibe_bridge.py   │  JSON stdout   │   vibe_tool_driver.py    │
└────────────────────┘                │   └─ mcp_server (fastmcp)│
        │                             └──────────────────────────┘
        │ data/vibe_panel.py（研究池 + k_daily/同花顺日线）
        │   → panel CSV ──ALPHAPILOT_VIBE_PANEL──> driver 注入宇宙加载器
        v 只读文件
data/vibe/alpha_bench_latest.json → build_daily_facts → 复盘 prompt
```

- `vibe-trading-ai` 及其重依赖（langchain/langgraph/fastmcp 等）只存在于
  独立 venv `~/.vibe-trading-venv`，主依赖树零新增。
- driver 在 vibe venv 内通过 **MCP 公开契约**（in-process fastmcp client）
  调用工具，不依赖其内部 API，升级 vibe-trading 不易坏。
- 工具白名单只含 `alpha_zoo`、`alpha_bench` 两个**只读研究工具**；vibe 侧
  shell 工具默认关闭，broker/下单类工具从不接触。
- 任何失败（未启用/未安装/超时/输出异常）都表现为静默降级：复盘里只是
  少一段因子证据，交易主链路不受影响。

## 安装（Linux 服务器或本地）

```bash
bash scripts/setup_vibe_trading.sh        # 创建 ~/.vibe-trading-venv 并固定版本安装
```

要求 Python ≥ 3.11。依赖较重（含 langchain 全家桶），首次安装耗时较长。
脚本最后会用 `alpha_zoo` 做一次冒烟测试。

## 启用与使用

`env.example` 中对应配置：

```bash
VIBE_TRADING_ENABLED=1          # 默认 0；主链路不依赖它
VIBE_PYTHON=                    # 留空=自动探测 ~/.vibe-trading-venv
VIBE_TOOL_TIMEOUT_SECONDS=900   # 单次工具调用超时
```

运行因子基准（默认用 AlphaPilot 本地数据构建研究池面板，不使用 Tushare）：

```bash
# 默认 universe=alphapilot:pool：研究池(data/research_universe.json)前 100 只
python scripts/vibe_alpha_screen.py --period 2024-2026

# 自定义代码文件（每行一个 6 位代码，# 为注释）
python scripts/vibe_alpha_screen.py --universe alphapilot:file:data/my_codes.txt
```

数据链路：`data/vibe_panel.py` 复用 `data.history.get_daily`（k_daily 缓存 →
同花顺 → 长桥 → Baostock 兜底）逐只取 qfq 日线，汇成长表 CSV 落在
`data/vibe/panel/`；driver 在 vibe venv 内把 `universe=alphapilot:*` 的宇宙
加载器替换为该 CSV（这是唯一 pin 的内部缝，缝不存在即显式失败，绝不静默
落入 Tushare）。单只代码日线少于 60 行会跳过并计入统计。
`--universe csi300` 仍可显式使用，但需要 TUSHARE_TOKEN，默认不推荐。

输出：

- `data/vibe/alpha_bench_latest.json` —— 盘后复盘自动引用的固定入口
- `data/vibe/alpha_bench_<universe>_<zoo>.json` —— 按宇宙/因子库归档
- vibe 侧同时生成 HTML 报告（路径见命令输出）

建议每周（或策略评审前）运行一次。结果不需要提交 git（`data/vibe/` 已忽略）。

## 它如何影响交易

因子证据是**参考信息，不是信号**：

- `build_daily_facts` 读取最新基准摘要，作为 `vibe_factors` 段进入日终事实；
- 复盘 prompt 中会附一行高 IC 因子清单（`GTJA#014(IC+0.051/IR+0.81)` 形式），
  供 LLM 在策略优化建议中交叉参考，不直接改变买卖评分或阈值；
- 未运行过基准时整段自动省略，行为与集成前完全一致。

后续可探索的方向（均未启用）：把稳定高 IC 因子接入 `signals/` 作为第 7 维
信号；用 vibe 的 ChinaA 回测引擎对晋升策略做二次复核。

## 验证与回滚

```bash
python tests/test_vibe_bridge.py     # 离线桥接契约测试（无需安装 vibe-trading）
python tests/test_vibe_panel.py      # 离线面板构建测试（数据获取全部打桩）
python -m strategy.vibe_bridge       # 手工自检：探测 venv 与基准文件
```

回滚：设 `VIBE_TRADING_ENABLED=0`（或删除 `~/.vibe-trading-venv`）即可。
主依赖树从未引入 vibe-trading，无需改动 `requirements.txt`。

## 版本固定

当前固定 `vibe-trading-ai==0.1.15`（见 `scripts/setup_vibe_trading.sh`）。
该项目迭代极快，升级前先跑 `tests/test_vibe_bridge.py` 并人工核对
`mcp_server.py` 的工具契约是否变化。
