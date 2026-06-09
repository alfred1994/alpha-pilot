"""
资金信号模块
====================================================================
信号源:
  1. 北向资金 - 沪深港通净流入趋势
  2. 主力资金 - 个股主力资金流向
  3. 融资融券 - 两融余额变化趋势

输入: 股票代码
输出: SignalResult(name, score, direction, confidence, detail)
====================================================================
"""
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional
from signals import SignalResult, Direction, score_to_direction, logger
from data import rate_limit


# ══════════════════════════════════════════════════════════════════
# 1. 北向资金信号
# ══════════════════════════════════════════════════════════════════
# 北向资金 = 沪股通 + 深股通
# 净流入持续为正 → 外资看好 → 偏多
# 净流入持续为负 → 外资撤退 → 偏空
# ──────────────────────────────────────────────────────────────────

def northbound_signal(code: str = None) -> SignalResult:
    """
    北向资金信号

    Args:
        code: 股票代码(可选), None则看整体北向资金趋势

    Returns:
        SignalResult: 基于北向资金净流入趋势打分
    """
    rate_limit("akshare", 2.5)
    try:
        # 获取北向资金历史数据
        df = ak.stock_hsgt_north_net_flow_in_em(symbol="北向")
        if df is None or df.empty:
            return SignalResult("北向资金", 50, Direction.HOLD, 0.0, "无北向资金数据")

        # 取最近10个交易日
        df = df.tail(10).copy()
        net_flow = pd.to_numeric(df.iloc[:, 1], errors="coerce").fillna(0)
        cur_flow = net_flow.iloc[-1]

        # 计算趋势
        score = 50
        signals = []

        # 最近一日净流入
        if cur_flow > 0:
            score += min(15, int(cur_flow / 10000))
            signals.append(f"今日净流入{cur_flow/10000:.1f}亿")
        else:
            score += max(-15, int(cur_flow / 10000))
            signals.append(f"今日净流出{abs(cur_flow)/10000:.1f}亿")

        # 连续流入/流出天数
        consecutive = 0
        for v in net_flow.iloc[::-1]:
            if (v > 0 and consecutive >= 0) or (v < 0 and consecutive <= 0):
                consecutive += (1 if v > 0 else -1)
            else:
                break

        if consecutive >= 3:
            score += 10
            signals.append(f"连续{abs(consecutive)}天净流入")
        elif consecutive <= -3:
            score -= 10
            signals.append(f"连续{abs(consecutive)}天净流出")

        # 近5日均值 vs 近10日均值
        if len(net_flow) >= 10:
            ma5 = net_flow.tail(5).mean()
            ma10 = net_flow.mean()
            if ma5 > ma10 * 1.2:
                score += 8
                signals.append("近5日均值加速流入")
            elif ma5 < ma10 * 0.8:
                score -= 8
                signals.append("近5日均值加速流出")

        score = max(0, min(100, score))
        conf = 0.6 if abs(score - 50) > 10 else 0.4
        detail = " | ".join(signals) if signals else "北向资金数据不足"
        return SignalResult("北向资金", score, score_to_direction(score), conf, detail)

    except Exception as e:
        logger.error(f"北向资金信号异常: {e}")
        return SignalResult("北向资金", 50, Direction.HOLD, 0.0, f"数据异常: {e}")


# ══════════════════════════════════════════════════════════════════
# 2. 主力资金信号
# ══════════════════════════════════════════════════════════════════
# 主力资金 = 超大单 + 大单 净流入
# 主力持续净流入 → 机构看好 → 偏多
# 主力持续净流出 → 机构撤退 → 偏空
# ──────────────────────────────────────────────────────────────────

def main_capital_signal(code: str) -> SignalResult:
    """
    个股主力资金信号

    Args:
        code: 股票代码, 如 "600519"

    Returns:
        SignalResult: 基于主力资金净流入打分
    """
    rate_limit("akshare", 2.5)
    try:
        df = ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith("6") else "sz")
        if df is None or df.empty:
            return SignalResult("主力资金", 50, Direction.HOLD, 0.0, "无主力资金数据")

        # 取最近5天
        df = df.tail(5).copy()
        score = 50
        signals = []

        # 主力净流入列
        net_col = None
        for col in df.columns:
            if "主力净流入" in str(col):
                net_col = col
                break

        if net_col is None:
            return SignalResult("主力资金", 50, Direction.HOLD, 0.0, "数据格式异常")

        net_flow = pd.to_numeric(df[net_col], errors="coerce").fillna(0)
        cur = net_flow.iloc[-1]

        # 当日主力净流入
        if cur > 0:
            score += min(20, int(cur / 1000))
            signals.append(f"主力净流入{cur:.0f}万")
        else:
            score += max(-20, int(cur / 1000))
            signals.append(f"主力净流出{abs(cur):.0f}万")

        # 连续流入/流出
        consecutive = 0
        for v in net_flow.iloc[::-1]:
            if (v > 0 and consecutive >= 0) or (v < 0 and consecutive <= 0):
                consecutive += (1 if v > 0 else -1)
            else:
                break

        if consecutive >= 3:
            score += 10
            signals.append(f"连续{abs(consecutive)}天主力流入")
        elif consecutive <= -3:
            score -= 10
            signals.append(f"连续{abs(consecutive)}天主力流出")

        # 5日累计
        total_5d = net_flow.sum()
        if total_5d > 5000:
            score += 8
            signals.append(f"5日累计净流入{total_5d:.0f}万")
        elif total_5d < -5000:
            score -= 8
            signals.append(f"5日累计净流出{abs(total_5d):.0f}万")

        score = max(0, min(100, score))
        conf = 0.55 if abs(score - 50) > 10 else 0.35
        detail = " | ".join(signals) if signals else "主力资金数据不足"
        return SignalResult("主力资金", score, score_to_direction(score), conf, detail)

    except Exception as e:
        logger.error(f"主力资金信号异常 {code}: {e}")
        return SignalResult("主力资金", 50, Direction.HOLD, 0.0, f"数据异常: {e}")


