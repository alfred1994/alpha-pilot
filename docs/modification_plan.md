# A股AI交易员 — 全面修改方案

基于 GPT-5.5 审核发现，分4阶段实施。

---

## Phase 1: 基础Bug修复（P0，必须修）

### Task 1: LLM超时真正生效
- **文件**: `strategy/llm_trader.py`
- **问题**: `ThreadPoolExecutor` context manager退出时仍等线程完成，90秒预算可能被拖死
- **改动**: 用 `threading.Thread` + `thread.daemon=True` 替代，或在 `executor.shutdown(wait=False)` 后强杀
- **验证**: mock一个60秒的LLM调用，确认30秒内返回timeout错误

### Task 2: 决策outcome回填关联决策ID
- **文件**: `review/daily_review.py`
- **问题**: 按股票代码匹配最近买卖，多笔交易互相污染
- **改动**: `llm_decisions`表新增`trade_id`字段，回填时通过trade_id精确关联
- **验证**: 同一天同一股票有多笔交易时，outcome正确关联

### Task 3: 卖出分析覆盖持仓
- **文件**: `strategy/llm_trader.py`
- **问题**: LLM只分析Top候选，不分析已有持仓是否该卖
- **改动**: 在scan阶段，对当前持仓的每只股票也生成卖出分析prompt，让LLM判断是否继续持有
- **验证**: 持仓股出现在决策结果中

### Task 4: get_realtime接口统一
- **文件**: `data/realtime.py`, `data/market.py`
- **问题**: 有时传list有时传string
- **改动**: 统一接受list参数，内部处理单只股票的情况
- **验证**: 所有调用点传入一致

### Task 5: 模拟账户并发锁
- **文件**: `execution/paper_account.py`
- **问题**: 同时运行多个命令可能损坏JSON
- **改动**: 加 `threading.Lock()` 或文件锁（`fcntl.flock`）
- **验证**: 并发读写不报错

---

## Phase 2: 数据模型统一（P1，重要）

### Task 6: 模拟账户迁移到SQLite
- **文件**: `execution/paper_account.py`, `data/database.py`
- **问题**: paper_account.json 和 SQLite trades/positions 表并存，数据不一致
- **改动**:
  1. paper_account.py 的所有读写改为走 `database.py` 的 trades/positions 表
  2. 保留 paper_account.json 作为只读备份，不再写入
  3. 新增 `migrate_json_to_db()` 函数做一次性迁移
- **验证**: 删除 paper_account.json 后系统正常运行

### Task 7: 决策记录完整化
- **文件**: `strategy/llm_trader.py`, `data/database.py`
- **问题**: llm_decisions表有54条记录但outcome和outcome_pct大部分为空
- **改动**:
  1. 收盘复盘时，遍历今日所有llm_decisions
  2. 通过code+date关联trades表获取实际成交价
  3. 计算outcome_pct = (当前价/决策价-1)*100
  4. 回填到llm_decisions表
- **验证**: 查询llm_decisions表，今日记录的outcome_pct非空

### Task 8: 每日快照写入SQLite
- **文件**: `scheduler/pipeline.py`, `data/database.py`
- **问题**: daily_snapshots表为空，账户变化无历史记录
- **改动**: 收盘时自动写入daily_snapshots（现金、市值、总资产、持仓数、市场环境）
- **验证**: 每天收盘后daily_snapshots新增一条记录

---

## Phase 3: 自我进化闭环（P1，重要）

### Task 9: Prompt进化SQL修复
- **文件**: `strategy/memory.py`
- **问题**: SQL查询用 `pnl_pct`/`regime`/`timestamp`，但实际表字段是 `outcome_pct`/`market_regime`/`created_at`
- **改动**: 修正所有SQL字段名
- **验证**: `get_evolution_context()`返回非空结果

### Task 10: 自适应参数真正生效
- **文件**: `strategy/adaptive.py`, `strategy/llm_trader.py`, `config.py`
- **问题**: AdaptiveEngine写adaptive_state.json但主链路用静态config
- **改动**:
  1. llm_trader.py的`make_decision()`在构建prompt前，先读取adaptive_state获取当前参数
  2. 用自适应参数覆盖config中的静态值（如止损比例、仓位上限、信号阈值）
  3. 每次决策后更新adaptive_state
- **验证**: 打印当前使用的参数，确认来自adaptive_state而非静态config

### Task 11: 复盘结果持久化+教训提取
- **文件**: `review/daily_review.py`, `review/llm_review.py`
- **问题**: 复盘结果输出到JSON文件但未写入lessons表
- **改动**:
  1. LLM复盘后，提取"教训"写入lessons表
  2. 教训关联具体的trades（通过related_trades字段）
  3. 下次决策时，从lessons表获取同类股票/市场环境的历史教训
- **验证**: lessons表新增记录，下次决策prompt包含历史教训

---

## Phase 4: 集成验证（P2）

### Task 12: 端到端测试
- **验证方式**: 运行 `python3 main.py --full` 全链路，检查：
  1. scan阶段：持仓股有卖出分析
  2. execute阶段：trades写入SQLite
  3. review阶段：llm_decisions的outcome已回填，lessons新增
  4. adaptive_state参数有变化
  5. daily_snapshots新增记录

### Task 13: Cron job验证
- **验证**: 等下一个交易日，确认早盘/午盘/收盘pipeline正常执行并发送Telegram通知

---

## 工作量估算

| Phase | 任务数 | 预估时间 |
|-------|--------|---------|
| Phase 1 | 5 | 2-3小时 |
| Phase 2 | 3 | 1-2小时 |
| Phase 3 | 3 | 1-2小时 |
| Phase 4 | 2 | 1小时 |
| **合计** | **13** | **5-8小时** |
