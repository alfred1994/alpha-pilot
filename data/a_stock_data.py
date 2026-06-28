"""
a-stock-data 集成模块 - Phase 1
====================================================================
提取 a-stock-data 项目核心接口，用于数据源冗余和新增选股维度

Phase 1 功能：
1. 融资融券明细（新增选股维度）
2. 限售解禁日历（风险预警）
3. mootdx K线备份（长桥故障降级）

数据源：simonlin1212/a-stock-data v3.3.0
====================================================================
"""
import time
import random
import logging
import requests
import socket
from typing import List, Dict, Optional
from datetime import datetime, timedelta

logger = logging.getLogger("data.a_stock_data")

# ═══════════════════════════════════════════════════════════════════
# 东财统一限流防封（所有东财API必须走这个）
# ═══════════════════════════════════════════════════════════════════

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA})
# 连接级自动重试：瞬态连接错误 / 429 / 5xx 指数退避重试。
# 403 通常是东财风控信号，不重试，靠降频处理。
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    _em_adapter = HTTPAdapter(max_retries=Retry(
        total=3,
        connect=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    ))
    EM_SESSION.mount("https://", _em_adapter)
    EM_SESSION.mount("http://", _em_adapter)
except Exception:
    pass
EM_MIN_INTERVAL = 1.5  # 1.5秒间隔（你的批量任务多，调高点）
_em_last_call = [0.0]

def em_get(url: str, params: dict = None, headers: dict = None, timeout: int = 15, **kwargs):
    """东财统一请求入口：自动节流 + 复用session + 默认UA"""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = time.time()


