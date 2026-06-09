"""
长桥数据源模块
封装所有长桥OpenAPI调用，提供日K线、分钟K线、实时行情、分时数据、资金流向
与SQLite联动：先查本地缓存，没有才调API

核心类: LongbridgeDataSource
工具函数: to_longport_code / from_longport_code
"""
import os
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict

import pandas as pd

logger = logging.getLogger("data.longbridge")

# 延迟导入长桥SDK，避免未安装时崩溃
_quote_ctx = None
_config = None


def _get_config():
    """获取长桥配置（从环境变量读取）"""
    global _config
    if _config is None:
        try:
            from longport.openapi import Config
            _config = Config.from_env()
        except Exception as e:
            logger.error(f"长桥配置初始化失败: {e}")
            return None
    return _config


def _get_quote_context():
    """获取行情上下文（单例）"""
    global _quote_ctx
    if _quote_ctx is None:
        cfg = _get_config()
        if cfg is None:
            return None
        try:
            from longport.openapi import QuoteContext
            _quote_ctx = QuoteContext(cfg)
            logger.info("长桥行情上下文初始化成功")
        except Exception as e:
            logger.error(f"长桥行情上下文初始化失败: {e}")
            return None
    return _quote_ctx


# ══════════════════════════════════════════════════════════════════
# 代码格式转换
# ══════════════════════════════════════════════════════════════════

def to_longport_code(code: str) -> str:
    """
    转换为长桥格式: 600519 → 600519.SH, 000001 → 000001.SZ

    支持输入:
        "600519"      → "600519.SH"
        "sh600519"    → "600519.SH"
        "sh.600519"   → "600519.SH"
        "600519.SH"   → "600519.SH" (原样返回)
        "sz000001"    → "000001.SZ"
    """
    code = code.strip()

    # 已经是长桥格式
    if "." in code and code.endswith((".SH", ".SZ")):
        return code

    # baostock格式: sh.600519
    if "." in code:
        parts = code.split(".")
        num = parts[-1]
    else:
        num = code

    # 去掉可能的前缀
    num = num.lstrip("shszSHSZ")

    # 判断市场: 6/5/9开头 → 上海, 其余 → 深圳
    # 注意: 000开头的代码可能是深圳股票(000001平安银行)或上海指数(000300沪深300)
    #       无法仅靠数字区分，建议调用方传显式格式如 "000300.SH"
    if num.startswith(("6", "5", "9")):
        return f"{num}.SH"
    return f"{num}.SZ"


def from_longport_code(code: str) -> str:
    """
    从长桥格式转换为系统格式: 600519.SH → 600519

    同时兼容其他格式输入，统一返回纯数字代码
    """
    code = code.strip()
    if "." in code:
        parts = code.split(".")
        # 长桥格式: 600519.SH → parts[0]="600519"
        # baostock格式: sh.600519 → parts[1]="600519"
        num = parts[1] if parts[0].lower() in ("sh", "sz") else parts[0]
        return num
    return code.lstrip("shszSHSZ")


# 兼容旧代码
to_longbridge_code = to_longport_code
to_system_code = from_longport_code


# ══════════════════════════════════════════════════════════════════
# LongbridgeDataSource 核心类
# ══════════════════════════════════════════════════════════════════

