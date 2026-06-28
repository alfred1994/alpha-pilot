"""
东方财富数据源 - CloakBrowser
涨停板、龙虎榜、融资融券、个股新闻
"""
import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, List

logger = logging.getLogger("data.eastmoney")

# ═══ 线程本地存储（每个线程独立的事件循环+浏览器实例） ═══
# 解决 ThreadPoolExecutor 并发打分时多线程竞争同一事件循环的问题
_thread_local = threading.local()


def cleanup_eastmoney():
    """清理当前线程的浏览器实例和事件循环（避免asyncio泄漏）"""
    browser = getattr(_thread_local, "browser", None)
    if browser is not None:
        try:
            loop = getattr(_thread_local, "loop", None)
            if loop and not loop.is_closed():
                loop.run_until_complete(browser.close())
        except Exception:
            pass
        _thread_local.browser = None
    loop = getattr(_thread_local, "loop", None)
    if loop and not loop.is_closed():
        try:
            loop.close()
        except Exception:
            pass
        _thread_local.loop = None


def _get_thread_loop():
    """获取当前线程的事件循环（线程安全）"""
    loop = getattr(_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _thread_local.loop = loop
    return loop


def _run_async(coro):
    """运行异步函数（线程安全，每个线程独立事件循环）"""
    try:
        # 检查是否有正在运行的循环
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is not None:
            # 已有循环在跑，用新线程执行
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result(timeout=60)
        else:
            loop = _get_thread_loop()
            return loop.run_until_complete(coro)
    except Exception as e:
        logger.error(f"异步执行失败: {e}")
        return None


async def _get_browser():
    """获取当前线程的浏览器实例（线程安全，每线程独立实例）"""
    browser = getattr(_thread_local, "browser", None)
    if browser is None or not browser.is_connected():
        import cloakbrowser
        browser = await cloakbrowser.launch_async(headless=True)
        _thread_local.browser = browser
    return browser


async def _fetch_with_cloak(url: str, base_url: str = "https://quote.eastmoney.com/ztb/"):
    """用 CloakBrowser 发起请求（复用共享浏览器实例，带重试）"""
    browser = await _get_browser()
    page = await browser.new_page()
    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        # 重试最多2次（首次fetch可能因页面未完全就绪而失败）
        for attempt in range(3):
            result = await page.evaluate(f'''async () => {{
                try {{
                    const resp = await fetch("{url}");
                    const text = await resp.text();
                    try {{
                        return JSON.parse(text);
                    }} catch(e) {{
                        const start = text.indexOf("(");
                        const end = text.lastIndexOf(")");
                        if (start >= 0 && end > start) {{
                            return JSON.parse(text.substring(start + 1, end));
                        }}
                        return {{raw: text.substring(0, 200)}};
                    }}
                }} catch(e) {{
                    return {{error: e.message}};
                }}
            }}''')
            if result and not result.get("error"):
                return result
            if attempt < 2:
                await asyncio.sleep(1)
        return result
    finally:
        await page.close()


# ══════════════════════════════════════════════════════════════════
# 涨停板
# ══════════════════════════════════════════════════════════════════

def get_limit_up(date: str = None, limit: int = 200) -> List[Dict]:
    """
    获取涨停板数据
    
    Args:
        date: 日期 YYYYMMDD，默认今天
        limit: 返回条数
    
    Returns:
        list: [{code, name, change_pct, price, amount, consecutive, first_seal_time, industry}, ...]
    """
    if not date:
        date = datetime.now().strftime("%Y%m%d")

    # 优先使用a-stock-data直连HTTP接口，失败或空结果再降级到CloakBrowser。
    try:
        from data.a_stock_data import get_limit_up_pool
        pool = get_limit_up_pool(date)[:limit]
        if pool:
            stocks = []
            for item in pool:
                first_seal = str(item.get("first_seal", ""))
                stocks.append({
                    "code": item.get("code", ""),
                    "name": item.get("name", ""),
                    "change_pct": item.get("pct", 0),
                    "price": item.get("price", 0),
                    "amount": item.get("amount", 0) / 1e8,
                    "consecutive": item.get("limit_days", 1),
                    "first_seal_time": first_seal[:5],
                    "industry": item.get("industry", ""),
                    "market_cap": item.get("float_cap", 0) / 1e8,
                    "turnover": item.get("turnover", 0),
                    "seal_amount": item.get("seal_fund", 0) / 1e4,
                    "break_times": item.get("break_times", 0),
                    "zt_stat": item.get("zt_stat", ""),
                })
            logger.info(f"涨停板 {date}: {len(stocks)}只 (直连)")
            return stocks
    except Exception as e:
        logger.debug(f"涨停板直连接口失败，降级CloakBrowser: {e}")
    
    url = f"https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize={limit}&sort=fbt:asc&date={date}"
    
    result = _run_async(_fetch_with_cloak(url))
    
    if not result or result.get("error"):
        logger.warning(f"涨停板API失败: {result}")
        return []
    
    pool = result.get("data", {}).get("pool", [])
    
    stocks = []
    for item in pool:
        fbt = str(item.get("fbt", ""))
        if len(fbt) >= 5:
            fbt = f"{fbt[:2]}:{fbt[2:4]}"
        
        stocks.append({
            "code": item.get("c", ""),
            "name": item.get("n", ""),
            "change_pct": item.get("zdp", 0),
            "price": item.get("p", 0) / 1000,
            "amount": item.get("amount", 0) / 1e8,  # 亿
            "consecutive": item.get("lbc", 1),
            "first_seal_time": fbt,
            "industry": item.get("hybk", ""),
            "market_cap": item.get("ltsz", 0) / 1e8,  # 流通市值(亿)
            "turnover": item.get("hs", 0),
            "seal_amount": item.get("fund", 0) / 1e4,  # 封单金额(万)
        })
    
    logger.info(f"涨停板 {date}: {len(stocks)}只")
    return stocks


# ══════════════════════════════════════════════════════════════════
# 跌停板
# ══════════════════════════════════════════════════════════════════

def get_limit_down(date: str = None, limit: int = 200) -> List[Dict]:
    """
    获取跌停板数据

    Args:
        date: 日期 YYYYMMDD，默认今天
        limit: 返回条数

    Returns:
        list: [{code, name, change_pct, price, amount, industry}, ...]
    """
    if not date:
        date = datetime.now().strftime("%Y%m%d")

    # 优先使用a-stock-data直连HTTP接口，失败或空结果再降级到CloakBrowser。
    try:
        from data.a_stock_data import get_limit_down_pool
        pool = get_limit_down_pool(date)[:limit]
        if pool:
            stocks = []
            for item in pool:
                stocks.append({
                    "code": item.get("code", ""),
                    "name": item.get("name", ""),
                    "change_pct": item.get("pct", 0),
                    "price": item.get("price", 0),
                    "amount": item.get("board_amount", 0) / 1e8,
                    "industry": item.get("industry", ""),
                    "market_cap": 0,
                    "turnover": item.get("turnover", 0),
                    "seal_amount": item.get("seal_fund", 0) / 1e4,
                    "dt_days": item.get("dt_days", 0),
                    "open_times": item.get("open_times", 0),
                })
            logger.info(f"跌停板 {date}: {len(stocks)}只 (直连)")
            return stocks
    except Exception as e:
        logger.debug(f"跌停板直连接口失败，降级CloakBrowser: {e}")

    url = f"https://push2ex.eastmoney.com/getTopicDTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize={limit}&sort=fbt:asc&date={date}"

    result = _run_async(_fetch_with_cloak(url))

    if not result or result.get("error"):
        logger.warning(f"跌停板API失败: {result}")
        return []

    pool = result.get("data", {}).get("pool", [])

    stocks = []
    for item in pool:
        stocks.append({
            "code": item.get("c", ""),
            "name": item.get("n", ""),
            "change_pct": item.get("zdp", 0),
            "price": item.get("p", 0) / 1000,
            "amount": item.get("amount", 0) / 1e8,  # 亿
            "industry": item.get("hybk", ""),
            "market_cap": item.get("ltsz", 0) / 1e8,  # 流通市值(亿)
            "turnover": item.get("hs", 0),
        })

    logger.info(f"跌停板 {date}: {len(stocks)}只")
    return stocks


# ══════════════════════════════════════════════════════════════════
# 龙虎榜
# ══════════════════════════════════════════════════════════════════

def get_dragon_tiger(date: str = None, limit: int = 100) -> List[Dict]:
    """
    获取龙虎榜数据（自动回退到最近有数据的交易日）
    
    Args:
        date: 日期 YYYY-MM-DD，默认自动查找最近交易日
        limit: 返回条数
    
    Returns:
        list: [{code, name, date, change_pct, net_buy, reason}, ...]
    """
    # 构建要尝试的日期列表
    if date:
        dates_to_try = [date]
    else:
        dates_to_try = []
        today = datetime.now()
        for i in range(7):
            d = today - timedelta(days=i)
            if d.weekday() < 5:
                dates_to_try.append(d.strftime("%Y-%m-%d"))
    
    for try_date in dates_to_try:
        date_filter = f"(TRADE_DATE>='{try_date}')(TRADE_DATE<='{try_date}')"
        url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?sortColumns=TRADE_DATE,SECURITY_CODE&sortTypes=-1,1&pageSize={limit}&pageNumber=1&reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&source=WEB&client=WEB&filter={date_filter}"
        
        result = _run_async(_fetch_with_cloak(url))
        
        if not result or result.get("error"):
            continue
        
        result_data = result.get("result")
        if not result_data:
            continue
        
        items = result_data.get("data", [])
        if items:
            break
    else:
        logger.warning("龙虎榜: 所有日期均无数据")
        return []
    
    stocks = []
    for item in items:
        stocks.append({
            "code": item.get("SECURITY_CODE", ""),
            "name": item.get("SECURITY_NAME_ABBR", ""),
            "date": str(item.get("TRADE_DATE", ""))[:10],
            "change_pct": item.get("CHANGE_RATE", 0) or 0,
            "net_buy": (item.get("BILLBOARD_NET_AMT", 0) or 0) / 1e4,  # 万
            "reason": item.get("EXPLAIN", ""),
        })
    
    logger.info(f"龙虎榜 {date}: {len(stocks)}只")
    return stocks


# ══════════════════════════════════════════════════════════════════
# 融资融券
# ══════════════════════════════════════════════════════════════════

_MARGIN_CACHE = {}
_MARGIN_CACHE_TIME = 0

def get_margin_data(code: str) -> Dict:
    """
    获取融资融券数据
    
    Args:
        code: 股票代码
    
    Returns:
        dict: {date, margin_balance, margin_buy, short_volume, short_balance}
    """
    global _MARGIN_CACHE, _MARGIN_CACHE_TIME
    
    # 缓存5分钟
    import time
    if code in _MARGIN_CACHE and time.time() - _MARGIN_CACHE_TIME < 300:
        return _MARGIN_CACHE[code]
    
    url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_RZRQ_GGMX&columns=ALL&filter=(SCODE=%22{code}%22)&pageNumber=1&pageSize=5&sortTypes=-1&sortColumns=DATE"
    
    result = _run_async(_fetch_with_cloak(url))
    
    if not result or result.get("error"):
        return {}
    
    items = result.get("result", {}).get("data", [])
    if not items:
        return {}
    
    latest = items[0]
    data = {
        "date": latest.get("DATE", "")[:10],
        "margin_balance": latest.get("RZYE", 0),  # 融资余额
        "margin_buy": latest.get("RZMRE", 0),     # 融资买入额
        "short_volume": latest.get("RQYL", 0),    # 融券余量
        "short_balance": latest.get("RQYE", 0),   # 融券余额
    }
    
    _MARGIN_CACHE[code] = data
    _MARGIN_CACHE_TIME = time.time()
    
    return data


# ══════════════════════════════════════════════════════════════════
# 个股新闻
# ══════════════════════════════════════════════════════════════════

def get_stock_news(code: str, limit: int = 10) -> List[Dict]:
    """
    获取个股新闻
    
    Args:
        code: 股票代码
        limit: 返回条数
    
    Returns:
        list: [{title, content, time, source, url}, ...]
    """
    url = f"https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery&param=%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22{code}%22%2C%22type%22%3A%5B%22cmsArticleWebOld%22%5D%2C%22client%22%3A%22web%22%2C%22clientType%22%3A%22web%22%2C%22clientVersion%22%3A%22curr%22%2C%22param%22%3A%7B%22cmsArticleWebOld%22%3A%7B%22searchScope%22%3A%22default%22%2C%22sort%22%3A%22default%22%2C%22pageIndex%22%3A1%2C%22pageSize%22%3A{limit}%2C%22preTag%22%3A%22%22%2C%22postTag%22%3A%22%22%7D%7D%7D"
    
    result = _run_async(_fetch_with_cloak(url))
    
    if not result or result.get("error"):
        return []
    
    items = result.get("result", {}).get("cmsArticleWebOld", [])
    if not isinstance(items, list):
        return []
    
    news = []
    for item in items:
        news.append({
            "title": item.get("title", "").replace("<em>", "").replace("</em>", ""),
            "content": item.get("content", "")[:200],
            "time": item.get("date", ""),
            "source": item.get("mediaName", ""),
            "url": item.get("url", ""),
        })
    
    logger.info(f"新闻 {code}: {len(news)}条")
    return news


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    
    print("=" * 60)
    print("东方财富数据源测试 (CloakBrowser)")
    print("=" * 60)
    
    # 1. 涨停板
    print("\n[1] 涨停板:")
    zt = get_limit_up()
    print(f"  共 {len(zt)} 只")
    for s in zt[:5]:
        print(f"  {s['code']} {s['name']} {s['change_pct']:.2f}% {s['consecutive']}板 {s['industry']}")
    
    # 2. 龙虎榜
    print("\n[2] 龙虎榜:")
    lhb = get_dragon_tiger()
    print(f"  共 {len(lhb)} 只")
    for s in lhb[:5]:
        print(f"  {s['code']} {s['name']} 净买:{s['net_buy']:.0f}万 {s['reason'][:25]}")
    
    # 3. 融资融券
    print("\n[3] 融资融券:")
    margin = get_margin_data("600519")
    if margin:
        print(f"  日期: {margin['date']}")
        print(f"  融资余额: {margin['margin_balance']:,.0f}")
    
    # 4. 个股新闻
    print("\n[4] 个股新闻:")
    news = get_stock_news("600519", 5)
    for n in news[:3]:
        print(f"  [{n['source']}] {n['title'][:40]}")
