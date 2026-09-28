# 看板审计逐项核实与修复记录

基线：`9ed2907`。原报告：[architecture-audit-2026-09-28.md](architecture-audit-2026-09-28.md)。以下记录基线核实与实际修复状态，最终全仓结果见汇总报告。仅本地源码与模拟响应检查，未访问或修改生产环境。

## E：后端与展示契约

| 条目 | 核实结论与证据 | 处理状态 |
|---|---|---|
| E1 | 成立。database router 使用默认 Database；positions、agent_status 的深层调用也会初始化 schema；shadow 显式 ensure_eval_tables。不能只改最外层连接。 | 已修：全调用链只读，缺库不得初始化账户。 |
| E2 | 部分成立。status 每次同步运行 health/watchdog/行情/事实汇总，没有缓存或并发合并。40线程耗尽属于容量风险，未在生产压测证明。 | 已修：有期限缓存、并发合并、失败退避；不返回永久过期健康快照。 |
| E3 | 成立。app.renderUnavailable 与真实 critical 都使用 danger，失败后保留旧页签数据没有标记。 | 已修：不可达与故障分开，旧数据可见陈旧标记。 |
| E4 | 成立。后端 int(value or 0) 与前端 Number(value or 0) 均丢失未知语义。 | 已修：全链路保留 null，图表与文字不伪造 0。 |
| E5 | 成立。positions 失败后回退存储价，没有时间/来源/陈旧标记，也未校验报价年龄。 | 已修：报价校验、来源与时间展示。 |
| E6 | 成立。timestamp 是快照构建时间，页脚写最后更新，无法表示底层数据日期。 | 已修：抓取/快照时间与账户、报价时间区分，缺失保留未知。 |
| E7a | 成立。每条 trade reason 查询一次 decisions，每条 decision name 最多两次查询。 | 已修：批量加载相关记录，保留历史证据语义。 |
| E7b | 成立。shadow 每变体全表载入 outcomes，baseline 还重复一遍。 | 已修：共享 outcome_map 的 evaluate_variants 接入只读路由。 |
| E7c | 部分成立。decisions SELECT * 拉 response/prompt，prompt 仍用于历史名称提取，不能直接删除该兼容路径。 | 已修：显式投影去掉未使用 response，批量名称解析。 |
| E8a | 成立。多数异常响应为 HTTP200 success=false；日期错误也夹在普通失败中。 | 已修：验证422、服务失败503，保留既有成功载荷兼容。 |
| E8b | 部分成立。不同成功格式是现有 API 契约，不能为统一外观破坏消费者；失败状态码才是实际问题。 | 保持成功格式，统一失败处理。 |
| E8c | 成立。decisions 无日期范围上限、page可很大、COUNT随请求重复。索引是否实际慢需EXPLAIN/规模验证，不以源码断言生产延迟。 | 已补日期/分页限制；总数保留，生产大库COUNT性能尚未验证。 |
| E8d | 成立。重请求无缓存/并发限制。无需引入全局第三方限流依赖即可限定重任务并发。 | 同E2。 |
| E8e | 成立。control 返回 str(e) 未脱敏。 | 已修：公共错误与503状态。 |
| E8f | 成立。adaptive 原样透传内部可变JSON，不满足白名单契约。 | 已修：字段白名单、数值类型校验。 |
| E8g | 部分成立。默认生产CORS是固定域名，并非默认*；显式*配credentials确实不合理。内部status显式开关也不能在生产泄漏密钥/命令。 | 已修：通配禁credentials；生产status始终脱敏。 |
| E8h | 成立但为可用性改进。截断没有总数，health忽略已脱敏error；通用错误摘要无法区分失败类别。 | 已提供安全数量/错误类别；健康页现显示已转义的安全错误摘要与“显示N/总M”截断提示，不暴露原始日志。 |
| E8i | 成立。单独“用户要求”导致整段合法内容折叠。 | 已修：收窄匹配，保留真正提示词与密钥防护。 |
| E8j | 部分成立。JS/CSS no-store 禁用缓存；原文称禁用ETag/304不严谨，响应仍可能附ETag。 | 已修：HTML/API no-store；静态资源允许重新验证。 |
| E8k | 部分成立。import时mkdir、sys.path修改可移除；“只读容器必崩”没有实测（已存在目录未必失败）。 | 已移除不必要副作用。 |
| E8l | 不成立。server middleware :54/:61 使用的都是 import快照 is_prod，并非每请求重读 is_production。 | 保留启动时环境配置语义。 |
| E8m | 缺HSTS属部署边界：应用未设置不代表HTTPS反向代理未设置。 | 仅应用HTTPS响应可加头，生产代理未核实。 |
| E9 | 优点，无需修复。 | 保留脱敏、CSP、只读生产与paper默认。 |

