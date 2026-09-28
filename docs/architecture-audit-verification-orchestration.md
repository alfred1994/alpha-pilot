# 编排审计逐项核实与修复（2026-09-28）

依据当前源码核实原报告，未访问生产、未调用真实行情或 LLM。测试由主代理使用离线回归运行器串行执行；本文件不把本地修改写成部署完成。

| 条目 | 结论与证据 | 处理 |
|---|---|---|
| A4/A12 | 成立。执行前 `total_assets()` 未传持仓实时价；数量预算使用同一旧估值。 | 批量严格报价校验后传 prices；不齐时不更新风控基线并禁买；成交之间及末尾按同一价格快照复评。账户层接口另见数据风控报告。 |
| A5 | 成立。系统风控调用不传 date。 | 两控制器与只读门禁统一传 plan_date。 |
| A8 | 成立。手动 execute/stop 未读共享暂停。 | pipeline 下单前及订单之间读共享控制；手动 stop 复用自动路径；账户层再兜底。 |
| A13 手动 stop | 成立。重复实现不校验日期、吞异常，且 stop执行与评估契约混淆。 | 删除重复逻辑，共用 `check_stops_once`，返回 errors，CLI据此非零退出。 |
| B1 | 部分成立。载体各有职责，数量本身不能证明必须整套状态机；阶段失败仅一个短命字段缺乏观察成立。 | 阶段完成/失败进入 auto_events；已有执行表补review领取与异常状态。保留阶段预算标记。 |
| B2 | 成立。四入口复盘守卫不共享。 | `run_review` 按日SQLite原子领取；completed才算完成，其它状态报告未完成，不自动重试有副作用的半成品。 |
| B3 | 成立。rescue run_scan覆盖主计划。 | rescue传 `persist_plan=False`，直接执行筛选产物；主cache保持主计划。 |
| B4 | 原结论不成立。plan_id是单次扫描产物身份，相同文件重试幂等；新scan_id代表新的观察和决策。 | 保留此语义，避免按代码/方向/权重合并不同扫描和清仓后重入。持仓去重和账户风控独立执行。 |
| B5 | 成立。异常可能留下executing，且缺少观察入口。 | Python异常标needs_review；hard kill保留executing，通过 `list_execution_reviews` 暴露超时和异常记录。禁止自动重放，人工先对账再处理。 |
| B6 | 成立。打分超时只回部分列表。 | 超时和失败候选加入plan.errors→PipelineResult.errors；HOLD只代表未评估，不宣称完整扫描。 |
| B7 | 成立。非原子JSON可损坏。 | 独立跨进程文件锁内读合并，唯一临时文件fsync/replace；共享control损坏按暂停处理。 |
| B8 | 成立。execution丢弃market_status，full默认路径无日历门禁。 | 正常执行仅交易日盘中；full在扫描前检查；历史回放须显式开关，临时账户隔离。手动scan仍允许只读研究生成产物。 |
| B9 | 成立。CLI及事件市场扫描未持锁。 | scan/execute/review/full和realtime扫描/复盘共用AutoLoopLock。实时止损传感器仍只建议，不直接成交。 |
| B10 | 部分成立。锁冲突确实无法自愈，但不能证明仍存在的critical是误报。Watchdog已有阶段预算豁免，预算内正常耗时不会触发该项。 | 保留未解除critical的安全暂停，明确区分“锁冲突未执行自愈”和“自愈失败”；不因锁占用掩盖真正critical。restart不应无条件清人工/安全暂停。 |
| B11 | 维护问题成立；文件规模不是正确性缺陷。 | 本次提取JSON持久化并修确定缺陷，不开展无收益巨型状态机重构。 |
| B12 | 数据层归属。 | 见数据风控核实文档。 |
| B13 超时计数 | 成立。confidence=0不是TimeoutError，计数到阈值后无法恢复。 | 删除跨扫描永久关闭；真实收集超时进入错误记录，下扫描允许重试，保持未返回HOLD。 |
| B13 stop吞错 | 成立。 | 复用自动stop并返回errors。 |
| B13 agent_status时区 | 成立。宿主时间用于今日进度。 | 北京日期与UTC+8 timestamp转换一致。 |
| B13 closure脚本 | 事实成立但缺陷推论不成立。脚本用于手动，Doctor已调用closure_repair。 | 不增加重复timer。 |
| B13 notifier | 未调用import事实成立；pipeline从不通知不等于系统无通知，自动层已推送。 | 删除死import，保留通知职责在auto_trader。 |
| B13 realtime | index无发布方属未使用入口；独立review会并发成立。 | 复盘共享领取与锁，市场事件扫描锁；未虚构index事件来源。 |
| C1 | 成立。CB直接追加绕过top_k/技术/稳定/LLM。CB专属评分不能冒充股票技术证据。 | 保留显式启用CB时的机会观察；默认缺等价技术/LLM/稳定性证据则阻断买入，并展示安全降级原因及共用预算/最低分限制。当前scanner不产生这些证据，故目前CB仅观察；未完成CB OOS或恢复买入能力。 |
| C3 | 成立。技术主路径可读半根当日线。 | get_daily请求已收盘cutoff并再过滤未收盘bar，ML/技术/影子诊断复用同份日线。 |
| C22 regime | 成立。陈旧记录不检查，异常静默。 | 只接受当日regime，缺失/异常中性confidence=0并记录plan.errors。 |
| C22 阈值 | 部分成立。综合分、技术分、按regime参数为不同语义，不能因数值不同判冲突。pipeline58是缺省。 | 不强行合并不同语义；其它策略参数整理见决策文档。 |
| D2 pipeline | 成立。attrs没有消费。 | 拒绝stale/invalid、stale_cache_days与严重内部交易日缺口；晚上市短序列可继续交给维度有效性门禁。 |
| D7 event_bus | 部分成立。await queue.put不会抛QueueFull，原报告每tick warning描述不准确；跨loop启动/停止风险成立。 | 启动前缓存普通对象，live owner loop投递；put_nowait有界丢弃用debug；跨线程stop在owner loop排空并记录数量，覆盖停止后再启动。 |
| G1 | 部分成立。原缺陷范围有缺席测试，不能以此证明套件只是测刚修路径。 | 新增进程并发JSON、异常claim、review状态、市场时段、日线cutoff/陈旧及事件loop回归。 |
| G2 | 部分成立。源码状态与部署状态必须分开；原文“已部署/未部署”依赖旧记忆，无当前生产证据。 | 本次仅本地修复和离线测试；不把commit message视为部署证明。 |
| G3 | 文档时效属于维护项。 | 更新本核实报告；待主报告汇总后应修 `docs/architecture.md` / `docs/TODO.md` 中入口、领取、CB观察和执行门禁说明。 |

## 验证与回滚

新增 `tests/test_orchestration_audit_guards.py`，覆盖跨进程JSON、失败领取、阶段状态、已收盘日线和CLI失败退出码。主代理串行完整回归 `python scripts/run_tests.py`：89 passed / 0 failed。测试使用临时账户/数据库和外部服务替身；早期子进程禁网漏拦与修复记录见总报告。

回滚只还原本次源码改动；无需部署、重启或修改真实账户。领取表中的执行记录是防重复成交证据，不应为了重试删除；应先人工核对成交与账户，再处理未完成记录。
