"""
数据快照层 - MarketDataSnapshot
====================================================================
核心原则：所有市场级数据只拉一次，存快照，各模块共享读取。

盘前预热: 拉全量数据（K线/基本面/资金/新闻/涨停板/候选池）
盘中更新: 只更新实时价格和少量变化数据
各信号模块: 只读快照，不允许自己调外部API

快照文件:
  market_snapshot_YYYYMMDD.json  涨停板/跌停板/北向资金/两融
  stock_snapshot_YYYYMMDD.json   每只股票的K线/基本面/新闻
  sentiment_snapshot_YYYYMMDD.json 舆情分析结果
  candidate_pool_YYYYMMDD.json   今日候选池
====================================================================
"""
import json
import os
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any

logger = logging.getLogger("data.snapshot")

SNAPSHOT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "snapshots",
)


def _snapshot_path(name: str, date: str = None) -> str:
    """快照文件路径"""
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    return os.path.join(SNAPSHOT_DIR, f"{name}_{date}.json")


def _save_snapshot(name: str, data: Any, date: str = None):
    """保存快照"""
    path = _snapshot_path(name, date)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"快照已保存: {name} → {path}")


def _load_snapshot(name: str, date: str = None, max_age: int = 7200) -> Optional[dict]:
    """
    加载快照

    Args:
        name: 快照名
        date: 日期(默认今天)
        max_age: 最大有效期(秒)，超期返回None

    Returns:
        快照数据或None
    """
    path = _snapshot_path(name, date)
    if not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
        if time.time() - mtime > max_age:
            logger.info(f"快照已过期({max_age}s): {name}")
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"加载快照失败 {name}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# 盘前数据预热
# ═══════════════════════════════════════════════════════════════════

def prefetch_market_data() -> dict:
    """
    盘前预热：拉取全市场级数据并缓存

    包含:
    - 涨停板/跌停板
    - 北向资金
    - 两融余额
    - 市场情绪指标

    Returns:
        market_snapshot dict
    """
    from data.eastmoney import get_limit_up, get_limit_down
    from data.market import get_north_flow, get_margin_balance

    t0 = time.time()
    snapshot = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "timestamp": time.time(),
        "limit_up": [],
        "limit_down": [],
        "north_flow": {},
        "margin": {},
    }

    # 涨停板 (timeout 15s)
    try:
        data = get_limit_up()
        if data:
            snapshot["limit_up"] = data
            logger.info(f"涨停板: {len(snapshot['limit_up'])}只")
    except Exception as e:
        logger.warning(f"涨停板获取失败(降级): {e}")

    # 跌停板
    try:
        data = get_limit_down()
        if data:
            snapshot["limit_down"] = data
            logger.info(f"跌停板: {len(snapshot['limit_down'])}只")
    except Exception as e:
        logger.warning(f"跌停板获取失败(降级): {e}")

    # 北向资金
    try:
        north = get_north_flow()
        if north:
            snapshot["north_flow"] = north
            logger.info(f"北向资金: {north.get('net_flow', 'N/A')}")
    except Exception as e:
        logger.warning(f"北向资金获取失败(降级): {e}")

    _save_snapshot("market", snapshot)
    logger.info(f"市场数据预热完成: {time.time()-t0:.1f}s")
    return snapshot


def prefetch_stock_data(codes: List[str]) -> dict:
    """
    为候选股票预取数据

    Args:
        codes: 股票代码列表

    Returns:
        stock_snapshot dict, key=code
    """
    from data.history import get_daily
    from data.eastmoney import get_stock_news

    t0 = time.time()
    snapshot = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "timestamp": time.time(),
        "stocks": {},
    }

    for code in codes:
        stock_data = {"code": code}

        # K线数据
        try:
            df = get_daily(code, start_date="20240101")
            if df is not None and len(df) > 0:
                # 只存最近60天的指标数据，不存全量K线（太大）
                stock_data["kline_count"] = len(df)
                stock_data["latest_close"] = float(df["close"].iloc[-1]) if "close" in df.columns else None
                stock_data["has_data"] = True
            else:
                stock_data["has_data"] = False
        except Exception as e:
            stock_data["has_data"] = False
            logger.debug(f"K线获取失败 {code}: {e}")

        # 新闻
        try:
            news = get_stock_news(code, limit=10)
            stock_data["news_count"] = len(news) if news else 0
        except Exception:
            stock_data["news_count"] = 0

        snapshot["stocks"][code] = stock_data

    _save_snapshot("stock", snapshot)
    logger.info(f"股票数据预热完成: {len(codes)}只, {time.time()-t0:.1f}s")
    return snapshot