## F：前端与交互

基线浏览器验证使用本地静态服务器和固定API模拟数据；1440×1000时净值数字 y=1597.4，标题字号57.6px；390×844无整页横向溢出，但topbar-status为display:none。截图保存在系统临时目录，没有调用真实行情服务。

| 条目 | 核实结论与证据 | 处理状态 |
|---|---|---|
| F1 | 布局现象成立，P0严重度为主观产品判断。账户摘要位于决策/策略之后，实测低于首屏。 | 已修：摘要与净值图移至市场/决策区域之前，缩短首屏标题。 |
| F2 | 成立。大量8–10px正文标签，readability不足；原文数量是特定数据/视口，不视为恒定值。 | 已修：原8–11px CSS文本统一至少12px，正文/表格主要内容14px；仍需浏览器像素核验。 |
| F3 | 成立。modal无dialog语义、焦点管理、Esc或Tab约束。 | 已修：dialog语义、打开聚焦、Tab约束、Esc与焦点归还；模拟浏览器已验证。 |
| F4 | 成立。图表只有鼠标事件，没有可读取数字或键盘等价操作。 | 已修：漏斗阶段下方列出语义数值与独立按钮。未知计数显示“未知”，图表不画未知阶段。 |
| F5 | 部分成立。颜色与背景必须按实际组合计算，大号文字AA为3:1而非统一4.5；确有小字低对比度且无focus-visible。 | 已调深文字、盈亏色及焦点轮廓。按 sRGB 公式计算：强调字/沙底5.84:1、正文/沙底5.75:1、亏损/沙底5.78:1、盈利/沙底5.57:1、顶栏浅字/深底9.52:1；覆盖主要文本配色。 |
| F6 | 成立。影子只在页面生命周期加载一次，无日期/TTL；教训每轮重取。 | 已修：影子5分钟、教训1分钟TTL和同请求复用；失败不伪装空数据，旧影子指标保留并标时间。 |
| F7 | 成立。已打分/LLM数是通过人数，点击却过滤拒绝原因，且默认候选30天与今日漏斗范围不同。 | 已修：通过人数只展示；明确“全部候选”“被拒绝候选”两个筛选按钮，跳转设置今日范围、重置分页。 |
| F8 | 成立但“死循环”不是准确术语。永久轮询可重叠，后台页照常请求、慢返回可覆盖新数据。 | 已修：隐藏页不轮询、可见后刷新、在途合并、请求序号与页签异常边界。 |
| F9a | control.js 和 _load_signal_cache 为无调用死代码；不构成当前运行失败。 | 可删除已核实死入口，不改变生产控制边界。 |
| F9b | returns从research私有函数导入是维护耦合。 | 已提取共享只读/数据转换工具。 |
| F9c | text/escape重复属维护建议，并非必须抽象的缺陷。 | 不为去重大重构。 |
| F9d | 成立。actionText未知action原样进innerHTML。evolution百分比经Number().toFixed不存在HTML注入，但无效值显示NaN成立。 | 已修转义和有限数校验，模拟注入用例验证。 |
| F9e | 成立。全零但有效的benchmark序列被隐藏。 | 已修按数据存在性判定，模拟浏览器检查仍显示两条序列。 |
| F9f | 成立。推理textContent含换行但CSS不pre-wrap。 | 已修 `white-space:pre-wrap`。 |
| F9g | 部分成立。returns构造器null会击穿初始化，但当前DOM存在，属于韧性缺口。 | 已加空值防护。 |
| F9h | 成立。用本地化字符串反解析Date没有跨引擎保证。 | 已改 `formatToParts` 和明确UTC日历运算。 |
| F9i | 成立。init异步未catch，finally子模块抛错可造成未处理rejection。 | 已加异步边界捕获和公共错误提示。 |
| F9j | 部分成立。不同模块各有版本号并不自动叫漂移；改模块却忘改引用才是风险。 | 静态资源重新验证策略解决，无需全项目打包重构。 |
| F9k | 缺深链接/noscript/reduced-motion成立；暗色主题从未承诺，属于新功能而非缺陷。 | 已加hash页签恢复、noscript、减少动态效果；未重设计暗色主题。 |
| F9l | 移动状态隐藏成立；没有460px断点本身不是缺陷，应测390/320px实际溢出。 | 已显示移动状态；模拟浏览器在1440/390/320px无整页横向溢出。 |
| F9m | 成立。table无scope/caption，可补语义。 | 已给成交和每日明细表增加caption/scope。 |
| F9n/F10 | 原文描述已有优点/空态，非缺陷。 | 保持空态、脱敏、离线资源与真实数据文案。 |

