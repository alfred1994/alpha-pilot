# AlphaPilot 全面架构审计 — 2026-09-28

审计对象：`main.py` @ `9ed2907`（branch `main`）
范围：运行编排 / 看板 / 决策信号 / 数据风控执行 四层
代码规模：230 个 Python 文件，约 38,356 行（非测试），89 个测试文件

## 方法与可信度

四条审计线并行展开：编排与幂等、看板前后端、决策与信号、数据/风控/执行。
主报告的结论**均由主控二次核实**（读源码 + 跑本地服务 + 查活运行时文件）。

标记约定：

- **[实测]** — 主控亲自运行或读取活数据确认
- **[代码]** — 源码层面确凿，但未在生产规模复现
- **[推断]** — 依赖对运行时环境的合理推断，未直接观测

### 已知的验证边界（重要）

- **本地库不是生产副本**：`trades` / `llm_decisions` / `auto_events` 均为 0 行，
  `candidate_outcomes` 10 行，`k_daily` 1222 行，库文件 442 KB。
  因此所有 N+1 / 全表扫描 / `COUNT(*)` 类性能结论是 **[代码]** 级的，
  实测稳态各端点 4–38 ms，无法在此数据量上复现真实延迟。
- **视觉呈现未做判断**：审计会话的模型不支持图片输入，未能审阅渲染截图。
  布局结论来自实测渲染几何（getBoundingClientRect）与计算样式，非目视。
- **生产机时区未读取**：`9ed2907` 部署在 `/home/ubuntu/...`（Linux），
  若为 UTC，§2.1 的时区问题严重度最高。

---

## 总览

| 域 | 条目 | P0 | P1 | P2 |
|---|---|---|---|---|
| A 风控与执行正确性 | 14 | 6 | 5 | 3 |
| B 编排与幂等 | 15 | 4 | 8 | 3 |
| C 决策与信号质量 | 26 | 5 | 12 | 9 |
| D 数据层 | 13 | 1 | 8 | 4 |
| E 看板后端 API | 16 | 3 | 7 | 6 |
| F 看板前端与布局 | 18 | 1 | 9 | 8 |
| G 测试与流程 | 3 | 0 | 2 | 1 |
| **合计** | **105** | **20** | **51** | **34** |

### 三条结构性主线

绝大多数 P0 不是孤立 bug，而是三条主线上的症状：

1. **状态没有单一权威** — 5 个互相重叠的状态载体，没有阶段枚举、转移日志、
   失败记录。整套编排建立在一堆松散标志位上（§3）。
2. **护栏放错了层** — 组合级风控只存在于 `pipeline.execute_trade_plan`
   这一个函数里，账户层完全不知情。任何一个新调用点就能悄悄解除武装（§2）。
3. **回测层不测实盘系统** — 三个回测器测三套不同的东西，实盘零滑点。
   意味着下面所有缺陷**在当前工具箱里无法被证伪**（§5、§4）。

---

## 1. 摘要（先读这 10 条）

1. 盘中止损熔断与日亏规则，**整场用成本价/昨收计算**（[实测]）
2. 组合级风控只在编排层，`PaperAccount` 与 `OrderManager` 零护栏（[实测]）
3. `account_state` 单行覆盖写、无 CAS，5 个并发计划任务会静默丢更新（[实测]）
4. 风控状态文件非原子写 + 读改写竞态 → **熔断标记 fail-open 静默重置**（[实测]）
5. 活的 `system_risk.json` 里 `2026-09-10` 有 **10 条重复记录** —— 竞态的物证（[实测]）
6. 技术面（35% 权重 + 58 分门槛）跑在**半成品当日日线**上（[实测]）
7. 可转债是**完全无护栏的第二条买入通道**，不计买入预算（[实测]）
8. 情绪维度 **LLM 解析失败被记成 0.7 置信度的中性 50 分**，fail-open（[实测]）
9. Windows 上 Baostock 调用**没有超时**（唯一实际发布平台）（[实测]）
10. 看板把"接口挂了"和"交易系统崩了"画成**同一个红色状态点**（[实测]）

---

## 2. A 域：风控与执行正确性

### A1 · P0 · 风控状态文件非原子写 + 读改写竞态 = fail-open [实测]

`risk/system_risk.py:115`：

```python
with open(self.state_file, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
```

`"w"` 立即截断，`json.dump` 非原子。读取失败时 `:97` 吞掉
`JSONDecodeError` 并**保留默认状态** —— 即 `system_halted=False`、
`forbid_new_buy=False`、`is_circuit_breaker=False`。

> 一次撕裂读就会**静默解除熔断与急停**，方向是放行而非阻断。
> `risk/drawdown.py:87` 同样写法、同样处理。

对比：`execution/paper_account.py:149-164` 已有正确模板
（`mkstemp` 同目录 + `flush` + `osync` + `os.replace` + 失败 unlink）。
**同一仓库两套写法。**

### A2 · P0 · 竞态已经在活数据上留下物证 [实测]

`system_risk.py:141` 的同日去重只比较 `records[-1]`：

```python
same_day = bool(records) and records[-1].get("date") == date
```

多个进程各持自己的 `daily_records` 快照（构造时载入）、各自追加、
各自整份覆写 → 后写者赢。本机活文件实测：

```
2026-09-10: 10 条    2026-09-05: 4 条
2026-09-14:  3 条    其余日期各 1 条   共 22 条
```

连带后果：

- `records[-2]["total_assets"]`（`:147`）在有重复记录时**可能取到同一天**，
  于是"日盈亏"退化为盘中瞬时差值 —— −5% 规则在测量错误的量。
- `is_new_buy_allowed()`（`:245`）是**带写副作用的谓词**，读一次 `_save_state()` 一次。
  "次日禁止开新仓"实际语义是"直到有人再调一次为止"，且任何并发调用方都能清掉它。

> `9ed2907` 修对了**文件路径**（风控状态改按账户目录推导），没修底下的竞态。
> **部署前不应视为已解决。**

### A3 · P0 · 熔断峰值是全局历史高点，永不重置 [实测]

`risk/drawdown.py:113`：

```python
if total_assets > self.state.peak_value:
    self.state.peak_value = total_assets
```

`peak_value` 只增不减，**不按交易日重基、不按账户隔离、不在熔断到期后重置**。
即：控制交易的那个回撤，测的是**历史最高点** —— 而那个最高点可能属于
另一个账户、另一个资金基数、一次回放运行。

`9ed2907` 用 `ALPHAPILOT_RISK_STATE_DIR` 把临时账户隔离出去，缓解了爆炸半径，
但没改模型本身。`preview()`（`:197`）虽标称不落盘，仍做
`self.state = DrawdownState(...)` 再在 `finally` 还原，非线程安全。

### A4 · P0 · 盘中风控用"未定价"的总资产计算 [实测]

```python
# scheduler/pipeline.py:1516-1520
total_assets = broker.total_assets()      # 不传价格
dc.update(total_assets, date=result.date)
sr_result = sr.update(total_assets)       # 也不传日期 → datetime.now()
```

`broker.total_assets()` 不带价格时回落到 `pos["current_price"]`，
而 `current_price` **只在收盘写回**（`pipeline.py:2158`）。

> **9:30–15:00 全时段，−15% 熔断与 −5% 日亏规则都在用成本价或昨收计算。
> 真实浮亏要等 15:00 写回才第一次被看见。**

且风险在 `:1516-1553` 一次性算完，下单循环在 `:1615-1849`，
**成交后无复评** —— 当日跌 6% 的组合要等下一个扫描周期才触发。

### A5 · P1 · 两个风控控制器时区不一致 [实测]

`dc.update(..., date=result.date)` 传北京时间（`_now_bj()`）；
`sr.update(total_assets)` 不传日期 → `system_risk.py:139` 走
`datetime.now()`（宿主本地时区）。**同一台机器上两个控制器记不同的"今天"。**
生产若为 UTC，风险日在北京时间 08:00（盘中）翻转，
并会立刻追加一条虚假日记录、重置日盈亏基线。

### A6 · P0 · 组合级风控全在编排层，账户层一无所知 [实测]

| 控制项 | 定义 | 实际执行位置 |
|---|---|---|
| 最多 5 仓 / 单票 20% / T+1 / 最小手数 | `config.py:18-19` | `paper_account.py` ✅ |
| **行业 ≤ 2 只** | `risk/position.py:98` | **零调用方，死代码** |
| 日亏 −5% 禁买 | `system_risk.py:162` | **仅** `pipeline.py:1544` |
| 连亏降仓 | `system_risk.py:177` | **仅** `pipeline.py:1550` |
| 回撤 −15% 熔断 | `drawdown.py:126` | **仅** `pipeline.py:1528` |
| 手动急停 | `control.py` | **仅** `auto_trader.py:500/623` |
| `trigger_system_halt` | `system_risk.py:327` | **零调用方，无法触达** |

直接后果：`python main.py --execute` 与 `--stop-check` 会在操作者
认为系统已暂停时照常下单。Web 控制台返回的"已暂停日内自动交易动作"
对自动循环是准确的，**作为急停开关是误导的**。

正确分层应反过来：硬不变量在账户层，策略意愿在编排层。

### A7 · P0 · 账户状态无 CAS，并发丢更新 [实测]

`data/database.py:1428` 把现金+持仓+`total_assets` 作为**单行**覆盖写入：

```sql
INSERT OR REPLACE INTO account_state
(id, initial_capital, cash, total_assets, position_count, positions, updated_at)
VALUES (1, ?, ?, ?, ?, ?, ?)
```