# ══════════════════════════════════════════════════════════════════
# 3. 融资融券信号
# ══════════════════════════════════════════════════════════════════
# 融资余额增加 → 杠杆资金看多 → 偏多
# 融资余额减少 → 杠杆资金撤退 → 偏空
# 融券余额增加 → 看空力量增强 → 偏空
# ──────────────────────────────────────────────────────────────────

def margin_signal() -> SignalResult:
    """
    融资融券信号

    基于沪深两市融资融券余额变化趋势打分

    Returns:
        SignalResult: 融资余额趋势分数
    """
    rate_limit("akshare", 2.5)
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
        df = ak.stock_margin_sse(start_date=start, end_date=end)

        if df is None or len(df) < 2:
            return SignalResult("融资融券", 50, Direction.HOLD, 0.0, "无融资融券数据")

        score = 50
        signals = []

        # 融资余额趋势
        margin_balance = pd.to_numeric(df.iloc[:, 1], errors="coerce").dropna()
        if len(margin_balance) >= 2:
            latest = margin_balance.iloc[-1]
            prev = margin_balance.iloc[-2]
            change_pct = (latest - prev) / prev * 100 if prev > 0 else 0

            if change_pct > 0.5:
                score += min(20, int(change_pct * 10))
                signals.append(f"融资余额增{change_pct:.2f}%")
            elif change_pct < -0.5:
                score += max(-20, int(change_pct * 10))
                signals.append(f"融资余额减{abs(change_pct):.2f}%")

            # 近5日趋势
            if len(margin_balance) >= 5:
                ma5_change = (margin_balance.iloc[-1] - margin_balance.iloc[-5]) / margin_balance.iloc[-5] * 100
                if ma5_change > 1:
                    score += 10
                    signals.append(f"5日融资增{ma5_change:.2f}%")
                elif ma5_change < -1:
                    score -= 10
                    signals.append(f"5日融资减{abs(ma5_change):.2f}%")

        score = max(0, min(100, score))
        conf = 0.5 if abs(score - 50) > 10 else 0.3
        detail = " | ".join(signals) if signals else "融资融券数据不足"
        return SignalResult("融资融券", score, score_to_direction(score), conf, detail)

    except Exception as e:
        logger.error(f"融资融券信号异常: {e}")
        return SignalResult("融资融券", 50, Direction.HOLD, 0.0, f"数据异常: {e}")


# ══════════════════════════════════════════════════════════════════
# 综合资金信号
# ══════════════════════════════════════════════════════════════════

def capital_signal(code: str) -> SignalResult:
    """
    综合资金信号

    融合北向资金 + 主力资金 + 融资融券三个维度

    Args:
        code: 股票代码

    Returns:
        SignalResult: 综合资金面分数
    """
    signals = []
    scores = []
    confs = []

    # 北向资金
    nb = northbound_signal(code)
    signals.append(nb)
    scores.append(nb.score)
    confs.append(nb.confidence)

    # 主力资金
    mc = main_capital_signal(code)
    signals.append(mc)
    scores.append(mc.score)
    confs.append(mc.confidence)

    # 融资融券
    mg = margin_signal()
    signals.append(mg)
    scores.append(mg.score)
    confs.append(mg.confidence)

    # 加权平均 (主力资金权重最高)
    weights = [0.3, 0.45, 0.25]
    final_score = sum(s * w for s, w in zip(scores, weights))
    final_conf = sum(c * w for c, w in zip(confs, weights))

    details = [f"{sig.name}={sig.score}" for sig in signals]
    detail = " | ".join(details)

    return SignalResult(
        "资金综合",
        int(final_score),
        score_to_direction(int(final_score)),
        round(final_conf, 2),
        detail,
    )


# 测试
if __name__ == "__main__":
    print("资金信号测试")
    print("=" * 60)

    print("\n【1】北向资金信号")
    r = northbound_signal()
    print(r)

    print("\n【2】主力资金信号(茅台)")
    r = main_capital_signal("600519")
    print(r)

    print("\n【3】融资融券信号")
    r = margin_signal()
    print(r)

    print("\n【4】综合资金信号(茅台)")
    r = capital_signal("600519")
    print(r)
