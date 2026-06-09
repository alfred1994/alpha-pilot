# A股量化全链路自动化系统 - 重构计划

## 架构：数据 → 分析 → 决策 → 执行 → 风控 → 复盘

### 1. 数据层 (data/) - 已完成，保持不变
- realtime.py - 腾讯API实时行情
- history.py - Baostock历史数据
- market.py - AKShare涨停/龙虎榜/融资融券
- sentiment.py - 微博+新闻舆情

### 2. 分析层 (signals/) - 升级为5维打分
- technical.py - 技术信号（已有6个高级指标）
- capital.py - 资金信号（北向资金、主力资金、融资融券）
- sentiment.py - 舆情信号（微博+新闻LLM分析）
- emotion.py - 情绪信号（恐贪指数、股吧情绪、搜索热度）
- fundamental.py - 基本面信号（PE/PB/ROE/营收增长）
- composite.py - 5维融合打分

### 3. 决策层 (strategy/) - 新建
- stock_picker.py - AI选股器（从全A股筛选Top N）
- signal_generator.py - 信号生成器（批量计算5维分数）
- decision.py - 决策引擎（综合信号+风控规则输出买卖决策）

### 4. 执行层 (execution/) - 新建
- paper_account.py - 模拟盘账户（纸面记账，JSON持久化）
- order.py - 订单管理（买入/卖出/调仓）
- monitor.py - 实时监控（盈亏计算、异常告警）

### 5. 风控层 (risk/) - 新建
- position.py - 仓位管理（单股上限、行业分散、总仓位控制）
- stop_loss.py - 止损止盈（固定比例、ATR动态、移动止损）
- drawdown.py - 回撤控制（最大回撤限制、熔断机制）
- exposure.py - 风险敞口（行业集中度、Beta暴露）

### 6. 复盘层 (review/) - 新建
- daily_review.py - 每日复盘（盈亏归因、信号命中率）
- performance.py - 绩效统计（夏普/索提诺/卡尔玛比率）
- optimizer.py - 策略优化（参数调优、权重调整）
- report.py - 报告生成（推送Telegram）

### 7. 调度层 (scheduler/) - 新建
- market_calendar.py - 交易日历（开盘/收盘时间）
- pipeline.py - 全链路管道（数据→分析→决策→执行→风控→复盘）
- notifier.py - 通知推送（Telegram消息）

## 交易规则
- 初始资金：100万（模拟盘）
- 最大持仓：5只
- 单只上限：20%（20万）
- 止损：-5%
- 止盈：+10%
- 移动止损：最高点回撤-3%
- 最大回撤：-15%（触发熔断，暂停交易1天）
- 调仓频率：每日收盘前

## 5维打分权重
- 技术面：30%
- 资金面：25%
- 舆情面：20%
- 情绪面：15%
- 基本面：10%

## 文件结构
```
~/projects/a-stock-quant/
├── data/                  # 数据层
├── signals/               # 分析层（5维信号）
├── strategy/              # 决策层
├── execution/             # 执行层（模拟盘）
├── risk/                  # 风控层
├── review/                # 复盘层
├── scheduler/             # 调度层
├── config.py              # 全局配置
├── main.py                # 主入口
└── REFACTOR_PLAN.md       # 本文件
```