class LongbridgeDataSource:
    """
    长桥数据源

    方法:
        get_daily_kline(code, start_date, end_date)  - 日K线，自动存入SQLite
        get_minute_kline(code, period, count)         - 分钟K线
        get_realtime_quote(codes)                     - 实时行情
        get_intraday(code)                            - 分时数据
        get_capital_flow(code)                        - 资金流向
    """

    def __init__(self):
        self._ctx = None

    @property
    def ctx(self):
        """懒加载行情上下文"""
        if self._ctx is None:
            self._ctx = _get_quote_context()
        return self._ctx

    # ── 日K线 ──

    def get_daily_kline(self, code: str, start_date: str = None,
                        end_date: str = None) -> pd.DataFrame:
        """
        获取日K线，自动存入SQLite

        Args:
            code: 股票代码，如 "600519"
            start_date: "YYYY-MM-DD"，默认30天前
            end_date:   "YYYY-MM-DD"，默认今天

        Returns:
            DataFrame: date, open, high, low, close, volume, amount, turn, pctChg
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        # 标准化日期
        start_date = _normalize_date(start_date)
        end_date = _normalize_date(end_date)

        system_code = from_longport_code(code)

        # 1. 先查SQLite缓存
        try:
            from data.database import Database
            with Database() as db:
                cached = db.get_k_daily(system_code, start_date, end_date)
                if cached:
                    df = pd.DataFrame(cached)
                    # 去掉内部字段
                    for col in ["source"]:
                        if col in df.columns:
                            df = df.drop(columns=[col])
                    logger.info(f"日K线命中缓存: {system_code} {len(df)}条")
                    return df
        except Exception as e:
            logger.debug(f"缓存查询失败: {e}")

        # 2. 调长桥API
        df = self._fetch_daily(code, start_date, end_date)

        # 3. 存入SQLite
        if df is not None and not df.empty:
            try:
                from data.database import Database
                records = df.to_dict("records")
                for r in records:
                    r["code"] = system_code
                with Database() as db:
                    db.insert_k_daily(records, source="longport")
            except Exception as e:
                logger.debug(f"缓存保存失败: {e}")

        return df if df is not None else pd.DataFrame()

    def _fetch_daily(self, code: str, start_date: str,
                     end_date: str) -> Optional[pd.DataFrame]:
        """从长桥API获取日K线"""
        if self.ctx is None:
            return None

        try:
            from longport.openapi import Period, AdjustType
            from datetime import date as date_cls

            lb_code = to_longport_code(code)

            # 解析日期
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")

            # 用 history_candlesticks_by_date 获取指定日期范围
            candlesticks = self.ctx.history_candlesticks_by_date(
                lb_code, Period.Day, AdjustType.NoAdjust,
                start_dt.date(), end_dt.date(),
            )

            if not candlesticks:
                # 回退到 candlesticks
                candlesticks = self.ctx.candlesticks(
                    lb_code, Period.Day, 2000, AdjustType.NoAdjust,
                )

            if not candlesticks:
                logger.warning(f"长桥日K线无数据: {lb_code}")
                return None

            records = []
            for c in candlesticks:
                dt_str = c.timestamp.strftime("%Y-%m-%d")
                if dt_str < start_date or dt_str > end_date:
                    continue
                records.append({
                    "date": dt_str,
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": float(c.volume),
                    "amount": float(c.turnover),
                    "turn": 0,
                    "pctChg": 0,
                })

            if not records:
                return None

            df = pd.DataFrame(records)
            df = df.sort_values("date").reset_index(drop=True)

            # 计算涨跌幅
            if len(df) > 1:
                df["pctChg"] = (df["close"].pct_change() * 100).round(2)

            logger.info(f"长桥日K线: {lb_code} {len(df)}条")
            return df

        except Exception as e:
            logger.error(f"长桥日K线失败: {code} - {e}")
            return None

    # ── 分钟K线 ──

    def get_minute_kline(self, code: str, period: str = "5m",
                         count: int = 100) -> pd.DataFrame:
        """
        获取分钟K线

        Args:
            code: 股票代码
            period: 周期 "1m"/"5m"/"15m"/"30m"/"60m"
            count: 返回条数

        Returns:
            DataFrame: datetime, open, high, low, close, volume, amount
        """
        if self.ctx is None:
            return pd.DataFrame()

        try:
            from longport.openapi import Period, AdjustType

            lb_code = to_longport_code(code)

            period_map = {
                "1m": Period.Min_1,
                "5m": Period.Min_5,
                "15m": Period.Min_15,
                "30m": Period.Min_30,
                "60m": Period.Min_60,
            }
            lb_period = period_map.get(period, Period.Min_5)

            candlesticks = self.ctx.candlesticks(
                lb_code, lb_period, min(count, 1000), AdjustType.NoAdjust,
            )

            if not candlesticks:
                logger.warning(f"长桥分钟K线无数据: {lb_code}")
                return pd.DataFrame()

            records = []
            for c in candlesticks:
                records.append({
                    "datetime": c.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": float(c.volume),
                    "amount": float(c.turnover),
                })

            df = pd.DataFrame(records).sort_values("datetime").reset_index(drop=True)
            logger.info(f"长桥分钟K线: {lb_code} {period} {len(df)}条")
            return df

        except Exception as e:
            logger.error(f"长桥分钟K线失败: {code} - {e}")
            return pd.DataFrame()

    # ── 实时行情 ──

    def get_realtime_quote(self, codes: List[str]) -> List[Dict]:
        """
        获取实时行情

        Args:
            codes: 股票代码列表，如 ["600519", "000001"]

        Returns:
            行情数据列表
        """
        if self.ctx is None:
            return []

        try:
            lb_codes = [to_longport_code(c) for c in codes]
            quotes = self.ctx.quote(lb_codes)

            results = []
            for q in quotes:
                last_done = float(q.last_done) if q.last_done else 0
                prev_close = float(q.prev_close) if q.prev_close else 0
                results.append({
                    "code": from_longport_code(q.symbol),
                    "name": "",  # SecurityQuote 无 name 字段，需另行获取
                    "price": last_done,
                    "open": float(q.open) if q.open else 0,
                    "high": float(q.high) if q.high else 0,
                    "low": float(q.low) if q.low else 0,
                    "prev_close": prev_close,
                    "volume": int(q.volume) if q.volume else 0,
                    "amount": float(q.turnover) if q.turnover else 0,
                    "change": round(last_done - prev_close, 2) if last_done and prev_close else 0,
                    "change_pct": round((last_done - prev_close) / prev_close * 100, 2) if last_done and prev_close and prev_close > 0 else 0,
                    "timestamp": q.timestamp.strftime("%Y-%m-%d %H:%M:%S") if q.timestamp else "",
                })

            logger.info(f"长桥实时行情: {len(results)}只")
            return results

        except Exception as e:
            logger.error(f"长桥实时行情失败: {e}")
            return []

    # ── 分时数据 ──

    def get_intraday(self, code: str) -> pd.DataFrame:
        """
        获取分时数据

        Args:
            code: 股票代码

        Returns:
            DataFrame: time, price, volume, avg_price
        """
        if self.ctx is None:
            return pd.DataFrame()

        try:
            from longport.openapi import Period, AdjustType

            lb_code = to_longport_code(code)

            # 使用1分钟K线作为分时数据
            candlesticks = self.ctx.candlesticks(
                lb_code, Period.Min_1, 240, AdjustType.NoAdjust,
            )

            if not candlesticks:
                return pd.DataFrame()

            records = []
            total_amount = 0.0
            total_volume = 0
            for c in candlesticks:
                total_volume += int(c.volume)
                total_amount += float(c.turnover)
                avg_price = total_amount / total_volume if total_volume > 0 else float(c.close)
                records.append({
                    "time": c.timestamp.strftime("%H:%M"),
                    "price": float(c.close),
                    "volume": int(c.volume),
                    "avg_price": round(avg_price, 2),
                })

            df = pd.DataFrame(records)
            logger.info(f"长桥分时数据: {lb_code} {len(df)}条")
            return df

        except Exception as e:
            logger.error(f"长桥分时数据失败: {code} - {e}")
            return pd.DataFrame()

    # ── 资金流向 ──

    def get_capital_flow(self, code: str) -> Dict:
        """
        获取资金流向

        Args:
            code: 股票代码

        Returns:
            资金流向数据 {inflow, outflow, net_flow, ...}
        """
        if self.ctx is None:
            return {}

        try:
            lb_code = to_longport_code(code)
            flow = self.ctx.capital_distribution(lb_code)

            if not flow:
                return {}

            # capital_distribution 返回单个对象，含 capital_in / capital_out
            # 每个是 CapitalDistribution { large, medium, small }
            cap_in = flow.capital_in
            cap_out = flow.capital_out
            inflow = cap_in.large + cap_in.medium + cap_in.small
            outflow = cap_out.large + cap_out.medium + cap_out.small

            result = {
                "code": from_longport_code(code),
                "inflow": float(inflow),
                "outflow": float(outflow),
                "net_flow": float(inflow - outflow),
                "capital_in_large": float(cap_in.large),
                "capital_in_medium": float(cap_in.medium),
                "capital_in_small": float(cap_in.small),
                "capital_out_large": float(cap_out.large),
                "capital_out_medium": float(cap_out.medium),
                "capital_out_small": float(cap_out.small),
            }

            logger.info(f"长桥资金流向: {code}")
            return result

        except Exception as e:
            logger.error(f"长桥资金流向失败: {code} - {e}")
            return {}


# ══════════════════════════════════════════════════════════════════
# 模块级便捷函数（兼容旧代码）
# ══════════════════════════════════════════════════════════════════

_default_source = None


def _get_default_source():
    global _default_source
    if _default_source is None:
        _default_source = LongbridgeDataSource()
    return _default_source


def get_daily_kline(code, start_date=None, end_date=None,
                    adjust="qfq", use_cache=True):
    """模块级日K线获取（兼容旧代码）"""
    return _get_default_source().get_daily_kline(code, start_date, end_date)


def get_minute_kline(code, period="5m", count=200, use_cache=True):
    """模块级分钟K线获取"""
    return _get_default_source().get_minute_kline(code, period, count)


def get_realtime_quote(codes):
    """模块级实时行情"""
    return _get_default_source().get_realtime_quote(codes)


def get_single_quote(code):
    """获取单只股票行情"""
    results = get_realtime_quote([code])
    return results[0] if results else None


def get_intraday_data(code):
    """模块级分时数据"""
    return _get_default_source().get_intraday(code)


def get_capital_flow(code):
    """模块级资金流向"""
    return _get_default_source().get_capital_flow(code)


# ══════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════

def _normalize_date(d: str) -> str:
    """标准化日期为 YYYY-MM-DD 格式"""
    d = d.strip().replace("/", "-")
    if len(d) == 8:  # YYYYMMDD
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def is_market_open() -> bool:
    """判断当前是否为交易时间"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    from datetime import time as dtime
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


