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
- **全自动调度**：14个cron任务覆盖全天（盘前预热→早盘扫描→盘中巡检→收盘执行→复盘）

## 快速开始

```bash
# 克隆
git clone https://github.com/alfred1994/quant-pilot.git
cd quant-pilot

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入API密钥

# 运行
python3 main.py --scan        # 扫描信号
python3 main.py --execute     # 执行交易
python3 main.py --review      # 每日复盘
python3 main.py --stop-check  # 止损巡检
```

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
