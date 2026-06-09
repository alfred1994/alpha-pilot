# A股AI量化交易系统 — 技术架构文档

> 版本: v1.0 | 日期: 2026-06-05 | 状态: 生产运行中（模拟盘）

---

## 一、系统概述

A股全自动AI量化交易系统，用LLM（大语言模型）替代传统的"加权打分"做交易决策。系统从多个数据源采集市场数据，通过5维信号体系打分，最终由LLM综合判断买入/卖出/持有。

**核心理念**: 不是用规则引擎硬编码交易逻辑，而是让LLM像人类交易员一样"看数据、做判断"。

**运行模式**: 模拟盘（100万初始资金），通过Hermes Agent定时任务自动调度。

---

## 二、技术栈

| 组件 | 技术选型 | 用途 |
|---|---|---|
| 语言 | Python 3.12 | 主开发语言 |
| 数据库 | SQLite | 本地存储（交易记录、信号缓存、教训库） |
| LLM | MiMo v2.5-pro (小米) | 决策引擎、市场环境识别、舆情分析 |
| 行情数据 | 长桥OpenAPI + Baostock | K线、实时行情 |
| 选股数据 | 华泰证券Skill (ai.zhangle.com) | 条件选股、个股诊断、行情查询 |
| 事件数据 | 东方财富API | 涨停板、龙虎榜、新闻 |
| 舆情数据 | AKShare + 东方财富新闻 | 北向资金、主力资金、微博财经 |
| 调度 | Hermes Agent Cron | 定时任务（5个时间段） |
| 通知 | Telegram | 实时推送交易信号和复盘报告 |

---

## 三、系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Hermes Agent Cron 调度                        │
│  9:35早盘  10:30盘中  13:30午盘  15:05执行  15:30复盘               │
└──────┬──────────────┬──────────────┬──────────────┬─────────────────┘
       │              │              │              │
       ▼              ▼              ▼              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     scheduler/pipeline.py 管道                       │
│                                                                     │
│  _step_scan()  →  _step_score()  →  _step_risk_check()  →  _step_  │
│  选股扫描         5维打分+决策       风控过滤              execute() │
│                                                             执行    │
└──────┬──────────────┬──────────────┬──────────────┬─────────────────┘
       │              │              │              │
       ▼              ▼              ▼              ▼
