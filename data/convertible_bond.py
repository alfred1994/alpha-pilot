"""
可转债数据源
基于AkShare获取集思录可转债数据，提供评分所需的全部字段
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger("data.convertible_bond")


def get_cb_list() -> List[dict]:
    """
    获取全市场可转债列表

    Returns:
        [{代码, 转债名称, 现价, 涨跌幅, 正股代码, 正股名称, 正股价, 正股涨跌,
          转股溢价率, 剩余规模, 换手率, 成交额, 转股价值, ...}]
    """
    try:
        import akshare as ak
        df = ak.bond_cb_jsl()
        if df is None or df.empty:
            logger.warning("可转债数据为空")
            return []

        records = []
        for _, row in df.iterrows():
            records.append({
                "cb_code": str(row.get("代码", "")),
                "cb_name": str(row.get("转债名称", "")),
                "cb_price": _safe_float(row.get("现价", 0)),
                "cb_change_pct": _safe_float(row.get("涨跌幅", 0)),
                "stock_code": str(row.get("正股代码", "")),
                "stock_name": str(row.get("正股名称", "")),
                "stock_price": _safe_float(row.get("正股价", 0)),
                "stock_change_pct": _safe_float(row.get("正股涨跌", 0)),
                "premium_rate": _safe_float(row.get("转股溢价率", 0)),
                "remaining_scale": _safe_float(row.get("剩余规模", 0)),
                "turnover_rate": _safe_float(row.get("换手率", 0)),
                "trade_amount": _safe_float(row.get("成交额", 0)),
                "conversion_value": _safe_float(row.get("转股价值", 0)),
                "stock_pb": _safe_float(row.get("正股PB", 0)),
                "bond_rating": str(row.get("债券评级", "")),
                "double_low": _safe_float(row.get("双低", 0)),
            })

        logger.info(f"可转债数据: {len(records)}只")
        return records

    except Exception as e:
        logger.error(f"获取可转债数据失败: {e}")
        return []


def get_cb_detail(cb_code: str) -> Optional[dict]:
    """获取单只可转债详情"""
    all_cbs = get_cb_list()
    for cb in all_cbs:
        if cb["cb_code"] == cb_code:
            return cb
    return None


def scan_cb_opportunities(min_score: float = 70) -> List[dict]:
    """
    扫描可转债套利机会

    筛选条件:
    - 正股涨幅 >= 9.5%（接近涨停）
    - 转股溢价率 <= 50%
    - 剩余规模 < 10亿

    Returns:
        符合条件的可转债列表
    """
    all_cbs = get_cb_list()
    opportunities = []

    for cb in all_cbs:
        # 基础筛选
        if cb["premium_rate"] > 50:
            continue
        if cb["remaining_scale"] > 10:
            continue
        if cb["cb_price"] <= 0:
            continue

        # 正股涨幅接近涨停（主板10%，创业板/科创板20%）
        stock_code = cb["stock_code"]
        is_gem = stock_code.startswith("3")  # 创业板
        is_star = stock_code.startswith("68")  # 科创板
        limit_pct = 20 if (is_gem or is_star) else 10

        # 放宽条件: 正股涨幅 >= 7%（不一定封板，但有强势迹象）
        if cb["stock_change_pct"] < 7:
            continue

        # 计算量比（用成交额粗略估算，需要历史数据做精确计算）
        opportunities.append(cb)

    logger.info(f"可转债机会扫描: {len(opportunities)}只符合条件")
    return opportunities


def _safe_float(val) -> float:
    """安全转换为float"""
    try:
        if val is None or str(val).strip() in ("", "-", "nan", "None"):
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    print("=== 可转债数据测试 ===")
    cbs = get_cb_list()
    print(f"全市场可转债: {len(cbs)}只")
    if cbs:
        print(f"\n前3只:")
        for cb in cbs[:3]:
            print(f"  {cb['cb_name']}({cb['cb_code']}): "
                  f"现价={cb['cb_price']} 溢价率={cb['premium_rate']:.1f}% "
                  f"正股={cb['stock_name']}({cb['stock_change_pct']:+.1f}%)")

    print(f"\n=== 套利机会扫描 ===")
    opps = scan_cb_opportunities()
    for opp in opps:
        print(f"  {opp['cb_name']}({opp['cb_code']}): "
              f"溢价率={opp['premium_rate']:.1f}% "
              f"正股涨幅={opp['stock_change_pct']:+.1f}% "
              f"剩余规模={opp['remaining_scale']:.1f}亿")
