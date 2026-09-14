# 2026-09-14 全面加固留档

日期：2026-09-14
执行者：ZCode（AI Agent）
提交范围：`a2d77b1..fa911d9`（9 个提交，均已推送 main 并自动部署到 quant-pilot-phx）
测试基线：`scripts/run_tests.py` 66 通过 / 3 存量失败（见"测试基线"）

## 背景

本次源于一次五路并行的全面代码评估（选股/技术指标、复盘闭环、数据层、回测研究、执行风控调度），
发现的确认 bug 与结构性欠缺按优先级分 P0/P1/P2 实施。P0 与 P1 已完成，P2 留档见
[docs/TODO.md](TODO.md)。

## P0 修复（均含回归测试）

### 风控三项 — `a2d77b1`（tests/test_risk_execution_guards.py）

| 问题 | 修复 |
|---|---|
| 单票 20% 上限在真实执行路径失效：`paper_account.buy` 只在 shares/amount 均为 None 时走 `max_buy_amount()`，执行链总是显式传 shares，实际仓位由 LLM `target_weight` 决定 | `buy()` 内对显式股数同样强制 `MAX_SINGLE_PCT` 钳制，超限缩减到整手、不足一手拒绝 |
| PositionManager 死代码：`pipeline.py` 创建后从未调用，行业/持仓数风控未接线 | 接线到 `execute_trade_plan` BUY 分支，提前拦截并留审计记录 |
| SystemRisk 日频语义被破坏：`update()` 每轮执行 append 一条 `daily_records`，盘中波动误触发单日-5%禁开仓、连亏天数虚增 | 同日只刷新估值记录；禁止开仓与连亏规则从日记录幂等重算 |

### 技术信号三项 — `4c41628`（tests/test_technical_signal_fixes.py）

| 问题 | 修复 |
|---|---|
| Wyckoff Spring/UT 恒不可达：区间高/低点取自包含检测窗口的整段数据，`low<range_low` 永假 | 区间只取检测窗口之前的 K 线 |
| 一目均衡表云位偏移 26 日：`senkou_a.iloc[-displacement]` 取的是 26 天前的云 | 改取 shift 后的 `iloc[-1]` |
| 涨跌停阈值写死 ±9.8%/±9.5%，创业板 20cm 与 ST 5% 板识别不到 | 新增 `board_limit_pct`（主板10%/创业科创20%/北交30%/ST 5%），zt_reversal / limit_down_reversal / uptrend_limit_down 三策略生效；顺带清理 limit_down_reversal 死变量与注释矛盾 |

### 复盘三项 — `166c651`（tests/test_review_attribution_fixes.py）

- outcome 词表统一为小写 `win/lose/breakeven`（旧写入 WIN/LOSS，消费端只认小写 → LLM 胜率被系统性低估）；
- 沪深300基准缺失日延续上一日累计值，不再归零作废后续曲线；
- `trade_reviews` 序列化补齐 `signal_score / market_regime / dimensions` 归因字段（此前自适应的信号维度准确率与来源有效性恒不触发）；
- `backfill_decision_outcomes` 尊重 reviewer 的 `db_path`（支持隔离测试/历史补跑）。

### 数据与选股 — `63db4a7`（tests/test_data_pipeline_fixes.py）

- 腾讯实时源缺失时间戳不再用 `now()` 顶替（fail-closed，过期快照无法绕过 300 秒新鲜度校验）；
- 事件链路盘后复盘：`market == "closed"` 恒 False（`get_market_status()` 返回中文状态）改为匹配"盘后"，并按日去重替代 sleep(3600) 盲窗；
- 选股过滤真正启用：ST 过滤（`_is_valid` / `_get_active_stocks`）、动态 PE 上限（东财 f9 字段，`PICKER_MAX_PE`）、成交额下限（`PICKER_MIN_AMOUNT`）——三个配置此前导入未用。

## P1 数据仓库建设 — `8522129` + 后续 4 个 backfill 修复提交

详见 [docs/data-warehouse.md](data-warehouse.md)。背景：服务器（4 OCPU/23GB/74GB 空闲）
硬盘内存富余，数据层从"按需缓存"升级为"本地全量库"是解锁全市场选股、RPS 横截面、
pooled ML 训练、快速回测与盘中复盘回放的地基。

1. **全市场日线回填** `data/universe_backfill.py`：`--backfill-kline`（pool/all/active 三种池，
   增量补尾 / `--backfill-full` 全区间两种模式），并发/磁盘水位按资源自适应，
   报告写 `data/backfill_report.json`。
2. **盘中分钟线落盘**：intraday_watch 的 1 分钟 K 线写入 `k_minute`（幂等、保留 30 天、
   每日最多清理一次、`(period,datetime)` 索引），失败不影响看盘。
3. **数据库维护** `data/db_maintenance.py`：`--db-maintenance` = 完整性检查 → 在线备份
   （backup API，保留 7 份）→ 运维日志清理（180 天）→ 可选 `--db-vacuum`；
   损坏库拒绝写操作；systemd 每日 23:40 timer。

### 部署后实跑发现并修复的问题（backfill 系列提交）

| 提交 | 问题 | 修复 |
|---|---|---|
| `7df927f` | 回填参数被 argparse 互斥组拒收 | 选项参数移出互斥组 |
| `868dad9` | 东财 push2 从服务器偶发 502 | 新增 Baostock `query_all_stock` 单日快照回退 |
| `7f95f4c` | Baostock 深夜链路 75s 超时 | 同花顺研究快照分页插为第二回退路径 |
| `b504040` | 默认起点 2016 距今 10.7 年，超同花顺 10 年窗口上限，每股 ValueError | 默认起点 2020 + 请求窗口钳制 3620 天 |
| `fa911d9` | `require_full_range=True` 把带缺口的结果全部拒收且不落缓存，回填空转 | 接受诚实降级结果并显式落库，缺口在报告标记 incomplete |

### 首次全市场回填（2026-09-14 凌晨）

- 范围 4241 只（同花顺快照源，成交额≥3000万），约 0.5 只/秒，预计 2.5 小时完成；
- 大多数股票报告 `incomplete`：同花顺上游历史存在少量内部缺口（数据诚实落库），
  缺口清单见 `data/backfill_report.json` 的 `incomplete_codes`；
- 交易服务（auto/web/doctor）全程无扰。

## 部署与运维链路诊断

服务器 `research-sync`（19:10）与 `pooled-ml`（21:10）自 2026-09-10 起连日失败，根因是同一条
数据饥饿链：研究池每天只同步 8 只（800 只池仅落了 260 只）→ ML 训练面板
`fresh_coverage 0.039 < 0.80` 被门禁 block。全市场回填完成后该链路自动恢复，无需改门禁。

## 测试基线

- `scripts/run_tests.py`：66 通过，3 个存量失败 —— `test_account_pnl_consistency` /
  `test_issue_fixes` / `test_multi_day_replay`，均为"禁网护栏触发"，在本次改动前的干净
  工作树上同样失败，属历史遗留（测试内部 mock 不完整），未在本次范围内修复。
- 本地开发机注意：Windows 下 Baostock 不走子进程无超时保护（`data/history.py:199`），
  生产 Linux 无此问题。

## 遗留未做（P2）

见 [docs/TODO.md](TODO.md)。