┌──────────┐  ┌──────────────┐  ┌──────────┐  ┌──────────────┐
│  数据层   │  │   策略层      │  │  风控层   │  │   执行层      │
│ data/    │  │  strategy/   │  │  risk/   │  │ execution/   │
└──────────┘  └──────────────┘  └──────────┘  └──────────────┘
```

---

## 四、数据层 (`data/`)

### 4.1 数据源清单

| 数据源 | 模块 | 提供数据 | 调用频率 |
|---|---|---|---|
| 长桥OpenAPI | `longbridge_data.py` | K线(日线)、实时行情 | 每只股票1次/扫描 |
| Baostock | `history.py` | 历史K线(含PE/PB) | 备用/缓存 |
| 东方财富 | `eastmoney.py` | 涨停板、龙虎榜、新闻、活跃股 | 每次扫描多次 |
| AKShare | `capital.py`/`fundamental.py` | 北向资金、主力资金、两融 | 每次扫描1次 |
| 腾讯行情 | `realtime.py` | 实时价格 | 执行阶段 |
| 华泰证券Skill | `htsc_client.py` | 条件选股、个股诊断、指标查询 | 每次扫描 |

### 4.2 华泰证券集成 (`data/htsc_client.py`)

华泰证券通过3个Python Skill提供专业金融数据接口：

| Skill | 功能 | 调用方式 |
|---|---|---|
| `select-stock` | 自然语言条件选股 | `python3 select_stock.py selectStock --query "..."` |
| `query-indicator` | 行情/指标查询 | `python3 query_indicator.py queryIndicator --query "..."` |
| `financial-analysis` | 个股诊断/市场洞察 | `python3 financial_analysis.py diagnosisStock --query "..."` |

所有Skill共用一个`HT_APIKEY`，服务端为`ai.zhangle.com`。

### 4.3 数据库 (`data/database.py`)

SQLite存储，主要表：

| 表 | 用途 |
|---|---|
| `market_regimes` | 每日市场环境记录 |
| `llm_decisions` | LLM决策记录（含推理过程） |
| `lessons` | 交易教训库 |
| `trades` | 成交记录 |
| `signal_cache` | 信号缓存（JSON文件，scan→execute传递） |

---

## 五、信号层 (`signals/`)

### 5.1 五维信号体系

每个信号输出统一结构：`SignalResult(name, score, direction, confidence, detail)`

```
score: 0-100 (50=中性, >50偏多, <50偏空)
direction: BUY / SELL / HOLD
confidence: 0.0-1.0
```

| 维度 | 权重 | 信号源 | 计算方式 |
|---|---|---|---|
| **技术面** | 40% | 一目均衡表、成交量分布、市场结构、Wyckoff、蜡烛形态、Fibonacci | 6个子信号取均值 |
| **资金面** | 10% | 北向资金净流入、主力资金流向、融资融券余额 | 趋势分析+阈值 |
| **舆情面** | 25% | 东方财富新闻 → LLM情感分析 | LLM打分(0-100) |
| **情绪面** | 15% | 涨跌停比、换手率异常、量能 | 市场统计指标 |
| **基本面** | 10% | PE/PB历史分位数 | 百分位计算 |

### 5.2 市场环境识别 (`strategy/market_regime.py`)

综合5维择时信号+沪深300趋势+市场广度，识别4种环境：

| 环境 | 含义 | 对策略的影响 |
|---|---|---|
| `bull` | 牛市 | 积极买入，放宽标准 |
| `bear` | 熊市 | 谨慎，提高选股标准 |
| `sideways` | 震荡 | 正常操作 |
| `rebound` | 反弹 | 关注超跌反弹机会 |

识别方式：规则引擎（五维择时总分）+ LLM辅助判断。

---

## 六、策略层 (`strategy/`)

### 6.1 选股流程 (`stock_picker.py`)

采用三级fallback机制：

```
华泰条件选股 (优先)
  │ 失败 ↓
技术形态扫描 (6个策略)
  │ 失败 ↓
事件选股 (涨停板/龙虎榜)
```

**华泰条件选股** (`pick_stocks_by_htsc`):
- 调用华泰`select-stock` Skill，自然语言描述选股条件
- 当前默认条件: "主力净流入超过3000万且换手率大于3%的沪深主板股票"
- 过滤: 北交所(8/4开头)、科创板(688)、退市股

**技术形态扫描** (`pick_stocks_by_strategy`):
- 从东方财富获取活跃股（成交额>5000万，取前200只）
- 每只跑6个技术策略，有BUY信号的入选

**6个技术策略**:

| 策略 | 文件 | 逻辑 |
|---|---|---|
| 均线金叉 | `ma_cross.py` | MA5上穿MA20 |
| 量价突破 | `volume_breakout.py` | 放量突破前期高点 |
| 平台突破 | `platform_breakout.py` | 横盘整理后向上突破 |
| RSI超卖反弹 | `rsi_bounce.py` | RSI<30后反弹 |
| 涨停洗盘 | `zt_reversal.py` | 涨停后回调洗盘 |
| 跌停反转 | `limit_down_reversal.py` | 跌停后反转 |

### 6.2 LLM决策引擎 (`llm_trader.py`)

**两阶段决策流程**:

```
第一阶段: 快速打分（所有候选）
  每只股票 → 5维信号计算 → 综合加权分 → 排序

