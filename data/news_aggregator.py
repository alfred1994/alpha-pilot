"""
新闻聚合模块 - 多源财经新闻获取
功能: 同花顺新闻、新浪财经新闻、涨停板块数据
用于收盘后收集财经资讯，辅助隔夜分析
"""
import time
import requests
from datetime import datetime
from data import logger

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


def _request_json_with_retry(url, max_retries=3, timeout=10, headers=None):
    """
    带重试的JSON请求
    
    Args:
        url: 请求地址
        max_retries: 最大重试次数
        timeout: 超时秒数
        headers: 自定义请求头
    
    Returns:
        dict 或 None
    """
    hdrs = {**HEADERS, **(headers or {})}
    
    for i in range(max_retries):
        try:
            resp = requests.get(url, headers=hdrs, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"JSON请求失败(第{i+1}次): {url} - {e}")
            if i < max_retries - 1:
                time.sleep(1)
    return None


def get_ths_news(page=1):
    """
    获取同花顺财经新闻
    
    Args:
        page: 页码，从1开始
    
    Returns:
        list[dict]: [{"source": "同花顺", "title": "...", "url": "...", "time": "...", "tags": [...]}]
    """
    url = f"https://news.10jqka.com.cn/tapp/news/push/stock/?page={page}&tag=&track=website&pagesize=10"
    
    data = _request_json_with_retry(url)
    if not data:
        logger.error("同花顺新闻获取失败")
        return []
    
    result = []
    try:
        news_list = data.get("data", {}).get("list", [])
        for item in news_list:
            tags = [t.get("name", "") for t in item.get("tags", []) if t.get("name")]
            # 处理时间戳
            ctime = item.get("ctime", "")
            if ctime and ctime.isdigit():
                ctime = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M")
            
            result.append({
                "source": "同花顺",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "time": ctime,
                "tags": tags,
            })
        logger.info(f"同花顺新闻: {len(result)}条 (第{page}页)")
    except Exception as e:
        logger.error(f"同花顺新闻解析失败: {e}")
    
    return result


def get_sina_news():
    """
    获取新浪财经新闻
    
    Returns:
        list[dict]: [{"source": "新浪财经", "title": "...", "url": "...", "time": "...", "tags": []}]
    """
    url = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=10&page=1"
    
    data = _request_json_with_retry(url)
    if not data:
        logger.error("新浪财经新闻获取失败")
        return []
    
    result = []
    try:
        news_list = data.get("result", {}).get("data", [])
        for item in news_list:
            # 处理时间戳
            intime = item.get("intime", "")
            if intime and str(intime).isdigit():
                intime = datetime.fromtimestamp(int(intime)).strftime("%Y-%m-%d %H:%M")
            
            result.append({
                "source": "新浪财经",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "time": str(intime),
                "tags": [],
            })
        logger.info(f"新浪财经新闻: {len(result)}条")
    except Exception as e:
        logger.error(f"新浪财经新闻解析失败: {e}")
    
    return result


def get_block_top():
    """
    获取同花顺涨停板块数据
    
    Returns:
        list[dict]: [{"source": "同花顺涨停", "name": "机器人概念", "change": 5.2, "limit_up_num": 21}]
    """
    url = "https://data.10jqka.com.cn/dataapi/limit_up/block_top/"
    
    data = _request_json_with_retry(url, headers={"Referer": "https://data.10jqka.com.cn"})
    if not data:
        logger.error("涨停板块数据获取失败")
        return []
    
    result = []
    try:
        blocks = data.get("data", [])
        for item in blocks:
            result.append({
                "source": "同花顺涨停",
                "code": item.get("code", ""),
                "name": item.get("name", ""),
                "change": item.get("change", 0),
                "limit_up_num": item.get("limit_up_num", 0),
            })
        logger.info(f"涨停板块: {len(result)}个")
    except Exception as e:
        logger.error(f"涨停板块解析失败: {e}")
    
    return result


def get_all_news():
    """
    聚合所有新闻源，返回统一格式的新闻列表
    
    Returns:
        dict: {
            "headlines": [{"source": ..., "title": ..., "url": ..., "time": ..., "tags": [...]}],
            "sector_heat": [{"name": ..., "change": ..., "limit_up_num": ...}],
        }
    """
    # 获取同花顺新闻
    ths_news = get_ths_news(page=1)
    
    # 获取新浪财经新闻
    sina_news = get_sina_news()
    
    # 获取涨停板块
    block_top = get_block_top()
    
    # 合并新闻，按时间排序
    all_headlines = ths_news + sina_news
    # 过滤空标题
    all_headlines = [n for n in all_headlines if n.get("title")]
    
    # 整理板块数据
    sector_heat = []
    for block in block_top:
        sector_heat.append({
            "name": block.get("name", ""),
            "change": block.get("change", 0),
            "limit_up_num": block.get("limit_up_num", 0),
        })
    
    logger.info(f"新闻聚合完成: 头条{len(all_headlines)}条, 板块{len(sector_heat)}个")
    
    return {
        "headlines": all_headlines,
        "sector_heat": sector_heat,
    }


# 测试入口
if __name__ == "__main__":
    import json
    print("=" * 60)
    print("新闻聚合模块测试")
    print("=" * 60)
    
    # 1. 同花顺新闻
    print("\n[1] 同花顺新闻:")
    ths = get_ths_news()
    for n in ths[:3]:
        print(f"  - [{n['source']}] {n['title']}")
    
    # 2. 新浪财经新闻
    print("\n[2] 新浪财经新闻:")
    sina = get_sina_news()
    for n in sina[:3]:
        print(f"  - [{n['source']}] {n['title']}")
    
    # 3. 涨停板块
    print("\n[3] 涨停板块:")
    blocks = get_block_top()
    for b in blocks[:5]:
        print(f"  - {b['name']}: {b['limit_up_num']}只涨停, 涨幅{b.get('change', 0)}%")
    
    # 4. 聚合
    print("\n[4] 聚合结果:")
    all_news = get_all_news()
    print(json.dumps(all_news, ensure_ascii=False, indent=2)[:1000])
