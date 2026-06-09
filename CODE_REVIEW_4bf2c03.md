# 代码审查报告: "Add unattended paper AI trader"

**Commit:** 4bf2c03  
**审查日期:** 2026-06-09  
**改动规模:** 59 files, +9970/-237 lines  

---

## 总体评价

本次commit为A股量化模拟盘系统新增了一套完整的"无人值守自动盯盘"能力，包括：
- 自动交易引擎 (`auto_trader.py`) + 单实例锁
- 完整运维体系：健康检查 / Watchdog / Doctor自愈 / 闭环诊断修复
- 多平台部署脚本生成器 (Windows计划任务 / Linux systemd用户级)
- 模拟盘观察器 / 连续演练器 / 启动引导 / 就绪门禁
- AI交易员试运行报告
- 22个测试文件

**整体质量中等偏上**，架构设计清晰、分层合理、测试覆盖面广。但存在若干需要修复的问题。

---

## 🔴 严重问题 (必须修复)

### 1. SQL注入风险 — `review/ai_trader_report.py` + `data/database.py`

`_query_range()` 使用 f-string 拼接表名和列名：
```python
# ai_trader_report.py:57
c.execute(f"""
    SELECT * FROM {table}
    WHERE {date_column} >= ? AND {date_column} <= ?
    ORDER BY {date_column} ASC, created_at ASC
    LIMIT ?
""", (start_date, end_date, limit))
```

虽然当前 `table` 和 `date_column` 都是硬编码常量，但这种模式是不安全的。如果未来有用户输入传入，将直接导致SQL注入。

同样在 `data/database.py` 中：
```python
# database.py:437
sets = ", ".join(f"{k}=?" for k in updates)
c.execute(f"UPDATE trades SET {sets} WHERE id=?", values)
```
`updates` 的 key 来自调用方，如果包含恶意字段名也会有问题。

**建议:** 对表名/列名做白名单校验，或使用固定的SQL模板。

### 2. `locals()` 反模式 — `scheduler/auto_trader.py:411`

```python
"order_audit": (
    getattr(locals().get("exec_result", None), "order_audit", [])
    if "exec_result" in locals()
    else []
),
```

这段代码依赖 `exec_result` 在 `try` 块内被赋值后，`finally` 块中的 `locals()` 仍能捕获到它。这在CPython中恰好可以工作，但：
- 依赖了 CPython 实现细节，不是Python语言规范保证的行为
- 如果代码重构（如将事件记录移出 `finally`），会静默失败
- 可读性极差

**建议:** 在 `try` 块开头声明 `exec_result = None`，然后直接使用 `getattr(exec_result, "order_audit", [])`。

### 3. 死锁竞争条件 — `scheduler/auto_trader.py` `AutoLoopLock.heartbeat()`

```python
def heartbeat(self):
    payload = self._read_payload()      # 步骤1: 读锁文件
    if payload.get("token") != self.token:  # 步骤2: 检查token
        ...
        return
    with open(self.lock_file, "w") as f:  # 步骤3: 写锁文件
        json.dump(...)
```

步骤1-3之间存在TOCTOU竞态：另一进程可能在步骤1和步骤3之间删除并重新创建锁文件。虽然有stale_after保护，但在极端情况下可能导致：
- 两个进程同时认为自己持有锁
- 锁文件被错误覆盖

**建议:** 使用 `os.open(O_CREAT|O_EXCL)` + rename 的原子操作模式来更新心跳。

---

## 🟡 中等问题 (建议修复)

### 4. 全局变量运行时修改 — `scheduler/auto_trader.py:461`

```python
def run_auto_loop(...):
    global AUTO_SCAN_INTERVAL
    if scan_interval is not None:
        AUTO_SCAN_INTERVAL = scan_interval
```

直接修改模块级全局变量 `AUTO_SCAN_INTERVAL`，这个变量同时也是从 `config` 导入的。在多线程环境下可能导致不可预测的行为。

**建议:** 将 `scan_interval` 作为参数传递给 `run_auto_cycle`，不修改全局状态。

### 5. `_force_paper_mode()` 重复实现

`closure_repair.py` 和 `paper_observer.py` 各自独立实现了完全相同的 `_force_paper_mode()` 函数。

**建议:** 提取到公共模块（如 `execution/broker.py` 或新建 `scheduler/utils.py`）。

### 6. `_sync_state_to_sqlite()` 在锁外执行 — `execution/paper_account.py`

```python
def _save(self):
    ...
    with self._lock:
        with open(self.filepath, "w") as f:
            json.dump(data, f, ...)
    self._sync_state_to_sqlite(data)  # 锁已释放！
```

`_sync_state_to_sqlite` 在 `_save()` 的锁释放后才执行，此时另一个线程可能已经开始修改 `self.positions` 等属性，导致SQLite中写入的是不一致的状态。

**建议:** 将 `_sync_state_to_sqlite(data)` 移入 `with self._lock:` 块内。由于它接受 `data` 参数（而非读取self属性），只需确保在JSON写入后、锁释放前执行即可。

### 7. `--python-cmd` 默认值不匹配 — `main.py:685`