无 `version`、无 `WHERE updated_at = ?`。`PaperAccount.buy/sell` 持锁做读改写，
但那是**进程内** `threading.RLock`。

`windows_tasks.py:227-249` 注册 5 个计划任务（Auto / Doctor / Auto-Restart /
Report / Status）加 Web 服务并发运行；`AutoLoopLock` 只保护自动循环。
**两个进程各持 `_load()` 快照，后提交者静默覆盖对方现金与持仓 —— 这是常态不是边缘。**

`record_account_transaction`（`:1504`）的 `BEGIN IMMEDIATE` 保证单次写原子，
**对丢失更新无效**。加一个 `version` 列 + CAS 即可。

### A8 · P1 · 急停开关只在 1/4 入口被读

`control.is_auto_paused` **零生产调用方**；真实门禁内联在
`auto_trader.py:500/623`，且只保护 `status == "盘中"` 分支。
未覆盖：`execute_trade_plan`、`main.py --execute`、`main.py --stop-check`、
`OrderManager.execute_order`。

### A9 · P1 · 风控谓词带写副作用

`drawdown.is_trading_allowed()`（`:182`）会翻 `is_circuit_breaker=False`
并 `_save_state()`；`system_risk.is_new_buy_allowed()` 同理。
**读操作改状态，是 §A1/A2 竞态的直接来源。**

### A10 · P1 · 熔断到期按自然日算

`drawdown.py:157` 用 `timedelta(days=...)`。周五触发 → 周六解封 →
**覆盖 0 个交易日**。长假前触发同理。交易日历就在
`scheduler/market_calendar.py`，未被调用。

### A11 · P1 · 限价约束在执行层完全缺失 [代码]

`grep 涨停|跌停|price_limit` 在 `data/ execution/ risk/ scheduler/`
只命中情绪用途与一处显式承认：

```python
# data/kt_realtime.py:16,237
"up_limit": float("nan"), "down_limit": float("nan"),
```

执行层从不读 `up_limit`/`down_limit`。后果：涨停板上模拟买入会成交；
**跌停板上的止损"卖出"不成立 —— 止损的安全性恰好在它唯一存在的场景里失效。**
停牌标的可交易。

### A12 · P1 · 单票上限用自指估值 [代码]

`paper_account.py:371` 的 `account_total = self.total_assets()` 不带价格，
盘中落到昨收/成本。`pipeline.py:1747` 的目标金额也用同一个未定价的数 ——
**意图与上限用同一个错数，彼此从不冲突，反而掩盖了 bug。**

### A13 · P2 · 若干执行层细节

- `paper_account.py:670` `check_stop_conditions` 忽略止损计算出的 `shares`，
  恒为全清仓；`shares` 字段是装饰性的。
- `paper_account.py:495` T+1 与止损冲突：当日买入的止损静默丢弃
  （`sell` 返回 `None` → 不追加任何记录 → 无重试无告警）。
- `risk/drawdown.py:292` 的 `__main__` 块用**默认 state_file**，
  跑一次就会用假净值覆写生产 `data/circuit_breaker.json`。
  （`system_risk.py:407` 同形状但正确重定向到临时目录。）
- `main.py --stop-check`（`:655`）取到报价直接卖，
  **不走 `validate_quote`**，而自动循环对应路径（`auto_trader.py:313`）
  显式传 `allow_historical=False`。手动路径是唯一可能在陈旧报价上清仓的路径。

---

## 3. B 域：编排与幂等

### B1 · P0 · 状态无单一权威

| 载体 | 写入方 | 读取方 | 原子写 |
|---|---|---|---|
| `auto_trader_state.json` | `auto_trader._save_state` | watchdog / doctor / agent_status / paper_observer | **否** |
| `auto_control.json` | `control.save_auto_control_state` | 自动循环 / health / ops_status | **否** |
| `signal_cache.json` | `fast_scan` | `execute_trades` | 是 |
| `auto_events` 表 | `_record_auto_event` | watchdog / closure_check / doctor | 是 |
| `trade_plan_executions` 表 | `execute_trade_plan` | **无读取方、无回收器** | 是 |

`AutoTraderState`（`auto_trader.py:66`）是 22 个 `last_*` 字段的平铺，
**无阶段枚举、无终态、无转移日志、无失败记录**。
`_run_stage`（`:546`）只记录开始/清除；阶段抛异常则 `_clear_active_stage`
在 `finally` 运行，阶段**看起来像从没开始过**。唯一的持久失败记录
`state.last_error` 是单个字符串，且每个循环开头就被清空（`:577`）。

### B2 · P0 · `run_review` 可被重复执行

三处调用方，三套独立守卫：

- `auto_trader.py:786` — 仅 `state.last_review_date != today`
- `closure_repair.py:160` — 读 **repair 事件行**（日志派生的启发式，非领取）
- `realtime/event_driven_trader.py:55` — **进程内局部变量**，且不落盘
- `main.py:170` — 无守卫

若 `scripts/quant-realtime.service` 与 `alpha-pilot-auto.service` 同时启用，
15:05 后两次完整 LLM 复盘 → 双倍成本、双份教训抽取、双份指令生成、
两次 `save_daily_snapshot`。

### B3 · P0 · `signal_cache.json` 是单一全局可变槽

`pipeline.py:1104`（主扫描）与 rescue 路径（`auto_trader.py:418`）**写同一个文件**。
rescue 之后，该文件保存的是**救援计划**（已确认标的、缩放权重），
而非主计划。`auto_trader.py:731` 在 `rescue_ran` 时跳过主扫描，
于是最长 `AUTO_SCAN_INTERVAL`（1800 秒）内文件里是救援计划。
此窗口内任何 `--execute` / `--full` / 手动 `main.py` 执行的是救援计划
（它有当日日期与有效 deadline，能通过 `:1441-1449` 两道门）。

### B4 · P1 · `plan_id` 名不副实，去重失效 [实测]

```python
# pipeline.py:1362-1366
canonical = {k: v for k, v in plan_data.items() if k != "plan_id"}
payload_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, ...)).hexdigest()
```

`canonical` 含 `scan_id`（`:327` 的 `uuid4`）与 `elapsed`（浮点），
所以 **`plan_id` 每次扫描都唯一，从不按交易意图去重**。
`trade_plan_executions` 主键只能防"同一文件重复执行"，
防不住"重扫后重买同名"。唯一实际拦截是 `PositionManager.check_position_limit`
的按名去重，不是按意图去重。

### B5 · P1 · 崩溃的计划永久卡在 `executing`

`complete_plan_execution()` 在 `:1865`，**不在 `finally` 里**。
claim 在 `:1464`，其间约 20 处 early return 与多个未包裹的 try。
异常后行永远停在 `status='executing'`，**无租约、无 TTL、无回收器**。
设计意图明确（`database.py:1540`："部分失败的计划也不允许自动重复执行"），
但没有任何东西记录或暴露这个孤儿。

### B6 · P1 · `_parallel_score` 超时后部分结果静默通过

```python
# pipeline.py:618
scored = _parallel_score(candidates, sentiment_scores, timeout=remaining())
```

超时时 `concurrent.futures.wait` 返回已完成的部分（`:1216-1223`），
**不追加 `plan.errors`，不追加 `PipelineResult.errors`**，
计划照常进入 LLM 与下单。一个 3/20 的候选集会被当成完整候选集。

### B7 · P1 · `auto_trader_state.json` 非原子写

`auto_trader.py:234` 裸 `open(w) + json.dump`，无 tmp+rename
（`pipeline.py:1239` 与 `paper_account.py:150` 都有）。
`_load_state` 吞掉解析错误返回全新 state（`:229`）→
`last_review_date` 变空 → **下个循环重跑全天 LLM 复盘**。
而 `closure_repair` / `watchdog` / `agent_status` 与循环并发读同一文件。

### B8 · P0 · `pipeline.py` 无交易日 / 交易时段门禁

`pipeline.py:49` 导入了 `is_trading_day`，**全文从未调用**。
`execute_trade_plan` 读出 `market_status`（`:1451`）后**丢弃**。
`run_daily_pipeline`（`:2356`）是 `main.py` **无参数时的默认行为**。

> 裸跑 `python main.py`，周六 09:05，会执行完整 LLM 扫描 + 模拟买入。
> 唯一拦截是 `TradePlan.deadline = "14:50"`（`:73`），
> 即 14:50 之前**没有任何市场状态门禁**。

### B9 · P1 · 只有一把锁，所有子命令绕过

唯一的并发原语是 `AutoLoopLock`（`auto_trader.py:98`）。
持有者：`run_auto_loop` / `run_locked_action` / `paper_observer`。
**未持有**：`cmd_scan` / `cmd_execute` / `cmd_review` / `cmd_full` /
`cmd_realtime` / `EventDrivenTrader`（后者**完全无锁**）。
`realtime/event_handlers.py:85` 的 `asyncio.to_thread(run_scan)`
无锁、无去重、无 plan 身份校验，与自动循环**最后写入者获胜**地竞争同一文件。

### B10 · P1 · Doctor 锁冲突被误判为自愈失败 → 误停交易

`doctor.py:195-203` 常驻循环持锁时记 `skipped_lock_conflict`，
但 `after_critical` 仍非空，于是**直接落入 `:221-229` 的自动暂停分支**。
**锁冲突与自愈失败不可区分** —— 正常运行时完全可能误停整个交易。
且 `restart_auto.sh` 重启服务后不清 `auto_control.json`，
留下"在跑但已暂停"的静默状态。

### B11 · P1 · `pipeline.py` 是 god-object

2363 行 / 34 个 `def`，其中三个占 62%：