第二阶段: LLM精细决策（仅Top 5）
  综合分最高的5只 → 构建Prompt → MiMo LLM → BUY/SELL/HOLD
  其余股票 → 直接HOLD（不调LLM，节省时间）
```

**LLM Prompt 结构**:
```
【股票信息】代码 + 名称
【市场环境】牛市/熊市/震荡/反弹
【5维信号】技术面/资金面/舆情面/情绪面/基本面 各维度分数+置信度+详情
【华泰专业诊断】(仅高分股票) 华泰Skill返回的个股诊断报告
【历史记忆】该股票过去的交易教训
【持仓信息】当前持仓、总资产、可用现金
【决策要求】综合判断，熊市谨慎，有亏损教训要注意，置信度>=60%才买入
→ 输出: {"action": "BUY/SELL/HOLD", "confidence": 0.0-1.0, "reasoning": "..."}
```

**LLM返回解析**:
- 优先取`content`字段
- 如果`content`为空，fallback到`reasoning_content`（MiMo特有）
- 正则提取JSON兜底

### 6.3 交易记忆系统 (`memory.py`)

从SQLite读取历史教训和决策记录，注入LLM Prompt：

- **教训库** (`lessons`表): 每次交易后LLM总结教训写入
- **决策记录** (`llm_decisions`表): 每次LLM决策自动记录
- **记忆检索**: 给定股票代码+市场环境，返回相关教训文本

### 6.4 策略配置中心 (`regime_config.py`)

根据市场环境动态调整策略参数：
- 牛市: 放宽买入阈值，加大仓位
- 熊市: 收紧买入阈值，减小仓位
- 震荡: 使用默认参数

---

## 七、风控层 (`risk/`)

### 7.1 仓位管理 (`position.py`)

| 规则 | 参数 |
|---|---|
| 最大持仓数 | 5只 |
| 单只上限 | 总资产的20% |
| 最小交易单位 | 100股（A股规定） |

### 7.2 止损管理 (`stop_loss.py`)

| 类型 | 参数 | 说明 |
|---|---|---|
| 固定止损 | -8% | 买入价跌8%止损 |
| ATR动态止损 | 2倍ATR | 根据波动率自适应（默认开启） |
| 止盈 | +10% | 买入价涨10%止盈 |
| 移动止损 | 最高点回撤3% | 股价创新高后回撤3%止损 |

### 7.3 回撤控制 (`drawdown.py`)

| 规则 | 参数 |
|---|---|
| 最大回撤 | -15% |
| 熔断暂停 | 1天（触发后暂停交易1天） |

---

## 八、执行层 (`execution/`)

### 8.1 模拟账户 (`paper_account.py`)

- JSON文件存储 (`data/paper_account.json`)
- 初始资金: 100万
- 模拟A股交易规则: T+1、涨跌停、佣金万三、印花税千一

### 8.2 订单管理 (`order.py`)

- 下单前检查: 资金充足、持仓限制、涨跌停限制
- 订单状态: pending → filled / rejected
- 成交记录写入SQLite

---

## 九、调度层 (`scheduler/`)

### 9.1 定时任务 (Hermes Agent Cron)

| 任务 | 时间 | 脚本 | 功能 |
|---|---|---|---|
| 早盘扫描 | 9:35 | `quant_scan.sh` | 选股+打分+决策 |
| 盘中扫描 | 10:30 | `quant_midday_scan.sh` | 同早盘，报告变化 |
| 午盘扫描 | 13:30 | `quant_midday_scan.sh` | 同早盘，报告变化 |
| 收盘执行 | 15:05 | `quant_execute.sh` | 执行交易（模拟盘下单） |
| 收盘复盘 | 15:30 | `quant_review.sh` | LLM复盘+教训总结 |

所有脚本timeout=600秒，通过Telegram推送结果。

### 9.2 管道流程 (`pipeline.py`)

```
run_scan() 完整流程:
  1. _step_scan()          选股扫描 (18s)
     → 华泰条件选股 → 技术形态扫描 → 事件选股
  
  2. 市场环境识别 (63s)
     → 5维择时信号 → 沪深300趋势 → MarketRegime
  
  3. _step_score()          5维打分+LLM决策 (300s)
     → 快速打分(全部) → 综合分排序 → Top5 LLM决策 → 其余HOLD
  
  4. 缓存决策结果 → signal_cache.json

