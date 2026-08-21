"""
东方财富数据源 - CloakBrowser
涨停板、龙虎榜、融资融券、个股新闻
"""
import asyncio
import atexit
import concurrent.futures
import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List

logger = logging.getLogger("data.eastmoney")

# CloakBrowser 的连接和页面对象绑定创建它们的事件循环。过去的“每线程一个
# 浏览器”在候选并发评分时会无上限扩张；这里把所有请求收敛到一个专属事件循环
# 和一个浏览器实例。上限实际为 1（小于运维上限 2），以换取 API 线程安全。
EASTMONEY_ASYNC_TIMEOUT = 45
EASTMONEY_BROWSER_IDLE_SECONDS = 20 * 60


class _CloakBrowserManager:
    """在单一专属事件循环中串行复用 CloakBrowser。

    CloakBrowser 没有跨事件循环共享连接的安全契约，因此不实现表面上的两实例
    池。单实例已经消除重复启动，且在低配主机上比跨线程池更可靠。
    """

    def __init__(self, *, launcher=None, idle_seconds: int = EASTMONEY_BROWSER_IDLE_SECONDS,
                 navigation_wait_seconds: float = 2, retry_wait_seconds: float = 1):
        self._launcher = launcher
        self._idle_seconds = max(0.01, float(idle_seconds))
        self._navigation_wait_seconds = max(0.0, float(navigation_wait_seconds))
        self._retry_wait_seconds = max(0.0, float(retry_wait_seconds))
        self._thread = None
        self._loop = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._browser = None
        self._request_lock = None
        self._idle_task = None
        self._last_activity = 0.0

    def _ensure_worker(self):
        with self._start_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()

            def _worker():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._request_lock = asyncio.Lock()
                self._ready.set()
                loop.run_forever()

            self._thread = threading.Thread(
                target=_worker,
                name="eastmoney-cloakbrowser",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("CloakBrowser事件循环启动超时")

    async def _close_browser(self):
        browser, self._browser = self._browser, None
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), timeout=10)
            except Exception as exc:
                logger.warning("关闭东方财富浏览器失败: %s", exc)

    async def _get_browser(self):
        browser = self._browser
        connected = False
        if browser is not None:
            try:
                connected = bool(browser.is_connected())
            except Exception:
                connected = False
        if not connected:
            await self._close_browser()
            launcher = self._launcher
            if launcher is None:
                import cloakbrowser
                launcher = cloakbrowser.launch_async
            self._browser = await launcher(headless=True)
        return self._browser

    def _touch(self):
        self._last_activity = time.monotonic()
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._close_when_idle())

    async def _close_when_idle(self):
        while self._browser is not None:
            remaining = self._idle_seconds - (time.monotonic() - self._last_activity)
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            await self._close_browser()
            return

    async def _fetch(self, url: str, base_url: str):
        # 页面请求串行化，保证一个 Browser/一个事件循环的使用契约，并避免同一
        # 数据源在并发扫描中产生放大式访问。
        async with self._request_lock:
            browser = await self._get_browser()
            page = None
            try:
                page = await browser.new_page()
                await page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(self._navigation_wait_seconds)
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
                        await asyncio.sleep(self._retry_wait_seconds)
                return result
            finally:
                try:
                    if page is not None:
                        await page.close()
                finally:
                    self._touch()

    def fetch(self, url: str, base_url: str):
        self._ensure_worker()
        future = asyncio.run_coroutine_threadsafe(self._fetch(url, base_url), self._loop)
        try:
            return future.result(timeout=EASTMONEY_ASYNC_TIMEOUT)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError("东方财富浏览器请求超时") from exc

    def close(self):
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(self._close_browser(), loop)
        try:
            future.result(timeout=10)
        except Exception:
            pass

    def snapshot(self) -> Dict:
        """只返回进程内状态，供测试和诊断使用；不触发浏览器启动。"""
        return {
            "max_instances": 1,
            "browser_active": self._browser is not None,
            "worker_alive": bool(self._thread and self._thread.is_alive()),
        }


_browser_manager = _CloakBrowserManager()


def cleanup_eastmoney(force: bool = False):
    """释放东方财富调用方的引用。

    并发评分器会在每个标的结束时调用本函数；此时立即关闭全局浏览器会打断
    其他标的并失去复用价值。因此常规清理仅交由20分钟空闲回收，进程退出或
    明确 force 时才同步关闭并等待 Browser.close() 完成。
    """
    if force:
        _browser_manager.close()


atexit.register(cleanup_eastmoney, True)


def _fetch_with_cloak(url: str, base_url: str = "https://quote.eastmoney.com/ztb/"):
    """通过唯一共享浏览器请求东方财富，失败时释放连接供下一轮重建。"""
    try:
        return _browser_manager.fetch(url, base_url)
    except TimeoutError:
        logger.warning("东方财富浏览器请求超时(%ss)，将释放共享连接", EASTMONEY_ASYNC_TIMEOUT)
        cleanup_eastmoney(force=True)
        return None
    except Exception as e:
        logger.error(f"异步执行失败: {e}")
        cleanup_eastmoney(force=True)
        return None


def get_cloakbrowser_process_snapshot(process_runner=None) -> Dict:
    """只读统计 Linux 上受管的无头浏览器进程，供 Watchdog 使用。

    只匹配 CloakBrowser，或带 headless/remote-debugging 参数的 Chromium，避免
    将用户交互式 Chrome 误记为交易任务。无法取得进程表时显式返回 unavailable。
    """
    runner = process_runner or subprocess.run
    try:
        proc = runner(
            ["ps", "-eo", "pid=,etimes=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return {"available": False, "error": str(exc)[:160], "count": 0, "oldest_age_seconds": 0}
    if getattr(proc, "returncode", 1) != 0:
        return {"available": False, "error": str(getattr(proc, "stderr", ""))[:160], "count": 0, "oldest_age_seconds": 0}

    processes = []
    instances = []
    for line in (getattr(proc, "stdout", "") or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid, elapsed, command = parts
        lowered = command.lower()
        is_cloak = "cloakbrowser" in lowered
        is_headless_chromium = (
            ("chromium" in lowered or "chrome" in lowered)
            and ("--headless" in lowered or "--remote-debugging-port" in lowered)
        )
        if not (is_cloak or is_headless_chromium):
            continue
        try:
            item = {"pid": int(pid), "age_seconds": max(0, int(elapsed))}
        except ValueError:
            continue
        processes.append(item)
        # Chromium 的 renderer/gpu/utility 子进程都带 --type=。实例上限应
        # 统计没有 --type= 的 browser 主进程，否则一个健康实例也会因多个
        # renderer 被误报成“超过2个实例”。
        if "--type=" not in lowered:
            instances.append(item)
    return {
        "available": True,
        "count": len(instances),
        "instance_count": len(instances),
        "process_count": len(processes),
        "oldest_age_seconds": max((item["age_seconds"] for item in processes), default=0),
        "pids": [item["pid"] for item in instances],
    }


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
    
    result = _fetch_with_cloak(url)
    
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

    result = _fetch_with_cloak(url)

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
        
        result = _fetch_with_cloak(url)
        
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
    
    result = _fetch_with_cloak(url)
    
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
    
    result = _fetch_with_cloak(url)
    
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