- `fast_scan` 815 行（`:293-1107`）
- `execute_trade_plan` 474 行（`:1395-1868`）
- `run_review` 286 行（`:1904-2189`）

同时承担：LLM 候选门控、并发打分、并发 LLM 编排、可转债扫描与退出、
下单定量、风控接线、止损执行、下单、订单审计、A/B 回填、记忆回填、
复盘、LLM 复盘、指令生成、记忆固化、日终快照、报告格式化、
计划序列化与哈希、SQLite 领取协议，以及 **70+ 处函数内 import 跨 12 个包**。
现有接缝（`run_scan` / `execute_trades` / `run_review`）是文件级的，不是对象级的。

### B12 · P1 · 每次开库执行全量 DDL

`data/database.py:52` 在 `__enter__` 里跑 `_init_tables()`：
约 30 条 `CREATE TABLE/INDEX IF NOT EXISTS` + 6 次 `ALTER TABLE` 探测 +
`PRAGMA table_info`，即 **约 40 条 schema 语句，各占一个隐式写事务**。
`pipeline.py` 有 10 处 `with Database(...)`，`PaperAccount._load` 开 2 次，
`get_daily` 缓存未命中再开 2 次。这是"database is locked"的主要来源。

### B13 · P2 · 编排层其他

- `pipeline.py:780` 的 `_llm_timeout_count` 实为 `confidence == 0` 计数
  （非超时），三次即进程内永久禁用 LLM 买入；
  而 `:865` 的 `USE_LLM_SELL` **不检查该计数** → 非对称失效。
- `main.py:696` `cmd_stop_check` 吞掉所有异常且恒返回 0，
  systemd 认为巡检成功。且它是 `auto_trader.check_stops_once` 的
  **第二份分歧实现**（不同行情源、无 `allow_historical`、无冷却登记）。
- `agent_status.py:164-175` 用 `datetime.now()` 而非 `_now_bj()`，
  约 16 小时/日错判"今日已扫描"。
- `linux_tasks.py:371` 生成孤儿 `run_closure_repair.sh`
  （install 脚本 chmod 但无 unit 挂载）。
- `pipeline.py:50-55` 四个 notifier import **零调用点** —— pipeline 从不通知。
- `realtime/` 半死但持有真实并发隐患：
  `index_change` 触发器是死代码（无发布方），
  但 `event_driven_trader` 独立调 `run_review` 且订阅同一账户。

---

## 4. C 域：决策与信号质量

### C1 · P0 · 可转债是完全无护栏的第二条买入通道 [实测]

`pipeline.py:1036-1095` 的可转债订单在股票下单循环（`:820`，
`top_k` 预算在此结算）**之后**追加。可转债路径**从不经过**：

- 技术分 58 门槛（`DECISION_MIN_BUY_TECHNICAL`）
- `min_score`
- 两轮稳定性追踪
- LLM 判断
- `top_k` 买入预算

权重（`cb_t0_strategy.py:44-50`：溢价 .40 / 跟涨 .20 / 规模 .20 /
换手 .15 / 量比 .05）是手调常数，**仓库内找不到任何样本外验证**。
单票可开至 8%（受指令 `max_weight` 限制），且不计当日买入预算。

附带：`pipeline.py:1043` 的 `hasattr(directive, "params")` 是死分支
（`get_effective_trade_policy` 返回 `dict`），只有下一行 `isinstance(dict)` 生效。

### C2 · P0 · 情绪维度解析失败 = 0.7 置信度的中性 50 分 [实测]

```python
# signals/sentiment.py:143-149
score, direction, summary = _parse_llm_response(raw)   # 失败 → (50, HOLD, "解析失败")
conf = 0.7 if raw else 0.0                              # raw 非空 → 0.7
```

`_parse_llm_response:88-91` 对任何 JSON 失败返回 `(50, HOLD, "解析失败")`。
因 `raw` 非空，**`conf` 仍为 0.7**，于是 `decision.py:80` 的
`confidence <= 0` 丢弃守卫不触发，伪造的 50 分带满权重进入综合分。

讽刺的是 `decision.py:61-64` 的注释恰好写了这个失败模式
（"`confidence<=0` 不应再用伪造的 50 分占据固定权重"）——
**模块本身知道，但被上游绕过了**。这是 20% 权重的维度，且 fail-open。

### C3 · P0 · 技术面（35% + 58 分门槛）跑在半成品日线上 [实测]

```python
# scheduler/pipeline.py:1133
df = get_daily(code, start_date="20240101")   # end_date 默认今天
dims = compute_dimension_scores(code, df)
```

`pooled_ml.py:478-486` 自己写明：

> 盘中链路会把"半成品"当日日线写进 k_daily（成交量/最高价都只走了一半）…
> 用半根 bar 反复重算会让同一只票的 ML 分数在盘中大幅漂移（实测单日摆动 42 分）

**但这个守卫只加在 ML 路径**（`pooled_ml.py:532`）。技术面没有。
`signals/technical.py` 的 Ichimoku / VWAP / Market-Structure / Wyckoff
全部基于不完整 bar，且**给 LLM 看的"收盘价"不是收盘价**。

与 `9ed2907` 的 commit message 互证：
"盘中技术面全天冻结（同票同日 8~15 次扫描技术分完全一致，93% 的分组如此）"
—— 分数冻结在**第一次扫描时刻的半根 bar** 上，之后从未重算。

### C4 · P0 · 18 套评分/选股实现并存，8 套能下单

能产生真实订单的：股票池合并（`stock_picker`）→ 低位池
（`low_position_picker`）→ 维度打分（`decision.compute_dimension_scores`）→
LLM 候选门控（`pipeline.select_llm_candidates`）→ **LLM 决策（唯一股票买入权威）**
→ 可转债旁路（`cb_t0_strategy`）→ 指令改写门槛（`directive`）。

**看似权威、实则死代码的约 2,600 行**：
`decision.make_decision`（自有阈值 + 置信门槛 + 技术门槛，**实盘从不调用**）、
`make_decision_with_cache` / `batch_decide`、`llm_trader.batch_decide`、
`signals/composite.composite_signal`（另一套 5 维权重 + `min_score=60`）、
`pick_stocks_by_strategy`、`pick_stocks_by_htsc`、
`technical_screen.evaluate_patterns`（`affects_orders: False`）。

后果：LLM 不可用时 `pipeline.py:826` 默认 `"HOLD"` →
**静默关闭全部股票交易**，而不是回落到加权打分器。
（这是安全的，但是单点完全失效，且与 `config.py:93 USE_LLM_TRADER` 的暗示不符。）

### C5 · P0 · 实盘零滑点，评估器假设滑点

`paper_account.py:386-392` 成交价原样使用，只计佣金。
而 `counterfactual.py:17,107,161`：

```python
COUNTERFACTUAL_SLIPPAGE_RATE = 0.0005
total_cost = fee_rate + 2 * slippage_rate
```

> **落到 `trades` 的实盘盈亏，系统性地比用于判断策略优劣的
> `candidate_outcomes` 净值更乐观。实盘与评估不在同一成本基准上。**

`config.py:79-82` 记录的 2.4% 追高亏损事件，正是实盘路径不收的那笔成本。

### C6 · P1 · 记忆从 n=2/3 提升为永久长期教训

`memory.py:391-421`：`n=2` 时 `win_rate` ∈ {1.0, 0.5, 0.0}，
2/2 是唯一不 `continue` 的结果；`n=3` 且 3/3 → 写入
`layer="long"`、`score=min(100, score+10)`、`expires_at=None`。
而 `_expire_short_memory`（`:303`）**只过期 `layer='short'`**，
所以 medium/long 记忆**永久**。`recall_layered` 按 `score` 降序
（`:151`）→ **提示词里排第一的记忆建自 3 笔交易**。

### C7 · P1 · 决策与卖出的配对可能时序颠倒

`memory.py:802-819` 的回退分支按 code 各取**最近**一笔买与卖，
**不校验先后**。而 `trade_id` 分支（`:795-800`）是正确的，
但 `llm_trader.make_decision` **从不传 `trade_id`**
（`:650-672`）—— 所以**生产只走有 bug 的那条**。
错误的 `win`/`lose` 标签被写入 `llm_decisions.outcome`，
正是 C6 的输入。

### C8 · P1 · "北向资金"特征实为季度持仓市值快照

`stock_picker.py:405-450` 用东财
`RPT_MUTUAL_HOLDSTOCKNORTH_STA`，按 `HOLD_MARKET_CAP` 降序取前 20：

```python
Candidate(..., north_flow=hold_cap / 1e8,   # 实际是持仓市值
           )
```

分数是**持仓市值的单调函数**，数据最旧滞后一个季度，
实质是一个被错标为"资金流"的大盘股筛选器。
该字段名出现在每份候选报告里。

### C9 · P1 · `_merge_candidates` 重复累加多源加分 [实测]

`low_position_picker.py:217` 已调用 `_merge_candidates`（含多源加分），
其结果又作为 `low_position` 池**二次传入**
`stock_picker.py:617` 的 `_merge_candidates`：

```python
base.score = min(100, total_score)   # 再次加总
```

**同时命中 ≥2 个低位子池 + ≥1 个其他池的标的得分约 90，
而其原始分量总和约 60。** 这正是实盘使用的方式
（`pipeline.py:434` `low_position_mode=True`）。

### C10 · P1 · 选股门槛 20 分 + 多源加分 = "今日出现在最多数据源"

`config.py:58` `PICKER_MIN_SCORE = 20`（原 55），
`stock_picker.py:529` 每个额外池 +10 分。9 个宽松过滤器之下，
top-10 实质是**"今天在最多数据源里出现过"** —— 流动性/关注度代理，
无任何样本外验证。

