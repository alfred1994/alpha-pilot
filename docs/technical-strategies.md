# K线技术策略库

更新日期：2026-07-16
执行者：Codex

策略仅产生候选信号；真实执行仍由模拟盘账户、风控和 TradePlan 控制。请先在回测中评估，再用于纸面观察。

| 策略标识 | 主要逻辑 | 更适合的市场环境 |
| --- | --- | --- |
| `ma_cross` | 均线金叉、趋势线与放量确认 | 趋势行情 |
| `macd_trend` | MACD 金叉/死叉、零轴、趋势均线和量能 | 趋势启动或延续 |
| `volume_breakout` | N 日新高与放量突破 | 强势突破 |
| `platform_breakout` | 窄幅整理后突破平台 | 低波动后的启动 |
| `bollinger_squeeze` | 布林带收敛后放量突破 | 盘整末期、波动扩张 |
| `rsi_bounce` | RSI 超卖反弹与布林下轨 | 震荡市、均值回归 |
| `kdj_reversal` | KDJ 低位金叉、高位死叉 | 震荡市短线反转 |
| `zt_reversal` | 涨停后洗盘形态 | 高波动题材行情 |
| `limit_down_reversal` | 连续大跌后止跌 | 高风险情绪修复 |
| `technical_ensemble` | 多个独立技术策略至少两票一致 | 降低单指标误触发 |

`technical_ensemble` 在技术性熊市中会将买入门槛从至少 2 票提高到至少 3 票；在其他环境维持至少 2 票。回测会根据截至信号日的 20/60 日均线关系和 20 日收益率推断 `bull`、`bear`、`sideways` 或 `rebound`，整个判断只使用当时已知 K 线数据。

## 回测用法

```bash
python main.py --backtest --mode strategy --strategy macd_trend --stocks 600519 000001 --start-date 2024-01-01 --end-date 2024-12-31
python main.py --backtest --mode strategy --strategy bollinger_squeeze --stocks 600519 000001 --start-date 2024-01-01 --end-date 2024-12-31
python main.py --backtest --mode strategy --strategy technical_ensemble --stocks 600519 000001 --start-date 2024-01-01 --end-date 2024-12-31
```

可通过 `python main.py --list-strategies` 查看当前策略标识和版本。

比较策略时应保持相同的股票池、时间区间、费用、T+1 规则和初始资金，并至少做样本外区间验证。策略参数只应在研究和模拟盘中调整，不应直接绕过风控或账户边界。
