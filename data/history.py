"""
历史数据模块 - 同花顺 + Baostock + 长桥 + SQLite缓存
适合获取日线/周线/月线/分钟线历史数据

数据获取优先级:
    1. SQLite本地缓存
    2. 同花顺API（显式启用且已配置）
    3. 长桥API（如已配置）
    4. Baostock（兜底）
"""
import baostock as bs
import math
import multiprocessing as mp
import os
import pandas as pd
import queue
from datetime import datetime, timedelta
from typing import Optional
import logging
from config import BAOSTOCK_TIMEOUT

logger = logging.getLogger("data.history")

# 日线缓存允许覆盖到最近一个已完成交易日；超过该窗口则必须尝试补拉，
# 不能因为区间内存在旧记录就永久复用整段旧数据。
HISTORY_CACHE_MAX_STALE_DAYS = int(os.environ.get("HISTORY_CACHE_MAX_STALE_DAYS", "3"))

# 日线端点可跨越周末及连续节假日，但不能把数月前的一根记录当作完整历史。
# 起点缺口会被保留为显式的 incomplete 标记；这既能如实反映晚上市/长期停牌
# 的可用历史，也让要求完整样本的研究任务继续尝试其他数据源。
HISTORY_COVERAGE_GRACE_DAYS = int(os.environ.get("HISTORY_COVERAGE_GRACE_DAYS", "10"))
HISTORY_COVERAGE_MIN_DENSITY = float(os.environ.get("HISTORY_COVERAGE_MIN_DENSITY", "0.20"))

# 字段映射
DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM"
DAILY_SIMPLE_FIELDS = "date,code,open,high,low,close,volume,amount,turn,pctChg"


def _assess_daily_coverage(df: pd.DataFrame, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """校验日线的日期、价格和请求区间端点，并把结果写入 attrs。

    日线不会假设每个工作日都有记录：周末、节假日、停牌和晚上市都会留下空档。
    但超过节假日容忍窗口的内部空档或明显稀疏的样本会保守标记 incomplete；
    结束端严重滞后、日期越界/重复或 OHLC 非法的数据不能作为新鲜行情。
    """
    if df is None or df.empty or "date" not in df.columns:
        return None
    try:
        def parse_requested_day(value):
            text = str(value).strip().replace("-", "").replace("/", "")
            return datetime.strptime(text[:8], "%Y%m%d")

        requested_start = parse_requested_day(start_date)
        requested_end = parse_requested_day(end_date)
    except (TypeError, ValueError):
        return None
    if requested_start > requested_end:
        return None

    frame = df.copy()
    dates = pd.to_datetime(
        frame["date"].astype(str).str.slice(0, 10), format="%Y-%m-%d", errors="coerce"
    )
    if dates.isna().any() or dates.duplicated().any():
        return None
    frame["date"] = dates.dt.strftime("%Y-%m-%d")

    for column in ("open", "high", "low", "close"):
        if column not in frame.columns:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or not values.map(math.isfinite).all() or (values <= 0).any():
            return None
        frame[column] = values
    if "close" not in frame.columns:
        return None
    if {"high", "low", "open"}.issubset(frame.columns):
        if ((frame["high"] < frame[["open", "low", "close"]].max(axis=1)) |
                (frame["low"] > frame[["open", "high", "close"]].min(axis=1))).any():
            return None

    earliest = dates.min().to_pydatetime()
    latest = dates.max().to_pydatetime()
    if earliest < requested_start or latest > requested_end:
        return None
    missing_start_days = max(0, (earliest - requested_start).days)
    end_gap_days = max(0, (requested_end - latest).days)
    ordered_dates = dates.sort_values().reset_index(drop=True)
    internal_gaps = ordered_dates.diff().dt.days.dropna()
    max_internal_gap_days = int(internal_gaps.max()) if not internal_gaps.empty else 0
    # 密度必须相对完整请求窗口计算，而不是已返回数据的首尾；否则一根
    # 位于短窗口中间的数据会把分母缩成 1 并被误判为完整。使用工作日只是
    # 无网络的保守下界：节假日/停牌会被标记 incomplete，而不会伪造齐全。
    expected_weekdays = max(1, len(pd.bdate_range(requested_start.date(), requested_end.date())))
    observed_density = len(frame) / expected_weekdays
    status = "ok"
    if end_gap_days > HISTORY_COVERAGE_GRACE_DAYS:
        status = "stale"
    elif (missing_start_days > HISTORY_COVERAGE_GRACE_DAYS
          or max_internal_gap_days > HISTORY_COVERAGE_GRACE_DAYS
          or observed_density < HISTORY_COVERAGE_MIN_DENSITY):
        status = "incomplete"
    frame = frame.sort_values("date").reset_index(drop=True)
    frame.attrs.update(getattr(df, "attrs", {}))
    frame.attrs.update(
        coverage_status=status,
        coverage_earliest=earliest.strftime("%Y-%m-%d"),
        coverage_latest=latest.strftime("%Y-%m-%d"),
        coverage_end_gap_days=end_gap_days,
        coverage_max_internal_gap_days=max_internal_gap_days,
        coverage_expected_weekdays=expected_weekdays,
        coverage_observed_density=observed_density,
        missing_start_days=missing_start_days,
    )
    return frame


def _coverage_status(df: Optional[pd.DataFrame]) -> str:
    return str(getattr(df, "attrs", {}).get("coverage_status") or "invalid")


def _prefer_daily_coverage(frames):
    """从降级结果中选端点最新、起点缺口最小的那一份，且保留不完整标记。"""
    usable = [frame for frame in frames if frame is not None and not frame.empty]
    if not usable:
        return None
    return min(
        usable,
        key=lambda frame: (
            int(frame.attrs.get("coverage_end_gap_days") or 0),
            int(frame.attrs.get("missing_start_days") or 0),
            -len(frame),
        ),
    )


def _query_history_rows(bs_code: str, fields: str, start_date: str, end_date: str,
                        frequency: str, adjustflag: str) -> dict:
    """在当前进程内执行Baostock历史行情查询。"""
    lg = bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            bs_code, fields,
            start_date=start_date, end_date=end_date,
            frequency=frequency, adjustflag=adjustflag
        )
        data = []
        while rs.error_code == "0" and rs.next():
            data.append(rs.get_row_data())
        return {"fields": rs.fields, "data": data, "error_code": rs.error_code, "error_msg": rs.error_msg}
    finally:
        bs.logout()


