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
    从近期涨停板中寻找"涨停后正常回踩、且出现企稳迹象"的标的
    """
    candidates = {}
    try:
        from data.eastmoney import get_limit_up
        from data.realtime import get_realtime_batch

        # 收集过去5天的涨停标的
        stocks_seen = set()
        candidates_raw = []
        for i in range(1, 6):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            zt = get_limit_up(date=date, limit=100)
            for s in zt:
                code = s.get("code", "")
                if code and code not in stocks_seen:
                    stocks_seen.add(code)
                    candidates_raw.append({
                        "code": code,
                        "name": s.get("name", ""),
                        "zt_price": float(s.get("price", 0) or 0),
                        "days_ago": i,
                    })
            if len(candidates_raw) >= 60:
                break

        if not candidates_raw:
            return {}

        # 批量获取最新行情，校验实际回撤幅度与企稳状态
        quotes = get_realtime_batch([c["code"] for c in candidates_raw[:50]])
        quote_by_code = {q.code: q for q in quotes}

        for item in candidates_raw:
            code = item["code"]
            quote = quote_by_code.get(code)
            if not quote or quote.price <= 0:
                continue

            current_price = quote.price
            zt_price = item["zt_price"] if item["zt_price"] > 0 else quote.close_prev
            # 回撤幅度：距涨停高点的回撤比例
            pullback = (zt_price - current_price) / zt_price if zt_price > 0 else 0.0

            # 筛选条件：回踩处于良性区间[2%, 18%]，且今日非跌停破位(跌幅>-8%)
            if 0.02 <= pullback <= 0.18 and quote.change_pct >= -8.0:
                c = Candidate(
                    code=code,
                    name=item["name"] or quote.name,
                    source=["回撤反弹"],
                    change_pct=quote.change_pct,
                    amount=quote.amount * 1e4,
                    industry=f"{item['days_ago']}日前涨停|回踩{pullback*100:.1f}%"
                )
                # 黄金回踩区间(5%~12%)赋高分；若当日企稳收红额外加分
                base_score = 25
                if 0.05 <= pullback <= 0.12:
                    base_score += 8
                if quote.change_pct > 0:
                    base_score += 5
                c.score = base_score
                candidates[code] = c

                if len(candidates) >= 30:
                    break

        logger.info(f"回撤反弹候选: {len(candidates)}只")
    except Exception as e:
        logger.warning(f"回撤反弹获取失败: {e}")
    return candidates


def _get_early_stage_candidates() -> Dict[str, Candidate]:
    """
    低位潜力股候选
    从活跃股中根据估值合理性与温和放量企稳形态筛选，避免将低价股误当低位股
    """
    candidates = {}
    try:
        from strategy.stock_picker import _get_active_stocks
        from data.realtime import get_realtime_batch

        active = _get_active_stocks(min_amount=2000, limit=100)
        logger.info(f"低位潜力股扫描池: {len(active)}只")

        active_items = list(active.items())[:60]
        quote_by_code = {
            quote.code: quote
            for quote in get_realtime_batch([code for code, _ in active_items])
        }

        for code, name in active_items:
            try:
                quote = quote_by_code.get(code)
                if not quote or quote.price <= 0:
                    continue

                change = quote.change_pct
                pe = quote.pe
                turnover = quote.turnover

                # 筛选：换手活跃(1%~10%)，涨幅温和未暴拉(-3%~4%)，排除流动性枯竭或恶性炒作
                if -3 <= change <= 4 and 1.0 <= turnover <= 10.0:
                    c = Candidate(
                        code=code, name=name, source=["低位潜力"],
                        change_pct=change,
                        amount=quote.amount * 1e4,
                        industry=f"PE={pe:.1f}|换手={turnover:.1f}%" if pe > 0 else f"换手={turnover:.1f}%"
                    )
                    # 综合估值与企稳形态打分，彻底告别“绝对名义价格越低分越高”的非量化逻辑
                    score = 20
                    if 0 < pe <= 25:
                        score += 10   # 低估值安全边际
                    elif 0 < pe <= 40:
                        score += 5    # 合理估值
                    elif pe > 80 or pe < 0:
                        score -= 5    # 估值过高或亏损标的降权

                    if 0 <= change <= 2.5:
                        score += 5    # 温和企稳微涨加分

                    c.score = max(10, score)
                    candidates[code] = c

                    if len(candidates) >= 20:
                        break
            except Exception:
                pass

        logger.info(f"低位潜力候选: {len(candidates)}只")
    except Exception as e:
        logger.warning(f"低位候选获取失败: {e}")
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
        if code.startswith(("8", "4", "920", "688")):
            return False
        return True

    filtered = [c for c in merged if _is_valid(c) and c.score >= min_score]
    result = filtered[:top_n]

    logger.info(f"低位选股完成: 合并{len(merged)}只, 过滤后{len(filtered)}只, 输出{len(result)}只")
    for c in result:
        logger.info(f"  {c}")

    return result
