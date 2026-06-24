"""
历史数据模块 - Baostock + 长桥 + SQLite缓存
完全免费，无需注册，无频率限制
适合获取日线/周线/月线/分钟线历史数据

数据获取优先级:
    1. SQLite本地缓存
    2. 长桥API（如已配置）
    3. Baostock（兜底）
"""
import baostock as bs
import multiprocessing as mp
import os
import pandas as pd
import queue
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger("data.history")
BAOSTOCK_TIMEOUT = int(os.environ.get("BAOSTOCK_TIMEOUT", "45"))

# 字段映射
DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM"
DAILY_SIMPLE_FIELDS = "date,code,open,high,low,close,volume,amount,turn,pctChg"


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
        return code
    if code.startswith(("6", "5")):
        return f"sh.{code}"
    return f"sz.{code}"


def _to_system_code(code: str) -> str:
    """转换为系统格式（纯数字）"""
    code = code.strip()
    if "." in code:
        return code.split(".")[-1]
    return code.lstrip("shszSHSZ")


def _try_cache(code: str, start_date: str, end_date: str,
               adjust: str = "qfq") -> Optional[pd.DataFrame]:
    """
    尝试从SQLite缓存获取数据

    Returns:
        DataFrame 或 None（缓存未命中）
    """
    try:
        from data.database import Database
        system_code = _to_system_code(code)
        with Database() as db:
            cached = db.get_k_daily(system_code, start_date, end_date)
            if cached and len(cached) > 0:
                df = pd.DataFrame(cached)
                # 清理内部字段
                for col in ["source"]:
                    if col in df.columns:
                        df = df.drop(columns=[col])
                logger.info(f"缓存命中: {system_code} {len(df)}条")
                return df
    except Exception as e:
        logger.debug(f"缓存查询失败: {e}")
    return None


def _save_to_cache(code: str, df: pd.DataFrame, adjust: str = "qfq",
                   source: str = "baostock"):
    """将数据保存到SQLite缓存"""
    if df is None or df.empty:
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
              adjust: str = "qfq", simple: bool = True) -> pd.DataFrame:
    """
    获取日线数据（带缓存 + 长桥优先）

    数据获取优先级:
        1. SQLite本地缓存
        2. 长桥API（如已配置）
        3. Baostock（兜底）

    Args:
        code: 股票代码, 如 "600519" 或 "sh.600519"
        start_date: 开始日期 "YYYY-MM-DD" 或 "YYYYMMDD", 默认30天前
        end_date: 结束日期, 默认今天
        adjust: "qfq"前复权 / "hfq"后复权 / ""不复权
        simple: True=简单字段(快), False=完整字段(含PE/PB等)

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
    if simple:
        # 1. 先查SQLite缓存
        df = _try_cache(code, start_date, end_date, adjust)
        if df is not None:
            return df

        # 2. 尝试长桥API
        df = _try_longbridge(code, start_date, end_date, adjust)
        if df is not None:
            _save_to_cache(code, df, adjust, source="longport")
            return df

    # 3. 回退到Baostock
    df = _fetch_baostock(code, start_date, end_date, adjust, simple)

    # 保存到缓存（仅简单字段模式）
    if simple and df is not None and not df.empty:
        _save_to_cache(code, df, adjust, source="baostock")

    return df


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