def _query_stock_basic_rows() -> dict:
    """在当前进程内执行Baostock股票列表查询。"""
    lg = bs.login()
    try:
        rs = bs.query_stock_basic()
        data = []
        while rs.error_code == "0" and rs.next():
            data.append(rs.get_row_data())
        return {"fields": rs.fields, "data": data, "error_code": rs.error_code, "error_msg": rs.error_msg}
    finally:
        bs.logout()


def _query_trade_dates_rows(start_date: str, end_date: str) -> dict:
    """在当前进程内执行Baostock交易日历查询。"""
    lg = bs.login()
    try:
        rs = bs.query_trade_dates(start_date=start_date, end_date=end_date)
        data = []
        while rs.error_code == "0" and rs.next():
            data.append(rs.get_row_data())
        return {"data": data, "error_code": rs.error_code, "error_msg": rs.error_msg}
    finally:
        bs.logout()


def _baostock_worker(kind: str, args: tuple, result_queue):
    """Baostock子进程入口。"""
    try:
        if kind == "history":
            result = _query_history_rows(*args)
        elif kind == "stock_basic":
            result = _query_stock_basic_rows()
        elif kind == "trade_dates":
            result = _query_trade_dates_rows(*args)
        else:
            result = {"error_code": "-1", "error_msg": f"未知Baostock任务: {kind}"}
        result_queue.put({"ok": True, "result": result})
    except Exception as e:
        result_queue.put({"ok": False, "error": str(e)})


def _run_baostock(kind: str, args: tuple = (), timeout: int = BAOSTOCK_TIMEOUT) -> Optional[dict]:
    """用子进程执行Baostock调用，超时后终止子进程。"""
    if os.name == "nt":
        try:
            if kind == "history":
                return _query_history_rows(*args)
            if kind == "stock_basic":
                return _query_stock_basic_rows()
            if kind == "trade_dates":
                return _query_trade_dates_rows(*args)
        except Exception as e:
            logger.warning(f"Baostock调用失败({kind}): {e}")
            return None

    ctx = mp.get_context("fork")
    result_queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_baostock_worker, args=(kind, args, result_queue))
    proc.daemon = True
    proc.start()
    proc.join(float(timeout))
    if proc.is_alive():
        proc.terminate()
        proc.join(3)
        if proc.is_alive():
            proc.kill()
            proc.join(1)
        logger.warning(f"Baostock调用超时({timeout}s)，已终止: {kind}")
        return None

    try:
        payload = result_queue.get_nowait()
    except queue.Empty:
        logger.warning(f"Baostock调用无返回({kind}), exitcode={proc.exitcode}")
        return None
    if not payload.get("ok"):
        logger.warning(f"Baostock调用失败({kind}): {payload.get('error')}")
        return None
    return payload.get("result") or {}


