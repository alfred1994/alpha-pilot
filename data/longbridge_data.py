"""
长桥数据源模块（SDK OAuth模式）
通过 longport Python SDK 直接调用 OpenAPI，OAuth token 自动刷新。

核心类: LongbridgeDataSource
工具函数: to_longport_code / from_longport_code
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import pandas as pd

logger = logging.getLogger("data.longbridge")

# ══════════════════════════════════════════════════════════════════
# OAuth 初始化（单例，懒加载）
# ══════════════════════════════════════════════════════════════════

_oauth_cfg = None
_token_path = os.path.expanduser(
    os.environ.get("LONGPORT_OAUTH_TOKEN_PATH", "~/.longbridge/openapi/tokens/default")
)


def _get_config():
    """获取长桥 SDK Config（OAuth 模式，懒加载单例）"""
    global _oauth_cfg
    if _oauth_cfg is not None:
        return _oauth_cfg

    try:
        from longbridge.openapi import Config, OAuthBuilder

        if not os.path.exists(_token_path):
            logger.error(f"OAuth token 文件不存在: {_token_path}")
            return None

        with open(_token_path) as f:
            token_data = json.load(f)
        client_id = token_data["client_id"]

        # build 需要 on_open_url 回调，已授权后不会触发，给空操作即可
        oauth = OAuthBuilder(client_id).build(lambda url: None)
        _oauth_cfg = Config.from_oauth(oauth)
        logger.info("长桥 SDK OAuth 认证成功")
        return _oauth_cfg

    except Exception as e:
        logger.error(f"长桥 SDK OAuth 初始化失败: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# 代码格式转换
# ══════════════════════════════════════════════════════════════════

def to_longport_code(code: str) -> str:
    """
    转换为长桥格式: 600519 → 600519.SH, 000001 → 000001.SZ
    """
    code = code.strip()
    if "." in code and code.endswith((".SH", ".SZ")):
        return code
    if "." in code:
        parts = code.split(".")
        num = parts[-1]
    else:
        num = code
    num = num.lstrip("shszSHSZ")
    if num.startswith(("6", "5", "9")):
        return f"{num}.SH"
    return f"{num}.SZ"


def from_longport_code(code: str) -> str:
    """从长桥格式转换为系统格式: 600519.SH → 600519"""
    code = code.strip()
    if "." in code:
        parts = code.split(".")
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
    长桥数据源（SDK OAuth模式，token 自动刷新）
    """

    def get_daily_kline(self, code: str, start_date: str = None,
                        end_date: str = None) -> pd.DataFrame:
        """获取日K线，自动存入SQLite"""
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")
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
                    for col in ["source"]:
                        if col in df.columns:
                            df = df.drop(columns=[col])
                    logger.info(f"日K线命中缓存: {system_code} {len(df)}条")
                    return df
        except Exception as e:
            logger.debug(f"缓存查询失败: {e}")

        # 2. SDK 获取
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
        """从 SDK 获取日K线"""
        config = _get_config()
        if config is None:
            return None
        try:
            from longbridge.openapi import QuoteContext, Period, AdjustType
            ctx = QuoteContext(config)
            lb_code = to_longport_code(code)

            klines = ctx.history_candlesticks_by_offset(
                lb_code, Period.Day, AdjustType.ForwardAdjust,
                count=365, forward=False,
            )
            if not klines:
                logger.warning(f"长桥日K线无数据: {lb_code}")
                return None

            records = []
            for k in klines:
                try:
                    dt = datetime.fromisoformat(str(k.timestamp).replace("Z", "+00:00"))
                    trade_date = (dt + timedelta(hours=8)).strftime("%Y-%m-%d")
                except Exception:
                    trade_date = str(k.timestamp)[:10]
                if trade_date < start_date or trade_date > end_date:
                    continue
                records.append({
                    "date": trade_date,
                    "open": float(k.open),
                    "high": float(k.high),
                    "low": float(k.low),
                    "close": float(k.close),
                    "volume": float(k.volume),
                    "amount": float(k.turnover),
                    "turn": 0,
                    "pctChg": 0,
                })

            if not records:
                return None
            df = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
            if len(df) > 1:
                df["pctChg"] = (df["close"].pct_change() * 100).round(2)
            logger.info(f"长桥日K线: {lb_code} {len(df)}条")
            return df
        except Exception as e:
            logger.error(f"SDK 日K线获取失败: {e}")
            return None

    def get_minute_kline(self, code: str, period: str = "5m",
                         count: int = 100) -> pd.DataFrame:
        """获取分钟K线"""
        config = _get_config()
        if config is None:
            return pd.DataFrame()
        try:
            from longbridge.openapi import QuoteContext, Period
            ctx = QuoteContext(config)
            lb_code = to_longport_code(code)
            period_map = {
                "1m": Period.Min1, "5m": Period.Min5,
                "15m": Period.Min15, "30m": Period.Min30, "60m": Period.Min60,
            }
            sdk_period = period_map.get(period, Period.Min5)
            klines = ctx.candlesticks(lb_code, sdk_period, count, None)
            if not klines:
                return pd.DataFrame()
            records = []
            for k in klines:
                try:
                    dt = datetime.fromisoformat(str(k.timestamp).replace("Z", "+00:00"))
                    dt_str = (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    dt_str = str(k.timestamp)[:16]
                records.append({
                    "datetime": dt_str,
                    "open": float(k.open), "high": float(k.high),
                    "low": float(k.low), "close": float(k.close),
                    "volume": float(k.volume), "amount": float(k.turnover),
                })
            df = pd.DataFrame(records).sort_values("datetime").reset_index(drop=True)
            logger.info(f"长桥分钟K线: {lb_code} {period} {len(df)}条")
            return df
        except Exception as e:
            logger.error(f"SDK 分钟K线获取失败: {e}")
            return pd.DataFrame()

    def get_realtime_quote(self, codes: List[str]) -> List[Dict]:
        """获取实时行情"""
        config = _get_config()
        if config is None:
            return []
        try:
            from longbridge.openapi import QuoteContext
            ctx = QuoteContext(config)
            lb_codes = [to_longport_code(c) for c in codes]
            quotes = ctx.quote(lb_codes)
            results = []
            for q in quotes:
                last_done = float(q.last_done) if q.last_done else 0
                prev_close = float(q.prev_close) if q.prev_close else 0
                results.append({
                    "code": from_longport_code(str(q.symbol)),
                    "name": str(q.name) if hasattr(q, "name") and q.name else "",
                    "price": last_done,
                    "open": float(q.open) if q.open else 0,
                    "high": float(q.high) if q.high else 0,
                    "low": float(q.low) if q.low else 0,
                    "prev_close": prev_close,
                    "volume": int(float(q.volume)) if q.volume else 0,
                    "amount": float(q.turnover) if q.turnover else 0,
                    "change": round(last_done - prev_close, 2) if last_done and prev_close else 0,
                    "change_pct": round((last_done - prev_close) / prev_close * 100, 2) if last_done and prev_close and prev_close > 0 else 0,
                    "timestamp": "",
                })
            logger.info(f"长桥实时行情: {len(results)}只")
            return results
        except Exception as e:
            logger.error(f"SDK 实时行情获取失败: {e}")
            return []

    def get_intraday(self, code: str) -> pd.DataFrame:
        """获取分时数据"""
        config = _get_config()
        if config is None:
            return pd.DataFrame()
        try:
            from longbridge.openapi import QuoteContext
            ctx = QuoteContext(config)
            lb_code = to_longport_code(code)
            intraday = ctx.intraday(lb_code)
            if not intraday:
                return pd.DataFrame()
            records = []
            total_amount = 0.0
            total_volume = 0
            for item in intraday:
                vol = int(float(item.volume)) if item.volume else 0
                amt = float(item.turnover) if item.turnover else 0
                total_volume += vol
                total_amount += amt
                avg_price = total_amount / total_volume if total_volume > 0 else float(item.price) if item.price else 0
                try:
                    dt = datetime.fromisoformat(str(item.timestamp).replace("Z", "+00:00"))
                    time_str = (dt + timedelta(hours=8)).strftime("%H:%M")
                except Exception:
                    time_str = str(item.timestamp)[:5]
                records.append({
                    "time": time_str,
                    "price": float(item.price) if item.price else 0,
                    "volume": vol,
                    "avg_price": round(avg_price, 2),
                })
            df = pd.DataFrame(records)
            logger.info(f"长桥分时数据: {lb_code} {len(df)}条")
            return df
        except Exception as e:
            logger.error(f"SDK 分时数据获取失败: {e}")
            return pd.DataFrame()

    def get_capital_flow(self, code: str) -> Dict:
        """获取资金流向"""
        config = _get_config()
        if config is None:
            return {}
        try:
            from longbridge.openapi import QuoteContext
            ctx = QuoteContext(config)
            lb_code = to_longport_code(code)
            flow = ctx.capital_flow(lb_code)
            if not flow:
                return {}
            inflow = 0.0
            outflow = 0.0
            for f in flow:
                inflow += float(f.inflow) if hasattr(f, "inflow") and f.inflow else 0
                outflow += float(f.outflow) if hasattr(f, "outflow") and f.outflow else 0
            result = {
                "code": from_longport_code(code),
                "inflow": inflow, "outflow": outflow, "net_flow": inflow - outflow,
                "capital_in_large": 0, "capital_in_medium": 0, "capital_in_small": 0,
                "capital_out_large": 0, "capital_out_medium": 0, "capital_out_small": 0,
            }
            logger.info(f"长桥资金流向: {code}")
            return result
        except Exception as e:
            logger.error(f"SDK 资金流向获取失败: {e}")
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


def get_daily_kline(code, start_date=None, end_date=None, adjust="qfq", use_cache=True):
    return _get_default_source().get_daily_kline(code, start_date, end_date)


def get_minute_kline(code, period="5m", count=200, use_cache=True):
    return _get_default_source().get_minute_kline(code, period, count)


def get_realtime_quote(codes):
    return _get_default_source().get_realtime_quote(codes)


def get_single_quote(code):
    results = get_realtime_quote([code])
    return results[0] if results else None


def get_intraday_data(code):
    return _get_default_source().get_intraday(code)


def get_capital_flow(code):
    return _get_default_source().get_capital_flow(code)


# ══════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════

def _normalize_date(d: str) -> str:
    """标准化日期为 YYYY-MM-DD 格式"""
    d = d.strip().replace("/", "-")
    if len(d) == 8:
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


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 60)
    print("长桥数据源测试 (SDK OAuth模式)")
    print("=" * 60)
    print("\n[1] 代码格式转换:")
    for c in ["600519", "sh.600519", "000001", "600519.SH", "300750"]:
        print(f"  {c:<12} → 长桥:{to_longport_code(c):<10} 系统:{from_longport_code(c):<8} Baostock:{to_baostock_code(c)}")
    source = LongbridgeDataSource()
    print("\n[2] 实时行情:")
    for q in source.get_realtime_quote(["600519", "000001"]):
        print(f"  {q['code']} 现价:{q['price']:.2f} 涨跌:{q['change_pct']:.2f}%")
    print("\n[3] 日K线:")
    df = source.get_daily_kline("600519")
    if not df.empty:
        print(df.tail(5).to_string(index=False))
    print("\n" + "=" * 60)
    print("测试完成")
