"""
Laya 文本情绪客户端（分布内任务：文本 → 情绪分类）
====================================================================
调用本机 laya 常驻服务对财经文本（市场头条/个股新闻/微博热帖）做快速
情绪分类，作为 sentiment 维度的交叉参考证据：

  - laya 0.3.5 的分布内任务是文本分类（官方 presets：工单分诊/邮件分类），
    中文金融文本判别服务器实测 6/8（强利好 pos 0.996 / 强利空 neg 0.997）；
  - 只做文本情绪，不碰 6 维数值特征（分布外任务，见 docs/laya-shadow.md
    「格式实验记录」）；
  - 任何失败（服务未起/超时/输出异常）都静默降级。

使用方法:
    from strategy.laya_text import run_text_sentiment, load_text_sentiment_summary
    summary = run_text_sentiment("2026-09-23")      # 盘后跑，落盘+事件
    line = format_text_sentiment_line(load_text_sentiment_summary())
====================================================================
"""
import json
import os
import sys
from typing import Any, Dict, List, Optional

import httpx

from config import LAYA_BASE_URL, LAYA_DATA_DIR, LAYA_TIMEOUT_SECONDS
from strategy.laya_client import is_available

# 情绪问题按服务器实测措辞固定（中文、分布内风格，引用文本字段而非数值特征）
SENTIMENT_QUESTION = {
    "sentiment": {
        "type": "choice",
        "instructions": (
            "判断这段A股财经文本的整体情绪倾向（对股价/市场的影响方向）。"
            "仅依据文本内容判断。"
        ),
        "criteria": {
            "positive": "明确利好：正面消息，通常推动股价上涨（如政策利好、业绩大增、重大订单、降息降准）",
            "neutral": "中性：无明显方向，或消息混杂无法判断，或例行公告",
            "negative": "明确利空：负面消息，通常压制股价（如处罚、业绩下滑、减持、事故）",
        },
    },
}

SENTIMENTS = ("positive", "neutral", "negative")


def score_texts(texts: List[str],
                batch_size: int = 10) -> Optional[List[Dict[str, Any]]]:
    """
    分批调用 /predict 对文本做情绪分类；失败返回 None（调用方静默降级）。

    返回 [{"choice": "positive", "probabilities": {...}}, ...]，顺序与输入一致。
    """
    if not texts:
        return []
    results: List[Dict[str, Any]] = []
    for start in range(0, len(texts), max(1, batch_size)):
        chunk = texts[start:start + batch_size]
        try:
            resp = httpx.post(
                f"{LAYA_BASE_URL}/predict",
                json={"states": [{"text": t} for t in chunk],
                      "questions": SENTIMENT_QUESTION},
                timeout=LAYA_TIMEOUT_SECONDS,
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return None
        if not payload.get("ok"):
            return None
        chunk_results = payload.get("results")
        if not isinstance(chunk_results, list) or len(chunk_results) != len(chunk):
            return None
        for result in chunk_results:
            answer = ((result or {}).get("answers") or {}).get("sentiment")
            if not isinstance(answer, dict) or answer.get("choice") not in SENTIMENTS:
                return None
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, dict):
                return None
            results.append({
                "choice": str(answer["choice"]),
                "probabilities": {
                    str(k): round(float(v), 4) for k, v in probabilities.items()
                },
            })
    return results


def _collect_headlines(limit: int = 20) -> List[str]:
    """市场头条文本（同花顺+新浪，既有聚合链路）。"""
    try:
        from data.news_aggregator import get_all_news
        headlines = (get_all_news() or {}).get("headlines") or []
        return [
            f"[{n.get('source', '?')}] {n.get('title', '')}".strip()
            for n in headlines[:limit] if n.get("title")
        ]
    except Exception:
        return []


def _collect_weibo(limit: int = 10) -> List[str]:
    """微博财经热帖文本（无 cookies 时返回空，不影响其他文本源）。"""
    try:
        from data.sentiment import get_weibo_finance
        posts = get_weibo_finance(limit) or []
        return [
            f"微博 @{p.get('author', '?')}: {str(p.get('text', ''))[:120]}"
            for p in posts if p.get("text")
        ]
    except Exception:
        return []


def _collect_stock_news(codes: List[str], per_stock_limit: int = 5):
    """
    个股新闻文本（东财搜索，既有链路；并行+限速与 signals/sentiment 一致）。

    返回 (texts, code_of_text, news_counts)：texts 与 code_of_text 一一对应，
    news_counts 记录每只股票取到的新闻条数（0 = 无新闻）。
    """
    import concurrent.futures

    from data import rate_limit
    from data.sentiment import get_stock_news

    texts: List[str] = []
    code_of_text: List[str] = []
    news_counts: Dict[str, int] = {}

    def _fetch(code: str):
        try:
            rate_limit("akshare", 0.5)
            news = get_stock_news(code) or []
        except Exception:
            news = []
        items = []
        for n in news[:per_stock_limit]:
            title = str(n.get("title") or "").strip()
            if not title:
                continue
            content = str(n.get("content") or "").strip()[:100]
            items.append(f"[{n.get('source', '?')}] {title}"
                         + (f": {content}" if content else ""))
        return code, items

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_fetch, c): c for c in codes}
        for future in concurrent.futures.as_completed(futures):
            try:
                code, items = future.result(timeout=15)
            except Exception:
                code, items = futures[future], []
            news_counts[str(code)] = len(items)
            for text in items:
                texts.append(text)
                code_of_text.append(str(code))
    return texts, code_of_text, news_counts