### C11 · P1 · `strategy/backtest.py` 零成本，且术语错误

```python
# :186, 231
pnl = (cur_close - trade.entry_price) / trade.entry_price
entry_price = float(sub_df['close'].iloc[-1])   # 信号与成交同一根 bar
# :273
result.total_return_pct = np.mean([t.pnl_pct for t in trades])  # 注释写"总收益率"
```

无佣金、无印花税、无滑点；`max_drawdown_pct` 是逐笔收益累加的
**资金回撤**，不是净值回撤。默认窗口 5 个月。

### C12 · P1 · 优化器是纯样本内爬坡

`optimizer.py:113-197`：`for round in range(max_rounds)` 在**同一区间**
反复 `run`，取 `best_return`，返回 `improvement = best - initial`。
调用方 `main.py:811-855` 默认 3 只大盘股 + 一年 + 3 轮。
**无 holdout、无显著性、无每轮 n**，而 `improvement` 实际是 delta
却与 1% 阈值比较（量纲不匹配）。这是用户被告知用来调参的工具，
它无法区分真实改进与噪声。

### C13 · P1 · 参数扫描 1944 组合，单样本内排序

`fast_backtest.py:407-415, 490-497`：默认网格 3×4×2×3×3×3×3 = 1,944 组合，
按 Sharpe 降序取最优，**无多重检验校正、无 OOS**。
`fast_backtest.py:591` 的 docstring 自己承认
"与 run_walk_forward（全区间样本内扫描，天然过拟合）不同"。

### C14 · P1 · walk-forward 的训练窗包含前一 fold 的验证窗

```python
# fast_backtest.py:566-572
train_start = max(0, valid_start - train_days)
folds.append((train_start, valid_start - 1, valid_start, valid_end))
valid_start += valid_days
```

`train_days=250, valid_days=60` 时，fold 2 的训练窗是 `[120, 309]`，
而 fold 1 的**验证窗是 `[250, 309]`** —— **每个 OOS 观察都被后续 fold 用于选参**。
docstring `:591-594` 声称"只有 OOS 结论可以外推到实盘"，该构造不支持这个声称。

雪上加霜：walk-forward 验证的是一个**实盘根本不交易的**玩具策略
（MA 交叉 + RSI 超卖，`:338-339`），且默认 1 年窗口只产生**一个 fold**。

### C15 · P1 · walk-forward 的 `max_drawdown` 排序方向反了

`fast_backtest.py:647-649` 用 `key = (getattr(res, sort_by), ...)` 统一取最大；
而 `run_parameter_sweep:491-495` 对 `max_drawdown` 做了 `reverse=False` 特判，
`run_walk_forward` **没有** → 传入 `sort_by="max_drawdown"` 会选出
**回撤最大**的组合。

### C16 · P1 · ML 用全量 refit 后给最新 bar 打分

`qlib_signal.py:247-257`（honest 时间切分 + purge/embargo）之后，
`:316-324` 用 `model.fit(X, y)` 在**全部**行上重拟合，
再对 `feat_df.iloc[[-1]]`（在训练集内）打分。
`pooled_ml.py:382, 553-554` 同构。
上报的 `validation_auc` 是诚实的，**实盘分数是样本内拟合**。
`min_child_samples=10` + 数百至数万行正是 LightGBM 会记忆的区间。

相关：`qlib_signal.py:558` 用**单一全局 `validation_auc`** 作为所有股票的
`confidence` → `ml` 维度的置信度是常数，对 `avg_confidence` 贡献恒定值。

### C17 · P1 · `technical` 把"数据不足"占位 50 分计入均值

`decision.py:143-153`：

```python
tech_signals = all_technical_signals(df)
avg_score = sum(s.score for s in tech_signals) / len(tech_signals)
```

`all_technical_signals`（`technical.py:1092-1106`）返回 9 个信号，
硬性最短长度：Ichimoku 62 根、volume_profile 60、Wyckoff 90、
Fibonacci 125、market_structure 30。不足时返回
`SignalResult(name, 50, HOLD, 0.0, "数据不足")`。
**这些中性 50 分同时进入分子与分母。**
结果：只有 40 根 bar 的股票得到一个**锚在 50 附近、置信度看起来健康**的
technical 分，并作为证据展示给 LLM。

### C18 · P1 · `emotion` 维度横截面恒定

`sentiment.py:303-340` 的 `market_mood_signal()` 不接受 `code`，
是全市场 `up/(up+down)`，**所有候选得到相同分数**（`conf=0.6`），
却占综合分 12%。叠加 C2（失败也是常数 50）与 `fundamental`
（纯 PE 分档），真正有横截面区分度的只有
`technical`(0.35) / `sentiment`(0.20) / `ml`(0.17) / `capital`(0.08)。

### C19 · P1 · 三个回测器测三套不同的东西

| 维度 | 实盘 | `portfolio/backtest` | `portfolio/fast_backtest` | `strategy/backtest` |
|---|---|---|---|---|
| 成交价 | 盘中报价 ±3% 漂移保护 | 当日收盘 | **信号 bar 收盘（前视）** | 信号与成交同 bar |
| 滑点 | **0** | 0 | 0 | 0 |
| 印花税 | 0.0005 | 0.0005 | **0.001（双边收）** | **0** |
| 最低佣金 ¥5 | 有 | 有 | **无** | 无 |
| T+1 | 三层强制 | 有 | **无** | 无 |
| 手数取整 | 有 | 有 | **无（碎股）** | 无 |
| 技术门槛 58 | 有 | **无** | **无** | **无** |
| 置信门槛 0.4 | 有 | **无** | **无** | **无** |
| 买入规则 | 仅 LLM action | composite ≥ 60 | 信号 | 策略 |

`backtest.py:818-823` 还把 `50.0/0.0` 的占位维度计入，
`total_w` 仅 0.83 → **两个常量维度固定给每笔加约 16.9 分**。
`fast_backtest` 用 `from_signals(fees=commission*2 + stamp)`，
费用按**每边**收取（买入也收印花税），且 `p.209` 默认 0.001 ≠
`config.py:21` 的 0.0005。

> **结论：仓库里没有任何一个回测在度量实盘系统。**
> 因此 §2/§4/§5 的全部缺陷，在当前工具箱下无法被证伪。

### C20 · P1 · 统计严谨性缺口

- **样本量 n 从不与"最优/胜率/夏普"并列展示**
  （`portfolio/report.py`、`review/performance.py`、`review/ai_trader_report.py`）。
- **无任何多重检验校正**，却有三个独立选择回路在乘以试验数：
  优化器（3 轮）、`adaptive._adjust_thresholds`、参数扫描（1,944 组合）。
- A/B 框架的 Welch 检验（`ab_test.py:87-111`）与
  `min_trades=10 / min_days=20 / p<0.05` 门槛（`:25-28`）**质量很好但用不上**：
  `:341-350` 只聚合 `action='SELL'`，而唯一生产者记录的是**信号**
  （`record_trade(pnl_pct=0)`），所以实验永远 running
  （`pipeline.py:660` 注释已承认）。
- 影子晋级门槛是 50bp @ n≈10（`shadow_eval.py:45-47, 131-137`），
  5 个变体在**同一候选集同一天**评估，样本高度重叠，无置信区间。
  （缓解：晋级需人工批准。）

### C21 · P1 · 有效但未接线的纪律

- `pooled_ml` 的 purge/embargo + 四道质量门（AUC / balanced acc /
  Brier / baseline gain）+ artifact hash + 原子替换 + 诚实的
  last_attempt，**是真正严谨的 ML 卫生**。
- `counterfactual.evaluate_candidate_outcomes` 拒绝在窗口未成熟时编造 T+5 统计。
- `shadow_eval` 拒绝自动改正式参数。
- `truncate_as_of` + `pattern_data.daily_window` +
  `technical_screen.completed_daily_cutoff`（含回归测试
  `test_strategy_as_of_guard.py`）显示了对前视的清醒认识。
- T+1 在三个独立层强制，语义一致。

**问题不在于缺少纪律，而在于这些纪律没有覆盖 C1–C5 的主路径。**

### C22 · P2 · 决策层其余

- `technical.py:97` 定义了前视序列 `chikou = close.shift(-displacement)`
  （当前未使用，但是留给下一个人的地雷）。
- `technical.py:258` `hvn_prices` 算了不用；`:296` LVN 分支只加日志
  （注释自承"LVN 本身不改变方向"）—— 计算、进 `detail`、进 LLM 提示词、
  **对分数零影响**。
- `decision.py:474-479` `make_decision_with_cache` 把
  `_compute_capital_score` 的三元组当 float 传给 `DimensionScore`，
  `get_effective_signal_weights:82` 吞掉 `TypeError` → capital 被静默丢弃；
  同时硬编码 `confidence=0.5` 丢弃真实置信度。
- `signal_stability.py:79-91` 是伪确认：其 docstring 自承
  "同一只票当天 8~15 次扫描的技术分完全一致"，
  **对冻结的输入统计 2 轮不产生任何信息**，
  却在 `pipeline.py:274-283` 被当作抗噪守卫。
- `shadow_traders._adjust_score:62-72` 重算加权平均时
  **忽略 `confidence > 0` 过滤**，与生产 composite 不同 → 影子结论口径不一致。
- `directive.py:86-97` 的护栏是**自证**：放松 `min_score` 仅在
  LLM 自己写 `evaluation.verdict="inconclusive"` 时被拦；写 `"supported"` 即绕过。
  且 `get_effective_trade_policy:251` 重读时不带 `current_params` 复检。
