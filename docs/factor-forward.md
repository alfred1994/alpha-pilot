# 因子实验的前向观察

承接 [因子发现研究](factor-discovery.md)，把原 60 特征、扩展 66 特征、三种类别消融和公式增强模型全部冻结，
在后续新交易日并列观察。六种模型共用一个冻结股票池；另保留同池动量、沪深300 ETF、中证500 ETF，
计划九个独立模拟研究账户，每账户初始资金 10 万元。

仅运行研究链路，保持 `BROKER_MODE=paper`。本功能不会写正式账户、pooled 模型、交易策略或已有前向状态库。
实现借鉴公式搜索方法，独立编写，没有引入 AlphaMaster 源码或依赖。

## 初次登记与当前运行版本

2026-10-13 19:06:05（北京时间），在本地登记了 `data/reports/factor-forward-20261013/forward.db`。
股票范围沿用此前固定的 584 股主板池，最新已收盘行情截至 2026-10-13；六模型各保存 574 只有效特征股票的预测。
未满足特征条件的证券仍在冻结股票池中，不通过剔除证券改变实验。原因包括新股预热不足、停牌和历史报价缺失。

初始化窗口没有公式通过内部筛选，因此本次冻结的 `formula_augmented` 与 `extended66` 相同。
没有因为历史公式增强收益较高而降低门槛或挑选早期窗口公式。

初始化为第 0 个观察日，尚无前向收益；状态 `waiting_execution_data` 表示严格成交证据尚不足。
新实验默认观察 60 个新交易日，到期停止，不延长到结果变好。**登记成功不等于 60 日验证完成，也不表示已安装定时任务。**

2026-10-15 运行修复：13日实验因11只证券的跨供应商历史差异被门禁阻断，原库保留。
已在15日开盘前以14日收盘快照重新登记六模型和原60特征对照，均记录574只股票预测。
新版本已部署到独立冻结目录，使用固定依赖和 systemd 用户定时任务；详见[运行修复与回滚](operations-repair-20261015.md)。
重新登记从第0个观察日开始，不拼接旧实验，不改变严格成交门禁。

## 数据采集

采集器使用固定快照中的 `expected_codes`，不按表现、数据长度或当前是否停牌重新选池。
调用公开行情接口，不读取付费凭证，不写本地行情库或正式账户。

```bash
python scripts/collect_factor_forward.py --seed-input stocks.json.gz --calendar-input baostock-calendar.json --output data/reports/factor-forward-input-new
```

输出目录必须不存在；保存 manifest、完整日线、独立日历、采集缺口和来源核验记录。
默认两路并发，最多四路；空行情、未知复权或当日缺报价都会阻止 `ready`。

- 优先读取腾讯明确标记为前复权的日线。
- 供应商只返回未复权数据时，必须分别核对新浪前复权、后复权事件记录：股票必须有完整且有限的单位因子；ETF 还要求份额与累计分红条件。空响应不是“没有公司行动”的证明。
- 腾讯不可用时，使用项目已有 Baostock `adjustflag=2` 前复权接口，保留完整来源响应。
- 缺当日日线时，只有 Baostock 明确报告该证券、该日 `tradestatus=0`，且成交量为空/零，才可规范化为有来源的停牌零量行。不能按昨天价格自行补造正常可交易行情。
- 字段与供应商交叉检查仍不能代替按公告逐笔核验公司行动或可实现成交证据。

失败快照保留。可对同日、同股票池另建目录重试缺口：

```bash
python scripts/collect_factor_forward.py --seed-input stocks.json.gz --calendar-input baostock-calendar.json --reuse-snapshot data/reports/previous-attempt --output data/reports/new-attempt
```

只复用已完整成功的证券，冻结父快照哈希，不覆盖父目录。新版快照必须再次经过前向输入校验。
首次登记可采用新快照；登记之后的历史修订会阻断，不能用新行情替换已冻结的特征或模型。

## 初始化和推进

使用已配置 LightGBM 的项目解释器，例如 Windows 本地 `.venv-ml-research/Scripts/python.exe`。

