# 冻结模型的前向观察

此阶段接续 [ML 研究基线](ml-research.md)：先诊断固定历史预测的失效与数据缺口，
再从真实登记时间开始保存新信号。它不回填历史信号、不把历史 +44.24% 作为账户初始收益，
不更新正式 pooled 模型，不连接 Broker 或切换自动交易策略。

## 已实现的边界

- 股票池、60 个因子、LightGBM 参数、训练输入、模型内容、代码和依赖版本在初始化时冻结。
- 使用最新已收盘交易日。必须在下一交易日 09:30 前实际记录预测；CLI 没有伪造时间参数。
- 训练窗口 250 日，收益标签实现日期严格早于信号日；观察期内固定模型，不依据新结果重新拟合。
- 默认观察 60 个新交易日，每日保存预测，每 5 日记录调仓目标；到期不延长、不自动晋级。
- 沪深300 ETF、中证500 ETF 和同池动量策略保留为固定对照。
- 仅有复权日线时可记录预测与成熟收益标签；**严格成交数据不足时账户收益为未提供，不记作 0%**。
- 独立 SQLite 状态使用事务。重复、并发执行不会重复追加；源代码变化、数据修订、漏跑日期均阻断。

## 初始化

在实际研究解释器中运行，保持 `BROKER_MODE=paper`：

```bash
python scripts/run_ml_forward.py init --state data/reports/ml-forward-new/forward.db --input stocks.json.gz --benchmark-input benchmarks.json --calendar-input baostock-calendar.json
```

股票输入支持 JSON 数组或含 `bars`、`expected_codes` 的包装；ETF 输入含 `510300`、`510500`。
输入必须截止最新已收盘日，并覆盖冻结股票池在该日的全部代码。停牌应提供来源明确的零量或
`tradable=false` 记录，不能把缺行情股票静默删掉。非当前交易日的数据不能冒充现场预测。

日历使用 Baostock `query_trade_dates` 的完整原始响应，含 `error_code="0"` 和
`data=[["YYYY-MM-DD","0或1"],...]`，所有自然日必须连续且不重复。
本入口不使用“工作日都开市”的降级猜测。日历覆盖不足时阻断；后续可追加新的已核验日历区间，
但不能改写先前冻结的日期。使用包含未来日期的交易日历不等于读取未来行情。

首次结果只有登记状态和预测数量，`observed_days=0`。每个独立研究账户计划初始资金 10 万元，
尚无前向成交或收益。模型保存在 SQLite 内的 LightGBM 原生文本中，不使用 pickle。
训练和模型校验都使用同一冻结输入精度，后续不可偷偷替换。

## 每日推进

收盘 15:15 之后、下一交易日开盘之前，提供新一天的日线：

```bash
python scripts/run_ml_forward.py advance --state data/reports/ml-forward-new/forward.db --input new-stocks.json --benchmark-input new-benchmarks.json --calendar-input baostock-calendar.json
python scripts/run_ml_forward.py status --state data/reports/ml-forward-new/forward.db
```

输入可以仅含新一天，也可含完整历史。提供重叠历史时，已有行情必须与冻结记录一致；发现供应商
修订会停止，不能覆盖旧输入再重算。只提供新一天无法发现供应商对未提供旧日期的修订，需额外
复权/公司行动核验。至少每次公司行动或来源变更时提供可核验的完整历史。

不自动补造遗漏日期。如果上次观察为周一、现在已到周三收盘，不能事后模拟周二已记录预测。
同一天相同输入返回 `unchanged`，已冻结当日的修订会被拒绝。观察达到预设天数后结束，
不会自动延长到结果变好为止。改规则、模型、股票池或依赖版本须新建实验。

状态数据库包括冻结 manifest、压缩训练输入、模型、每日现场记录时间、预测、目标、
逐日结果及已经核验的执行输入。状态命令只读，不创建空库。

2026-10-15 已接入独立服务器调度。原实验因漏日保留为中断记录，新版本在15日开盘前以14日收盘数据登记，
冻结574只股票预测；与六模型实验共用公开行情采集。详见[运行修复](operations-repair-20261015.md)。

本命令不安装定时任务，也不自动向行情商发请求。生产服务器的定时执行和数据供给接入是单独的
部署步骤；不能把一次初始化称作持续监控已运行。停止调用即可停止此实验，无需修改正式账户。

## 严格成交数据门禁

可为初始化或推进增加 `--execution-input execution.json`。格式复用 [研究证据层](research-evidence.md)：

- `bars` 应覆盖观察起点以来的冻结股票池和两只 ETF，包含 `raw_open/raw_close`、真实涨跌停价、
  `tradable`、来源及公司行动信息；全部股票/日期缺口都会显式列出。
- `metadata.universe` 必须为 `forward_observed`，并声明真实数据来源、复权口径、
  独立日历来源和公司行动核验结果，不能为通过检查随意写 `true`。
- 公司行动未实现账务，发现事件会阻断，不把现金分红/拆股忽略成净收益。
- 不完整批次只记录缺口，不冻结成已确认执行事实；补齐后可用先前现场记录的信号重放。
  已确认的执行价格不可修订；任何已生成的历史净值也不得改变。

成交元数据可能晚于预测到达，但预测本身必须事先保存。只有完整数据才通过已有严格账本计算：
次日开盘、T+1、整手、现金限制、最低佣金、滑点和涨跌停受阻。
无法排队成交、市场冲击、真实账户回报仍未验证；即使严格日线模拟成功，也不自动批准上线。

`waiting_execution_data` 表示预测研究可继续、账户收益尚不可计算。
`label_metrics` 只是已经成熟的比例扣费标签统计，不含真实账户的整手和最低佣金约束，不可当作 P&L。

## 固定预测诊断

```bash
python scripts/diagnose_ml_research.py --experiment data/reports/existing-ml-run --calendar baostock-calendar.json --suspension-evidence suspended-code-a.json suspended-code-b.json --output data/reports/ml-diagnostic-new
```

诊断验证原输入 SHA256，重新核对冻结标签，输出：独立日历缺口、各窗口的因子 RankIC、
选股的因子暴露、尚未成熟标签与真正缺报价的区分、第三方停牌状态。
它不重训、不修改历史收益、不按结果翻转因子方向。因子与表现的相关变化只形成待验证解释，
不证明因果。停牌证据不把旧价变成可卖出的价格，也不消除历史股票池偏差。

## 回归检查

```bash
python scripts/run_tests.py --run-one tests/test_ml_forward.py
python scripts/run_tests.py --run-one tests/test_ml_research.py
python scripts/run_tests.py --run-one tests/test_research_evidence.py
python scripts/run_tests.py --run-one tests/test_etf_forward.py
```

覆盖真实时间截止、训练标签隔离、漏跑/修订阻断、并发幂等、日历扩展、模型校验、
严格成交门禁与补齐、历史账本不可改变以及观察终止。合成数据测试只检验实现契约，不是收益证据。