- **买入门槛有四处定义**：`regime_config.py:273`（按市况）、
  `regime_config.py:50/151`、`config.py:68/153`（`DECISION_BUY_THRESHOLD=60`）、
  `pipeline.py:653-655`（默认 `min_score=58` —— 不等于其他任何一处）。
- **273 个硬编码数值字面量 / 约 1,600 处**。`config.py` 对**超时**有良好
  env 覆盖，但**对真正重要的阈值没有**：`DECISION_BUY_THRESHOLD=60`、
  `DECISION_SELL_THRESHOLD=35`、`DECISION_MIN_CONFIDENCE=0.4`、
  `SIGNAL_WEIGHTS`、`STOP_LOSS=-0.08`、`CB_MIN_SCORE=70`、`PICKER_MIN_SCORE=20`
  全是裸字面量。而 `DEFAULT_MIN_BUY_TECHNICAL=58` **是** env 可覆盖的 ——
  最需要调阈值的人（写 `config.py:29-32` 三连亏移动止损复盘的人、
  调 CB 止损的 `config.py:171-172`）**都选择了改字面量**。
  这就是 config 表面不被信任的实证。
- `signals/__init__.py:5-9` 与 `composite.py:5` 声称权重 30/25/20/15/10，
  实际 `config.py:45-52` 是 35/8/20/12/8/17。
- `strategy/dual_style.py`（490 行）仅被 `strategy/backtest.py:325` 导入，
  后者无人导入。
- `pipeline.py:551-559` 的 regime 陈旧未检，异常时静默降级
  `sideways / conf=0.5` 且不追加错误。
- `pooled_ml.py:33-35` 门槛接近噪声：`MIN_VALIDATION_AUC=0.52`
  （与抛硬币不可区分）却占买入分 17%；
  `qlib_signal.py:29-35` 的 `FEATURE_COLS` 用**未做横截面标准化的原始价格**
  （`ma5/ma10/ma20/ma60`）→ 池化横截面模型主要在学每只票的价格水位，
  会系统性偏好低价股，且经不起价格水位 regime 切换。
- `adaptive` 的自动采纳当前是关闭的（`pipeline.py:1981`
  传 `apply_adjustments=False`）→ **747 行的自适应调权在生产中完全不生效**，
  但 `adaptive.py:227-299` 仍在持续**创建**新的 A/B 实验。

### C23 · 决策层优点（勿回退）

- `llm_trader.py:248-303` 三层解析（直接 JSON → markdown 围栏 →
  首个 `{` 到末个 `}`），动作白名单、置信度 clamp，
  **任何畸形输出硬降级为 `HOLD, conf=0.0`**，注释写明理由。
  少见且正确。
- Prompt injection 卫生：`_untrusted_context` 显式边界 +
  system prompt 点名威胁模型（`llm_trader.py:31-36, 64-71`）。
- `get_effective_signal_weights`（`decision.py:57-85`）
  在**可用维度上重新归一化**，而不是让缺失数据变成自信的 50 分。
- `pipeline.py:1727-1744` 双向信号价漂移熔断（`MAX_SIGNAL_PRICE_DRIFT`），
  注释里有具体的漫步者 10.02→10.26 案例。**原始版本只有单边下行保护，
  等于零。**
- `config.py:29-32` 的 `TRAILING_STOP` 附 17 样本 MFE/MAE 分析。
  **团队是从证据推理的。**

---

## 5. D 域：数据层

### D1 · P0 · Windows 上 Baostock 无超时 [实测]

```python
# data/history.py:279-293
def _run_baostock(kind, args=(), timeout=BAOSTOCK_TIMEOUT):
    if os.name == "nt":
        if kind == "history": return _query_history_rows(*args)   # timeout 被丢弃
        ...
    # 以下是 POSIX 的 fork 子进程 + proc.join(timeout)
```

Windows 分支直接返回，**根本没用 `timeout`**。
而 `windows_tasks.py:227-249` 注册的每个计划任务都跑在 Windows 上。
`bs.query_history_k_data_plus` 是阻塞 socket 调用、无读超时。
`is_trading_day` → `_load_trading_calendar` 走这条路径：

> **一个挂死的 Baostock socket 会同时挂死整个盘中循环和交易日历**
> ——从任何触碰日历的线程。`config.py:114` 的 `BAOSTOCK_TIMEOUT=75` 在 Windows 上是虚构的。

### D2 · P1 · 陈旧缓存回退只存在于 `df.attrs`，所有消费者都不读

`history.py:424-428, 631-643` 把陈旧度写进 `df.attrs["stale_cache_days"]`，
然后返回一个**外观正常的 DataFrame**。而全部生产消费者忽略 `attrs`：
`pipeline.py:1133`（→ 技术面 35% 权重）、`backtest.py:410`、
`fast_backtest.py:171`、`qlib_signal.py:285`、`stock_picker.py:755`。

> 打分或回测可以在**数周陈旧**的序列上完成，唯一痕迹是
> `data.history` 里一行 `logger.warning`。

新鲜度机器本身**很好**（`_assess_daily_coverage`，`history.py:91-182`：
重复日期、OHLC 序、有限性、正值、密度、**按交易日**的内部缺口，
且注释解释了为何按自然日会误报春节）—— **只是没有接到消费者上。**

### D3 · P1 · `k_daily` 各源互相覆盖并把好数据写成 NULL

`database.py:502` `INSERT OR REPLACE INTO k_daily` 无来源守卫。
`history.py:503-504`：

```python
"amount": raw["amount_yuan"].astype(float),
"turn": float("nan"),        # KT 无已验证换手率
```

SQLite 把 Python `float('nan')` 存为 **NULL**（已验证）。
**一次 KT 兜底抓取就把 Baostock 完整行的 `turn`/`amount` 覆盖成 NULL。**
表也**无 `adjust` 维度**，`hfq` 只靠调用方检查（`history.py:387`）而非 schema。

### D4 · P1 · 熔断按自然日、节假日表到 2026 止

- `drawdown.py:157` 自然日到期（见 A10）。
- `market_calendar.py:52-79`：`_HOLIDAY_WEEKDAYS` 止于 2026。
  Baostock 不可用且年份不在表内 → **"周一至五全是交易日"**，
  2027 年起 01-01 / 05-01 / 10-01 都会被当成交易日。
  唯一检测手段是一行 `logger.warning`。
  （`tests/test_market_calendar_fallback.py:48`
  把这个失败**作为预期行为固化在测试里**。）

### D5 · P1 · `candidate_outcomes` 无清理策略

`db_maintenance.py:98-113` 只清理 `auto_events` 与 `trade_plan_executions`。
`candidate_outcomes`（**每候选 × 每扫描**一行）**从不清**，
而 `data/research_universe.py` 在跑全市场滚动同步。
`db_maintenance.py:11` 陈述的策略（"业务与训练数据不清理"）
**根本没提到这张表**。按 5 仓 × 8–15 次盘中扫描 × 250 天/年，
这将是主导文件体积的表。

### D6 · P1 · 并发配置不一致

`database.py:49-51` 设 WAL + `foreign_keys=ON` + `busy_timeout=5000`。
但 `db_maintenance.py:52,70,103,117,173` 有 5 处裸
`sqlite3.connect(timeout=30/60)` **无 WAL pragma**；
`_prune_ops_tables:103` 是裸 `DELETE` + commit，落在盘中扫描期间
可独占写锁整个 DELETE 时长。
另外**全库零个 `FOREIGN KEY` 子句** → `PRAGMA foreign_keys=ON` 与
`foreign_key_check` 都是空操作（返回 `[]` 意为"什么都没查"，非"干净"）。

### D7 · P2 · 数据层其余

- `positions` 投影（`database.py:1446-1462`）只写 10 列，
  缺 `allow_t0` / `atr_at_buy` / `current_price` 陈旧标记
  （今天不致运行时损坏，但该表是**可读、可导出**的，比真相少信息）。
- **无迁移机制**：`_init_tables` + 6 个裸
  `try: ALTER … except OperationalError: pass`（`:210-232`）
  + 一个 `SAVEPOINT` 保护的表重建（`:404-481`）。
  无 `schema_version` / `user_version`，无有序迁移列表。
  那一个 `SAVEPOINT`/`ROLLBACK TO` 的表重建是唯一真正仔细的迁移。
- `data/__init__.py:14-21` 限速器非线程安全：`_last_call_time` 是无锁模块字典，
  读改写后 `sleep` **在更新时间戳之前** → N 线程同时读到同一陈旧值、
  睡同样久、然后一起发。（`hithink.py:95` 用 `threading.Lock` 跨网络时间，
  是正确实现 —— 同包内两个实现互相矛盾。）
- **无跨源对账、无异常值检测**：
  Baostock 收盘 vs KT 收盘对同一 `(code, date)` 从不比对。
  `data/quote_validation.py` 是唯一真实质量门，做得很好
  （代码匹配、正值、有限性、时区归一、未来 +30s clamp、
  `MAX_EXECUTION_QUOTE_QUOTE_AGE_SECONDS`），但**只接入了两个路径**。
- `realtime/event_bus.py:57-68`：`_loop` 未运行时走 `asyncio.run()`，
  把事件塞进**属于另一个 loop 的队列**且新 loop 立即关闭；
  `stop()`（`:96`）不清队列 → 关停时静默丢弃至多 1000 条报价事件。
  `QueueFull` 记 `warning` 而非 `debug`（`:44`）——
  高频报价路径上，每丢一 tick 打一条 warning 本身会成为瓶颈。

---

## 6. E 域：看板后端 API

### 端点清单

