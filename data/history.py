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
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger("data.history")

# 字段映射
DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM"
DAILY_SIMPLE_FIELDS = "date,code,open,high,low,close,volume,amount,turn,pctChg"


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

    lg = bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            bs_code, fields,
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag=adjustflag
        )

        data = []
        while rs.error_code == "0" and rs.next():
            data.append(rs.get_row_data())

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=rs.fields)

        # 类型转换
        numeric_cols = ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]
        if not simple:
            numeric_cols += ["preclose", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        logger.info(f"Baostock获取: {code} {len(df)}条")
        return df
    finally:
        bs.logout()


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

    lg = bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            bs_code, DAILY_SIMPLE_FIELDS,
            start_date=start_date, end_date=end_date,
            frequency=freq, adjustflag="2"
        )
        data = []
        while rs.error_code == "0" and rs.next():
            data.append(rs.get_row_data())
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data, columns=rs.fields)
        for col in ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    finally:
        bs.logout()


def get_stock_list() -> pd.DataFrame:
    """获取全部A股列表"""
    lg = bs.login()
    try:
        rs = bs.query_stock_basic()
        data = []
        while rs.error_code == "0" and rs.next():
            data.append(rs.get_row_data())
        df = pd.DataFrame(data, columns=rs.fields)
        # 只保留正常上市的A股
        df = df[df["type"] == "1"]  # 1=股票
        df = df[df["status"] == "1"]  # 1=上市
        return df
    finally:
        bs.logout()


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
