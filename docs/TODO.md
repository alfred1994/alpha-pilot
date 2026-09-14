# TODO 待办事项

> 2026-06 的旧清单已完成清理：Telegram Bot 控制面板整节未实施但暂缓；
> 重构主线 Phase 2-7 实际早已落地（market_regime/llm_trader/regime_config/memory 均已存在）。
> 当前遗留按优先级重写如下（2026-09-14 全面加固后），已完成项见 docs/hardening-2026-09-14.md。

## P2 研究能力（依赖全市场数据地基，收益最大）

- [ ] **walk-forward 回测**：fast_backtest 补 vectorbt 依赖并接 CLI，参数网格改为
  滚动 train/validation 评估（当前 fast_backtest/optimizer 均为样本内过拟合，
  全库无 walk-forward）
- [ ] **回测与实盘打分口径统一**：SimpleBacktestEngine 把 sentiment/fundamental
  填中性 50 分跳过（portfolio/backtest.py:810），实盘是五维+LLM+舆情，
  `--backtest` 结论无法外推
- [ ] **绩效接线**：PerformanceAnalyzer（夏普/索提诺/卡尔玛/最大回撤）接进
  run_review 与 LLM 复盘 prompt；information_ratio 声明未实现
- [ ] **基准进决策链**：benchmark_pnl_pct 目前只进 JSON 和 web 画图，
  不进 LLM 复盘 prompt、不进策略指令 prompt（指令却要求"相对基准改善"）
- [ ] **regime 归因**：`trades.market_regime` 列闲置，无分环境胜率归因；
  `candidate_outcomes.regime` 未被聚合使用
- [ ] **RPS 横截面激活**：数据地基就绪后把 rps_breakout 从"影子专用"纳入评分

## P2 执行与研究纪律

- [ ] **A/B 自动采用参数门槛过低**：ab_test 3 笔/2 天即可改写生产参数
  （adaptive.enable_ab_testing 默认开启），应提高到与 shadow_eval 一致
  （20 交易日/10 笔）并加显著性检验
- [ ] **影子晋级人工闭环**：shadow_eval 晋级候选只写 `shadow_promotions`
  candidate 行，缺 `--shadow-approve` 审批命令
- [ ] **老 10 策略补 as_of 防穿越**：ma_cross/rsi_bounce 等不支持 `as_of`、
  无 daily_window 校验（新 4 策略已有）
- [ ] **撮合现实性**：涨跌停无法成交/一字板判断（需每日涨跌停价表）、
  滑点模型、部分成交模拟；回测撮合层同缺
- [ ] **复权因子表**：k_daily 只存 qfq 且基准固化在写入时刻，除权后旧历史
  行混合复权风险；建议每年对持仓+研究池跑 `--backfill-full` 重置（临时方案），
  长期补复权因子表

## P3 选股增强

- [ ] **行业维度**：Candidate.industry 当前存"量比3.5"类垃圾文本，舆情板块
  加成用关键词匹配股票名称几乎不命中；需接真实行业数据后做行业中性化/集中度
- [ ] **打分反馈闭环**：各池打分分段硬编码，无历史胜率回溯自动调权
- [ ] **全市场技术扫描**：本地全量库就绪后，把形态扫描从活跃股 Top200
  扩展到全市场（并行化）
- [ ] **指标收敛**：RSI/MACD/ATR/KDJ/BOLL 各有 2-4 套实现且数值不一致，
  收敛到 ths_indicators.py 向量化实现 + 常驻缓存层
- [ ] **解禁/质押/商誉风险维度**深度集成（解禁目前仅活跃股前 30 只软惩罚）

## P3 运维与数据质量

- [ ] **3 个存量禁网测试失败**：test_account_pnl_consistency /
  test_issue_fixes / test_multi_day_replay 内部 mock 不完整
- [ ] **k_daily 历史缺口修复**：同花顺上游 ~11 天缺口，可用 Baostock 逐股
  定点补（夜间低峰执行）
- [ ] **降级交易日历**：Baostock 失败时回退"周一至五全是交易日"（market_calendar.py:73），
  可选本地节假日表兜底
- [ ] **通知分级**：Telegram 无 severity 分层/重试/备用渠道，熔断/停机无主动推送
- [ ] **event_driven 链路**：`--realtime` 事件进程无单实例锁、可与 --auto 并发写
  共享状态（执行有 DB claim 兜底）