| 方法 | 路径 | 处理 | 每请求工作量 |
|---|---|---|---|
| GET | `/api/status` | `status.py:8` | **极重** |
| GET | `/api/public/status` | `status.py:17` | **极重** + 脱敏 |
| GET | `/api/positions` | `status.py:23` | 重（外网 HTTP + DB + ATR） |
| GET | `/api/shadow/leaderboard` | `status.py:114` | **重且写库** |
| GET | `/api/trades` | `database.py:138` | **N+1** + 全表 `COUNT(*)` |
| GET | `/api/decisions` | `database.py:186` | **N+1** + `SELECT *` |
| GET | `/api/lessons` | `database.py:247` | 轻 |
| GET | `/api/performance` | `database.py:274` | 中（listdir + N 文件读 + 外网） |
| GET | `/api/research/market` | `research.py:73` | 轻，**只读连接 ✅** |
| GET | `/api/research/candidates` | `research.py:133` | 中，**只读连接 ✅** |
| GET | `/api/research/returns` | `returns.py:18` | 中，**只读连接 ✅** |
| POST | `/api/control/pause` \| `/resume` | `control.py:57,74` | 变更操作 |

### E1 · P0 · 无鉴权公开 GET 在生产库上走写事务路径 [实测]

`web/routers/database.py:11-13`：

```python
def _get_db():
    from data.database import Database
    return Database()
```

`Database.__enter__`（`data/database.py:45-51`）每次执行
`PRAGMA journal_mode=WAL` + 约 40 条 DDL + `commit()`（`__exit__:59-66`，
`busy_timeout=5000`）。影响 `/trades`、`/decisions`、`/lessons`、`/performance`。
`/shadow/leaderboard` 更进一步：`status.py:124` 显式调
`ensure_eval_tables`（DDL + commit）。

> **实测澄清**：发出多个 GET 后主库 mtime **未变**（schema 已是最新，
> DDL 全是 no-op），所以**这不是数据损坏问题，而是写锁争用问题** ——
> 40 条 DDL 风暴会与真正的交易循环抢 SQLite 单写锁。

正确范式就在同仓库里 —— `research.py:32-46`：

```python
# Bypass Database.__enter__: dashboard reads must not initialize or migrate DBs.
conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
conn.execute("PRAGMA query_only=ON")
```

**7 个数据 router 里有 2 个用了，4 个没有，且没有共享访问器。**

### E2 · P0 · 最重端点无缓存、无限流，且 shell out 到操作系统

`/api/public/status` → `agent_status.py:6` 一次请求内：

- `run_health_check` — 7 次 `importlib`、2 次 `Database()`、
  **14 张表的 `COUNT(*)`**、读 LongPort OAuth token 文件
- `run_auto_watchdog` — **`subprocess.run(["ps","-eo",...], timeout=5)`**（`eastmoney.py:220`）
- `build_daily_facts` — `get_auto_events(limit=None)` 与
  `get_llm_decisions(limit=None)`，即**刻意不截断**（`database.py:1821`），
  再在 Python 里遍历
- `get_realtime` — **外网 HTTP 到腾讯**，`timeout=5`
- 另 3 次 `Database()` 打开 + 3 个 JSON 文件读

**零缓存、零后台刷新、零 `async`。** 全部是同步 `def`
（FastAPI 推到 anyio 线程池，默认 40 线程）——事件循环本身安全，
但 **40 个并发的未鉴权公开请求 = 40 个并发的重任务**。
`app.js:28` 每个打开的页签 **15 秒轮询 5 个接口**，
无 `visibilitychange` 守卫 → **后台标签页照转**。

### E3 · P0 · 状态点把"接口挂了"和"交易系统崩了"画成同一个红色 [实测]

```js
// app.js:61 —— 真的是严重故障
const critical = !data.health?.ok || !data.watchdog?.ok || data.crash_open || data.control?.paused;
// app.js:78-86 —— 仪表盘不可达，也产出 danger
renderUnavailable() → state-dot danger
```

一次 CDN 抖动，就会把公开首页画成"AI 交易员处于严重故障"。
且 `app.js:51` 在失败时**故意不重载**健康/进化/决策页签，
让它们继续渲染上一次的陈旧数据，**且无陈旧标记**。

> 对一个价值主张是"这是系统实际做了什么"的页面，这是最伤信任的一处。

### E4 · P1 · 漏斗把"未知"渲染成 0，并画成真图表 [实测]

```python
# web/public_safety.py:148-152
"funnel": {key: int(funnel.get(key) or 0) for key in (...)},
```

**全项目其他数字都保留 `None`**（`_nullable_float` / `_number` / `_positive`），
**唯独页面上最显眼的那个图表把缺失值变成实打实的 0**，
再被 `dashboard.js:228-232` 画成漏斗条。

> 部分填充的漏斗会产出一张**看起来完整、实则声称"LLM 判断 0 次"**的自信图表。
> **这是整个看板上最误导人的一处。**（今天是休市日，0 恰好正确；
> 但这个设计缺陷在交易日会真正说谎。）

### E5 · P1 · 陈旧价格与实时价格无法区分 [实测]

```python
# web/routers/status.py:41-42
except Exception:
    prices = {}
```

响应体**没有任何字段标记报价陈旧**。持仓的"当前价格""浮动盈亏"
于是用昨收算出，`dashboard.js:352-353` 渲染得与实时价完全一致。
**用户无法分辨哪个是今天的价格。**

### E6 · P1 · 页脚时间是抓取时间，不是数据时间 [实测]

```js
// app.js:70
this.setText('footer-update-time', `最后更新 ${this.formatTime(data.timestamp)}`);
```

`timestamp` 是 `datetime.now()`（`agent_status.py:322`），即**请求被服务的时刻**。
"最后更新 14:32:05" 读起来像"数据来自 14:32"。
**账户、持仓、成交三处都不带数据时间戳**，
全站唯一诚实的新鲜度信号是顶栏 regime 日期（`app.js:68`）。

### E7 · P1 · N+1 与全表扫描

| 端点 | 问题 |
|---|---|
| `/api/trades` | `_resolve_trade_reason`（`:114`）每行一次 `SELECT ... FROM llm_decisions` → **最多 201 次查询/请求** |
| `/api/decisions` | `_resolve_stock_name`（`:92`）每行最多 2 次查询 → **最多 151 次** |
| `/api/shadow/leaderboard` | `evaluate_variants`（`shadow_eval.py:64-67`）逐变体调 `compute_variant_metrics`，每次都重跑 `_load_outcome_map`（`shadow_traders.py:169`）= **全表 `candidate_outcomes` 载入 Python dict，6 次/请求** |
| `/api/decisions` | `SELECT *`（`database.py:213`）为正则提取股票名而拉 50 行**完整 LLM prompt/response blob**（`database.py:81-89`），用完即弃 |

### E8 · P1 · 其他 API 层

- **所有端点失败都返回 HTTP 200**（`database.py:184,245,272,338`；
  `status.py:110,135`；`research.py:104,186`；`returns.py:101`）。
  **任何外部监控看到 100% 成功率，仪表盘完全故障与健康在 HTTP 层无法区分。**
- **四种响应格式并存**：裸对象无 `success`（`public_safety.py:215`）/
  `{success}` / HTTP 422 `{"detail"}`（`research.py:140`）/
  HTTP **200** + `{"success":false}`（`database.py:193`）。
  客户端已经必须知道哪个是哪个（`app.js:45` vs `dashboard.js:78`）。
- `/api/decisions` **无日期区间上限**（`page` 允许 100000，`:192`）、
  谓词无法走索引（`:197-198`）、每次翻页 `COUNT(*)`（`:211`）。
  对比：`/api/research/candidates` 有 366 天上限。
- **无任何速率限制**，`requirements.txt` 无相关库。
- `control.py:71, 88` 返回原始 `str(e)`，**不使用 `public_error_message()`** ——
  全应用唯一绕过脱敏层的端点（会暴露绝对文件路径）。
- `public_safety.py:238` `"adaptive": snapshot.get("adaptive") or {}` ——
  **脱敏器里唯一完全原样透传的字段**，发布实时权重调参内部与逐维准确率统计。
- `server.py:25-26` 默认 `cors_origins=["*"]` + `allow_credentials=True`；
  若显式设 `ALPHAPILOT_CORS_ORIGINS=*` 则**反射任意 Origin**。
  `ALPHAPILOT_EXPOSE_INTERNAL_STATUS=true` 会把
  `recent_logs[].action`（含 `systemctl --user restart ...` shell 命令）
  与原始 `error` 全部放行。
- 截断无总数提示（`public_safety.py:131,161,202,260`），
  UI 无"还有 N 条"。
- `public_safety.py:209` 算了 `recent_logs[].error`、脱敏、发送 ——
  **前端 `health.js:50` 只渲染 `status` 与 `action`，从不渲染它**。
  死负载，且导致健康页永远无法显示*为什么*失败。
- `public_safety.py:75` 把所有非 doctor 事件错误压成同一句
  "公开页面已隐藏错误细节" → 可重试的抖动与真实故障不可区分。
- `public_safety.py:87-88` `_PROMPT_LEAK_MARKERS`：命中任一中文标记
  就**整段替换**为固定句子 —— 合法解释里含"用户要求"会被静默替换。
- `server.py:66-67` 生产对 `*.js` 也发 `no-store`，
  **禁用 ETag/304**，每次访问重下 1.3 MB vendor。
- `server.py:86` import 期 `os.makedirs`（只读容器会崩）；
  `server.py:10` import 期改 `sys.path`；
  `paper_account.py:60` 每次 GET 都 `_ensure_data_dir()` + `_load()`。
- `server.py:14` 在 import 时快照 `is_prod`，`:54`/`:61` 每请求重读 ——
  两个真相来源。
