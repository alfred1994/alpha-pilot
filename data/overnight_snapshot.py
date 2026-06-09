"""
隔夜快照模块 - 综合外围市场+新闻生成隔夜分析快照
功能: 生成、保存、加载隔夜快照，格式化为LLM prompt
用于收盘后自动收集外围市场和新闻数据，供AI决策参考
"""
import os
import json
import re
from datetime import datetime
from data import logger

# 快照保存目录
SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

# 风险关键词列表
RISK_KEYWORDS = [
    "降息", "加息", "降准", "加息预期",
    "贸易战", "关税", "制裁", "出口管制",
    "地缘政治", "冲突", "战争", "军事",
    "金融危机", "银行暴雷", "流动性危机", "债务违约",
    "经济衰退", "失业率", "通胀", "CPI",
    "黑天鹅", "暴跌", "熔断", "系统性风险",
    "美联储", "Fed", "FOMC",
    "人民币贬值", "资本外流", "外汇",
]


def _extract_risk_keywords(news_headlines):
    """
    从新闻标题中提取风险关键词
    
    Args:
        news_headlines: 新闻标题列表
    
    Returns:
        list[str]: 命中的风险关键词
    """
    found = set()
    for item in news_headlines:
        title = item.get("title", "")
        for kw in RISK_KEYWORDS:
            if kw in title:
                found.add(kw)
    return sorted(found)


def _build_risk_factors(us_market):
    """
    根据外围市场数据构建风险因素描述
    
    Args:
        us_market: get_us_market_summary() 的返回值
    
    Returns:
        list[str]: 风险因素描述列表
    """
    factors = []
    
    # 美股指数
    us_indices = us_market.get("us_indices", {})
    for key, label in [("djia", "道琼斯"), ("nasdaq", "纳斯达克"), ("sp500", "标普500")]:
        idx = us_indices.get(key, {})
        pct = idx.get("change_pct", 0)
        if abs(pct) >= 0.01:
            direction = "上涨" if pct > 0 else "下跌"
            factors.append(f"{label}{direction}{abs(pct):.1f}%")
    
    # 港股
    hsi = us_market.get("hk_indices", {}).get("hsi", {})
    pct = hsi.get("change_pct", 0)
    if abs(pct) >= 0.01:
        direction = "上涨" if pct > 0 else "下跌"
        factors.append(f"恒生指数{direction}{abs(pct):.1f}%")
    
    # A50期货
    a50 = us_market.get("a50_futures", {})
    pct = a50.get("change_pct", 0)
    if abs(pct) >= 0.01:
        direction = "上涨" if pct > 0 else "下跌"
        factors.append(f"A50期货{direction}{abs(pct):.1f}%")
    
    if not factors:
        factors.append("外围市场变化极小")
    
    return factors


def _build_risk_summary(us_market, risk_keywords):
    """
    构建风险评估总结
    
    Args:
        us_market: 外围市场数据
        risk_keywords: 风险关键词列表
    
    Returns:
        str: 风险总结
    """
    level = us_market.get("risk_level", "low")
    
    parts = []
    if level == "high":
        parts.append("外围市场大幅波动，存在较高风险")
    elif level == "medium":
        parts.append("外围市场有一定波动，需关注风险")
    else:
        parts.append("外围市场平稳")
    
    if risk_keywords:
        parts.append(f"关注: {', '.join(risk_keywords[:5])}")
    else:
        parts.append("无重大风险事件")
    
    return "，".join(parts)


def generate_overnight_snapshot():
    """
    生成完整隔夜快照，包含外围市场数据和新闻聚合
    
    Returns:
        dict: 完整快照数据
    """
    from data.us_market import get_us_market_summary
    from data.news_aggregator import get_all_news
    
    date_str = datetime.now().strftime("%Y-%m-%d")
    generated_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    
    logger.info(f"开始生成隔夜快照: {date_str}")
    
    # 获取外围市场数据
    try:
        us_market = get_us_market_summary()
    except Exception as e:
        logger.error(f"获取外围市场数据失败: {e}")
        us_market = {"timestamp": generated_at, "us_indices": {}, "hk_indices": {}, "a50_futures": {}, "risk_level": "unknown"}
    
    # 获取新闻数据
    try:
        news_data = get_all_news()
    except Exception as e:
        logger.error(f"获取新闻数据失败: {e}")
        news_data = {"headlines": [], "sector_heat": []}
    
    headlines = news_data.get("headlines", [])
    sector_heat = news_data.get("sector_heat", [])
    
    # 提取风险关键词
    risk_keywords = _extract_risk_keywords(headlines)
    
    # 构建风险评估
    risk_factors = _build_risk_factors(us_market)
    risk_summary = _build_risk_summary(us_market, risk_keywords)
    risk_level = us_market.get("risk_level", "unknown")
    
    # 组装快照
    snapshot = {
        "date": date_str,
        "generated_at": generated_at,
        "us_market": us_market,
        "news": {
            "headlines": headlines[:10],
            "sector_heat": sector_heat,
            "risk_keywords": risk_keywords,
        },
        "risk_assessment": {
            "level": risk_level,
            "factors": risk_factors,
            "summary": risk_summary,
        },
    }
    
    # 保存快照
    snapshot_path = os.path.join(SNAPSHOT_DIR, f"overnight_{date_str}.json")
    try:
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        logger.info(f"隔夜快照已保存: {snapshot_path}")
    except Exception as e:
        logger.error(f"保存快照失败: {e}")
    
    return snapshot