def eastmoney_datacenter(report_name: str, columns: str = "ALL",
                          filter_str: str = "", page_size: int = 50,
                          sort_columns: str = "", sort_types: str = "-1") -> List[Dict]:
    """东财数据中心统一查询"""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = em_get(DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ═══════════════════════════════════════════════════════════════════
# Phase 1.1: 融资融券明细
# ═══════════════════════════════════════════════════════════════════

def get_margin_detail(code: str, days: int = 30) -> Optional[Dict]:
    """
    获取个股融资融券明细

    Args:
        code: 股票代码（6位）
        days: 查询天数

    Returns:
        {
            "code": "600519",
            "name": "贵州茅台",
            "latest_date": "2026-06-10",
            "financing_balance": 1234567890.0,  # 融资余额（元）
            "financing_buy": 12345678.0,        # 融资买入额
            "financing_ratio": 0.05,            # 融资余额占流通市值比例
            "margin_balance": 1234567.0,        # 融券余额（元）
            "margin_sell": 123456.0,            # 融券卖出量
            "trend": "增长",                     # 融资余额趋势
        }
    """
    try:
        # 东财融资融券明细
        data = eastmoney_datacenter(
            report_name="RPT_RZRQ_LSHJ",
            columns="TRADE_DATE,SECURITY_CODE,SECUCODE,RZYE,RZMRE,RZCHE,RQYE,RQMCL,RQCHL",
            filter_str=f'(SECURITY_CODE="{code}")',
            page_size=days,
            sort_columns="TRADE_DATE",
            sort_types="-1",
        )

        if not data:
            logger.warning(f"融资融券数据为空: {code}")
            return None

        latest = data[0]

        # 计算趋势（最近5天均值 vs 前5天均值）
        trend = "持平"
        if len(data) >= 10:
            recent_avg = sum(d.get("RZYE", 0) or 0 for d in data[:5]) / 5
            prev_avg = sum(d.get("RZYE", 0) or 0 for d in data[5:10]) / 5
            if recent_avg > prev_avg * 1.1:
                trend = "快速增长"
            elif recent_avg > prev_avg * 1.02:
                trend = "增长"
            elif recent_avg < prev_avg * 0.9:
                trend = "快速下降"
            elif recent_avg < prev_avg * 0.98:
                trend = "下降"

        return {
            "code": code,
            "name": latest.get("SECUCODE", "").split(".")[0],
            "latest_date": latest.get("TRADE_DATE", "")[:10],
            "financing_balance": latest.get("RZYE", 0) or 0,
            "financing_buy": latest.get("RZMRE", 0) or 0,
            "financing_repay": latest.get("RZCHE", 0) or 0,
            "margin_balance": latest.get("RQYE", 0) or 0,
            "margin_sell": latest.get("RQMCL", 0) or 0,
            "margin_repay": latest.get("RQCHL", 0) or 0,
            "trend": trend,
        }
    except Exception as e:
        logger.error(f"获取融资融券失败 {code}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# Phase 1.2: 限售解禁日历
# ═══════════════════════════════════════════════════════════════════

def get_unlock_schedule(code: str, days_ahead: int = 90) -> Optional[Dict]:
    """
    获取限售解禁日历

    Args:
        code: 股票代码（6位）
        days_ahead: 未来多少天内的解禁

    Returns:
        {
            "code": "600519",
            "name": "贵州茅台",
            "upcoming_unlocks": [
                {
                    "date": "2026-07-15",
                    "unlock_amount": 123456789.0,  # 解禁股数
                    "unlock_ratio": 0.05,          # 解禁占总股本比例
                    "days_to_unlock": 35,
                },
                ...
            ],
            "total_unlock_amount": 234567890.0,
            "has_major_unlock": True,  # 是否有重大解禁（>5%）
        }
    """
    try:
        end_date = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        data = eastmoney_datacenter(
            report_name="RPT_LIFT_STAGE",
            columns="SECURITY_CODE,SECURITY_NAME_ABBR,NEW_PRICE,LISTING_DATE,LIFT_DATE,LIFT_SHARES,ACTUAL_LIFT_SHARES,FREE_RATIO,FREE_SHARES_RATIO",
            filter_str=f'(SECURITY_CODE="{code}")(LIFT_DATE>=\'{datetime.now().strftime("%Y-%m-%d")}\')',
            page_size=20,
            sort_columns="LIFT_DATE",
            sort_types="1",  # 升序
        )

        if not data:
            return {
                "code": code,
                "upcoming_unlocks": [],
                "total_unlock_amount": 0,
                "has_major_unlock": False,
            }

        upcoming = []
        total_unlock = 0
        has_major = False

        for item in data:
            lift_date = item.get("LIFT_DATE", "")[:10]
            unlock_shares = item.get("ACTUAL_LIFT_SHARES", 0) or item.get("LIFT_SHARES", 0) or 0
            unlock_ratio = (item.get("FREE_SHARES_RATIO", 0) or 0) / 100

            days_to_unlock = (datetime.strptime(lift_date, "%Y-%m-%d") - datetime.now()).days

            if days_to_unlock > days_ahead:
                break

            if unlock_ratio >= 0.05:
                has_major = True

            upcoming.append({
                "date": lift_date,
                "unlock_amount": unlock_shares,
                "unlock_ratio": unlock_ratio,
                "days_to_unlock": days_to_unlock,
            })

            total_unlock += unlock_shares

        return {
            "code": code,
            "name": data[0].get("SECURITY_NAME_ABBR", ""),
            "upcoming_unlocks": upcoming,
            "total_unlock_amount": total_unlock,
            "has_major_unlock": has_major,
        }
    except Exception as e:
        logger.error(f"获取解禁日历失败 {code}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# Phase 1.3: 打板情绪数据（涨停/炸板/跌停/昨日涨停）
# ═══════════════════════════════════════════════════════════════════

ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _fmt_zt_time(t) -> str:
    """涨停板时间整数转HH:MM:SS"""
    if t in (None, "", 0):
        return ""
    s = str(t).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def _num(value, default: float = 0.0) -> float:
    """安全转换数值字段"""
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _em_zt_api(endpoint: str, sort: str, date: str, page_size: int = 10000) -> List[Dict]:
    """东方财富涨停板行情中心通用请求"""
    url = f"https://push2ex.eastmoney.com/{endpoint}"
    params = {
        "ut": ZTB_UT,
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": page_size,
        "sort": sort,
        "date": date,
    }
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        r = em_get(url, params=params, headers=headers, timeout=10)
        return (r.json().get("data") or {}).get("pool") or []
    except Exception as e:
        logger.warning(f"涨停板池请求失败 {endpoint}: {e}")
        return []


def get_limit_up_pool(date: str = None) -> List[Dict]:
    """
    获取涨停池，包含连板数、封板资金、炸板次数和N天M板。
    """
    if not date:
        date = datetime.now().strftime("%Y%m%d")

    rows = []
    for p in _em_zt_api("getTopicZTPool", "fbt:asc", date):
        zt_stat = p.get("zttj") or {}
        rows.append({
            "code": p.get("c", ""),
            "name": p.get("n", ""),
            "price": _num(p.get("p")) / 1000,
            "pct": round(_num(p.get("zdp")), 2),
            "amount": _num(p.get("amount")),
            "float_cap": _num(p.get("ltsz")),
            "turnover": round(_num(p.get("hs")), 2),
            "limit_days": int(_num(p.get("lbc"), 1)),
            "first_seal": _fmt_zt_time(p.get("fbt")),
            "last_seal": _fmt_zt_time(p.get("lbt")),
            "seal_fund": _num(p.get("fund")),
            "break_times": int(_num(p.get("zbc"))),
            "industry": p.get("hybk", ""),
            "zt_stat": f'{zt_stat.get("days", "?")}天{zt_stat.get("ct", "?")}板',
        })
    return rows


def get_break_board_pool(date: str = None) -> List[Dict]:
    """获取炸板池（涨停后开板）"""
    if not date:
        date = datetime.now().strftime("%Y%m%d")

    rows = []
    for p in _em_zt_api("getTopicZBPool", "fbt:asc", date):
        zt_stat = p.get("zttj") or {}
        rows.append({
            "code": p.get("c", ""),
            "name": p.get("n", ""),
            "price": _num(p.get("p")) / 1000,
            "limit_price": _num(p.get("ztp")) / 1000,
            "pct": round(_num(p.get("zdp")), 2),
            "turnover": round(_num(p.get("hs")), 2),
            "first_seal": _fmt_zt_time(p.get("fbt")),
            "break_times": int(_num(p.get("zbc"))),
            "amplitude": round(_num(p.get("zf")), 2),
            "speed": round(_num(p.get("zs")), 2),
            "industry": p.get("hybk", ""),
            "zt_stat": f'{zt_stat.get("days", "?")}天{zt_stat.get("ct", "?")}板',
        })
    return rows


def get_limit_down_pool(date: str = None) -> List[Dict]:
    """获取跌停池"""
    if not date:
        date = datetime.now().strftime("%Y%m%d")

    rows = []
    for p in _em_zt_api("getTopicDTPool", "fund:asc", date):
        rows.append({
            "code": p.get("c", ""),
            "name": p.get("n", ""),
            "price": _num(p.get("p")) / 1000,
            "pct": round(_num(p.get("zdp")), 2),
            "turnover": round(_num(p.get("hs")), 2),
            "pe": _num(p.get("pe")),
            "seal_fund": _num(p.get("fund")),
            "last_seal": _fmt_zt_time(p.get("lbt")),
            "board_amount": _num(p.get("fba")),
            "dt_days": int(_num(p.get("days"))),
            "open_times": int(_num(p.get("oc"))),
            "industry": p.get("hybk", ""),
        })
    return rows


def get_yesterday_limit_up_pool(date: str = None) -> List[Dict]:
    """获取昨日涨停池，用于计算晋级率和赚钱效应"""
    if not date:
        date = datetime.now().strftime("%Y%m%d")

    rows = []
    for p in _em_zt_api("getYesterdayZTPool", "zs:desc", date):
        zt_stat = p.get("zttj") or {}
        rows.append({
            "code": p.get("c", ""),
            "name": p.get("n", ""),
            "price": _num(p.get("p")) / 1000,
            "pct": round(_num(p.get("zdp")), 2),
            "turnover": round(_num(p.get("hs")), 2),
            "amplitude": round(_num(p.get("zf")), 2),
            "speed": round(_num(p.get("zs")), 2),
            "y_first_seal": _fmt_zt_time(p.get("yfbt")),
            "y_limit_days": int(_num(p.get("ylbc"))),
            "industry": p.get("hybk", ""),
            "zt_stat": f'{zt_stat.get("days", "?")}天{zt_stat.get("ct", "?")}板',
        })
    return rows


def get_limit_up_sentiment(date: str = None) -> Dict:
    """
    打板情绪温度计：涨停/炸板/跌停数量、炸板率、最高连板、连板梯队、昨日涨停晋级率。
    """
    if not date:
        date = datetime.now().strftime("%Y%m%d")

    zt = get_limit_up_pool(date)
    zb = get_break_board_pool(date)
    dt = get_limit_down_pool(date)
    yzt = get_yesterday_limit_up_pool(date)

    ladder = {}
    for s in zt:
        days = int(s.get("limit_days") or 0)
        ladder[days] = ladder.get(days, 0) + 1

    zt_count = len(zt)
    zb_count = len(zb)
    yzt_count = len(yzt)
    promoted = sum(1 for s in yzt if _num(s.get("pct")) >= 9.8)

    return {
        "date": date,
        "zt_count": zt_count,
        "zb_count": zb_count,
        "dt_count": len(dt),
        "break_rate": round(zb_count / (zt_count + zb_count) * 100, 1) if (zt_count + zb_count) else 0,
        "max_height": max((int(s.get("limit_days") or 0) for s in zt), default=0),
        "ladder": dict(sorted(ladder.items())),
        "yzt_count": yzt_count,
        "promotion_rate": round(promoted / yzt_count * 100, 1) if yzt_count else 0,
    }


# ═══════════════════════════════════════════════════════════════════
# Phase 1.4: mootdx K线备份
# ═══════════════════════════════════════════════════════════════════

_TDX_SERVERS = [
    ("119.97.185.59", 7709),
    ("124.70.133.119", 7709),
    ("116.205.183.150", 7709),
    ("123.60.73.44", 7709),
    ("116.205.163.254", 7709),
    ("121.36.225.169", 7709),
    ("123.60.70.228", 7709),
    ("124.71.9.153", 7709),
    ("110.41.147.114", 7709),
    ("124.71.187.122", 7709),
]


def _probe(ip: str, port: int, timeout: float = 2.0) -> bool:
    """TCP握手探测，判断通达信服务器是否可达"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def tdx_client(market: str = "std"):
    """
    创建mootdx客户端，规避新环境 BESTIP 为空导致初始化失败的问题。

    兜底顺序：
    1. 探测内置通达信服务器，使用第一个可达节点；
    2. 回退mootdx bestip测速；
    3. 回退裸factory；
    4. 全部失败时抛出明确错误。
    """
    from mootdx.quotes import Quotes

    for ip, port in _TDX_SERVERS:
        if _probe(ip, port):
            return Quotes.factory(market=market, server=(ip, port))

    try:
        return Quotes.factory(market=market, bestip=True)
    except Exception:
        pass

    try:
        return Quotes.factory(market=market)
    except Exception as e:
        raise RuntimeError(
            "所有mootdx服务器均不可达，请检查网络或更新_TDX_SERVERS列表。"
            f"原始错误: {e}"
        )


def get_kline_mootdx(code: str, start_date: str = None, end_date: str = None,
                     period: str = "daily") -> Optional[List[Dict]]:
    """
    mootdx获取K线数据（通达信TCP，不封IP）

    Args:
        code: 股票代码（6位）
        start_date: 开始日期 YYYY-MM-DD（可选）
        end_date: 结束日期 YYYY-MM-DD（可选）
        period: daily/weekly/monthly

    Returns:
        [
            {
                "date": "2026-06-10",
                "open": 1850.0,
                "high": 1860.0,
                "low": 1840.0,
                "close": 1855.0,
                "volume": 12345678,
                "amount": 1234567890.0,
            },
            ...
        ]
    """
    try:
        code = normalize_code(code)

        # 市场：0=深圳, 1=上海
        market = 1 if code.startswith(("6", "9")) else 0

        # mootdx参数名必须是frequency，不是category；传错会被**kwargs静默吞掉。
        # 0=5分钟, 1=15分钟, 2=30分钟, 3=60分钟, 4=日线, 5=周线, 6=月线, 8=1分钟
        frequency_map = {
            "5m": 0,
            "15m": 1,
            "30m": 2,
            "60m": 3,
            "daily": 4,
            "weekly": 5,
            "monthly": 6,
            "1m": 8,
        }
        frequency = frequency_map.get(period, 4)
        client = tdx_client()

        # mootdx需要指定offset，我们拉最近1000条
        bars = client.bars(symbol=code, frequency=frequency, offset=1000, market=market)

        if bars is None or bars.empty:
            logger.warning(f"mootdx K线为空: {code}")
            return None

        # 转换格式
        result = []
        for _, row in bars.iterrows():
            result.append({
                "date": str(row["datetime"])[:10],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["vol"]),
                "amount": float(row["amount"]),
            })

        # 日期过滤
        if start_date:
            result = [r for r in result if r["date"] >= start_date]
        if end_date:
            result = [r for r in result if r["date"] <= end_date]

        logger.info(f"mootdx K线获取成功: {code} {len(result)}条")
        return result

    except Exception as e:
        logger.error(f"mootdx K线获取失败 {code}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

def normalize_code(code: str) -> str:
    """归一化股票代码（去除市场前缀）"""
    code = code.upper().strip()
    # 去除 SH/SZ/BJ 前缀
    for prefix in ["SH", "SZ", "BJ"]:
        if code.startswith(prefix):
            code = code[2:]
    # 去除 .SH/.SZ/.BJ 后缀
    if "." in code:
        code = code.split(".")[0]
    return code


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # 测试融资融券
    print("=" * 50)
    print("测试融资融券")
    print("=" * 50)
    margin = get_margin_detail("600519")
    if margin:
        print(f"融资余额: {margin['financing_balance']/1e8:.2f}亿")
        print(f"趋势: {margin['trend']}")

    time.sleep(2)

    # 测试限售解禁
    print("\n" + "=" * 50)
    print("测试限售解禁")
    print("=" * 50)
    unlock = get_unlock_schedule("600519", days_ahead=90)
    if unlock:
        print(f"未来90天解禁: {len(unlock['upcoming_unlocks'])}次")
        print(f"有重大解禁: {unlock['has_major_unlock']}")
        for u in unlock['upcoming_unlocks'][:3]:
            print(f"  {u['date']} 解禁 {u['unlock_ratio']:.2%}")

    # 测试mootdx
    print("\n" + "=" * 50)
    print("测试mootdx K线")
    print("=" * 50)
    kline = get_kline_mootdx("600519", start_date="2026-05-01")
    if kline:
        print(f"K线条数: {len(kline)}")
        print(f"最新: {kline[-1]}")