execute_trades() 完整流程:
  1. 加载信号缓存
  2. _step_risk_check()     风控过滤
  3. _step_execute()        模拟盘下单
  4. 推送执行报告
```

---

## 十、当前性能指标

| 指标 | 数值 | 说明 |
|---|---|---|
| 华泰选股 | ~18s | 1条查询，10只候选 |
| 市场环境识别 | ~63s | 含涨停板API多次请求 |
| 5维打分(10只) | ~180s | 每只~18s（K线+资金+舆情） |
| LLM决策(Top5) | ~100s | 每只~20s |
| **总耗时** | **~394s (6.5min)** | 从启动到输出报告 |

---

## 十一、已知问题和优化方向

### 11.1 性能瓶颈

| 瓶颈 | 耗时 | 优化方向 |
|---|---|---|
| 涨停板API重复请求 | 30s+ | 市场环境识别和5维打分都各自请求涨停板数据，应共享缓存 |
| 资金面信号(AKShare) | 每只3-5s | 北向/主力资金是全市场数据，应预取一次而不是每只股票单独取 |
| 舆情分析(OpenRouter LLM) | 30s | 已做batch处理，但OpenRouter免费模型不稳定 |
| 每只股票串行处理 | 18s/只 | 可以用asyncio或多线程并行处理 |

### 11.2 决策逻辑问题

| 问题 | 说明 | 优化方向 |
|---|---|---|
| 熊市几乎不出手 | LLM Prompt中"熊市谨慎"导致保守 | 改为"熊市降低仓位+提高标准"而非"熊市不买" |
| 综合分阈值过高 | 65%阈值+LLM保守=双重过滤 | 降低阈值或改为动态阈值(根据市场环境) |
| LLM解析偶尔失败 | MiMo返回reasoning_content而非content | 已有fallback，但仍偶尔解析失败 |
| 基本面分数长期偏低 | 资金面/基本面数据不稳定，经常给30-40分 | 数据源质量需要提升 |

### 11.3 数据质量问题

| 问题 | 说明 |
|---|---|
| 微博cookies过期 | 微博财经数据无法获取，需要更新cookies |
| 东方财富API不稳定 | 涨停板API偶尔返回空数据 |
| AKShare限流 | 北向资金等接口有频率限制 |

### 11.4 架构优化建议

1. **数据预取层**: 开盘前一次性拉取全市场基础数据（涨停板/北向资金/两融），存入缓存，各模块共享
2. **并行处理**: 10只股票的5维打分可以并行（asyncio/threading）
3. **LLM批处理**: 一次发10只股票的数据给LLM做批量决策，而不是逐只调用
4. **动态候选数**: 根据市场环境调整候选数量（牛市多选，熊市少选）
5. **止损优化**: 目前ATR止损参数固定，可以根据个股波动率自适应
6. **回测验证**: 新策略上线前先回测验证，避免实盘踩坑

---

## 十二、文件清单

```
a-stock-quant/
├── config.py                    # 全局配置（资金/风控/权重/参数）
├── main.py                      # 主入口 (--scan/--execute/--review/--full)
├── CLAUDE.md                    # 项目说明文档
│
├── data/                        # 数据层
│   ├── __init__.py              # 限流器
│   ├── database.py              # SQLite存储层
│   ├── longbridge_data.py       # 长桥OpenAPI客户端
│   ├── history.py               # Baostock历史数据
│   ├── eastmoney.py             # 东方财富API
│   ├── realtime.py              # 腾讯实时行情
│   ├── market.py                # 市场数据聚合
│   ├── sentiment.py             # 微博/新闻舆情
│   ├── atr.py                   # ATR计算
│   └── htsc_client.py           # 华泰证券Skill客户端
│
├── signals/                     # 信号层
│   ├── __init__.py              # Direction/SignalResult定义
│   ├── technical.py             # 技术面信号(6个子信号)
│   ├── capital.py               # 资金面信号(北向/主力/两融)
│   ├── sentiment.py             # 舆情信号(LLM情感分析)
│   ├── emotion.py               # 情绪面信号(涨跌停/换手率)
│   ├── fundamental.py           # 基本面信号(PE/PB分位)
│   ├── composite.py             # 综合信号
│   └── market_timing.py         # 市场择时
│
├── strategy/                    # 策略层
│   ├── stock_picker.py          # 选股器(华泰+技术+事件三级fallback)
│   ├── decision.py              # 加权打分决策引擎
│   ├── llm_trader.py            # LLM决策引擎(两阶段:快速打分+Top5精细)
│   ├── market_regime.py         # 市场环境识别
│   ├── regime_config.py         # 策略配置中心
│   ├── memory.py                # 交易记忆系统
│   ├── adaptive.py              # 自适应引擎
│   ├── optimizer.py             # 策略优化器(LLM调参)
│   └── strategies/              # 6个技术策略
│       ├── base.py              # 策略基类
│       ├── ma_cross.py          # 均线金叉
│       ├── volume_breakout.py   # 量价突破
│       ├── platform_breakout.py # 平台突破
│       ├── rsi_bounce.py        # RSI超卖反弹
│       ├── zt_reversal.py       # 涨停洗盘
│       └── limit_down_reversal.py # 跌停反转
│
├── execution/                   # 执行层
│   ├── paper_account.py         # 模拟账户
│   └── order.py                 # 订单管理
│
├── risk/                        # 风控层
│   ├── position.py              # 仓位管理
│   ├── stop_loss.py             # 止损管理(固定+ATR动态)
│   └── drawdown.py              # 回撤控制+熔断
│
├── review/                      # 复盘层
│   ├── daily_review.py          # 每日复盘
│   ├── performance.py           # 绩效统计
│   └── llm_review.py            # LLM复盘(教训总结)
│
├── portfolio/                   # 组合层
│   └── backtest.py              # 回测引擎
│
└── scheduler/                   # 调度层
    ├── pipeline.py              # 管道(scan→score→risk→execute)
    ├── market_calendar.py       # 交易日历(Baostock)
    └── notifier.py              # Telegram通知推送
```

---

## 十三、定时任务脚本

所有脚本在 `~/.hermes/scripts/`:

```bash
# quant_scan.sh — 早盘扫描
cd ~/projects/a-stock-quant
set -a; source ~/.hermes/.env; set +a
timeout 600 python3 main.py --scan 2>&1

# quant_midday_scan.sh — 盘中/午盘扫描（同上）
# quant_execute.sh — 收盘执行
timeout 600 python3 main.py --execute 2>&1

# quant_review.sh — 收盘复盘
timeout 600 python3 main.py --review 2>&1
```

---

## 十四、环境变量

```env
# 长桥OpenAPI
LONGPORT_APP_KEY=...
LONGPORT_APP_SECRET=...
LONGPORT_ACCESS_TOKEN=...

# MiMo LLM
XIAOMI_API_KEY=...
XIAOMI_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1

# OpenRouter (舆情分析备用)
OPENROUTER_API_KEY=...

# 华泰证券
HT_APIKEY=...

# 功能开关
USE_LLM_TRADER=True        # True=LLM决策, False=加权打分
USE_LLM_REGIME=True        # True=LLM识别市场环境, False=规则引擎
USE_ATR_STOP=True          # True=ATR动态止损, False=固定止损
```
