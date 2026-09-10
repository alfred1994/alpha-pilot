# Sequoia-X 技术选股借鉴

参考仓库：https://github.com/sngyai/Sequoia-X

审阅版本：`444c0db69ff36b46ef2b22ab265051d60c16029d`。README 声明 MIT，但此版本树未找到 LICENSE 文件；只借鉴通用算法思想，独立实现，不复制上游策略源码或引入其运行时依赖。

## 值得借鉴与实际差异

| 上游模块 | 价值及限制 | 本项目处理 |
| --- | --- | --- |
| `high_tight_flag.py` | 强势、窄幅、缩量候选；40 日高低价比未保证先上涨后整理，且没有突破确认 | `high_tight_flag` 明确拆成 30 日上涨、10 日整理、当前放量突破；所有前高和均量基线排除当前日 |
| `rps_breakout.py` | 多股收益横截面排名值得采用；120 条数据收益未保证各股起止日一致，接近滚动高点 90% 并非突破 | `rps_breakout` 默认 60 日收益、至少 20 只日期完整对齐样本，RPS ≥ 90 且收盘突破此前 20 日高点 |
| `turtle_trade.py` | 20 日新高 + 成交额 + 阳线过滤，但不是完整海龟（无单位仓位/加仓/退出） | `turtle_trade`：20 日突破入场、成交额≥1亿、阳线确认；10 日低点退出；metadata 提供 ATR 单位股数、止损价与 0.5N 加仓档位（最多 4 单位）。不自动改账户 |
| `limit_up_shakeout.py` | 涨停洗盘与现有策略重叠 | 保留 `zt_reversal`，不堆叠重复投票 |
| `uptrend_limit_down.py`（上游思想） | 上升趋势中的跌停反包 | `uptrend_limit_down`：MA20>MA60 + 近 60 日涨幅≥8% + 跌停日 + 次日阳线吞没 + 量能不萎缩 |

## 接入位置

| 层级 | 策略 | 说明 |
| --- | --- | --- |
| 注册表 | 全部 4 个 | `get_strategy("high_tight_flag"|"rps_breakout"|"turtle_trade"|"uptrend_limit_down")` |
| 影子筛选 | 全部 4 个 | `technical_screen.STRATEGIES`，盘中只读诊断，不直接下单 |
| 技术集成 | 海龟/旗形/跌停反包 | `technical_ensemble` v1.1；RPS 因需横截面不进投票，仅影子 |
| 盘中 raw_scores | technical_patterns | `pipeline` 写入 `raw_scores.technical_patterns`，默认 shadow |

## 输入契约

通过 `get_strategy(name)` 使用，均返回标准 `Signal`。`score` 是形态强度/排名，不是上涨概率。

```python
signal = strategy.generate_signals(code, daily_bars, as_of="2026-09-08")
rps_signal = rps.generate_signals(
    code, daily_bars, as_of="2026-09-08", universe_closes=panel,
)
turtle_signal = turtle.generate_signals(
    code, daily_bars, as_of="2026-09-08", total_assets=1_000_000,
)
```

- `daily_bars`：同一复权口径完整日线，含 `date/open/high/low/close/volume`（可选 `amount`）。
- `panel`：日期索引、代码列的收盘价宽表；RPS 无横截面时明确 HOLD。
- `total_assets`：海龟单位仓位计算用；缺省 0 时只给入场/退出信号，`unit_shares=0`。

## 验证和上线边界

`python tests/test_sequoia_patterns.py` 覆盖：

- 高窄旗形：先跌后整理反例、未突破/未缩量、shift 前高、未来数据截断
- RPS：相对排名 vs 绝对涨幅、样本不足、复权不一致、重复日期
- 海龟：突破入场、成交额过滤、跌破窗口低点退出、ATR 单位
- 跌停反包：趋势成立/不成立、吞没形态

交易有效性仍需滚动样本外、成本、涨跌停可成交性、T+1 与回撤验证。形态通过 ≠ 可自动下单；ensemble 共识仍受账户、风控与 TradePlan 约束。
