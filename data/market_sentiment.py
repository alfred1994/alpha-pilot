"""
市场舆情采集模块
数据源: AKShare + 东方财富
功能: 热门板块、涨停分析、市场情绪
"""
import akshare as ak
import requests
import pandas as pd
from datetime import datetime
from typing import List, Dict, Optional
from data import logger


def get_hot_stocks(limit: int = 20) -> List[Dict]:
    """获取热门股票"""
    try:
        df = ak.stock_hot_rank_em()
        if df is not None and not df.empty:
            stocks = []
            for _, row in df.head(limit).iterrows():
                stocks.append({
                    "code": str(row.get("股票代码", "")),
                    "name": str(row.get("股票简称", "")),
                    "rank": int(row.get("当前排名", 0)),
                    "change_pct": float(row.get("涨跌幅", 0)),
                    "hot_value": float(row.get("热度", 0))
                })
            return stocks
    except Exception as e:
        logger.warning(f"获取热门股票失败: {e}")
    return []


def get_limit_up_analysis(date: str = None) -> Dict:
    """获取涨停分析"""
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    
    try:
        df = ak.stock_zt_pool_em(date=date)
        if df is not None and not df.empty:
            # 统计涨停板块
            sectors = {}
            for _, row in df.iterrows():
                # 这里可以添加板块分类逻辑
                pass
            
            return {
                "date": date,
                "count": len(df),
                "stocks": df[['代码', '名称', '涨跌幅', '最新价', '成交额']].to_dict('records'),
                "sectors": sectors
            }
    except Exception as e:
        logger.warning(f"获取涨停分析失败: {e}")
    return {"date": date, "count": 0, "stocks": [], "sectors": {}}


def get_market_emotion() -> Dict:
    """获取市场情绪指标"""
    try:
        # 获取涨跌家数
        df = ak.stock_market_activity_legu()
        if df is not None and not df.empty:
            return {
                "up_count": int(df.get("上涨家数", [0])[0]) if "上涨家数" in df.columns else 0,
                "down_count": int(df.get("下跌家数", [0])[0]) if "下跌家数" in df.columns else 0,
                "limit_up": int(df.get("涨停家数", [0])[0]) if "涨停家数" in df.columns else 0,
                "limit_down": int(df.get("跌停家数", [0])[0]) if "跌停家数" in df.columns else 0
            }
    except Exception as e:
        logger.warning(f"获取市场情绪失败: {e}")
    return {}


def get_sector_hot() -> List[Dict]:
    """获取热门板块"""
    try:
        df = ak.stock_board_concept_name_em()
        if df is not None and not df.empty:
            sectors = []
            for _, row in df.head(20).iterrows():
                sectors.append({
                    "name": str(row.get("板块名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0)),
                    "lead_stock": str(row.get("领涨股票", "")),
                    "lead_change": float(row.get("领涨股票-涨跌幅", 0))
                })
            return sectors
    except Exception as e:
        logger.warning(f"获取热门板块失败: {e}")
    return []


def analyze_sentiment() -> Dict:
    """综合分析市场舆情"""
    hot_stocks = get_hot_stocks(limit=10)
    limit_up = get_limit_up_analysis()
    emotion = get_market_emotion()
    sectors = get_sector_hot()
    
    # 计算情绪指数
    up_count = emotion.get("up_count", 0)
    down_count = emotion.get("down_count", 0)
    total = up_count + down_count
    emotion_index = (up_count / total * 100) if total > 0 else 50
    
    # 分析热门板块
    hot_sectors = [s["name"] for s in sectors[:5]]
    
    return {
        "timestamp": datetime.now().isoformat(),
        "emotion_index": round(emotion_index, 1),
        "hot_stocks": hot_stocks,
        "limit_up": limit_up,
        "emotion": emotion,
        "hot_sectors": hot_sectors,
        "summary": f"市场情绪指数: {emotion_index:.1f}%, 涨停{limit_up.get('count', 0)}只, 热门板块: {', '.join(hot_sectors[:3])}"
    }


if __name__ == "__main__":
    # 测试
    result = analyze_sentiment()
    print("=" * 60)
    print("市场舆情分析")
    print("=" * 60)
    print(f"时间: {result['timestamp']}")
    print(f"情绪指数: {result['emotion_index']}%")
    print(f"涨停数量: {result['limit_up'].get('count', 0)}")
    print(f"热门板块: {', '.join(result['hot_sectors'])}")
    print()
    print("热门股票:")
    for i, stock in enumerate(result['hot_stocks'][:5], 1):
        print(f"  {i}. {stock['name']}({stock['code']}) 涨跌幅:{stock['change_pct']:.2f}%")