```python
parser.add_argument("--python-cmd", default="python", ...)
```

在Linux环境下，默认值应该是 `python3`。当前默认 `python` 在大多数Linux发行版上会找不到命令。

`linux_tasks.py` 的 `LinuxTaskConfig` 默认 `python_cmd="python3"` 是正确的，但 `main.py` 的argparse默认值覆盖了它。

**建议:** 根据平台设置默认值，或统一改为 `python3`。

### 8. `ops_status.py` 中硬编码Windows路径

```python
# ops_status.py:107
"powershell -ExecutionPolicy Bypass -File data\\task_scripts\\restart_auto.ps1",
```

在Linux环境下运行 `--ops-status` 时，会输出Windows PowerShell命令作为"建议动作"，对用户造成困惑。

**建议:** 根据 `unattended_platform` 配置选择合适的命令模板。

### 9. `readiness.py` 平台耦合

```python
from scheduler.windows_tasks import UnattendedItem, run_unattended_status
```

`readiness.py` 在模块顶层导入了 `windows_tasks`。虽然 `run_paper_readiness` 内部有平台判断逻辑，但顶层导入意味着即使在Linux环境下也会加载Windows模块。

**建议:** 将Windows相关导入移到函数内部（延迟导入）。

---

## 🟢 轻微问题 (可改进)

### 10. 演练数据硬编码 — `scheduler/rehearsal.py`

演练器使用硬编码的股票代码（600519贵州茅台、000001平安银行、300750宁德时代）和固定盈亏序列 `[-2.5, -1.8, -3.2, 2.1, 1.4]`。

虽然这只是演练数据，但如果未来演练器用于回测验证，固定数据会导致过拟合幻觉。

**建议:** 在文档中明确标注"仅用于结构验证，不代表策略有效性"。

### 11. 大量顶层导入增加启动开销

`doctor.py` 在顶层导入了 `Database`, `run_auto_cycle`, `pause_auto_trader` 等重量级模块。当用户仅运行 `--health` 或 `--watchdog` 时，这些模块不会被用到但仍需加载。

**建议:** 将 `doctor.py`、`ops_status.py` 的顶层导入改为函数内延迟导入。

### 12. `watchdog.py` 的 `BROKER_MODE` 检查过于严格

```python
items.append(_make_item(
    "交易通道",
    BROKER_MODE == "paper",
    ...
    "critical" if BROKER_MODE != "paper" else "ok",
))
```

如果未来配置为 `real`，Watchdog会始终报critical，Doctor也无法自愈（因为 `NON_RECOVERABLE_CRITICALS` 包含"交易通道"）。

**建议:** 这是设计意图（保护不误入实盘），但应在未来实盘适配器就绪时提供配置开关。

### 13. 测试文件22个但集中在scheduler层

新增22个测试文件覆盖了新模块的核心逻辑，但缺少：
- `execution/broker.py` 的独立单元测试
- `review/ai_trader_report.py` 的边界条件测试（如空数据库、日期范围错误）
- 跨模块集成测试（如 `auto_trader → broker → paper_account → database` 全链路）

### 14. `scheduler/market_calendar.py` 被删除了1行

```diff
-1 line from market_calendar.py
```

需要确认删除的是否为死代码。

---

## ✅ 设计亮点

1. **服务注入模式** — `run_auto_cycle` 的 `services` 参数允许测试替身注入，演练器能完全隔离真实API调用，这是优秀的设计。

2. **分层运维体系** — Health → Watchdog → Doctor → Closure Check/Repair 的层次清晰，每层职责明确，只读/可写边界明确。

3. **安全保护** — `BROKER_MODE=paper` 在多层强制检查（脚本、systemd unit、closure_repair、paper_observer），不容易误入实盘。

4. **锁机制设计** — `AutoLoopLock` 使用 `O_CREAT|O_EXCL` 原子创建 + stale检测 + heartbeat刷新，虽然有上述竞态问题但整体设计合理。

5. **测试覆盖** — 22个测试文件覆盖了自动交易、Watchdog、Doctor、演练、就绪检查等核心流程，测试注入点设计良好。

6. **通知过滤** — `notifier.py` 的 `should_notify_auto_cycle` 智能过滤避免了无人值守时的通知刷屏。

---

## 修复优先级

| 优先级 | 问题 | 文件 | 预计工作量 |
|--------|------|------|-----------|
| P0 | SQL注入风险 | ai_trader_report.py, database.py | 30min |
| P0 | locals()反模式 | auto_trader.py:411 | 5min |
| P1 | 锁竞态条件 | auto_trader.py | 30min |
| P1 | 全局变量修改 | auto_trader.py:461 | 10min |
| P1 | SQLite同步在锁外 | paper_account.py | 5min |
| P2 | _force_paper_mode重复 | closure_repair.py, paper_observer.py | 10min |
| P2 | --python-cmd默认值 | main.py | 5min |
| P2 | Windows路径硬编码 | ops_status.py | 15min |
| P2 | readiness.py平台耦合 | readiness.py | 10min |
| P3 | 顶层重量级导入 | doctor.py, ops_status.py | 15min |
| P3 | 演练数据标注 | rehearsal.py | 5min |
