# AI交易员系统实施计划

## 目标
将系统从"加权打分机器"改造为"全自动AI交易员"——LLM推理决策、自我复盘、记忆学习、自我纠正。

## 实施状态: ✅ 已完成 (2026-06-04)

## 当前系统分析

**已有的坚实基础（不改动）：**
- 数据层：realtime, history, market, eastmoney, sentiment, longbridge_data, database(SQLite)
- 信号层：5维信号(technical/capital/emotion/fundamental/sentiment) + market_timing
- 执行层：paper_account, order
- 风控层：position, stop_loss, drawdown
- 调度层：pipeline, market_calendar, notifier

**需要新建的4个核心模块：**
1. `strategy/market_regime.py` — 市场环境识别器
2. `strategy/memory.py` — 交易记忆系统
3. `strategy/llm_trader.py` — LLM决策引擎
4. `strategy/regime_config.py` — 策略配置中心

**需要修改的文件：**
- `scheduler/pipeline.py` — 集成新模块到管道
- `review/llm_review.py` — 增强复盘，写入教训库

## 架构设计

```
选股(stock_picker)
    ↓
市场环境识别(market_regime) ← SQLite market_regimes表
    ↓
策略配置(regime_config) ← 根据环境选配置
    ↓
LLM决策(llm_trader) ← 5维信号 + 记忆(memory) + 环境
    ↓
风控过滤(risk/*)
    ↓
执行交易(execution/*)
    ↓
复盘(review/*) → LLM复盘 → 教训写入memory → 自我纠正
```

## 实施步骤

### 步骤1: market_regime.py — 市场环境识别器

功能：
- 综合5维择时信号(market_timing) + 沪深300技术指标 + 涨跌停数据
- 识别市场环境：牛市(bull)、熊市(bear)、震荡(sideways)、反弹(rebound)
- LLM辅助判断（可选，fallback到规则判断）
- 结果存入SQLite market_regimes表

关键接口：
```python
class MarketRegime:
    regime: str          # bull/bear/sideways/rebound
    confidence: float    # 0-1
    indicators: dict     # 原始指标
    llm_reasoning: str   # LLM推理过程

def detect_regime() -> MarketRegime
def get_regime_history(days=10) -> List[MarketRegime]
```

### 步骤2: memory.py — 交易记忆系统

功能：
- 从SQLite lessons表读取历史教训
- 从SQLite llm_decisions表读取历史决策及结果
- 提供"相关记忆检索"——给定股票/市场环境，返回相关教训
- 复盘后自动写入新教训（由llm_review触发）

关键接口：
```python
class TradeMemory:
    def recall(stock_code, regime) -> str          # 检索相关记忆（供LLM参考）
    def recall_lessons(category, limit) -> List     # 按类别检索教训
    def save_lesson(category, content, importance)  # 保存教训
    def save_decision(code, action, prompt, response, reasoning)  # 保存决策
    def update_decision_outcome(decision_id, outcome, pct)  # 更新决策结果
    def get_pattern_summary() -> str                # 生成模式总结（供LLM参考）
```

### 步骤3: llm_trader.py — LLM决策引擎

功能：
- 接收5维信号数据 + 市场环境 + 记忆
- 构建结构化prompt，调用MiMo LLM做决策
- LLM输出：BUY/SELL/HOLD + 置信度 + 推理过程
- 与现有decision.py并存，通过配置切换

关键接口：
```python
class LLMTrader:
    def decide(code, name, dimensions, regime, memory) -> TradeDecision
    def batch_decide(candidates, regime, memory) -> List[TradeDecision]
```

prompt结构：
```
系统角色: 你是A股量化AI交易员
市场环境: {regime}
相关记忆: {memory.recall()}
5维信号: {dimensions}
候选股票: {code} {name}

请决策: BUY/SELL/HOLD + 置信度 + 推理
返回JSON: {"action":"BUY","confidence":0.8,"reasoning":"..."}
```

### 步骤4: regime_config.py — 策略配置中心

功能：
- 根据市场环境选择不同的策略参数
- 牛市: 放宽买入阈值, 提高仓位上限
- 熊市: 收紧买入阈值, 降低仓位, 优先防守
- 震荡: 中性参数, 注重止损

关键接口：
```python
class RegimeConfig:
    def get_config(regime) -> StrategyConfig
    # 返回: buy_threshold, max_positions, max_single_pct, stop_loss, ...
```

### 步骤5: 更新pipeline.py集成

修改 `run_daily_pipeline()`:
1. 选股后 → 调用 `detect_regime()` 识别市场环境
2. 用 `RegimeConfig` 获取当前环境的策略参数
3. 用 `LLMTrader.batch_decide()` 替代 `batch_decide()`
4. 记忆系统提供历史教训给LLM参考

修改 `run_review()`:
1. 复盘后 → LLM生成教训
2. 教训写入 `TradeMemory`
3. 更新历史决策的outcome

### 步骤6: 增强llm_review.py

- 复盘分析结果自动提取教训
- 写入lessons表
- 更新llm_decisions表的outcome字段

## 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| strategy/market_regime.py | 新建 | 市场环境识别器 |
| strategy/memory.py | 新建 | 交易记忆系统 |
| strategy/llm_trader.py | 新建 | LLM决策引擎 |
| strategy/regime_config.py | 新建 | 策略配置中心 |
| scheduler/pipeline.py | 修改 | 集成新模块 |
| review/llm_review.py | 修改 | 增强复盘写入记忆 |

## 向后兼容

- 原有的 `decision.py` 保留不删除
- 通过配置 `USE_LLM_TRADER=true` 切换新旧引擎
- pipeline中增加try/except，新模块失败时fallback到旧逻辑
