# 旁路研究：先验证，再接管

这套功能检查研究数据，登记实验，并使用同一执行账本比较最后一道 LLM 买入过滤的增益。
它不连接 Broker、不改变正式模拟账户、不修改次日策略生效方式，不向复盘 Prompt 注入新结论。
`BROKER_MODE=paper` 保持不变；自动记录和盘后任务默认关闭。

## 入口与档案

```bash
python main.py --research-audit --research-input panel.json
python main.py --research-report
python main.py --research-report --research-id EXPERIMENT_ID
python main.py --research-audit --research-import old-report.json --research-hypothesis momentum-v1
python main.py --research-audit --research-archive --research-id EXPERIMENT_ID
```

`EXPERIMENT_ID` 是命令返回的 32 位十六进制标识。可加 `--research-db /absolute/path/evidence.db`。
默认使用 `data/research_evidence.db`，环境变量 `RESEARCH_EVIDENCE_DB` 可覆盖。
`--research-report` 完全只读，不建库、不迁移、不计算；分页使用 `--research-limit`（1–100）与 `--research-offset`。
`audit`、`compare` 仅写研究库；不得将研究库路径指定成正式账户库。

多因子与 ML 纯计算函数返回 `data_audit`，仍不执行 IO；对应 CLI 完成后登记报告及参数。
ETF 比较脚本保存独立 `data-audit.json` 并登记数据和最终比较摘要，不修改旧报告。
上述既有研究均登记为 `review`，不能把已经看过的历史重新称作独立样本外验证。
旧报告通过适配器原样嵌入档案，未知的代码、来源和区间信息保持未知。

实验记录使用输入指纹、代码版本（Git HEAD + 实际相关源码 SHA256）、参数、假设和区间角色识别。
同一登记内容复用 ID；参数或代码变更新建版本，可用 `--research-parent` 关联前次实验。
报告按内容去重、只追加，保留失败原因和全部历史快照。状态为 `registered/running/waiting_data/completed/archived`；
结论独立存储为 `supported/refuted/inconclusive`。本版自动比较只给描述性结果，结论保持 `inconclusive`，不把盈利或满 60 日当成显著性证明。
归档仅停止该实验后续更新，不删除数据。

## 本地数据格式和审计

输入是 UTF-8 JSON 对象。以下结构仅展示字段，单行不构成完整可运行实验：

```json
{
  "calendar": ["2026-10-12", "2026-10-13"],
  "metadata": {
    "calendar_source": "独立交易日历来源",
    "source": "已核验的数据供应方",
    "adjustment": "raw",
    "universe": "point_in_time",
    "delisted_coverage_verified": true,
    "corporate_actions_verified": true
  },
  "bars": [{
    "date": "2026-10-12", "code": "600001",
    "open": 10, "high": 10.1, "low": 9.9, "close": 10, "volume": 100000,
    "raw_open": 10, "raw_close": 10, "tradable": true,
    "limit_up": 11, "limit_down": 9, "eligible": true,
    "source": "供应方", "corporate_action": false
  }]
}
```

- `calendar` 必须来自独立日历，不能由行情日期并集冒充；它用于发现所有股票同时缺一整天的情况。布尔字段必须是布尔值或数字 0/1。
- `raw_open/raw_close` 为未复权价，涨跌停价为当日实际限制。元数据应在核验后填写，不能为了通过检查直接填 true。
- `universe=point_in_time` 需要历史成员与退市覆盖说明；自动前向实验使用现场冻结的 `forward_observed` 股票池，不声称还原了历史全市场。
- 复权口径接受 `raw/qfq/hfq/mixed_documented`，未声明即未知。历史行业、市值覆盖也展示，但本次不启用中性化规则。
- 公司行动未核验则只能近似研究；出现公司行动会阻断，因为共享账本尚未实现分红送转账务。
- 逐年、逐股票、逐字段报告覆盖和缺失日期。`strict_ready` 表示具备进入严格模拟的输入条件；`approximate_only` 表示关键证据不全；`unavailable` 表示空输入或无效行情。
- `execution_verified` 始终不是数据审计的盈利结论。声明齐全不等于独立验证了提供方，也不保证集合竞价成交。

比较任务要求输入覆盖实验起点以来的候选股票与 510300、510500 日线，使用共同交易日轴。
缺数据输出 `waiting_data` 和具体原因，不将失败计成现金收益。当前 `k_daily` 未必具备所需原始成交元数据；本任务不自动下载或购买补齐。
文件应由现有数据准备流程更新，建议原子替换。已用于计算的行情发生修订时必须新建实验，旧记录不覆盖。

## 开启前向观察

在本地或已另行批准的部署环境中：

```bash
# 先使用完整本地输入创建实验，保存返回的 experiment_id。
python main.py --research-compare --research-input /absolute/path/panel.json
# 查询或手动更新同一实验，勿反复新建前向实验。
python main.py --research-compare --research-input /absolute/path/panel.json --research-id EXPERIMENT_ID
```

