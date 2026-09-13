"""
全市场日线数据回填工具

把本地 SQLite k_daily 从"按需缓存"升级为"全量本地库"，为全市场选股、
RPS 横截面、pooled ML 训练和快速回测提供数据地基。

股票池类型:
    pool:   data/research_universe.json 研究池（pooled ML 训练面板）
    active: 东财成交额 Top 活跃股（当日有效）
    all:    Baostock 全部 A 股（主板+创业板，排除科创/北交所）

运行模式:
    增量(默认): 只补每只股票最新K线之后的尾部缺口，适合每日定时任务
    full:       全区间重拉并覆盖，可修复历史中间缺口；覆盖不足的股票会
                在 get_daily 内部继续回退到下一数据源

资源自适应:
    - 并发数 = min(CPU核数, 6)，可用内存不足时下调；环境变量
      UNIVERSE_BACKFILL_WORKERS 可显式覆盖
    - 启动前检查磁盘空闲空间，低于预估需求(×2 安全系数)时拒绝运行
    - Windows 下 Baostock 不走子进程隔离，默认并发降为 2
"""
import json
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

logger = logging.getLogger("data.universe_backfill")

BACKFILL_REPORT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "backfill_report.json"
)

# 全区间回填默认起点。同花顺日线接口窗口上限10年（hithink.py 契约），
# 2020年距今约6.7年，为各数据源留出余量。
DEFAULT_BACKFILL_START = "20200101"
# 单次请求窗口上限（自然日）：同花顺日线契约最大10年，留一个月余量
MAX_REQUEST_WINDOW_DAYS = 3620
# 增量模式：最新K线距请求末端超过该自然日数才视为需要补拉
INCREMENTAL_STALE_DAYS = 3
# 磁盘安全系数：预估需求 × 该系数 > 空闲空间则拒绝运行
DISK_SAFETY_FACTOR = 2.0
# 每行K线在 SQLite 中的近似存储成本（字节，含索引/WAL放大）
APPROX_BYTES_PER_ROW = 200


def _detect_resources(workers: int = None) -> dict:
    """探测机器资源并给出并发数建议（环境变量优先）。"""
    cpu = os.cpu_count() or 2
    env_workers = os.environ.get("UNIVERSE_BACKFILL_WORKERS")
    if env_workers:
        try:
            workers = max(1, min(int(env_workers), 16))
            worker_source = "env:UNIVERSE_BACKFILL_WORKERS"
        except ValueError:
            workers = None
    else:
        worker_source = ""

    mem_available_gb = None
    try:
        # Linux: /proc/meminfo；其他平台尽力而为
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    mem_available_gb = float(line.split()[1]) / 1024 / 1024
                    break
    except (OSError, ValueError, IndexError):
        mem_available_gb = None

    if not workers:
        # 内存充裕时按CPU数封顶6；内存紧张(<2GB)时降到2
        workers = min(cpu, 6)
        if mem_available_gb is not None and mem_available_gb < 2.0:
            workers = min(workers, 2)
        if os.name == "nt":
            # Windows 下 Baostock 无子进程隔离，保守并发
            workers = min(workers, 2)
        worker_source = worker_source or "auto"
    return {
        "cpu": cpu,
        "mem_available_gb": round(mem_available_gb, 2) if mem_available_gb else None,
        "workers": workers,
        "worker_source": worker_source,
    }


def _check_disk(code_count: int, start_date: str) -> dict:
    """预估回填所需磁盘空间，不足时抛异常。"""
    try:
        start = datetime.strptime(start_date, "%Y%m%d")
        trading_days = max(1, (datetime.now() - start).days * 0.68)
    except ValueError:
        trading_days = 2420
    estimated_gb = code_count * trading_days * APPROX_BYTES_PER_ROW / 1024 ** 3
    free_gb = shutil.disk_usage(os.path.dirname(BACKFILL_REPORT_FILE)).free / 1024 ** 3
    required_gb = estimated_gb * DISK_SAFETY_FACTOR
    if free_gb < required_gb:
        raise RuntimeError(
            f"磁盘空间不足: 预估需要{required_gb:.2f}GB(含{DISK_SAFETY_FACTOR:.0f}倍安全系数), "
            f"当前空闲{free_gb:.2f}GB。请清理磁盘或缩小回填范围。"
        )
    return {"estimated_gb": round(estimated_gb, 3), "free_gb": round(free_gb, 2)}


