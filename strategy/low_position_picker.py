"""
低位潜力股选股器 v2
使用现有数据源 + 技术分析，避免直接调用不稳定的API
"""
import logging
from typing import List, Dict
from datetime import datetime, timedelta

from strategy.stock_picker import Candidate

logger = logging.getLogger("strategy.low_position")


def _get_pullback_candidates() -> Dict[str, Candidate]:
    """
    回撤反弹候选
    从涨停板历史中找"曾经涨停，但现在回落"的股票
    """
    candidates = {}
    try:
        from data.eastmoney import get_limit_up
        from datetime import datetime, timedelta

        # 获取过去5天的涨停板数据
        stocks_seen = set()
        for i in range(1, 6):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            zt = get_limit_up(date=date, limit=100)
            for s in zt:
                code = s.get("code", "")
                if code and code not in stocks_seen:
                    # 曾经涨停，现在可能回落，有反弹机会
                    c = Candidate(
                        code=code,
                        name=s.get("name", ""),
                        source=["回撤反弹"],
                        change_pct=s.get("change_pct", 0),
                        amount=s.get("amount", 0) * 1e4,
                        industry=f"{i}天前涨停"
                    )
                    # 越早的涨停分数越高（回调充分）
                    c.score = 25 + i * 2
                    candidates[code] = c
                    stocks_seen.add(code)

                    if len(candidates) >= 30:
                        break
            if len(candidates) >= 30:
                break

        logger.info(f"回撤反弹候选: {len(candidates)}只")
    except Exception as e:
        logger.warning(f"回撤反弹获取失败: {e}")
    return candidates


def _get_early_stage_candidates() -> Dict[str, Candidate]:
    """
    低价股候选（股价<15元，避免高价股）
    从活跃股中筛选低价股
    """
    candidates = {}
    try:
        from strategy.stock_picker import _get_active_stocks
        from data.a_stock_data import get_realtime_quote, normalize_code

        active = _get_active_stocks(min_amount=2000, limit=100)
        logger.info(f"低价股扫描池: {len(active)}只")

        for code, name in list(active.items())[:60]:
            try:
                quote = get_realtime_quote(normalize_code(code))
                if not quote:
                    continue

                price = quote.get("price", 0)
                change = quote.get("change_percent", 0)

                # 筛选：股价5-15元，涨跌幅-3%~5%
                if 5 <= price <= 15 and -3 <= change <= 5:
                    c = Candidate(
                        code=code, name=name, source=["低价股"],
                        change_pct=change,
                        industry=f"价格{price:.2f}元"
                    )
                    # 价格越低分越高
                    if price < 8:
                        c.score = 25
                    elif price < 12:
                        c.score = 20
                    else:
                        c.score = 15
                    # 小幅上涨加分
                    if 0 < change <= 3:
                        c.score += 5

                    candidates[code] = c

                    if len(candidates) >= 20:
                        break
            except Exception:
                pass

        logger.info(f"低价股候选: {len(candidates)}只")
    except Exception as e:
        logger.warning(f"低价股获取失败: {e}")
    return candidates


def _get_stable_candidates() -> Dict[str, Candidate]:
    """
    稳健型候选（从北向资金中筛选近期横盘的）
    北向持有 = 相对优质，但近期不涨 = 低位
    """
    candidates = {}
    try:
        from data.eastmoney import get_dragon_tiger

        # 龙虎榜中净买入不大的（避免爆炒）
        lhb = get_dragon_tiger(limit=50)

        for s in lhb:
            code = s.get("code", "")
            name = s.get("name", "")
            net_buy = s.get("net_buy", 0)
            change = s.get("change_pct", 0)

            # 筛选：净买额适中（1000-5000万），涨幅不大（<5%）
            if 1000 <= net_buy <= 5000 and -2 <= change <= 5:
                c = Candidate(
                    code=code, name=name, source=["稳健型"],
                    change_pct=change,
                    industry=f"净买{net_buy:.0f}万"
                )
                c.score = 20
                # 涨幅小加分
                if 0 <= change <= 2:
                    c.score += 5

                candidates[code] = c

                if len(candidates) >= 20:
                    break

        logger.info(f"稳健型候选: {len(candidates)}只")
    except Exception as e:
        logger.warning(f"稳健型获取失败: {e}")
    return candidates


def pick_low_position_stocks(
    top_n: int = 20,
    min_score: float = 20,
) -> List[Candidate]:
    """
    低位潜力股选股主函数 v2

    专注于挖掘：
    1. 回撤反弹股（曾经涨停，现在回落）
    2. 低价股（5-15元，避免高价股）
    3. 稳健型（龙虎榜净买适中，涨幅不大）

    Returns:
        候选股票列表，按分数排序
    """
    logger.info("开始低位选股 v2...")

    # 获取各维度候选
    pullback = _get_pullback_candidates()
    early_stage = _get_early_stage_candidates()
    stable = _get_stable_candidates()

    # 合并
    from strategy.stock_picker import _merge_candidates
    merged = _merge_candidates(
        pullback, early_stage, stable, {}, {}, {}, {}, {}, {}
    )

    # 过滤
    def _is_valid(c: Candidate) -> bool:
        name = c.name
        code = c.code
        if "退" in name or "ST" in name:
            return False
        if code.startswith(("8", "4", "688")):
            return False
        return True

    filtered = [c for c in merged if _is_valid(c) and c.score >= min_score]
    result = filtered[:top_n]

    logger.info(f"低位选股完成: 合并{len(merged)}只, 过滤后{len(filtered)}只, 输出{len(result)}只")
    for c in result:
        logger.info(f"  {c}")

    return result
