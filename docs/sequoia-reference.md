# Sequoia-X 技术选股借鉴

参考仓库：https://github.com/sngyai/Sequoia-X

审阅版本：`444c0db69ff36b46ef2b22ab265051d60c16029d`。README 声明 MIT，但此版本树未找到 LICENSE 文件；本次只借鉴通用算法思想，独立实现，不复制上游策略源码或引入其运行时依赖。

## 值得借鉴与实际差异

| 上游模块 | 价值及限制 | 本项目处理 |
| --- | --- | --- |
| `high_tight_flag.py` | 强势、窄幅、缩量候选；40 日高低价比未保证先上涨后整理，且没有突破确认 | `high_tight_flag` 明确拆成 30 日上涨、10 日整理、当前放量突破；所有前高和均量基线排除当前日 |
| `rps_breakout.py` | 多股收益横截面排名值得采用；120 条数据收益未保证各股起止日一致，接近滚动高点 90% 并非突破 | `rps_breakout` 默认 60 日收益、至少 20 只日期完整对齐样本，RPS ≥ 90 且收盘突破此前 20 日高点 |
| `turtle_trade.py` | 20 日前高使用 shift(1)，另有成交额与阳线过滤；不构成包含仓位/退出的完整海龟系统 | 已有 `volume_breakout` 覆盖同类突破，不重复注册另一套 |
| `limit_up_shakeout.py` 等 | 涨停洗盘与现有策略重叠 | 保留现有 `zt_reversal`，不堆叠重复投票 |

上游 `tests/test_strategy.py` 主要验证空行情下返回列表类型，没有提供上述策略的样本外收益证据。筛选条件通过、单元测试通过不表示盈利有效；两项新策略仅用于发现、诊断与研究，没有加入 `technical_ensemble` 的自动投票。

## 输入契约

通过 `get_strategy("high_tight_flag")` 或 `get_strategy("rps_breakout")` 使用，均返回标准 `Signal`。`score` 是形态强度/排名，不是上涨概率。

```python
signal = strategy.generate_signals(code, daily_bars, as_of="2026-09-08")
rps_signal = rps.generate_signals(
    code, daily_bars, as_of="2026-09-08", universe_closes=panel,
)
```

`daily_bars` 为同一复权口径的完整日线，含 `date/open/high/low/close/volume`。调用者必须排除尚未收盘的日 K；`as_of` 是包含端点的历史截止日期，省略则使用输入最后日期，不会自动猜测交易所日历。输出 metadata 含逐条件布尔值、窗口开始、突破基线和可核对指标。

`panel` 为日期索引、证券代码列的收盘价宽表，必须包含目标证券同口径历史，并由调用者按当时可知的股票池构建。收益窗口要求每个交易日有有限正价格，不前向填充缺失/停牌行情；不足 20 只则 HOLD。目标日线中零量、无效价格、重复日期同样 HOLD。只有价格宽表无法判断上游已人为填充的停牌行情，调用者需将这种数据置为缺失。代码按精确字符串匹配；此层不猜测交易所。

RPS 未传横截面时明确 HOLD。策略声明 `requires_cross_section=True`，回测入口 `--backtest --mode strategy --strategy rps_breakout` 按信号日组装多股横截面；有效标的不满 20 只不生成买入。`python -m strategy.technical_screen` 可读取本地研究数据库作横截面筛选，不自动联网补行情。不能用今天仍上市的股票列表替代历史股票池；财务/成分数据亦须遵循披露时点。

## 验证和上线边界

`python tests/test_sequoia_patterns.py` 覆盖先跌后整理反例、未突破/未缩量、shift(1) 前高、横截面相对排名、未来数据截断、复权不一致、缺失/停牌/重复日期与不足样本。交易有效性仍需滚动样本外、成本、涨跌停可成交性、T+1 与回撤验证。

盘中 `raw_scores.technical_patterns` 保存最近完整交易日的候选形态诊断，默认 shadow，不用日线 BUY 直接下单。撤回时移除调用/策略注册即可；不涉及数据库迁移或已有持仓退出规则。
