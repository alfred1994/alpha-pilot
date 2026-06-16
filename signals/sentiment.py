"""
舆情信号模块
数据源: 微博财经 + 个股新闻(AKShare)
分析方式: LLM(MiMo) 对舆情文本做情感分析
输出: SignalResult (0-100分数 + 方向)
"""
import json
import os
import requests
import pandas as pd
from datetime import datetime
from typing import List, Optional
from signals import SignalResult, Direction, score_to_direction, logger

# MiMo API 配置（与其他模块一致）
MIMO_API_KEY = os.environ.get("XIAOMI_API_KEY", "")
MIMO_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
LLM_MODEL = "mimo-v2.5-pro"


def _call_llm(prompt: str, max_tokens: int = 2000) -> Optional[str]:
    """调用 MiMo LLM"""
    if not MIMO_API_KEY:
        logger.warning("XIAOMI_API_KEY 未设置, 跳过LLM分析")
        return None

    headers = {
        "Authorization": f"Bearer {MIMO_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是A股量化分析师。分析舆情文本,判断市场情绪。"
                    "严格返回JSON格式: {\"score\": 0-100整数, \"direction\": \"买入/卖出/持有\", \"summary\": \"一句话理由\"}\n"
                    "score规则: 50=中性, >50偏多(利好), <50偏空(利空)\n"
                    "只返回JSON,不要其他文字。"
                )
            },
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }

    url = f"{MIMO_BASE_URL}/chat/completions"
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = (msg.get("content", "") or "").strip()
        reasoning = (msg.get("reasoning_content", "") or "").strip()
        if not content and reasoning:
            # MiMo推理模型：content为空时从reasoning中提取JSON
            import re
            json_match = re.search(r'\{[^{}]*"score"\s*:\s*\d+[^{}]*\}', reasoning)
            if json_match:
                content = json_match.group(0)
        # 提取JSON部分
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return content.strip()
    except Exception as e:
        logger.error(f"LLM调用失败: {e}")
        return None


def _parse_llm_response(raw: Optional[str]) -> tuple:
    """
    解析LLM返回的JSON

    Returns:
        (score, direction, summary) 或 (50, HOLD, "解析失败")
    """
    if not raw:
        return 50, Direction.HOLD, "LLM无响应"

    try:
        data = json.loads(raw)
        score = int(data.get("score", 50))
        score = max(0, min(100, score))
        dir_str = data.get("direction", "持有")
        direction = {"买入": Direction.BUY, "卖出": Direction.SELL, "持有": Direction.HOLD}.get(dir_str, Direction.HOLD)
        summary = data.get("summary", "")
        return score, direction, summary
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning(f"LLM返回解析失败: {raw[:100]}")
        return 50, Direction.HOLD, f"解析失败: {e}"


def sentiment_signal(code: str = None, weibo_posts: list = None,
                     news_list: list = None) -> SignalResult:
    """
    舆情情感分析信号

    Args:
        code: 股票代码, 用于自动获取新闻(可选)
        weibo_posts: 微博帖子列表 [{author, text, ...}, ...] (可选)
        news_list: 新闻列表 [{title, content, ...}, ...] (可选)

    Returns:
        SignalResult: LLM分析的舆情分数
    """
    # 自动获取数据
    if weibo_posts is None and news_list is None:
        from data.sentiment import get_weibo_finance, get_stock_news
        weibo_posts = get_weibo_finance(10)
        if code:
            news_list = get_stock_news(code)

    # 构建分析文本
    parts = []
    if weibo_posts:
        weibo_text = "\n".join(
            f"- @{p.get('author', '?')}: {p.get('text', '')[:150]}"
            for p in weibo_posts[:10]
        )
        parts.append(f"【微博财经热帖】\n{weibo_text}")

    if news_list:
        news_text = "\n".join(
            f"- [{n.get('source', '?')}] {n.get('title', '')}: {n.get('content', '')[:100]}"
            for n in news_list[:10]
        )
        parts.append(f"【个股新闻】\n{news_text}")

    if not parts:
        return SignalResult("舆情分析", 50, Direction.HOLD, 0.0, "无舆情数据")

    prompt = (
        f"分析以下A股{'个股' if code else '市场'}舆情,判断短期走势:\n\n"
        + "\n\n".join(parts)
        + "\n\n请给出0-100的分数(50中性,>50看多,<50看空)和方向(买入/卖出/持有)。"
    )

    raw = _call_llm(prompt)
    score, direction, summary = _parse_llm_response(raw)
    conf = 0.7 if raw else 0.0

    detail = summary if summary else f"LLM舆情分析 score={score}"
    return SignalResult("舆情分析", score, direction, conf, detail)