def to_baostock_code(code: str) -> str:
    """转换为Baostock格式: 600519 → sh.600519"""
    code = code.strip()
    if "." in code and code.startswith(("sh.", "sz.")):
        return code
    num = from_longport_code(code)
    if num.startswith(("6", "5", "9")):
        return f"sh.{num}"
    return f"sz.{num}"


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    print("=" * 60)
    print("长桥数据源测试")
    print("=" * 60)

    # 代码转换测试
    print("\n[1] 代码格式转换:")
    test_codes = ["600519", "sh.600519", "000001", "sz.000001", "600519.SH", "300750"]
    for c in test_codes:
        lp = to_longport_code(c)
        sys_code = from_longport_code(c)
        bs = to_baostock_code(c)
        print(f"  {c:<12} → 长桥:{lp:<10} 系统:{sys_code:<8} Baostock:{bs}")

    # 使用类的方式
    source = LongbridgeDataSource()

    # 实时行情
    print("\n[2] 实时行情:")
    quotes = source.get_realtime_quote(["600519", "000001"])
    for q in quotes:
        print(f"  {q['code']} {q['name']} 现价:{q['price']:.2f} 涨跌:{q['change_pct']:.2f}%")

    # 日K线
    print("\n[3] 日K线:")
    df = source.get_daily_kline("600519")
    if not df.empty:
        print(df.tail(5).to_string(index=False))

    print("\n" + "=" * 60)
    print("测试完成")