- **缺 `Strict-Transport-Security`**，而站点是固定 HTTPS 源
  （`index.html:8`）。

### E9 · 看板后端优点（勿回退）

- `public_safety.py` 是**真实且有测试覆盖的安全边界**，不是清单打勾：
  基于**白名单**的字段投影（不是黑名单）、
  secret/路径/traceback/shell 命令的正则脱敏、提示词泄漏抑制、长度上限，
  加上控制 API 的**双层生产门**（`server.py:54` + `:80-82`），
  以及 `tests/test_web_public_dashboard.py:110-157` **断言具体泄漏字符串不存在**。
- 安全响应头与 CSP 严格且无第三方源：
  `nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy`、
  `Permissions-Policy`、`frame-ancestors 'none'`、`base-uri 'self'`、
  `form-action 'self'`、无 `unsafe-eval`。
- `BROKER_MODE` 默认 `paper`，未知值**向 paper 降级**（`broker.py:218-219`）。
  `RealBrokerAdapter` 在 `__init__` 与每个抽象方法都 `NotImplementedError`，
  且 docstring 列明实盘前必须完成什么。

---

## 7. F 域：看板前端与布局

> 以下 F1/F2 为**实测渲染几何**（1440×1000 视口，`getBoundingClientRect`）。

### F1 · P0（体验）· 信息层级倒置 [实测]

今日页区块视觉顺序（y 坐标，视口高 1000，页面总高 2494）：

| y | 区块 |
|---|---|
| 234 | 主标题（**57.6px**） |
| 461 | 市场温度与证据 |
| 777 | 决策旅程（漏斗） |
| 1277 | 策略交接 / 计划结果 / 当前与下一交易日 |
| **1559** | **账户结果四卡：账户净值 / 今日盈亏 / 可用资金 / 当前持仓** |
| 1755 | 净值变化 / 当前持仓 |
| 2177 | 最近模拟成交 |

> **你来看盘最想知道的四个数字在 y=1559，需要滚过 1.5 屏漏斗、图表和策略 diff。**
> 那个 57.6px 的巨号标题，用途是一行状态字符串。
> 同时 16 个 `.metric-card` 散落各页签，"当前持仓"**既是第 5 块的指标卡、
> 又是 y=1772 的独立板块**（`dashboard.js:279` 与 `evolution.js:169`
> 还各实现了一份近乎相同的 `renderDiff`）。

### F2 · P1 · 121 个 ≤10px 元素 vs 209 个 16px [实测]

`9px` × 25、`10px` × 94、`8.33px` × 2；`16px` × 209。
表格单元格、指标卡说明、时间线文字全在 ≤10px 区间。
`styles.css` 中 14 条 `9px` / 22 条 `10px` 规则硬编码，无响应式字号。

### F3 · P1 · 弹窗不是 dialog [代码]

`index.html:239` + `decisions.js:75-82, 136-146`：
无 `role="dialog"`、无 `aria-modal`、无 `aria-labelledby`、
**无焦点移入、无焦点归还、无焦点陷阱、无 Escape 键处理**
（全仓库零个 `keydown` 监听）。键盘用户打开"判断详情"后，
焦点仍在遮罩后的按钮上，只能 Tab 到关闭键去猜。

### F4 · P1 · 漏斗下钻只能鼠标点，且对辅助技术隐藏自身数据

`index.html:86` 给漏斗 `role="img"` + **不含数字**的静态 `aria-label`，
无 `tabindex`；`dashboard.js:198-202` 只绑 echarts `click`。
**屏幕阅读器与键盘用户拿到一张图，拿不到任何数字，也无处可去。**

### F5 · P1 · 对比度不达 AA，盈亏颜色本身不达标

实算（WCAG AA 正文需 4.5:1）：

| 元素 | 对比度 | 位置 |
|---|---|---|
| `.green-text`（**负盈亏**） | **3.97:1** | `styles.css:206` |
| `.red-text`（**正盈亏**） | **4.09:1** | `styles.css:205` |
| `.eyebrow` terracotta | **2.78:1** | `styles.css:144`（~15 个区块标签） |
| `.metric-card:nth-child(even)` | **3.63:1** | `styles.css:176`（半数头条指标卡） |
| `--muted` | **3.65:1** | `styles.css:30`（所有次级标签、表单元格、空状态） |

**盈亏的红绿本身不达标，这比一般对比度问题严重。**
另：**零个 `:focus-visible` 样式**，而 `.nav-btn` 等是 `border: 0`。

### F6 · P1 · 影子数据每页只加载一次且无陈旧标记

`evolution.js:62-63` 页面生命周期内只取一次 `/shadow/leaderboard`。
离开"策略与复盘"页签，8 小时后回来，**仍是今天早上的排行榜，无年龄标记**。
它自己还写着这是日频数据（`evolution.js:60-61`）。
`evolution.js:39` 则每 15 秒重取 `/lessons?limit=60` —— 一张一天变一次的表。

### F7 · P1 · 漏斗 → 候选验证的交叉筛选映射到不同人群

`dashboard.js:3-11` 映射：完成打分 → `score_gate`（**评分不足**）、
LLM 判断 → `ranking_gate`（**判断名额限制**）、观察结论 → `llm`、
交易信号 → `buy_budget`。

> 用户点一个"N 只股票被打分"的漏斗条，落到的是
> "**打分失败**的股票"列表。**数量对不上，且没有任何解释。**

### F8 · P1 · 无死循环保护

`app.js:28` 单一 15 秒 `setInterval` 永不清理，
无 `visibilitychange` 守卫、无 `AbortController`。
`dashboard.js` 与 `evolution.js` **无 `requestId` 竞态保护**
（`decisions.js:29` 与 `research.js:46,74` 有）——
慢响应可覆盖新数据。

### F9 · P2 · 前端其余

- `modules/control.js` **完全死代码**：`app.js:1-6` 从不 import；
  它需要的 6 个 DOM id 在 `index.html` 中全不存在；
  还调用了 `App` 上不存在的 `app.loadGlobalStatus()`（`:86`）。
  `web/routers/database.py:57` `_load_signal_cache()` 同样零调用。
- 跨 router 私有耦合：`returns.py:8` 从 `web.routers.research`
  导入下划线私有的 `_number/_object/_open_db/_table`。
- 四个模块重复实现 `text`/`escape`/`setText`/`pct`/`money`
  （`dashboard.js:26-71`、`decisions.js:13-20`、`evolution.js:28-35`、
  `health.js:4-7`）。`research.js:4-9` 的 `element()` + `textContent`
  是最佳范式，**其余四个应向它看齐**。
- **未转义的 `innerHTML` sink**：`dashboard.js:260` 与 `:371` 把
  `actionText(trade.action)`（原始 `String(action)`，`:58-60`）
  插入 `innerHTML`。今天数据非用户输入，但它是**活的未转义 sink，
  且与同文件里其余全部转义的做法并存** —— 一次不慎的改动就是公开页面的存储型 XSS。
  `evolution.js:162` 另有未转义、未防 `NaN` 的百分比。
- `dashboard.js:382`：真实平坦的基准（恒 0.00%）被**静默丢弃**，
  图例项同时消失。"无数据"与"无波动"被混为一谈。
- `.reasoning-text` 缺 `white-space: pre-wrap`，
  而 `decisions.js:135` 设置的内容含 `\n` → 弹窗多行推理渲染成一行。
- `returns.js:10` 在**构造函数**里无 null 守卫地
  `getElementById('returns-apply').addEventListener` ——
  元素缺失会抛异常并击穿整个 `new App()` 与其他所有页签。
- `returns.js:21` 把 `toLocaleString('en-US', ...)` 字符串解析回 `Date`
  （V8 可行，其他引擎未定义）。
- `app.js:103` `this.init()` 既不 await 也不 catch
  → 首屏失败是未处理的 rejection + 空白页。
- 手工 `?v=YYYYMMDD` 缓存击穿，跨两个文件，且**已经开始漂移**
  （`app.js:6` `2026080201` vs `index.html:16` `2026092401`）。
- 无深链接（刷新永远回"今日"）、无 `<noscript>`、无错误边界、
  **无暗色模式**（`prefers-color-scheme` 全仓零匹配）、
  **无 `prefers-reduced-motion`**（而 `styles.css:110-114` 有无限 `breathe` 动画）。
- 移动端：`.topbar-status` 在 760px 以下 `display:none`（`styles.css:304`），
  **对所有移动用户隐藏实时净值与市况指示且无替代**；
  `research.css` 有 760px 断点但**无 460px 断点**。
- 表格 `<th>` 无 `scope`、无 `<caption>`。
- 实测：今日页仅剩 **2 个占位符**未填
  （`current-strategy-meta` / `pending-strategy-meta`）——
  **空状态覆盖是完整的**。

### F10 · 前端优点（勿回退）

- **数据诚实性文案极佳**：
  "持仓读取失败，不能判定为空仓"（`dashboard.js:107`）、
  "不能视为零候选"（`research.js:100`）、
  "不能判定为零收益"（`returns.js:47`）、
  "未记录，不用最新数据代替"（`decisions.js:135`）、
  指标置 `'不可用'`/`'未知'` 而非 `0`。
  **前端拒绝让失败的请求读起来像零。这比任何图表都稀有且有价值。**
- 空状态**完整**：每个列表、图表、表格都有
  （`dashboard.js:149,180,253,283,334,365`；`decisions.js:106`；
  `evolution.js:84,173,202`；`health.js:20,36,46`；`research.js:123,136`）。
