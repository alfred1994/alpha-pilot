"""
a-stock-data 集成模块 - Phase 1
====================================================================
提取 a-stock-data 项目核心接口，用于数据源冗余和新增选股维度

Phase 1 功能：
1. 融资融券明细（新增选股维度）
2. 限售解禁日历（风险预警）
3. mootdx K线备份（长桥故障降级）

数据源：simonlin1212/a-stock-data v3.2.2
====================================================================
"""
import time
import random
import logging
import requests
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
# Phase 1.3: mootdx K线备份
# ═══════════════════════════════════════════════════════════════════

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
        from mootdx.quotes import Quotes

        # 市场：0=深圳, 1=上海
        market = 1 if code.startswith(("6", "9")) else 0

        # 周期：4=日线, 5=周线, 6=月线
        category_map = {"daily": 4, "weekly": 5, "monthly": 6}
        category = category_map.get(period, 4)

        # 先尝试自动选择最佳服务器
        try:
            client = Quotes.factory(market='std', bestip=True)
        except:
            # 降级：手动指定服务器
            client = Quotes.factory(market='std', multithread=True)

        # mootdx需要指定offset，我们拉最近1000条
        bars = client.bars(symbol=code, category=category, offset=1000, market=market)

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
