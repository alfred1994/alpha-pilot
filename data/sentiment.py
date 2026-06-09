"""
舆情数据模块
数据源: AKShare新闻 + 微博财经API
功能: 个股新闻、市场快讯、微博舆情
"""
import os
import json
import requests
import pandas as pd
from datetime import datetime
from data import rate_limit, logger

# 微博配置
WEIBO_COOKIES_FILE = os.path.expanduser("~/.hermes/weibo_cookies.json")
WEIBO_FINANCE_GID = "4730324816239267"


def _get_weibo_cookies() -> str:
    """读取微博cookies，支持JSON数组格式和字符串格式"""
    try:
        with open(WEIBO_COOKIES_FILE, "r") as f:
            raw = f.read().strip()
        
        # JSON数组格式（Playwright/CDP导出）
        if raw.startswith("["):
            cookies = json.loads(raw)
            parts = []
            for c in cookies:
                name = c.get("name", "")
                value = c.get("value", "")
                if name and value:
                    parts.append(f"{name}={value}")
            return "; ".join(parts)
        
        # 已经是cookie字符串
        return raw
    except Exception as e:
        logger.error(f"微博cookies读取失败: {e}")
        return ""


def get_weibo_finance(limit: int = 10) -> list:
    """
    获取微博财经分组最新帖子
    
    Returns:
        list of dict: [{author, text, created_at, reposts, comments, likes}, ...]
    """
    cookies = _get_weibo_cookies()
    if not cookies:
        return []
    
    url = f"https://weibo.com/ajax/feed/groupstimeline?list_id={WEIBO_FINANCE_GID}&refresh=4&count={limit}"
    headers = {
        "Cookie": cookies,
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://weibo.com/mygroups?gid={WEIBO_FINANCE_GID}"
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        # 检查是否返回了HTML登录页面（cookies过期）
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type or resp.text.strip().startswith("<"):
            logger.warning("微博cookies已过期，返回登录页面")
            return []
        data = resp.json()
        statuses = data.get("statuses", []) or data.get("data", {}).get("statuses", [])
        
        posts = []
        for s in statuses:
            posts.append({
                "author": s.get("user", {}).get("screen_name", "未知"),
                "text": s.get("text_raw", s.get("text", "").replace("<[^>]+>", "")),
                "created_at": s.get("created_at", ""),
                "mid": s.get("mid", ""),
                "reposts": s.get("reposts_count", 0),
                "comments": s.get("comments_count", 0),
                "likes": s.get("attitudes_count", 0),
            })
        
        logger.info(f"微博财经: 获取{len(posts)}条")
        return posts
    except Exception as e:
        logger.error(f"微博获取失败: {e}")
        return []


def get_stock_news(code: str) -> list:
    """
    获取个股新闻（东方财富搜索API）
    
    Returns:
        list of dict: [{title, content, time, source, url}, ...]
    """
    import requests
    import re
    import json

    try:
        url = (
            f"https://search-api-web.eastmoney.com/search/jsonp?"
            f"cb=jQuery&param=%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22{code}%22"
            f"%2C%22type%22%3A%5B%22cmsArticleWebOld%22%5D%2C%22client%22%3A%22web%22"
            f"%2C%22clientType%22%3A%22web%22%2C%22clientVersion%22%3A%22curr%22"
            f"%2C%22param%22%3A%7B%22cmsArticleWebOld%22%3A%7B%22searchScope%22%3A%22default%22"
            f"%2C%22sort%22%3A%22default%22%2C%22pageIndex%22%3A1%2C%22pageSize%22%3A10"
            f"%2C%22preTag%22%3A%22%22%2C%22postTag%22%3A%22%22%7D%7D%7D"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://so.eastmoney.com/",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        match = re.search(r"jQuery\w*\((.+)\)", resp.text)
        if match:
            data = json.loads(match.group(1))
            items = data.get("result", {}).get("cmsArticleWebOld", [])
            news = []
            for item in items:
                news.append({
                    "title": item.get("title", "").replace("<em>", "").replace("</em>", ""),
                    "content": item.get("content", "")[:200],
                    "time": item.get("date", ""),
                    "source": item.get("mediaName", ""),
                    "url": item.get("url", ""),
                })
            logger.info(f"新闻 {code}: {len(news)}条 (东方财富)")
            return news
    except Exception as e:
        logger.error(f"新闻获取失败: {e}")
        return []


def get_market_sentiment() -> dict:
    """
    综合市场情绪指标
    
    Returns:
        dict: {
            "limit_up_count": 涨停数,
            "limit_down_count": 跌停数,
            "margin_trend": 融资余额趋势,
            "top_concepts": 热门概念,
            "weibo_mood": 微博舆情摘要
        }
    """
    from datetime import datetime
    from data.eastmoney import get_limit_up
    
    result = {}
    
    # 涨停跌停（东方财富）
    try:
        zt = get_limit_up()
        result["limit_up_count"] = len(zt) if zt else 0
    except:
        result["limit_up_count"] = -1
    
    # 跌停数（从情绪模块获取）
    try:
        from signals.sentiment import market_mood_signal
        mood = market_mood_signal()
        # 从mood.detail中提取跌停数
        detail = mood.detail
        if "跌停" in detail:
            import re
            match = re.search(r"跌停(\d+)只", detail)
            if match:
                result["limit_down_count"] = int(match.group(1))
            else:
                result["limit_down_count"] = 0
        else:
            result["limit_down_count"] = 0
    except:
        result["limit_down_count"] = -1
    
    # 融资余额（东方财富datacenter）
    try:
        import requests
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "reportName=RPTA_WEB_RZRQ_GGMX&columns=DATE,RZYE"
            "&pageSize=1&pageNumber=1&sortTypes=-1&sortColumns=DATE"
            "&source=WEB&client=WEB"
        )
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/",
        }
        resp = requests.get(url, headers=headers, timeout=8)
        data = resp.json()
        if data.get("result") and data["result"].get("data"):
            total = sum(float(item.get("RZYE", 0)) for item in data["result"]["data"])
            result["margin_balance"] = total / 1e8  # 亿
        else:
            result["margin_balance"] = -1
    except:
        result["margin_balance"] = -1
    
    # 微博舆情
    posts = get_weibo_finance(5)
    result["weibo_summary"] = f"最新{len(posts)}条微博" if posts else "无数据"
    
    return result


# 测试
if __name__ == "__main__":
    print("【微博财经最新帖子】")
    posts = get_weibo_finance(3)
    for p in posts:
        print(f"  @{p['author']}: {p['text'][:80]}...")
    
    print("\n【茅台新闻】")
    news = get_stock_news("600519")
    for n in news[:3]:
        print(f"  [{n['source']}] {n['title']}")
    
    print("\n【市场情绪】")
    mood = get_market_sentiment()
    print(f"  涨停: {mood['limit_up_count']}只")
    print(f"  跌停: {mood['limit_down_count']}只")
    print(f"  融资余额: {mood['margin_balance']:.0f}亿")
    print(f"  热门概念: {mood['top_concepts'][:3]}")
