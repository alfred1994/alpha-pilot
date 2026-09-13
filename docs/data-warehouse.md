# 全市场日线回填与数据仓库

日期：2026-09-14

本文档说明如何把本地 SQLite `k_daily` 从"按需缓存"升级为"全量本地库"，
以及配套的分钟线落盘和数据库维护任务。设计目标运行环境：
4 OCPU / 24GB 内存 / 96GB 磁盘的 OCI A1 实例（quant-pilot-phx）。

## 为什么需要

| 之前的瓶颈 | 回填后 |
|---|---|
| 研究池 800 只、每轮只同步 8 只，全池一轮约 100 个交易日 | 全市场约 4900 只一次性回填 10 年日线 |
| pooled ML 因 `fresh_coverage<0.80` 长期被门禁 block | 训练面板覆盖满格，每日增量保新鲜 |
| RPS 横截面策略在单股打分上下文拿不到面板，恒 HOLD | 本地全量数据可支撑横截面计算 |
| 选股只看成交额 Top 500，中低成交额股不可见 | 可扩展全市场技术扫描 |
| 回测数据逐次现拉，2024 年之前不在库里 | 本地 10 年数据秒级读取 |

## 全市场日线回填

```bash
# 研究池（800只，pooled ML 训练面板）全区间回填，修复中间缺口
python3 main.py --backfill-kline --backfill-universe pool --backfill-full

# 全市场（沪深主板+创业板，排除科创/北交所/指数/ETF）
python3 main.py --backfill-kline --backfill-universe all --backfill-full

# 日常增量（默认只补尾部缺口，适合定时任务）
python3 main.py --backfill-kline --backfill-universe all
```

参数：

- `--backfill-universe pool|all|active`：股票池，默认 pool
- `--backfill-full`：全区间重拉（默认从 20160101 起），可修复历史中间缺口；
  覆盖不足的股票会在 `get_daily` 内部继续回退到下一数据源
- `--backfill-start YYYYMMDD`：全区间起点，默认 20200101（同花顺日线接口窗口上限10年，超出会被自动钳制）
- `--backfill-workers N`：并发数；默认按资源自适应

### 资源自适应

- 并发数 = min(CPU 核数, 6)，可用内存 < 2GB 时降到 2；Windows 下 Baostock
  无子进程隔离，默认并发降到 2
- 环境变量 `UNIVERSE_BACKFILL_WORKERS` 可显式覆盖
- 启动前检查磁盘空闲，低于预估需求（×2 安全系数）时拒绝运行
- 汇总写入 `data/backfill_report.json`，失败数 ≥ 成功数才返回非零退出码

服务器上长时间任务建议用 `systemd-run --user` 启动以免 SSH 断开中断：

```bash
systemd-run --user --unit=alpha-pilot-backfill-manual \
  --working-directory=$HOME/projects/alpha-pilot \
  env BROKER_MODE=paper HITHINK_ENABLED=1 HITHINK_FINANCE_API_KEY=$HITHINK_FINANCE_API_KEY \
  $HOME/projects/alpha-pilot/.venv/bin/python main.py --backfill-kline \
  --backfill-universe all --backfill-full
journalctl --user -u alpha-pilot-backfill-manual -f
```

## 盘中分钟线落盘

盘中盯盘（`scheduler/intraday_watch.py`）拉取的 1 分钟 K 线现在会写入
SQLite `k_minute` 表（`period='1m'`，INSERT OR REPLACE 幂等），盘后复盘可以
回放"当时为什么这么决策"的分钟级现场。

- 保留期默认 30 天，环境变量 `K_MINUTE_RETENTION_DAYS` 可调（0=不清理）
- 每个进程每天最多清理一次，删除时按 `(period, datetime)` 索引扫描
- 落盘失败只记 debug 日志，绝不影响看盘主流程

## 数据库维护任务

```bash
python3 main.py --db-maintenance           # 完整性检查+在线备份+运维日志清理
python3 main.py --db-maintenance --db-vacuum  # 每周手动加一次 VACUUM
```

- 完整性检查（`PRAGMA quick_check`）失败时标记 corrupt，拒绝继续清理/写操作
- 备份走 sqlite3 backup API（对 WAL 库安全，不需要停写），保留最近 7 份于
  `data/backups/`
- 清理范围只有运维日志表（`auto_events`、`trade_plan_executions`，默认保留
  180 天）；trades / llm_decisions / k_daily 等业务与训练数据不清理
- 汇总写入 `data/db_maintenance_report.json`

Linux 无人值守部署会生成每日 23:40 的 `alpha-pilot-db-maintenance.timer`
（`python3 main.py --linux-tasks && bash data/linux_tasks/install_systemd_user.sh`
后生效）。

## 数据质量注意

- `k_daily` 只存前复权（qfq）。前复权基准固化在写入时刻，除权除息后更早的
  历史行保留旧基准——**建议每年对持仓和研究池跑一次 `--backfill-full`** 重置
  基准（复权因子表为后续规划）
- 回填使用 `get_daily` 既有降级链（缓存→同花顺→长桥→Baostock），覆盖率
  校验（`_assess_daily_coverage`）与诚实降级标记全程生效
