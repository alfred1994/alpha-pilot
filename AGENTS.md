# A股量化交易系统 - 重构为AI交易员

## 项目概述
A股量化模拟盘系统，正在从"固定规则+打分机器"改造为"全自动AI交易员"。

## 技术栈
- Python 3.12
- SQLite (本地数据存储)
- LongBridge OpenAPI (A股数据源)
- MiMo LLM API (决策引擎)
- Baostock (备用历史数据)
- 东方财富 API (涨停板/龙虎榜)

## 项目结构
```
a-stock-quant/
├── config.py              # 全局配置
├── main.py                # 主入口 (--scan/--execute/--review/--full)
├── data/                  # 数据层
│   ├── realtime.py        # 腾讯实时行情
│   ├── history.py         # Baostock历史数据
│   ├── eastmoney.py       # 东方财富数据
│   ├── market.py          # 市场数据聚合
│   ├── sentiment.py       # 舆情数据
│   └── database.py        # [新建] SQLite存储层
├── signals/               # 信号层
│   ├── technical.py       # 技术信号 (Ichimoku/Volume Profile等)
│   ├── capital.py         # 资金面信号
│   ├── emotion.py         # 情绪面信号
│   ├── fundamental.py     # 基本面信号
│   ├── sentiment.py       # 舆情信号 (LLM)
│   ├── composite.py       # 综合信号
│   └── market_timing.py   # 市场择时
├── strategy/              # 策略层
│   ├── stock_picker.py    # 选股器
│   ├── decision.py        # 决策引擎
│   ├── adaptive.py        # 自适应引擎
│   ├── llm_trader.py      # [新建] LLM决策引擎
│   ├── market_regime.py   # [新建] 市场环境识别
│   ├── regime_config.py   # [新建] 策略配置中心
│   └── memory.py          # [新建] 交易记忆系统
├── execution/             # 执行层
│   ├── paper_account.py   # 模拟账户
│   └── order.py           # 订单管理
├── risk/                  # 风控层
│   ├── position.py        # 仓位管理
│   ├── stop_loss.py       # 止损管理
│   └── drawdown.py        # 回撤控制
├── review/                # 复盘层
│   ├── daily_review.py    # 每日复盘
│   ├── performance.py     # 绩效统计
│   └── llm_review.py      # LLM复盘
├── scheduler/             # 调度层
│   ├── pipeline.py        # 管道
│   ├── market_calendar.py # 交易日历
│   └── notifier.py        # 通知推送
└── data/                  # 数据目录
    ├── paper_account.json # 模拟账户 (将迁移到SQLite)
    ├── signal_cache.json  # 信号缓存
    ├── adaptive_state.json # 自适应状态
    └── quant.db           # [新建] SQLite数据库
```

## 关键命令
- `python3 main.py --scan` — 扫描信号（选股+打分+决策）
- `python3 main.py --execute` — 执行交易（风控+模拟盘）
- `python3 main.py --review` — 每日复盘
- `python3 main.py --full` — 全链路

## 数据源配置
长桥API凭证在 ~/.hermes/.env:
- LONGPORT_APP_KEY
- LONGPORT_APP_SECRET
- LONGPORT_ACCESS_TOKEN

## 重构目标
将系统从"加权打分"改为"LLM推理决策"，分7个阶段：
1. SQLite存储层 + 长桥数据源集成
2. 市场环境识别器
3. LLM决策引擎
4. 交易记忆系统
5. 策略配置中心
6. 执行层+风控层改造
7. 回测引擎改造

## 代码规范
- 所有中文注释
- 每个模块有docstring
- 错误处理完善，API调用有重试
- 数据库操作用context manager