```bash
python scripts/run_factor_forward.py init --state data/reports/new-factor-forward/forward.db --input stocks.json.gz --benchmark-input benchmarks.json.gz --calendar-input baostock-calendar.json --protocol data/reports/factor-discovery-wide-20261013/manifest.json --evaluation-days 60
```

`--protocol` 读取研究 manifest 中预先确定的配置、资源预算、因子和公式定义，不读取收益、冠军或排名。
特征定义、模型参数或公式清单与当前代码不一致时拒绝登记。省略时使用固定默认配置。
初始化记录父协议哈希、输入哈希、模型原生文本及哈希、代码和依赖指纹。全体模型冻结后不重新训练、重新选公式或改特征。

已有每日输入可直接推进：

```bash
python scripts/run_factor_forward.py advance --state data/reports/factor-forward-20261013/forward.db --input stocks-new-day.json.gz --benchmark-input benchmarks-new-day.json.gz --calendar-input baostock-calendar.json
python scripts/run_factor_forward.py status --state data/reports/factor-forward-20261013/forward.db
```

也可一次完成当天采集和推进：

```bash
python scripts/run_factor_forward.py daily --state data/reports/factor-forward-20261013/forward.db --calendar-input data/reports/factor-forward-input-20261013-complete/calendar.json --snapshot-root data/reports/factor-forward-daily
```

每日任务适合收盘 15:15 之后运行，必须在下一交易日 09:30 之前保存预测；CLI 不提供伪造时间参数。
重复当天运行返回 `unchanged`；同一天输入修订、漏跑交易日、模型/代码变更和日历修订均阻断。
日历可追加未来日期，但不能更改已有区间；当前已核验日历仅至 2026-12-31，跨年之前须提供新的原始日历响应。
每五个交易日记录一次新调仓目标，最终观察日不产生新的待执行目标。

`daily` 具备幂等和到期停止能力。服务器通过 `scripts/run_forward_jobs.py` 共用一次行情采集，
在北京时间16:20执行，并于17:20、20:20及次日08:30提供有限重试；休市日不追加虚构观察。

## 严格成交证据和迟到结算

没有原始开收盘价、真实涨跌停、可交易状态、独立日历、来源和公司行动核验时，账户收益为未提供。
预测及成熟标签仍可单独记录，标签统计不能冒充账户 P&L。

可以在初始化、推进时传入 `--execution-input`，或在来源核验完成后单独结算：

```bash
python scripts/run_factor_forward.py reconcile --state data/reports/factor-forward-20261013/forward.db --execution-input execution-evidence.json
```

成交输入沿用[研究证据数据契约](research-evidence.md)，股票池声明为 `forward_observed`。
只允许观察起点至已记录最新观察日的冻结股票池及两只基准 ETF。完整数据才使用已有严格 A 股账本结算。
补齐数据不产生新预测、不改原信号记录时间；已经计算出的历史成交和净值不得变化。
重复结算没有新副作用；观察到期后仍可补充成交证据，但不能新增观察日或重新训练。

系统不会自动放宽缺口、按新行情修订旧收益、产生真实订单或批准策略上线。
即使到期且严格日线模拟完成，也仅给出 `review_required_no_automatic_promotion`。
历史公司行动账务和开盘排队/市场冲击仍未实现，不能把严格日线模拟称为真实可成交收益。

## 回滚与验证

停止调用 `daily/advance` 即停止新实验；保留独立 SQLite 和快照用于审计，不需恢复正式账户。
不要删除失败快照、修改旧 manifest 或替换数据库内模型来继续实验；修改协议须另建状态库。

```bash
python scripts/run_tests.py --run-one tests/test_factor_discovery.py
python scripts/run_tests.py --run-one tests/test_factor_forward.py
python scripts/run_tests.py --run-one tests/test_collect_factor_forward.py
python scripts/run_tests.py --run-one tests/test_ml_forward.py
```

覆盖公式/模型冻结、采集复权与停牌证据、预算、未来/迟到输入拒绝、每日幂等、并发事务、
严格成交门禁、迟到结算、历史净值不可修改、到期终止，以及原 ML 前向流程回归。
