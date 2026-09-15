# TODO 待办事项

> 2026-06 的旧清单已完成清理：Telegram Bot 控制面板整节未实施但暂缓；
> 重构主线 Phase 2-7 实际早已落地（market_regime/llm_trader/regime_config/memory 均已存在）。
> 当前遗留按优先级重写如下（2026-09-14 全面加固后），已完成项见 docs/hardening-2026-09-14.md。
> 2026-09-16 复盘研究闭环批次：绩效接线/基准进决策链/regime 归因/A-B 门槛/影子晋级审批 已完成（勾选项，
> 测试 tests/test_review_research_discipline.py）。
> 2026-09-16 第二批：walk-forward 回测、老10策略 as_of 防穿越、交易日历节假日表降级、
> 3个存量禁网测试修复 已完成（测试基线首次全绿 73/0）。

## P2 研究能力（依赖全市场数据地基，收益最大）

- [x] **walk-forward 回测**（2026-09-16）：fast_backtest 补 vectorbt 依赖
  （requirements: vectorbt>=1.0 + plotly<6，7 移除 scattermapbox 会导致
  import 失败），新增 `--walk-forward` CLI（默认训练250根/验证60根，
  `--wf-train-days/--wf-valid-days` 可调）。每折训练窗网格选参 → 紧随的
  验证窗样本外评估，汇总以 OOS 复利收益/平均夏普/最差回撤为准并报告
  夏普衰减。注意：optimizer（LLM 建议参数）仍是样本内口径，walk-forward
  可作为其外部校验
- [ ] **回测与实盘打分口径统一**：SimpleBacktestEngine 把 sentiment/fundamental
  填中性 50 分跳过（portfolio/backtest.py:810），实盘是五维+LLM+舆情，
  `--backtest` 结论无法外推
- [x] **绩效接线**（2026-09-16）：PerformanceAnalyzer 补齐 information_ratio
  （基准累计净值对齐、跟踪误差），run_review 落盘 `performance` 块并进
  format_review 与 LLM 复盘 prompt
- [x] **基准进决策链**（2026-09-16）：benchmark_pnl_pct/超额/区间绩效/
  分环境归因进 LLM 复盘 prompt 与策略指令 prompt（指令的"相对基准改善"
  要求现在有数据可依）
- [x] **regime 归因**（2026-09-16）：`trades.market_regime` 与
  `candidate_outcomes.regime` 按环境聚合胜率/均盈（近90天），随复盘落盘
  并进两类 prompt
- [ ] **RPS 横截面激活**：数据地基就绪后把 rps_breakout 从"影子专用"纳入评分

## P2 执行与研究纪律

- [x] **A/B 自动采用参数门槛过低**（2026-09-16）：评估门槛对齐影子评估
  （20交易日/10笔），新增无依赖 Welch t 检验，p>=0.05 一律判平局；
  adaptive 自动采用双重校验 `significant`；顺带修复 test_id 秒级碰撞
  主键冲突静默失败；expire_stale_ab_tests 默认年龄 14→45 天
- [x] **影子晋级人工闭环**（2026-09-16）：新增 `--shadow-approve VARIANT_ID`
  / `--shadow-reject VARIANT_ID`（+`--shadow-note`），审批只改候选状态
  （approved/rejected 不再被重新提名），正式参数切换仍走人工指令流程
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

- [x] **3 个存量禁网测试失败**（2026-09-16）：三个测试在 run_review 内未
  mock `_fetch_hs300_daily_pct` 触发禁网护栏，按仓库离线回归惯例在模块
  导入处统一 mock；基线 73 通过/0 失败（首次全绿）
- [ ] **k_daily 历史缺口修复**：同花顺上游 ~11 天缺口，可用 Baostock 逐股
  定点补（夜间低峰执行）
- [x] **降级交易日历**（2026-09-16）：Baostock 失败时先走内置节假日表
  （2024/2025 交易所公告、2026 国务院安排，仅工作日休市）剔除休市日，
  表外年份保持"周一至五全是交易日"并显式告警（market_calendar.py）
- [ ] **通知分级**：Telegram 无 severity 分层/重试/备用渠道，熔断/停机无主动推送
- [ ] **event_driven 链路**：`--realtime` 事件进程无单实例锁、可与 --auto 并发写
  共享状态（执行有 DB claim 兜底）