在 15:15 前首次运行只登记实验，等待新扫描；盘后才执行比较。可用 `--research-start YYYY-MM-DD` 指定今天或未来的起点。
前向实验仅接受登记后由扫描接入点现场保存的数据，不能导入历史 LLM 回答。
在应用启动环境中设置 `RESEARCH_EVIDENCE_ENABLED=1` 后，完整主扫描会保存旁路样本；盘后自动循环独立启动研究子进程。
子进程只读取该实验登记的本地输入文件，不调用行情供应方或 LLM。`RESEARCH_EVIDENCE_DB` 应使用绝对路径，使 CLI、Web 与自动循环指向同一档案。

盘后任务与正式复盘的成功、失败及领取状态独立。成功每天只跑一次；失败或中断 30 分钟后可重试。
每个实验具有进程间锁；重复调用不会重复成交。健康状态记录原因代码、尝试时间和最后成功时间，30 分钟未结束的任务标记过期。
研究子进程没有交易接口；其异常不改变订单、复盘结论或正式循环结果。
未创建实验、未维护本地输入或未启用现场记录时，不会凭空产生有效的前向证据。

## 比较口径

每天选北京时间 09:30 至 14:50 内最后一份完整主扫描；评分未完成、救援扫描和超过截止时间的扫描不入选。
LLM 超时不将整个扫描抹掉，而是记录失败类别，保留系统整体效果和有效判断配对统计。
收盘后冻结选择；缺扫描不产生新目标，旧目标未成交部分仍按原账本约定重试。

|组合|规则|
|---|---|
|A|当天评分过门槛后按分数排序选 Top K；同分按代码排序|
|B|只保留 A 中已有合法 LLM BUY 的股票；不从后续排名补选|
|C|510300 买入持有|
|D|510500 买入持有|

A、B 单股权重均为 `min(max_weight, 1/top_k)`；过滤的份额留现金，不集中补满。
二者共用日频目标、次日开盘执行和退出规则，衡量最后一道买入过滤，不复刻正式盘中卖出与止损。
候选评分和策略参数本身可能含 AI 信息，因此 A 不是完全无 AI 系统。

每个组合初始资金 10 万元。股票单边佣金 0.03%、最低 5 元、卖出印花税 0.05%、单边滑点 0.1%；
ETF 不收股票印花税、价格刻度 0.001。共享原有整手、T+1、现金约束、涨跌停受阻、旧价估值等逻辑。
缺严格成交元数据不会切换成近似模式。期末保留持仓，不虚构平仓。

报告包含连续净值、净收益、回撤、换手、费用、仓位、成交受阻、扫描和策略版本，以及 B-A 净收益差。
配对指标另以有效判断的同一候选构造 T+5 收益标签；它扣比例费用，不具有整手和最低佣金账务，不能当成账户收益。
LLM 判断区分 `valid/not_evaluated/timeout/parse_error/unavailable/error`。
记录已返回个股决策调用耗时（含上下文构建）；供应方账单和 token 用量未暴露给现有决策对象，因此费用为 `null`，绝不填写为零。

满 60 个有效扫描交易日只标记首次评审到期，不自动停止后续记录或启用策略。
缺扫描、少于 30 个成熟配对样本、研究数据不完整或收益差集中在少数日期时明确提示证据不足。
前五日贡献占比按每日 B-A 净值差变化的绝对值计算，超过 50% 提示集中；这是诊断规则，不是显著性检验。

历史复查使用 `--research-retrospective --research-start ... --research-as-of ...`，可在输入中增加 `scans`。
每条扫描包含 `scan_id/day/observed_at/complete/strategy_version/params/candidates`；时间戳必须带时区，
`params` 含 `top_k/min_score/max_weight`，候选含 `code/composite/llm_action/llm_confidence` 和可选 `llm_status`。
历史导入永远标记 `review`，不重新请求 LLM；未来行情会在计算前截断。

## 只读 API、验证与回滚

- `GET /api/research/experiments?limit=50&offset=0`：实验摘要列表。
- `GET /api/research/experiments/{id}`：单个摘要。
- `GET /api/research/health`：旁路任务状态和最后成功时间。

GET 仅使用 SQLite `mode=ro` 读取已保存结果，不计算、不抓取、不迁移。
公开响应使用字段白名单，不输出输入文件路径、原始报告、自由文本假设、Prompt 或密钥。
完整诊断通过本地 CLI 查阅。没有数据库时返回空列表，不创建空库。

```bash
python scripts/run_tests.py --run-one tests/test_research_evidence.py
python scripts/run_tests.py --run-one tests/test_factor_research.py
python scripts/run_tests.py --run-one tests/test_ml_baseline.py
python scripts/run_tests.py --run-one tests/test_research_dashboard.py
python scripts/run_tests.py
```

设置 `RESEARCH_EVIDENCE_ENABLED=0` 可停止后续自动采样与调度；已经启动的只读研究计算可能完成当前报告。
归档实验可停止其后续更新，保留独立数据库供复查。回滚不要求删除或修改正式账户。
代码验收、真实数据完整性验收、长期前向收益验证与生产部署是不同阶段，需分别报告。