def query_baostock_history_rows(bs_code: str, fields: str, start_date: str = "",
                                end_date: str = "", frequency: str = "d",
                                adjustflag: str = "2",
                                timeout: int = BAOSTOCK_TIMEOUT) -> Optional[dict]:
    """对外提供带超时保护的Baostock历史行情原始查询。"""
    return _run_baostock(
        "history",
        (bs_code, fields, start_date, end_date, frequency, adjustflag),
        timeout=timeout,
    )


def query_baostock_stock_basic(timeout: int = BAOSTOCK_TIMEOUT) -> Optional[dict]:
    """对外提供带超时保护的Baostock股票列表原始查询。"""
    return _run_baostock("stock_basic", timeout=timeout)


def query_baostock_trade_dates(start_date: str, end_date: str,
                               timeout: int = BAOSTOCK_TIMEOUT) -> Optional[dict]:
    """对外提供带超时保护的Baostock交易日历原始查询。"""
    return _run_baostock("trade_dates", (start_date, end_date), timeout=timeout)


def _to_bs_code(code: str) -> str:
    """转换为baostock格式: 600519 → sh.600519"""
    code = code.strip()
    if "." in code:
        left, right = (part.strip() for part in code.split(".", 1))
        if right.lower() in ("sh", "sz") and left:
            return f"{right.lower()}.{left}"
        if left.lower() in ("sh", "sz") and right:
            return f"{left.lower()}.{right}"
        return code
    if code.startswith(("6", "5")):
        return f"sh.{code}"
    return f"sz.{code}"


def _to_system_code(code: str) -> str:
    """转换为系统格式（纯数字）"""
    code = code.strip()
    if "." in code:
        left, right = (part.strip() for part in code.split(".", 1))
        if right.lower() in ("sh", "sz"):
            return left
        if left.lower() in ("sh", "sz"):
            return right
        return right or left
    return code.lstrip("shszSHSZ")


def _try_cache(code: str, start_date: str, end_date: str,
               adjust: str = "qfq", allow_stale: bool = False,
               require_full_range: bool = False) -> Optional[pd.DataFrame]:
    """
    尝试从SQLite缓存获取数据

    Returns:
        DataFrame 或 None（缓存未命中）
    """
    # 共享表没有复权维度，只存前复权；其他模式不能混用。
    if adjust != "qfq":
        return None
    try:
        from data.database import Database
        system_code = _to_system_code(code)
        with Database() as db:
            cached = db.get_k_daily(system_code, start_date, end_date)
            if cached and len(cached) > 0:
                df = pd.DataFrame(cached)
                # 清理内部字段
                df.attrs["source"] = sorted(set(df.get("source", pd.Series(dtype=str)).dropna()))
                df.attrs["adjust"] = "qfq"
                for col in ["source"]:
                    if col in df.columns:
                        df = df.drop(columns=[col])
                df = _assess_daily_coverage(df, start_date, end_date)
                if df is None:
                    logger.warning("历史缓存数据无效: %s", system_code)
                    return None
                latest_date = str(df.attrs.get("coverage_latest") or "")
                stale_days = int(df.attrs.get("coverage_end_gap_days") or 0)
                missing_start_days = int(df.attrs.get("missing_start_days") or 0)

                if stale_days <= HISTORY_CACHE_MAX_STALE_DAYS and (
                    not require_full_range or _coverage_status(df) == "ok"
                ):
                    logger.info(
                        f"缓存命中: {system_code} {len(df)}条 latest={latest_date} "
                        f"gap={stale_days}d"
                    )
                    return df

                logger.warning(
                    f"历史缓存过期: {system_code} latest={latest_date} "
                    f"requested_end={end_date} gap={stale_days}d "
                    f"start_gap={missing_start_days}d"
                )
                if allow_stale:
                    df.attrs["stale_cache_days"] = stale_days
                    df.attrs["stale_cache_latest"] = latest_date
                    df.attrs["missing_start_days"] = missing_start_days
                    return df
    except Exception as e:
        logger.debug(f"缓存查询失败: {e}")
    return None