def batch_sentiment_signal(stocks: list, weibo_posts: list = None) -> dict:
    """
    批量舆情分析 - 一次LLM调用分析所有候选股票

    Args:
        stocks: [{"code": "600519", "name": "贵州茅台"}, ...]
        weibo_posts: 共享的微博数据(避免重复获取)

    Returns:
        dict: {"600519": SignalResult, "300750": SignalResult, ...}
    """
    from data.sentiment import get_weibo_finance, get_stock_news

    # 共享微博数据(只获取一次)
    if weibo_posts is None:
        weibo_posts = get_weibo_finance(10)

    # 构建微博文本
    weibo_text = ""
    if weibo_posts:
        weibo_text = "\n".join(
            f"- @{p.get('author', '?')}: {p.get('text', '')[:120]}"
            for p in weibo_posts[:8]
        )

    # 为每只股票获取新闻(使用并行获取减少等待)
    stock_news_parts = []
    import concurrent.futures
    from data import rate_limit as _rate_limit

    def _fetch_news(s):
        code = s.get("code", "")
        name = s.get("name", code)
        try:
            _rate_limit("akshare", 0.5)
            news = get_stock_news(code)
            if news:
                news_text = "; ".join(f"{n.get('title', '')[:40]}" for n in news[:3])
                return f"[{code} {name}] {news_text}"
        except Exception:
            pass
        return f"[{code} {name}] 无新闻"

    # 并行获取新闻(最多3个线程)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_fetch_news, s): s for s in stocks}
        for future in concurrent.futures.as_completed(futures):
            try:
                stock_news_parts.append(future.result(timeout=5))
            except Exception:
                s = futures[future]
                stock_news_parts.append(f"[{s.get('code','')} {s.get('name','')}] 超时")

    # 一次LLM调用批量分析所有股票
    stock_list_str = ", ".join(f"{s.get('code','')}" for s in stocks)
    prompt = (
        f"批量分析以下{len(stocks)}只A股候选股票的舆情,判断短期走势。\n\n"
        f"【候选股票】{stock_list_str}\n\n"
    )
    if weibo_text:
        prompt += f"【微博财经热帖】\n{weibo_text}\n\n"
    if stock_news_parts:
        prompt += f"【个股新闻】\n" + "\n".join(stock_news_parts) + "\n\n"

    prompt += (
        "请为每只股票返回JSON数组,每个元素: "
        '{"code":"股票代码","score":0-100整数,"direction":"买入/卖出/持有","summary":"一句话理由"}\n'
        "score规则: 50=中性,>50看多,<50看空\n"
        "只返回JSON数组,不要其他文字。示例: [{\"code\":\"600519\",\"score\":65,\"direction\":\"买入\",\"summary\":\"利好\"}]"
    )

    raw = _call_llm(prompt, max_tokens=4000)
    results = {}

    if raw:
        try:
            # 清理LLM返回
            content = raw.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            # 容错解析: 支持截断的JSON
            arr = None
            try:
                arr = json.loads(content)
            except json.JSONDecodeError:
                # 尝试从截断的JSON中提取完整的对象
                logger.info("JSON不完整，尝试容错解析...")
                arr = _parse_partial_json(content)

            if isinstance(arr, list):
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("code", "")).strip()
                    score = int(item.get("score", 50))
                    score = max(0, min(100, score))
                    dir_str = item.get("direction", "持有")
                    direction = {"买入": Direction.BUY, "卖出": Direction.SELL, "持有": Direction.HOLD}.get(dir_str, Direction.HOLD)
                    summary = item.get("summary", "")
                    results[code] = SignalResult("舆情分析", score, direction, 0.7, summary)
            elif isinstance(arr, dict):
                code = str(arr.get("code", "")).strip()
                score = int(arr.get("score", 50))
                dir_str = arr.get("direction", "持有")
                direction = {"买入": Direction.BUY, "卖出": Direction.SELL, "持有": Direction.HOLD}.get(dir_str, Direction.HOLD)
                summary = arr.get("summary", "")
                results[code] = SignalResult("舆情分析", score, direction, 0.7, summary)

            logger.info(f"舆情解析成功: {len(results)}/{len(stocks)}只")
        except (ValueError, TypeError) as e:
            logger.warning(f"批量舆情LLM解析失败: {e}, raw={raw[:200]}")

    # 未返回的股票默认50分
    for s in stocks:
        code = s.get("code", "")
        if code not in results:
            results[code] = SignalResult("舆情分析", 50, Direction.HOLD, 0.0, "LLM未返回")

    return results


def _parse_partial_json(raw: str) -> list:
    """从截断的JSON字符串中提取完整的对象"""
    results = []
    depth = 0
    obj_start = None
    for i, ch in enumerate(raw):
        if ch == '{':
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    obj = json.loads(raw[obj_start:i+1])
                    if isinstance(obj, dict):
                        results.append(obj)
                except json.JSONDecodeError:
                    pass
                obj_start = None
    return results


def market_mood_signal() -> SignalResult:
    """
    市场整体情绪信号 (基于涨停跌停比)

    Returns:
        SignalResult: 涨停多→偏多, 跌停多→偏空
    """
    from data.market import get_limit_up, get_limit_down

    df_up = get_limit_up()
    df_down = get_limit_down()

    up_count = len(df_up) if df_up is not None else 0
    down_count = len(df_down) if df_down is not None else 0

    if up_count == 0 and down_count == 0:
        return SignalResult("市场情绪", 50, Direction.HOLD, 0.0, "无涨跌停数据")

    total = up_count + down_count
    ratio = up_count / total if total > 0 else 0.5

    # ratio > 0.6 偏多, < 0.4 偏空
    score = int(ratio * 100)
    direction = score_to_direction(score)

    if up_count > 0 and down_count > 0:
        detail = f"涨停{up_count}只 vs 跌停{down_count}只, 比值{ratio:.2f}"
    elif up_count > 0:
        detail = f"涨停{up_count}只, 无跌停"
        score = 80
        direction = Direction.BUY
    else:
        detail = f"跌停{down_count}只, 无涨停"
        score = 20
        direction = Direction.SELL

    conf = 0.6 if total >= 10 else 0.3
    return SignalResult("市场情绪", score, direction, conf, detail)


# 测试
if __name__ == "__main__":
    print("舆情信号测试")
    print("=" * 60)

    print("\n【1】市场情绪(涨跌停比)")
    result = market_mood_signal()
    print(result)

    print("\n【2】LLM舆情分析(茅台)")
    result = sentiment_signal(code="600519")
    print(result)