## 验证

- 修复前全仓离线回归：`python scripts/run_tests.py`，85 passed，0 failed。
- 修复后全仓离线回归：`python scripts/run_tests.py`，89 passed，0 failed。
- 前端本地模拟 API 浏览器回归：`python scripts/run_tests.py --run-one tests/browser_dashboard_audit.py`，4 passed；覆盖1440/390/320px、未知漏斗、旧价标记、全零有效基准、未知动作注入、今日下钻、键盘弹窗、挂起请求超时恢复、健康页错误摘要转义及截断数量。仅本地源码，未部署或真实业务验收。

## E 域本地修复结果

| 条目 | 已实施 | 仍需真实环境验证 |
|---|---|---|
| E1 | `Database(readonly=True)`、`PaperAccount(read_only=True)` 由数据库/账户层提供；`agent_status`、`health`、`watchdog`、`trader_brief` 及 router 的公开GET显式调用只读入口。研究/收益共用 `web/read_store.py`。影子计算不建表，缺表返回 `available:false`。 | 缺库或缺账户不会初始化默认余额；未在生产DB上做IO跟踪。 |
| E2 | `SnapshotCache` 对状态、持仓、影子榜单提供10秒TTL、在途合并、3秒失败退避，最多128键；区分获取与构建时间。 | 本地12线程同键只构建一次，未做生产容量压测；首次请求仍需访问数据源。 |
| E4 | funnel仅接受有限非负整数，缺失/非法保留 `null`。 | 前端按未知状态渲染已由F浏览器回归覆盖。 |
| E5/E6 | 持仓价格经`validate_quote`，逐项返回`price_fresh`/`price_source`/`price_as_of`；响应返回`fetched_at`/`snapshot_at`/`data_as_of`。 | 旧账户无存储更新时间时为null，不推断为当前。 |
| E7 | 成交理由和股票名按页批量读取；决策使用显式列投影，不再拉完整模型response；影子共享outcome_map。 | 历史股票名仍用prompt兼容回填；真实大库COUNT耗时未测。 |
| E8a/b/c | 后端数据源失败返回503；日期、页码、kind错误返回422；成功载荷保持现有契约。判断列表默认近365日，最大366日窗口和1000页。 | 历史日期可显式选择不超过366日窗口。 |
| E8d/e/f/g | 重GET由快照缓存限并发；控制API异常脱敏503；adaptive只投射白名单；通配CORS禁credentials，生产status始终脱敏。 | 生产控制API仍默认不挂载。 |
| E8h/i | 公开日志保留安全错误类别与列表总数；移除单词“用户要求”造成的误折叠，保留其他提示词/密钥/命令脱敏。 | 日志原始异常不外显。 |
| E8j/k/l/m | HTML/API继续no-store；JS/CSS改must-revalidate；删除server import期sys.path与mkdir。E8l原断言已纠正为不成立。HTTPS响应加HSTS。 | 反向代理是否配置HSTS未读取。 |

`execution_reviews` 对外只投射待核对计划数、复盘数及需关注布尔值，原始错误与执行记录仅内部可见。后端本地回归 `test_web_public_dashboard.py`、`test_web_dashboard_data.py`、`test_web_audit_backend.py`、`test_decision_evidence.py`、`test_account_pnl_consistency.py`、`test_regime_current_endpoint.py` 已逐一通过。`test_web_audit_backend.py` 用临时数据库并拒绝可写Database，确认status、positions、shadow GET未调用初始化/迁移路径。全仓最终结果以主报告为准。