def _save_to_cache(code: str, df: pd.DataFrame, adjust: str = "qfq",
                   source: str = "baostock"):
    """将数据保存到SQLite缓存"""
    if adjust != "qfq" or df is None or df.empty:
        return
    try:
        from data.database import Database
        system_code = _to_system_code(code)
        records = df.to_dict("records")
        for r in records:
            r["code"] = system_code
        with Database() as db:
            db.insert_k_daily(records, source=source)
    except Exception as e:
        logger.debug(f"缓存保存失败: {e}")


def _try_hithink(code: str, start_date: str, end_date: str,
                 adjust: str = "qfq") -> Optional[pd.DataFrame]:
    """可选同花顺日线源；失败不改变已有源的降级顺序。"""
    try:
        from data.hithink import get_client
        client = get_client()
        if client is None:
            return None
        expected_adjust = {"qfq": "forward", "hfq": "backward", "": "none"}.get(adjust)
        if expected_adjust is None:
            return None
        df = client.get_daily(code, start_date, end_date, adjust=expected_adjust)
        if df is not None and not df.empty and df.attrs.get("adjust") == expected_adjust:
            df.attrs["adjust"] = adjust
            df.attrs["source"] = "hithink"
            return df
    except Exception as exc:
        logger.warning("同花顺日线不可用，回退已有源: %s", type(exc).__name__)
    return None


def _try_longbridge(code: str, start_date: str, end_date: str,
                    adjust: str = "qfq") -> Optional[pd.DataFrame]:
    """
    尝试从长桥API获取数据

    Returns:
        DataFrame 或 None（长桥失败）
    """
    try:
        from data.longbridge_data import get_daily_kline as lb_get_daily
        df = lb_get_daily(code, start_date, end_date, adjust, use_cache=False)
        if df is not None and not df.empty:
            logger.info(f"长桥获取成功: {code} {len(df)}条")
            return df
    except Exception as e:
        logger.debug(f"长桥获取失败: {e}")
    return None


def get_daily(code: str, start_date: str = None, end_date: str = None,
              adjust: str = "qfq", simple: bool = True,
              require_full_range: bool = False) -> pd.DataFrame:
    """
    获取日线数据（缓存 + 可选同花顺 + 长桥 + Baostock）

    数据获取优先级:
        1. SQLite本地缓存
        2. 同花顺API（显式启用且已配置）
        3. 长桥API（如已配置）
        4. Baostock（兜底）

    Args:
        code: 股票代码, 如 "600519" 或 "sh.600519"
        start_date: 开始日期 "YYYY-MM-DD" 或 "YYYYMMDD", 默认30天前
        end_date: 结束日期, 默认今天
        adjust: "qfq"前复权 / "hfq"后复权 / ""不复权
        simple: True=简单字段(快), False=完整字段(含PE/PB等)
        require_full_range: True时缓存必须覆盖起始日期，供研究数据补齐使用

    Returns:
        DataFrame: date, open, high, low, close, volume, amount, turn, pctChg
    """
    if not start_date:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # 标准化日期格式
    start_date = start_date.replace("-", "").replace("/", "")
    end_date = end_date.replace("-", "").replace("/", "")
    start_date = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    end_date = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

    # 简单字段模式才走缓存和长桥（完整字段需要PE/PB等，长桥不一定有）
    stale_cache = None
    degraded = []

    def accept_source(candidate: Optional[pd.DataFrame], source: str) -> Optional[pd.DataFrame]:
        """仅把端点覆盖合格的数据当作当前源成功，其他结果留作诚实降级。"""
        frame = _assess_daily_coverage(candidate, start_date, end_date)
        if frame is None:
            logger.warning("历史数据无效: %s source=%s", code, source)
            return None
        frame.attrs["source"] = source
        status = _coverage_status(frame)
        # 常规查询允许晚上市股票返回可用的部分历史，但 attrs 必须明确标出；
        # 研究任务要求全区间时，会继续回退到其他源补齐。
        if status == "ok" or (status == "incomplete" and not require_full_range):
            return frame
        logger.warning(
            "历史数据覆盖不足: %s source=%s status=%s latest=%s end_gap=%sd start_gap=%sd",
            code, source, status, frame.attrs.get("coverage_latest"),
            frame.attrs.get("coverage_end_gap_days"), frame.attrs.get("missing_start_days"),
        )
        degraded.append(frame)
        return None

    if simple:
        # 1. 先查SQLite缓存
        df = _try_cache(code, start_date, end_date, adjust,
                        require_full_range=require_full_range)
        if df is not None:
            return df
        # 外部源失败时保留旧缓存作为明确降级结果，但不把它当作新鲜数据。
        stale_cache = _try_cache(code, start_date, end_date, adjust,
                                 allow_stale=True,
                                 require_full_range=require_full_range)

        df = accept_source(_try_hithink(code, start_date, end_date, adjust), "hithink")
        if df is not None:
            _save_to_cache(code, df, adjust, source="hithink")
            return df

        # 同花顺未配置、失败或不支持时，保持原有回退。
        df = accept_source(_try_longbridge(code, start_date, end_date, adjust), "longport")
        if df is not None:
            _save_to_cache(code, df, adjust, source="longport")
            return df

    # 3. 回退到Baostock
    df = accept_source(_fetch_baostock(code, start_date, end_date, adjust, simple), "baostock")

    # 保存到缓存（仅简单字段模式）
    if simple and df is not None and not df.empty:
        _save_to_cache(code, df, adjust, source="baostock")
    if df is not None:
        return df

    if (df is None or df.empty) and stale_cache is not None:
        logger.warning(
            f"历史数据源不可用，回退过期缓存: {code} "
            f"latest={stale_cache.attrs.get('stale_cache_latest', '')} "
            f"stale_days={stale_cache.attrs.get('stale_cache_days', '?')}"
        )
        degraded.append(stale_cache)

    # 所有源都未达到请求区间时，仍返回最完整的一份并显式标记，供研究同步
    # 报告 incomplete/stale，而不是将它伪装为成功或丢失可诊断的覆盖信息。
    fallback = _prefer_daily_coverage(degraded)
    if fallback is not None:
        return fallback

    return pd.DataFrame()