def load_overnight_snapshot(date=None):
    """
    加载指定日期的隔夜快照
    
    Args:
        date: 日期字符串 "YYYY-MM-DD"，默认今天
    
    Returns:
        dict 或 None
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    
    snapshot_path = os.path.join(SNAPSHOT_DIR, f"overnight_{date}.json")
    
    if not os.path.exists(snapshot_path):
        logger.warning(f"快照不存在: {snapshot_path}")
        return None
    
    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        logger.info(f"加载快照成功: {date}")
        return snapshot
    except Exception as e:
        logger.error(f"加载快照失败: {e}")
        return None


def format_for_llm_prompt(snapshot):
    """
    将快照格式化为LLM prompt片段，便于AI读取分析
    
    Args:
        snapshot: generate_overnight_snapshot() 的返回值
    
    Returns:
        str: 格式化的文本
    """
    if not snapshot:
        return "【外围市场·隔夜快照】\n暂无数据"
    
    lines = []
    date = snapshot.get("date", "未知日期")
    lines.append(f"【外围市场·隔夜快照 {date}】")
    
    # 美股指数
    us_indices = snapshot.get("us_market", {}).get("us_indices", {})
    us_parts = []
    for key, label in [("djia", "道琼斯"), ("nasdaq", "纳斯达克"), ("sp500", "标普500")]:
        idx = us_indices.get(key, {})
        pct = idx.get("change_pct", 0)
        sign = "+" if pct >= 0 else ""
        us_parts.append(f"{label} {sign}{pct:.1f}%")
    if us_parts:
        lines.append("美股: " + " | ".join(us_parts))
    
    # 港股
    hsi = snapshot.get("us_market", {}).get("hk_indices", {}).get("hsi", {})
    if hsi:
        pct = hsi.get("change_pct", 0)
        sign = "+" if pct >= 0 else ""
        lines.append(f"港股: 恒生指数 {sign}{pct:.1f}%")
    
    # A50期货
    a50 = snapshot.get("us_market", {}).get("a50_futures", {})
    if a50:
        price = a50.get("price", 0)
        pct = a50.get("change_pct", 0)
        sign = "+" if pct >= 0 else ""
        lines.append(f"A50期货: {price} ({sign}{pct:.1f}%)")
    
    # 风险等级
    risk = snapshot.get("risk_assessment", {})
    level = risk.get("level", "unknown")
    level_label = {"low": "低", "medium": "中", "high": "高"}.get(level, "未知")
    lines.append("")
    lines.append(f"隔夜风险等级: {level_label}")
    
    # 风险因素
    factors = risk.get("factors", [])
    if factors:
        lines.append(f"风险因素: {', '.join(factors)}")
    
    # 热点板块
    sector_heat = snapshot.get("news", {}).get("sector_heat", [])
    if sector_heat:
        lines.append("")
        lines.append("今日热点板块:")
        for block in sector_heat[:5]:
            name = block.get("name", "")
            num = block.get("limit_up_num", 0)
            if name:
                lines.append(f"- {name} ({num}只涨停)")
    
    # 重要新闻
    headlines = snapshot.get("news", {}).get("headlines", [])
    if headlines:
        lines.append("")
        lines.append("重要新闻:")
        for item in headlines[:10]:
            source = item.get("source", "")
            title = item.get("title", "")
            tags = item.get("tags", [])
            tag_str = f"[{'/'.join(tags)}]" if tags else ""
            if title:
                lines.append(f"- {tag_str} {title}".strip())
    
    # 风险关键词
    risk_kw = snapshot.get("news", {}).get("risk_keywords", [])
    if risk_kw:
        lines.append("")
        lines.append(f"风险关键词: {', '.join(risk_kw)}")
    
    return "\n".join(lines)


# 测试入口
if __name__ == "__main__":
    print("=" * 60)
    print("隔夜快照模块测试")
    print("=" * 60)
    
    # 生成快照
    print("\n[1] 生成隔夜快照:")
    snapshot = generate_overnight_snapshot()
    
    # 加载快照
    print("\n[2] 加载快照:")
    loaded = load_overnight_snapshot(snapshot["date"])
    if loaded:
        print(f"  日期: {loaded['date']}")
        print(f"  风险等级: {loaded['risk_assessment']['level']}")
    
    # 格式化为LLM prompt
    print("\n[3] LLM Prompt格式:")
    prompt = format_for_llm_prompt(snapshot)
    print(prompt)
