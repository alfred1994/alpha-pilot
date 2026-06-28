"""
舆情分析模块
功能: 从微博财经采集A股讨论，分析热门板块和市场情绪
数据源: 微博财经分组 + AKShare涨停数据
"""
import re
import json
from datetime import datetime
from typing import List, Dict, Optional
from data import logger
from data.sentiment import get_weibo_finance
from strategy.mimo_client import post_chat_completion

# 热门板块关键词
SECTOR_KEYWORDS = {
    "科技": ["科技", "AI", "人工智能", "芯片", "半导体", "算力", "数据中心", "机器人"],
    "新能源": ["新能源", "锂电", "光伏", "风电", "储能", "充电桩", "氢能"],
    "医药": ["医药", "医疗", "生物", "创新药", "中药", "医疗器械"],
    "消费": ["消费", "白酒", "食品", "家电", "旅游", "免税"],
    "金融": ["金融", "银行", "证券", "保险", "券商"],
    "地产": ["地产", "房地产", "物业", "建材"],
    "军工": ["军工", "国防", "航天", "航空", "船舶"],
    "电子": ["电子", "PCB", "面板", "LED", "光学"],
    "汽车": ["汽车", "新能源车", "智能驾驶", "车联网"],
    "传媒": ["传媒", "游戏", "影视", "广告", "网红"]
}


def extract_sectors_from_text(text: str) -> List[str]:
    """从文本中提取提及的板块"""
    sectors = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text:
                if sector not in sectors:
                    sectors.append(sector)
                break
    return sectors


def analyze_weibo_sentiment(posts: List[Dict]) -> Dict:
    """分析微博舆情"""
    if not posts:
        return {"sectors": {}, "hot_topics": [], "emotion": "neutral"}
    
    # 统计板块提及次数
    sector_counts = {}
    hot_topics = []
    
    for post in posts:
        text = post.get("text", "")
        likes = post.get("likes", 0)
        comments = post.get("comments", 0)
        reposts = post.get("reposts", 0)
        
        # 提取板块
        sectors = extract_sectors_from_text(text)
        for sector in sectors:
            if sector not in sector_counts:
                sector_counts[sector] = {"count": 0, "engagement": 0}
            sector_counts[sector]["count"] += 1
            sector_counts[sector]["engagement"] += likes + comments + reposts
        
        # 提取热门话题（互动量高的帖子）
        engagement = likes + comments + reposts
        if engagement > 10 or likes > 5:
            # 提取话题标签
            topics = re.findall(r'#([^#]+)#', text)
            for topic in topics:
                if len(topic) > 2 and len(topic) < 20:
                    hot_topics.append({
                        "topic": topic,
                        "engagement": engagement,
                        "text": text[:100]
                    })
    
    # 按互动量排序板块
    sorted_sectors = sorted(
        sector_counts.items(),
        key=lambda x: x[1]["engagement"],
        reverse=True
    )
    
    # 分析情绪
    positive_words = ["涨", "利好", "突破", "新高", "强势", "涨停", "牛市", "反弹"]
    negative_words = ["跌", "利空", "破位", "新低", "弱势", "跌停", "熊市", "回调"]
    
    positive_count = 0
    negative_count = 0
    
    for post in posts:
        text = post.get("text", "")
        for word in positive_words:
            if word in text:
                positive_count += 1
        for word in negative_words:
            if word in text:
                negative_count += 1
    
    total = positive_count + negative_count
    if total > 0:
        emotion_ratio = positive_count / total
        if emotion_ratio > 0.6:
            emotion = "bullish"
        elif emotion_ratio < 0.4:
            emotion = "bearish"
        else:
            emotion = "neutral"
    else:
        emotion = "neutral"
    
    return {
        "sectors": dict(sorted_sectors[:10]),
        "hot_topics": hot_topics[:10],
        "emotion": emotion,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "total_posts": len(posts)
    }


def get_sector_recommendations(analysis: Dict) -> List[Dict]:
    """根据舆情分析生成板块推荐"""
    recommendations = []
    
    for sector, data in analysis.get("sectors", {}).items():
        count = data.get("count", 0)
        engagement = data.get("engagement", 0)
        
        # 计算推荐分数
        score = min(100, count * 10 + engagement / 10)
        
        if score > 30:  # 只推荐分数大于30的板块
            recommendations.append({
                "sector": sector,
                "score": round(score, 1),
                "mention_count": count,
                "engagement": engagement,
                "reason": f"微博讨论热度{count}次，互动量{engagement}"
            })
    
    # 按分数排序
    recommendations.sort(key=lambda x: x["score"], reverse=True)
    return recommendations[:5]


def generate_sentiment_report(analysis: Dict, recommendations: List[Dict]) -> str:
    """生成舆情分析报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    lines = [
        "=" * 60,
        f"微博舆情分析报告 ({now})",
        "=" * 60,
        "",
        f"📊 分析样本: {analysis.get('total_posts', 0)} 条微博",
        f"📈 市场情绪: {analysis.get('emotion', 'neutral')}",
        f"  - 积极信号: {analysis.get('positive_count', 0)} 次",
        f"  - 消极信号: {analysis.get('negative_count', 0)} 次",
        "",
        "🔥 热门板块:"
    ]
    
    for i, rec in enumerate(recommendations[:5], 1):
        lines.append(f"  {i}. {rec['sector']} (分数:{rec['score']}, 讨论:{rec['mention_count']}次)")
    
    if analysis.get("hot_topics"):
        lines.append("")
        lines.append("💬 热门话题:")
        for topic in analysis["hot_topics"][:5]:
            lines.append(f"  - #{topic['topic']}# (互动:{topic['engagement']})")
    
    lines.append("")
    lines.append("=" * 60)
    
    return "\n".join(lines)


def run_sentiment_analysis() -> Dict:
    """运行完整的舆情分析"""
    logger.info("开始微博舆情分析...")
    
    # 获取微博数据
    posts = get_weibo_finance(limit=30)
    
    if not posts:
        logger.warning("未获取到微博数据")
        return {
            "success": False,
            "error": "未获取到微博数据"
        }
    
    # 分析舆情
    analysis = analyze_weibo_sentiment(posts)
    
    # 获取板块推荐
    recommendations = get_sector_recommendations(analysis)
    
    # 生成报告
    report = generate_sentiment_report(analysis, recommendations)
    
    logger.info(f"舆情分析完成: {len(posts)}条微博, {len(recommendations)}个推荐板块")
    
    return {
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "posts_count": len(posts),
        "analysis": analysis,
        "recommendations": recommendations,
        "report": report
    }


if __name__ == "__main__":
    # 测试
    result = run_sentiment_analysis()
    if result.get("success"):
        print(result["report"])
    else:
        print(f"分析失败: {result.get('error')}")