def _fetch_baostock(code: str, start_date: str, end_date: str,
                    adjust: str = "qfq", simple: bool = True) -> pd.DataFrame:
    """
    从Baostock获取日线数据（原有逻辑）

    Args:
        code: 股票代码
        start_date: 开始日期 "YYYY-MM-DD"
        end_date: 结束日期 "YYYY-MM-DD"
        adjust: 复权类型
        simple: 是否简单字段

    Returns:
        DataFrame
    """
    bs_code = _to_bs_code(code)
    adjustflag = {"qfq": "2", "hfq": "1", "": "3"}.get(adjust, "2")
    fields = DAILY_SIMPLE_FIELDS if simple else DAILY_FIELDS

    raw = query_baostock_history_rows(bs_code, fields, start_date, end_date, "d", adjustflag)
    if not raw or raw.get("error_code") != "0" or not raw.get("data"):
        if raw and raw.get("error_code") not in (None, "0"):
            logger.warning(f"Baostock获取失败 {code}: {raw.get('error_msg')}")
        return pd.DataFrame()

    df = pd.DataFrame(raw["data"], columns=raw["fields"])

    # 类型转换
    numeric_cols = ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]
    if not simple:
        numeric_cols += ["preclose", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    logger.info(f"Baostock获取: {code} {len(df)}条")
    return df


def get_weekly(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取周线数据"""
    return _get_period_data(code, "w", start_date, end_date)


def get_monthly(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取月线数据"""
    return _get_period_data(code, "m", start_date, end_date)


def _get_period_data(code: str, freq: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取周/月线数据"""
    bs_code = _to_bs_code(code)

    if not start_date:
        start_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")
    start_date = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    end_date = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

    raw = query_baostock_history_rows(bs_code, DAILY_SIMPLE_FIELDS, start_date, end_date, freq, "2")
    if not raw or raw.get("error_code") != "0" or not raw.get("data"):
        return pd.DataFrame()
    df = pd.DataFrame(raw["data"], columns=raw["fields"])
    for col in ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_stock_list() -> pd.DataFrame:
    """获取全部A股列表"""
    raw = query_baostock_stock_basic()
    if not raw or raw.get("error_code") != "0" or not raw.get("data"):
        return pd.DataFrame()
    df = pd.DataFrame(raw["data"], columns=raw["fields"])
    # 只保留正常上市的A股
    df = df[df["type"] == "1"]  # 1=股票
    df = df[df["status"] == "1"]  # 1=上市
    return df


# 测试
if __name__ == "__main__":
    # 测试茅台日线
    print("贵州茅台 近5日日线:")
    df = get_daily("600519")
    print(df.tail(5).to_string(index=False))

    print(f"\n获取A股列表...")
    stocks = get_stock_list()
    print(f"A股总数: {len(stocks)}")
    print(stocks.head(5).to_string(index=False))