def _tally(results: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {s: 0 for s in SENTIMENTS}
    for r in results:
        counts[r["choice"]] += 1
    return counts


def run_text_sentiment(date: str = None,
                       db_path: str = None,
                       max_codes: int = 50) -> Optional[Dict[str, Any]]:
    """
    对指定日期 MiMo 判断过的候选 + 市场头条/微博热帖做 laya 文本情绪分类。

    盘后运行；结果落盘 data/laya/text_sentiment_<date>.json（并刷新
    text_sentiment_latest.json）并写一条 auto_event
    （event_type=laya_text_sentiment）。未启用/服务不可用/无候选返回 None。
    """
    if not is_available():
        return None

    from data.database import Database

    with Database(db_path=db_path) as db:
        codes = [
            str(r[0]) for r in db.conn.execute(
                "SELECT DISTINCT code FROM llm_decisions "
                "WHERE date=? AND action IS NOT NULL ORDER BY code", (date or "",),
            ).fetchall()
        ]
    if not codes:
        return None
    codes = codes[:max_codes]

    headline_texts = _collect_headlines()
    weibo_texts = _collect_weibo()
    stock_texts, code_of_text, news_counts = _collect_stock_news(codes)

    market_results = score_texts(headline_texts + weibo_texts) if (
        headline_texts or weibo_texts) else []
    if (headline_texts or weibo_texts) and market_results is None:
        return None
    market_results = market_results or []

    stock_results = score_texts(stock_texts) if stock_texts else []
    if stock_texts and stock_results is None:
        return None
    stock_results = stock_results or []

    per_stock: Dict[str, Dict[str, Any]] = {
        c: {"positive": 0, "neutral": 0, "negative": 0, "news_count": 0,
            "examples": []}
        for c in codes
    }
    for text, code, result in zip(stock_texts, code_of_text, stock_results):
        entry = per_stock[code]
        entry[result["choice"]] += 1
        if len(entry["examples"]) < 2:
            entry["examples"].append(text[:60])
    for c in codes:
        # news_count 取抓取到的条数（评分条数与之相同；抓取成功但评分失败
        # 时整体已返回 None，不会走到这里）
        per_stock[c]["news_count"] = news_counts.get(c, 0)

    summary = {
        "date": date,
        "market": {**_tally(market_results),
                   "total": len(market_results)},
        "per_stock": per_stock,
        "stats": {
            "n_codes": len(codes),
            "n_headlines": len(headline_texts),
            "n_weibo": len(weibo_texts),
            "n_stock_news": len(stock_texts),
            "n_texts_scored": len(market_results) + len(stock_results),
        },
    }

    os.makedirs(LAYA_DATA_DIR, exist_ok=True)
    path = os.path.join(LAYA_DATA_DIR, f"text_sentiment_{date}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(LAYA_DATA_DIR, "text_sentiment_latest.json"),
              "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    summary["path"] = path

    try:
        with Database(db_path=db_path) as db:
            db.insert_auto_event({
                "date": date,
                "event_type": "laya_text_sentiment",
                "status": "盘后",
                "actions": ["Laya 文本情绪分类完成"],
                "details": {
                    "n_texts_scored": summary["stats"]["n_texts_scored"],
                    "market": summary["market"],
                },
            })
    except Exception:  # noqa: BLE001 —— 事件写入失败不影响摘要返回
        pass
    return summary


def load_text_sentiment_summary() -> Dict[str, Any]:
    """只读最新一次文本情绪摘要；文件缺失/损坏返回空（brief 静默降级）。"""
    path = os.path.join(LAYA_DATA_DIR, "text_sentiment_latest.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) and payload.get("market") else {}
    except (OSError, json.JSONDecodeError):
        return {}


def format_text_sentiment_line(summary: Dict[str, Any]) -> str:
    """把文本情绪摘要压成一行复盘 prompt 证据文本。"""
    if not summary:
        return ""
    market = summary.get("market") or {}
    parts = [
        f"头条/热帖 {market.get('positive', 0)}利好/{market.get('neutral', 0)}中性/"
        f"{market.get('negative', 0)}利空"
    ]
    per_stock = summary.get("per_stock") or {}
    with_news = [
        (code, entry) for code, entry in per_stock.items()
        if entry.get("news_count")
    ]
    if with_news:
        detail = "，".join(
            f"{code}({entry['positive']}利/{entry['neutral']}中/{entry['negative']}空)"
            for code, entry in sorted(with_news, key=lambda kv: -kv[1]["news_count"])[:10]
        )
        parts.append(f"个股新闻: {detail}")
    else:
        parts.append("个股新闻: 无")
    return f"{summary.get('date')} 文本情绪(laya本地): " + "；".join(parts)


if __name__ == "__main__":
    # 手工自检: python -m strategy.laya_text [YYYY-MM-DD]
    from datetime import datetime
    target = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    print(f"enabled={is_available()} base_url={LAYA_BASE_URL}")
    result = run_text_sentiment(target)
    print(json.dumps(result, ensure_ascii=False, indent=2) if result
          else "无结果（未启用/服务未起/当日无候选）")
    print(format_text_sentiment_line(load_text_sentiment_summary()))
    sys.exit(0)
