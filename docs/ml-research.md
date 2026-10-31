# 横截面 LightGBM 研究基线

本入口将固定的 60 个量价候选因子、跨股票 LightGBM、滚动时间验证和已有组合账本连接起来。
它用于第一阶段的非线性关系研究，不是自动发现可靠 Alpha 的证明，也没有模型自动晋级机制。
现有交易链路、`pooled_model.pkl` 和正式模拟账户均不读取这里的产物，继续保持 `BROKER_MODE=paper`。
后续的固定预测诊断、模型冻结和新交易日观察见 [前向观察](ml-forward.md)。

## 运行与数据

仅使用现有 `requirements.txt` 的 pandas、numpy、scikit-learn、LightGBM。
必须在实际运行研究的解释器中安装 LightGBM；缺失时明确失败，不换成另一种模型。

```bash
python scripts/run_ml_research.py --input stocks.json.gz --benchmark-input benchmarks.json --start-date 2024-02-19 --end-date 2026-10-09 --output data/reports/ml-study-new
```

默认严格模式需要完整、可信来源声明的原始成交价格、涨跌停、可交易状态、独立日历等。
数据不满足时保留审计报告并返回退出码 2，不训练模型，不把没有成交解释成策略收益。
明确做近似研究时加 `--execution research`；报告仍保留全部数据缺口，不将复权价冒充原始成交价。

两个输入都支持 JSON 数组，或如下包装对象；`.json.gz` 同样可读：

```json
{
  "bars": [{"code":"600001","date":"2024-02-19","open":10,"high":11,"low":9,"close":10,"volume":100000,"source":"verified_provider"}],
  "expected_codes": ["600001"],
  "metadata": {"source":"verified_provider","adjustment":"qfq","universe":"current_pool"}
}
```

上面的单行只展示格式，不能用于训练。`calendar` 可提供独立交易日列表，但必须在
`metadata.calendar_source` 说明出处，股票和基准的声明须一致。不能从当前行情日期并集
生成一个“独立日历”。更多来源声明遵循 [研究证据层](research-evidence.md)。

基准输入必须含 `510300`、`510500`，并覆盖整个评估期。其他 ETF 不参与本实验。
代码必须保留为 6 位字符串。股票范围沿用现有账本的普通主板契约，不能混入创业板、科创板或指数。
审计拒绝周末、盘中时间、重复行情、错误 OHLC、缺失的指定证券；不会修改、补齐或静默清洗原始输入。
缺少独立日历时，工作日假期或全市场一起缺一天的情况仍无法完整判定。

可从本地已有数据库生成只读、冻结的主板研究池快照：

```bash
python scripts/export_ml_research_panel.py --db data/quant.db --universe data/research_universe.json --start-date 2024-02-19 --end-date 2026-10-09 --adjustment qfq --output stocks.json.gz
```

`--adjustment` 是来源核验后的声明，默认 `unknown`，不能为通过检查随意填写。
导出器使用 SQLite `mode=ro` 和同一读事务，按批编码，默认最多 100 万行。
不按收益、新鲜度或历史长度筛选股票；仅排除执行契约不支持的板块，排除清单写入元数据。
没有日线的指定股票仍留在 `expected_codes`，后续审计会阻止把它悄悄删除。
`--output -` 输出 gzip 二进制流，摘要走 stderr，可用于 SSH 只读传输。失败或中断的快照不可用于训练。

## 因子与标签

在 5、10、20、60 个观测交易日窗口分别计算 14 类因子，共 56 个，再加入 4 个当日形态因子：

- 收益、相对均线、收益波动、量比、成交量变异系数。
- 区间振幅、区间位置、距窗口最高价回撤。
- 日内收益均值/标准差、隔夜跳空均值/标准差、上涨比例、收益与量变相关性。
- 实体、当日振幅、上影线、下影线比例。