- 前端架构干净：ES 模块、每页签一个类、约 1500 行、
  事件委托、**零内联 `onclick`**、零 `eval`、零 `document.write`。
- LLM 生成文本在所有渲染点均正确转义
  （`decisions.js:118`、`research.js:143`、`evolution.js:209`、`dashboard.js:260`），
  `modal-reasoning` sink 用 `textContent`。
- 响应式是真做了的：三个断点、表格横向滚动、移动导航。
- "服务存活不等于能力完整"的健康页把存活分解为 6 个独立上报的
  能力维度，**比单一红绿灯是更好的设计**。
- 字体自托管、正确的 `unicode-range` 子集、`font-display: swap`，
  `CSP font-src 'self'` 恰好容纳，无 CDN 无 SRI 缺口。
- `research.js:100` 会**原样打印服务端的方法论警告**（`data.note`）；
  `returns.js:65` 内联报告 `invalid_snapshots` / `benchmark_points` /
  `reset_suspected`；`research.js:86,90-91` 在趋势数据陈旧时
  **主动 unset HS300 指标而不是显示陈旧数字**。

---

## 8. G 域：测试与流程

### G1 · P1 · 测试覆盖"最近修过的路径"，关键并发/崩溃场景缺席

**已覆盖且良好**：`test_risk_execution_guards`（6 项，含连亏去重）、
`test_paper_account_integrity`、`test_execution_quote_validation`、
`test_intraday_replay`（幂等）、`test_entry_and_risk_guards`（435 行，
`9ed2907` 新增）、`test_history_cache_freshness`、
`test_walk_forward`、`test_pooled_ml`、`test_strategy_as_of_guard`。

**缺席**：
- 两个 `PaperAccount` 实例并发（对应 A7）
- 崩溃的 `trade_plan_executions` 领取可回收（对应 B5）
- `main.py --stop-check` 拒绝陈旧报价（对应 A13）
- 急停能拦住 `main.py --execute`（对应 A8）
- `system_risk.json` 损坏（对应 A1/A9）
- 跨进程 `DrawdownController` 峰值污染（对应 A3）
- 涨跌停拒绝成交（对应 A11）
- walk-forward 的 OOS 窗与后续训练窗不相交（对应 C14）
  —— `test_walk_forward.py` 测的是几何形状，不是泄漏

> 套件全绿，因为它测的是**刚修过的路径**。

### G2 · P1 · "已修"与"已部署"必须分开看

记忆与文档显示 `9ed2907`（风控状态污染 + 入场信号噪声）、
`8447fa3`（自动盘测试锁隔离）、自适应连挂等**已修但未部署**。
而 A1/A2 表明 `9ed2907` 修的是**文件路径**，不是底下的竞态。
**部署前不应把该项视为已解决。**

### G3 · P2 · 文档与现实脱节

`docs/` 下有 24 份文档，其中 `signal-audit-2026-09-17.md` 是诚实的
事后复盘（**点名了自己未解决的发现**，包括零成交 HOLD 循环与
"历史判断被后来证据污染"的看板 bug）——**这份文档比多数代码更诚实**。
但 `docs/TODO.md`、`docs/architecture.md`（8月）已落后于当前实现。

---

## 9. 前置判断：样本量可能是元问题

`docs/signal-audit-2026-09-17.md` 自己记着 09-16：
**6 轮 / 38 个候选观察 / 12 次 LLM 调用 / 0 笔成交 / 31 次 HOLD**。

如果系统持续不产生样本，那么反事实观测、影子对比、A/B 框架
**都没有东西可学**，每日 LLM 指令也在对着空仓簿反复调参
（`adaptive` 每轮还持续创建新 A/B 实验，§C22）。

> **在做任何架构重构之前，值得先确认"为什么不下单"是否已经解决。**
> 否则重构的是一条不产生流量的管道。

---

## 10. 重构建议（按改动量 / 收益比排序）

### 建议 1：把风险不变量下沉到账户层

现状是"编排层决定能不能买，账户层只管钱够不够"。改为：

```
PaperAccount.buy(..., intent=TradeIntent(weight=..., reason=...))
  内部强制：风控状态 → 组合限额 → 单票限额 → T+1 → 手数 → 现金
```

- 账户层**只读**风控状态，从不写（消除 A9 类竞态）
- 风控状态改用 **SQLite 行 + CAS**（`UPDATE ... WHERE version = ?`），
  与账户状态同模式（同时修 A1/A2/A7）
- 熔断峰值改"按账户 + 按交易日重置"（修 A3）
- 熔断到期改按**交易日**（修 A10）
- 补 `trigger_system_halt` 的调用方（修 A9/A6）
- 接线 `risk/position.py:98` 的行业 ≤2 限制（修 A6）

**预期**：`--execute` / Web API / `OrderManager` 全部自动继承完整风控，
竞态窗口消失。**这是唯一一条能让"手动命令绕过全部风控"永久关闭的改动。**

### 建议 2：引入显式的"交易日状态机"作为唯一权威

```sql
CREATE TABLE day_runs (
  trading_day TEXT NOT NULL,
  stage       TEXT NOT NULL,   -- prefetch|scan|execute|review|snapshot
  status      TEXT NOT NULL,   -- pending|running|done|failed|skipped
  attempts    INTEGER DEFAULT 0,
  last_error  TEXT,
  payload_ref TEXT,            -- 指向本次产物，替代 signal_cache.json 全局槽
  started_at  TEXT, finished_at TEXT,
  PRIMARY KEY (trading_day, stage)
);
```

一步同时解决：
- **B2** `run_review` 重复执行 → 改 `UPDATE ... WHERE status != 'done'` 原子领取
- **B3** `signal_cache.json` 被抢占 → payload 按 `plan_id` 存
- **B4** `plan_id` 名不副实 → 改对**交易意图**（代码+方向+权重档位）哈希
- **B5** 崩溃卡 `executing` → 加租约 + 回收器
- **B1** 无失败记录 → `attempts` / `last_error` 落库，且**可显示在状态块上**
- **B8** 无交易日门禁 → 门禁成为状态机的一部分

### 建议 3：让回测与实盘共用一个执行模型

抽 `execution_model.py`（成本 + 滑点 + 涨跌停 + T+1 + 最小手数 + 数量档位），
实盘与三个回测器都调它。顺带修：
- `fast_backtest` 补手数取整 / T+1 / 滑点；印花税 0.001→0.0005 且只收卖出边；
  补 ¥5 最低佣金
- `fast_backtest:266` 信号 bar 收盘成交的前视
- `backtest.py:525-535` 用当日收盘同时做止损观测与止损成交
- `backtest.py:818` 占位维度固定加 16.9 分
- `C14` walk-forward 训练窗泄漏、`C15` 排序方向反了

**这条是元问题**：没有它，上面所有缺陷都无法被证伪。

### 建议 4：看板后端从"每次请求算一遍"改成"后台算一次、前端读快照"

- 后台线程每 N 秒刷新写快照，前端只读快照
- 4 个数据 router 统一用 `research.py:32-46` 的只读访问器，
  **并把它提取成公共工具**（不要让每个人手抄）
- 统一响应信封；失败用真 5xx
- 加缓存 + 速率限制 + 分页上限

### 建议 5：可转债接入统一门控

要么关掉（`CB_T0_ENABLED=0`），要么把它接进
`top_k` 预算与技术分门槛，并用样本外证据替换手调权重。
**现状是"第二条无护栏通道"，不能这样留在生产。**

### 建议 6：决策层三处定点修复

1. **C2** `sentiment.py:149` → 解析失败时 `conf = 0.0`，让现有守卫生效
2. **C3** `pipeline.py:1133` → 把 `pooled_ml.py:532` 的
   `drop_in_progress_bar` 守卫同样接到技术面
3. **C4** → 明确声明 LLM 不可用时的回退策略
   （现在是静默全 HOLD，不是回落到加权打分器）
4. **C5** → 实盘接入与 `counterfactual` 相同的成本模型

### 建议 7：看板七处低成本改动

1. 把四个账户数字**提到漏斗上方**（用户为钱而来）
2. 漏斗**区分 `null` 与 `0`**，未知画灰并标"无数据"
3. 每卡加**数据时间**，页脚改"抓取于 X / 数据截至 Y"
4. 价格加**陈旧标记**（E5）
5. **拆开 `danger`**：接口不可达（中性/黄）vs 系统故障（红）（E3）
6. **删死代码** `modules/control.js`（F9）
7. **修对比度**（盈亏红绿必须到 4.5:1）、补 `:focus-visible`、
   弹窗加 `role="dialog"` + 焦点管理 + Escape、
   漏斗加 `tabindex` 与带数字的 aria-label（F3/F4/F5）

### 建议 8：阈值全面进 config

把 `DECISION_BUY_THRESHOLD` / `DECISION_SELL_THRESHOLD` /
`DECISION_MIN_CONFIDENCE` / `SIGNAL_WEIGHTS` / `STOP_LOSS` /
`CB_MIN_SCORE` / `PICKER_MIN_SCORE` 等 273 个字面量收进 `config.py`，
并**统一四处互相冲突的买入门槛定义**（C22）。

---

## 11. 审计元信息

- 审计基线：`9ed2907`，branch `main`
- 审计日期：2026-09-28
- 规模：230 个 Python 文件 / 约 38,356 行非测试代码 / 89 个测试文件
- 方式：四线并行静态审计 + 主控二次核实（源码阅读、本地服务实跑、
  活运行时文件检查、渲染几何实测）
- **未做**：性能压测（本地数据量不足）、视觉审阅（模型不支持图片输入）、
  生产机时区确认、LLM 决策质量评估、生产库规模下的 N+1 复现