def prefetch_sentiment(codes: List[str], stock_news: Dict = None) -> dict:
    """
    批量预取舆情分析

    Args:
        codes: 股票代码列表
        stock_news: 预取的新闻数据(可选)

    Returns:
        sentiment_snapshot dict
    """
    from data.sentiment import get_weibo_finance
    from data.eastmoney import get_stock_news
    from signals.sentiment import batch_sentiment_signal

    t0 = time.time()
    snapshot = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "timestamp": time.time(),
        "scores": {},
    }

    # 获取微博数据
    weibo_posts = []
    try:
        weibo_posts = get_weibo_finance(10)
    except Exception:
        pass

    # 获取新闻
    stock_list = []
    news_map = {}
    for code in codes:
        name = ""  # 名称会在后续获取
        stock_list.append({"code": code, "name": name})
        if stock_news and code in stock_news:
            news_map[code] = stock_news[code]
        else:
            try:
                news = get_stock_news(code, limit=10)
                news_map[code] = news or []
            except Exception:
                news_map[code] = []

    # 批量舆情分析
    try:
        scores = batch_sentiment_signal(stock_list, weibo_posts)
        for code, result in scores.items():
            snapshot["scores"][code] = {
                "score": result.score,
                "direction": result.direction.value if hasattr(result.direction, 'value') else str(result.direction),
                "confidence": result.confidence,
                "detail": result.detail,
            }
        logger.info(f"舆情分析完成: {len(scores)}/{len(codes)}只")
    except Exception as e:
        logger.warning(f"批量舆情分析失败(降级): {e}")

    _save_snapshot("sentiment", snapshot)
    logger.info(f"舆情预热完成: {time.time()-t0:.1f}s")
    return snapshot


# ═══════════════════════════════════════════════════════════════════
# 读取快照（供各模块使用）
# ═══════════════════════════════════════════════════════════════════

def get_market_snapshot(max_age: int = 1800) -> Optional[dict]:
    """获取市场快照(30分钟有效期)"""
    return _load_snapshot("market", max_age=max_age)


def get_stock_snapshot(max_age: int = 7200) -> Optional[dict]:
    """获取股票快照(2小时有效期)"""
    return _load_snapshot("stock", max_age=max_age)


def get_sentiment_snapshot(max_age: int = 3600) -> Optional[dict]:
    """获取舆情快照(1小时有效期)"""
    return _load_snapshot("sentiment", max_age=max_age)


def get_candidate_pool(max_age: int = 7200) -> Optional[dict]:
    """获取候选池(2小时有效期)"""
    return _load_snapshot("candidate_pool", max_age=max_age)


def save_candidate_pool(candidates: list):
    """保存候选池"""
    data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "timestamp": time.time(),
        "candidates": [
            {
                "code": c.code,
                "name": c.name,
                "source": c.source,
                "score": c.score,
            }
            for c in candidates
        ],
    }
    _save_snapshot("candidate_pool", data)


# ═══════════════════════════════════════════════════════════════════
# 超时降级工具
# ═══════════════════════════════════════════════════════════════════

def with_timeout(func, timeout: int = 15, fallback=None, desc: str = ""):
    """
    带超时的函数调用，超时返回fallback

    Args:
        func: 要执行的函数(无参数)
        timeout: 超时秒数
        fallback: 超时返回值
        desc: 描述(日志用)

    Returns:
        函数返回值或fallback
    """
    import concurrent.futures

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(func)
            return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        logger.warning(f"超时降级({timeout}s): {desc}")
        return fallback
    except Exception as e:
        logger.warning(f"异常降级: {desc}: {e}")
        return fallback


# ═══════════════════════════════════════════════════════════════════
# 盘前预热入口
# ═══════════════════════════════════════════════════════════════════

def run_prefetch(candidate_codes: List[str] = None):
    """
    完整盘前预热流程

    Args:
        candidate_codes: 候选股票代码列表(如已有)

    Returns:
        预热结果摘要
    """
    t0 = time.time()
    results = {"started": datetime.now().isoformat()}

    # 1. 市场级数据
    market = prefetch_market_data()
    results["market"] = {
        "limit_up": len(market.get("limit_up", [])),
        "limit_down": len(market.get("limit_down", [])),
    }

    # 2. 股票级数据(如有候选)
    if candidate_codes:
        stock = prefetch_stock_data(candidate_codes)
        results["stocks"] = len(stock.get("stocks", {}))

        # 3. 舆情分析
        sentiment = prefetch_sentiment(candidate_codes)
        results["sentiment"] = len(sentiment.get("scores", {}))

    elapsed = time.time() - t0
    results["elapsed"] = f"{elapsed:.1f}s"
    results["finished"] = datetime.now().isoformat()
    logger.info(f"盘前预热完成: {elapsed:.1f}s")
    return results