全部无量纲，避免把绝对价格差异学成跨股票规律；每天只在当时可用股票间做百分位排名。
没有全样本标准化、未来值填补或随机切分。缺报价与零量不构造新信号，滚动窗口中的缺失不自动跨越。
同日可用性只依赖当日数据和 `eligible`；未来是否有标签不影响当天候选排名。

默认标签是信号日 t 之后 **t+1 开盘至 t+6 开盘** 的收益，扣比例佣金、印花税、滑点后，
减去当日可标注股票的平均收益。它用于相对强弱排序，不是未来账户净收益。
最低佣金、整手、排队、涨跌停受阻等没有放进单样本标签；最终组合回测再通过账本约束实际成交。

模型固定为 160 棵、深度 4、15 叶、学习率 0.04 的 LightGBM 回归器，最多 3 个线程。
训练目标只按训练段 1%/99% 分位裁剪，样本权重让各训练日期等权。
没有用验证期做早停、调参、筛选因子或挑选最优模型；模型自己学习交互和选择分裂。
本版尚不生成新因子表达式，也不接入无界搜索。

## 验证和比较

默认训练 250 个交易日，验证 60 日，然后向前滚动。约需 371 个以上完整交易日覆盖特征预热、
训练、首个验证窗和成交；缺行情、股票数量不足会增加实际需求。
每一折训练标签的**最终实现日期严格早于验证首日**。验证股票缺少未来报价时，预测保留，标签记缺失。

默认 10 万元初始资金，每 5 日等权选择预测前 20 只，与以下预先固定的方案比较：

1. 相同候选、调仓日期、持仓数和费用下的 20 日动量排序。
2. 沪深300 ETF、中证500 ETF 的同期买入持有。
3. 现金收益 0。

所有账户起止日期必须一致，否则停止比较。ETF 不收股票印花税，价格刻度为 0.001；
股票价格刻度 0.01；均计佣金 0.03%、最低 5 元、单边滑点 0.1%、100 股/份整手及 T+1。
各验证窗共用连续账本，资金不足一手留现金，不重置账户，不虚构期末平仓。
持仓缺报价会产生 `incomplete_valuation`；近似成交为 `approximate`，都不是执行级验收通过。

报告同时保留收益、回撤、换手、仓位、费用、受阻原因、每日 RankIC 和预测误差。
窗口标签互相重叠、同日股票相关，不能把样本数当成独立统计观测，也不能把重要性当作因果。

## 产物和运行边界

输出目录必须不存在。脚本先写 `manifest.json`，冻结输入文件哈希、源代码指纹、依赖版本、
配置、因子和比较规则，再计算收益。执行结束重新校验输入哈希，拒绝运行中改变的输入。

- `report.md` / `summary.json`：结果、数据缺口及进程耗时、CPU 时间、峰值驻留内存。
- `report.json`：完整审计、逐窗验证、训练特征重要性和样本覆盖。
- `account-*.json`：每个组合的连续净值、持仓、成交和受阻订单。
- `predictions.csv.gz` / `selections.json`：可追溯的逐股票预测及调仓目标。
- `evidence.sqlite3`：复用现有研究登记契约，独立于正式账户和默认研究库。
- `failure.json`：异常原因；不覆盖旧目录，不删除失败实验。

全部历史运行标记 `interval_role=review`、`promotion_allowed=false`，结论保持 `inconclusive`。
执行完成、近似历史收益和独立前向验证是三件不同的事。已经多次研究过的 2026 年数据不能重新
包装成全新独立测试；下一步需冻结规则后收集新市场数据。

本入口不请求行情或 LLM、不访问 Broker、不启动调度、不部署和重启服务。停止运行研究入口即可回滚；
保留独立报告用于复查，不需修改正式账户。资源测量属于实际运行它的机器，不可当成另一台服务器的压测。

```bash
python scripts/run_tests.py --run-one tests/test_ml_research.py
python scripts/run_tests.py --run-one tests/test_research_evidence.py
python scripts/run_tests.py --run-one tests/test_factor_research.py
python scripts/run_tests.py --run-one tests/test_ml_baseline.py
```