def _load_pool_universe() -> list:
    """研究池（pooled ML 训练面板）。"""
    pool_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "research_universe.json"
    )
    if not os.path.exists(pool_file):
        logger.warning("研究池文件不存在: %s", pool_file)
        return []
    with open(pool_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    codes = [c.get("code") for c in data.get("codes") or [] if c.get("code")]
    return sorted(set(codes))


# 沪深A股主板+创业板代码前缀（排除科创688/689、北交所、B股、指数、ETF）
ALL_UNIVERSE_CODE_PREFIXES = (
    "600", "601", "603", "605",          # 沪主板
    "000", "001", "002", "003",          # 深主板
    "300", "301", "302",                 # 创业板
)


def _fetch_all_universe_eastmoney() -> list:
    """东财 clist 分页拉全市场A股列表（无需登录，约30页）。"""
    import requests

    codes = []
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    page_size = 200
    pn, total = 1, None
    while True:
        params = {
            "pn": pn, "pz": page_size, "po": 0, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f12",  # 按代码排序，分页稳定
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",  # 沪深A股
            "fields": "f12,f14",
        }
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json() or {}
        payload = data.get("data") or {}
        diff = payload.get("diff") or []
        if total is None:
            total = payload.get("total") or 0
        for item in diff:
            code = str(item.get("f12", ""))
            if code.startswith(ALL_UNIVERSE_CODE_PREFIXES):
                codes.append(code)
        if not diff or pn * page_size >= total:
            break
        pn += 1
        time.sleep(0.2)
    logger.info("东财全市场列表: %d只 (%d页)", len(set(codes)), pn)
    return sorted(set(codes))


def _fetch_all_universe_baostock_daily() -> list:
    """Baostock 单交易日全证券快照（query_all_stock）。

    比 query_stock_basic 快一个量级（单日~5400行 vs 全量含退市证券）；
    非交易日返回空，从今天向前最多探测10天覆盖节假日。
    """
    from data.history import query_baostock_all_stock

    for offset in range(0, 10):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        raw = query_baostock_all_stock(day)
        if not raw or raw.get("error_code") != "0" or not raw.get("data"):
            continue
        codes = []
        for row in raw["data"]:
            code = str(row[0] if row else "")
            clean = code.split(".")[-1]
            if code.startswith(("sh.", "sz.")) and clean.startswith(ALL_UNIVERSE_CODE_PREFIXES):
                codes.append(clean)
        if codes:
            logger.info("Baostock单日快照(%s): %d只", day, len(set(codes)))
            return sorted(set(codes))
    return []


def _fetch_all_universe_hithink() -> list:
    """同花顺研究快照分页（服务器已配置key时的可靠路径，~60页覆盖全市场）。"""
    try:
        from data.research_universe import _hithink_active_stocks
        stocks = _hithink_active_stocks(100000)
        codes = [c for c in stocks if c.startswith(ALL_UNIVERSE_CODE_PREFIXES)]
        if codes:
            logger.info("同花顺全市场列表: %d只", len(set(codes)))
        return sorted(set(codes))
    except Exception as exc:
        logger.warning("同花顺全市场列表失败: %s", type(exc).__name__)
        return []


def _fetch_all_universe() -> list:
    """全部A股（主板+创业板，排除科创/北交所/指数/ETF）。

    回退顺序：东财分页（最快，但push2偶发502）→ 同花顺研究快照分页
    （需key，已验证可用）→ Baostock单日快照 → Baostock全量stock_basic
    （易超时，最后手段）。
    """
    try:
        codes = _fetch_all_universe_eastmoney()
        if codes:
            return codes
    except Exception as exc:
        logger.warning("东财全市场列表失败，回退同花顺: %s", type(exc).__name__)

    codes = _fetch_all_universe_hithink()
    if codes:
        return codes

    codes = _fetch_all_universe_baostock_daily()
    if codes:
        return codes

    from data.history import get_stock_list

    df = get_stock_list()
    if df is None or df.empty:
        return []
    codes = []
    for raw in df["code"].astype(str):
        clean = raw.split(".")[-1]
        if clean.startswith(ALL_UNIVERSE_CODE_PREFIXES):
            codes.append(clean)
    return sorted(set(codes))


def _load_active_universe() -> list:
    """东财活跃股（成交额Top，当日有效）。"""
    from strategy.stock_picker import _get_active_stocks

    stocks = _get_active_stocks()
    return sorted(stocks.keys())


def load_universe(universe: str) -> list:
    """按类型加载股票池，返回去重后的代码列表。"""
    if universe == "pool":
        codes = _load_pool_universe()
    elif universe == "all":
        codes = _fetch_all_universe()
    elif universe == "active":
        codes = _load_active_universe()
    else:
        raise ValueError(f"未知股票池类型: {universe}")
    logger.info("股票池[%s]: %d只", universe, len(codes))
    return codes


def _query_latest_dates(db_path: str = None) -> dict:
    """一次性读取所有股票在 k_daily 中的最新日期，避免逐只查询。"""
    from data.database import Database

    latest = {}
    with Database(db_path=db_path) as db:
        c = db.conn.cursor()
        c.execute("SELECT code, MAX(date) FROM k_daily GROUP BY code")
        for code, max_date in c.fetchall():
            latest[code] = max_date
    return latest


def _backfill_one(code: str, start_date: str, end_date: str,
                  full: bool) -> dict:
    """回填单只股票，返回 {code, status, rows, latest}。"""
    from data.history import get_daily, _assess_daily_coverage

    # 钳制请求窗口：超出同花顺10年上限会让每股请求都失败并落到慢速源
    window_floor = (datetime.now() - timedelta(days=MAX_REQUEST_WINDOW_DAYS)).strftime("%Y%m%d")
    if len(start_date) == 8 and start_date < window_floor:
        start_date = window_floor

    start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
    try:
        # require_full_range 恒为 False：同花顺上游历史普遍带少量内部缺口，
        # 强制全区间会把每股结果都拒收并落到慢速源且不落缓存。
        # 回填接受"诚实降级"结果，缺口状态在报告中如实标记，供后续修复。
        df = get_daily(
            code, start_date=start_date, end_date=end_date,
            adjust="qfq", simple=True,
            require_full_range=False,
        )
        if df is None or df.empty:
            return {"code": code, "status": "empty", "rows": 0, "latest": ""}
        # 与 research_universe 一致：不信任链路上遗留的 attrs，重新校验覆盖状态
        if "coverage_status" not in df.attrs:
            df = _assess_daily_coverage(df, start_fmt, end_fmt) or df
        latest = ""
        if "date" in df.columns and len(df) > 0:
            latest = str(df["date"].iloc[-1])
        status = str(df.attrs.get("coverage_status") or "ok")
        # get_daily 只在接受的源内落缓存；降级兜底结果需显式落库，
        # 否则带缺口的股票永远进不了本地库。
        # 缓存命中帧的 attrs["source"] 是 list，说明数据已在库里，跳过重写。
        source_attr = df.attrs.get("source")
        if not isinstance(source_attr, list):
            try:
                from data.history import _save_to_cache
                _save_to_cache(code, df, "qfq",
                               source=str(source_attr or "backfill"))
            except Exception as exc:
                logger.debug("显式落缓存失败 %s: %s", code, exc)
        return {
            "code": code,
            "status": status,
            "rows": int(len(df)),
            "latest": latest,
        }
    except Exception as exc:
        logger.debug("回填失败 %s: %s", code, exc)
        return {"code": code, "status": "error", "rows": 0, "latest": "",
                "error": str(exc)[:200]}



def run_backfill(
    universe: str = "pool",
    full: bool = False,
    start_date: str = DEFAULT_BACKFILL_START,
    workers: int = None,
    end_date: str = None,
    db_path: str = None,
) -> dict:
    """
    执行日线回填。

    Args:
        universe: pool / all / active
        full: True=全区间重拉(修复中间缺口)；False=增量补尾
        start_date: 全区间起点 "YYYYMMDD"
        workers: 并发数（None=按资源自适应）
        end_date: 请求末端（默认今天）
        db_path: SQLite 路径（默认正式库；测试可注入临时库）

    Returns:
        汇总 dict（同时写入 data/backfill_report.json）
    """
    started_at = datetime.now().isoformat()
    end_date = end_date or datetime.now().strftime("%Y-%m-%d")
    end_compact = end_date.replace("-", "")

    codes = load_universe(universe)
    if not codes:
        return {"status": "empty", "reason": f"股票池[{universe}]为空",
                "started_at": started_at, "finished_at": datetime.now().isoformat()}

    resources = _detect_resources(workers)
    disk = _check_disk(len(codes), start_date if full else "20240101")
    logger.info(
        "回填启动: universe=%s %d只 mode=%s workers=%d(%s) cpu=%s mem=%sGB "
        "disk_free=%sGB estimated=%sGB",
        universe, len(codes), "full" if full else "incremental",
        resources["workers"], resources["worker_source"], resources["cpu"],
        resources["mem_available_gb"], disk["free_gb"], disk["estimated_gb"],
    )

    latest_dates = _query_latest_dates(db_path=db_path)
    stale_cutoff = (
        datetime.now() - timedelta(days=INCREMENTAL_STALE_DAYS)
    ).strftime("%Y-%m-%d")

    # 组织任务：增量模式下跳过已新鲜的股票
    tasks = []
    skipped_fresh = 0
    for code in codes:
        latest = latest_dates.get(code)
        if not full and latest and latest >= stale_cutoff:
            skipped_fresh += 1
            continue
        if latest and not full:
            # 增量：从最新K线次日补拉
            tail_start = (
                datetime.strptime(latest.replace("-", ""), "%Y%m%d") + timedelta(days=1)
            ).strftime("%Y-%m-%d")
        else:
            tail_start = None  # full 模式或无历史：全区间
        tasks.append((code, tail_start))

    results = []
    ok = incomplete = failed = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=resources["workers"]) as pool:
        futures = {
            pool.submit(
                _backfill_one, code,
                tail_start or start_date, end_compact, full,
            ): (code, tail_start)
            for code, tail_start in tasks
        }
        for i, future in enumerate(as_completed(futures), 1):
            res = future.result()
            results.append(res)
            if res["status"] in ("ok", ""):
                ok += 1
            elif res["status"] == "error" or res["status"] == "empty":
                failed += 1
            else:  # incomplete / stale
                incomplete += 1
            if i % 100 == 0 or i == len(futures):
                elapsed = time.time() - started
                rate = i / elapsed if elapsed > 0 else 0
                logger.info(
                    "回填进度: %d/%d (ok=%d incomplete=%d failed=%d, %.1f只/秒)",
                    i, len(futures), ok, incomplete, failed, rate,
                )

    failed_codes = [r["code"] for r in results if r["status"] in ("error", "empty")][:50]
    incomplete_codes = [r["code"] for r in results if r["status"] not in ("ok", "", "error", "empty")][:50]

    summary = {
        "status": "ok" if failed <= ok else "degraded",
        "universe": universe,
        "mode": "full" if full else "incremental",
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "codes_total": len(codes),
        "skipped_fresh": skipped_fresh,
        "tasks": len(tasks),
        "ok": ok,
        "incomplete": incomplete,
        "failed": failed,
        "failed_codes": failed_codes,
        "incomplete_codes": incomplete_codes,
        "resources": resources,
        "disk": disk,
        "elapsed_seconds": round(time.time() - started, 1),
    }

    try:
        tmp = BACKFILL_REPORT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BACKFILL_REPORT_FILE)
    except OSError as exc:
        logger.warning("回填报告写入失败: %s", exc)

    logger.info(
        "回填完成: ok=%d incomplete=%d failed=%d skipped_fresh=%d 耗时%.0fs",
        ok, incomplete, failed, skipped_fresh, time.time() - started,
    )
    return summary
