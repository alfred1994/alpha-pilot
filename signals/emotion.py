"""
情绪信号模块
====================================================================
信号源:
  1. 涨跌停比 - 涨停家数/跌停家数比值
  2. 换手率异常 - 个股换手率相对历史分位
  3. 量能信号 - 成交量相对历史分位 + 量比

输入: 股票代码 / 市场数据
输出: SignalResult(name, score, direction, confidence, detail)
====================================================================
"""
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from signals import SignalResult, Direction, score_to_direction, logger
from data import rate_limit


# ══════════════════════════════════════════════════════════════════
# 1. 涨跌停比信号
# ══════════════════════════════════════════════════════════════════
# 涨停家数 >> 跌停家数 → 市场亢奋 → 偏多(短期)
# 跌停家数 >> 涨停家数 → 市场恐慌 → 偏空(短期)
# 极端值可能预示反转
# ──────────────────────────────────────────────────────────────────

def limit_ratio_signal() -> SignalResult:
    """
    涨跌停比信号

    Returns:
        SignalResult: 基于涨停/跌停家数比值打分
    """
    date = datetime.now().strftime("%Y%m%d")

    # 获取涨停数据
    rate_limit("akshare", 2.5)
    try:
        df_up = ak.stock_zt_pool_em(date=date)
        up_count = len(df_up) if df_up is not None else 0
    except Exception as e:
        logger.error(f"涨停数据获取失败: {e}")
        up_count = 0

    # 获取跌停数据
    rate_limit("akshare", 2.5)
    try:
        df_down = ak.stock_zt_pool_dtgc_em(date=date)
        down_count = len(df_down) if df_down is not None else 0
    except Exception as e:
        logger.error(f"跌停数据获取失败: {e}")
        down_count = 0

    total = up_count + down_count
    if total == 0:
        return SignalResult("涨跌停比", 50, Direction.HOLD, 0.0, "无涨跌停数据")

    ratio = up_count / total
    score = 50
    signals = []

    # 基础分: 涨停占比映射到分数
    if up_count > 0 and down_count == 0:
        score = 80
        signals.append(f"涨停{up_count}只, 无跌停, 极度亢奋")
    elif down_count > 0 and up_count == 0:
        score = 20
        signals.append(f"跌停{down_count}只, 无涨停, 极度恐慌")
    else:
        score = int(ratio * 100)
        signals.append(f"涨停{up_count}只 vs 跌停{down_count}只, 比值{ratio:.2f}")

    # 极端值修正: 过度亢奋可能是顶部信号
    if up_count > 80 and ratio > 0.9:
        score = max(55, score - 15)
        signals.append("涨停过多, 警惕短期见顶")
    elif down_count > 80 and ratio < 0.1:
        score = min(45, score + 15)
        signals.append("跌停过多, 可能超卖反弹")

    score = max(0, min(100, score))
    conf = 0.6 if total >= 20 else 0.3
    detail = " | ".join(signals)
    return SignalResult("涨跌停比", score, score_to_direction(score), conf, detail)


# ══════════════════════════════════════════════════════════════════
# 2. 换手率异常信号
# ══════════════════════════════════════════════════════════════════
# 换手率突然放大 → 资金活跃 → 可能有行情
# 换手率持续萎缩 → 无人关注 → 阴跌
# 结合价格方向判断: 放量涨=多, 放量跌=空, 缩量=中性
# ──────────────────────────────────────────────────────────────────

def turnover_signal(code: str, df: pd.DataFrame = None) -> SignalResult:
    """
    换手率异常信号

    Args:
        code: 股票代码
        df: 日线DataFrame(可选, 含turn列), 为None则自动获取

    Returns:
        SignalResult: 基于换手率异常程度打分
    """
    if df is None:
        from data.history import get_daily
        df = get_daily(code, start_date=(datetime.now() - timedelta(days=60)).strftime("%Y%m%d"))

    if df is None or len(df) < 20 or "turn" not in df.columns:
        return SignalResult("换手率", 50, Direction.HOLD, 0.0, "数据不足")

    turn = pd.to_numeric(df["turn"], errors="coerce").dropna()
    close = pd.to_numeric(df["close"], errors="coerce").dropna()

    if len(turn) < 20:
        return SignalResult("换手率", 50, Direction.HOLD, 0.0, "换手率数据不足")

    cur_turn = turn.iloc[-1]
    ma20_turn = turn.tail(20).mean()
    pct_rank = (turn < cur_turn).mean() * 100  # 当前换手率在历史中的分位

    # 价格方向
    price_change = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100 if len(close) >= 2 else 0

    score = 50
    signals = []

    # 换手率放大倍数
    if ma20_turn > 0:
        vol_ratio = cur_turn / ma20_turn
    else:
        vol_ratio = 1.0

    if vol_ratio > 2.0:
        # 换手率异常放大
        if price_change > 0:
            score = 75
            signals.append(f"放量上涨 换手{cur_turn:.1f}% 是均值{vol_ratio:.1f}倍")
        else:
            score = 30
            signals.append(f"放量下跌 换手{cur_turn:.1f}% 是均值{vol_ratio:.1f}倍")
    elif vol_ratio > 1.5:
        if price_change > 0:
            score = 65
            signals.append(f"温和放量涨 换手{cur_turn:.1f}%")
        elif price_change < -2:
            score = 38
            signals.append(f"温和放量跌 换手{cur_turn:.1f}%")
        else:
            score = 55
            signals.append(f"温和放量 换手{cur_turn:.1f}%")
    elif vol_ratio < 0.5:
        # 极度缩量
        score = 48
        signals.append(f"极度缩量 换手{cur_turn:.1f}% 仅均值{vol_ratio:.1f}倍")
    else:
        score = 50
        signals.append(f"换手率正常 {cur_turn:.1f}%")

    # 分位数加成
    if pct_rank > 90:
        signals.append(f"换手率处于{pct_rank:.0f}%分位(极高)")
    elif pct_rank < 10:
        signals.append(f"换手率处于{pct_rank:.0f}%分位(极低)")

    score = max(0, min(100, score))
    conf = 0.5 if vol_ratio > 1.5 or vol_ratio < 0.5 else 0.3
    detail = " | ".join(signals)
    return SignalResult("换手率", score, score_to_direction(score), conf, detail)


# ══════════════════════════════════════════════════════════════════
# 3. 量能信号
# ══════════════════════════════════════════════════════════════════
# 量比 = 当日成交量 / 近5日平均成交量
# 量比 > 2 → 明显放量
# 量比 < 0.5 → 明显缩量
# 结合价格方向: 放量突破=多, 放量破位=空
# ──────────────────────────────────────────────────────────────────

def volume_energy_signal(code: str, df: pd.DataFrame = None) -> SignalResult:
    """
    量能信号

    Args:
        code: 股票代码
        df: 日线DataFrame(可选), 为None则自动获取

    Returns:
        SignalResult: 基于量能变化打分
    """
    if df is None:
        from data.history import get_daily
        df = get_daily(code, start_date=(datetime.now() - timedelta(days=60)).strftime("%Y%m%d"))

    if df is None or len(df) < 10:
        return SignalResult("量能", 50, Direction.HOLD, 0.0, "数据不足")

    volume = pd.to_numeric(df["volume"], errors="coerce").dropna()
    close = pd.to_numeric(df["close"], errors="coerce").dropna()

    if len(volume) < 10:
        return SignalResult("量能", 50, Direction.HOLD, 0.0, "量能数据不足")

    cur_vol = volume.iloc[-1]
    ma5_vol = volume.tail(5).mean()
    ma20_vol = volume.tail(20).mean() if len(volume) >= 20 else ma5_vol

    # 量比
    vol_ratio = cur_vol / ma5_vol if ma5_vol > 0 else 1.0
    # 20日分位
    pct_rank = (volume.tail(20) < cur_vol).mean() * 100 if len(volume) >= 20 else 50

    # 价格方向
    price_chg = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100 if len(close) >= 2 else 0

    score = 50
    signals = []

    if vol_ratio > 2.5:
        # 极端放量
        if price_chg > 3:
            score = 80
            signals.append(f"巨量拉升 量比{vol_ratio:.1f} 涨{price_chg:.1f}%")
        elif price_chg < -3:
            score = 20
            signals.append(f"巨量杀跌 量比{vol_ratio:.1f} 跌{price_chg:.1f}%")
        elif price_chg > 0:
            score = 68
            signals.append(f"放量上涨 量比{vol_ratio:.1f}")
        else:
            score = 35
            signals.append(f"放量下跌 量比{vol_ratio:.1f}")
    elif vol_ratio > 1.5:
        if price_chg > 1:
            score = 65
            signals.append(f"温和放量涨 量比{vol_ratio:.1f}")
        elif price_chg < -1:
            score = 40
            signals.append(f"温和放量跌 量比{vol_ratio:.1f}")
        else:
            score = 52
            signals.append(f"温和放量 量比{vol_ratio:.1f}")
    elif vol_ratio < 0.4:
        score = 47
        signals.append(f"极度缩量 量比{vol_ratio:.1f}")
    elif vol_ratio < 0.7:
        score = 48
        signals.append(f"缩量 量比{vol_ratio:.1f}")
    else:
        score = 50
        signals.append(f"量能正常 量比{vol_ratio:.1f}")

    # 趋势加成: 连续放量/缩量
    if len(volume) >= 5:
        recent_avg = volume.tail(3).mean()
        older_avg = volume.iloc[-6:-3].mean()
        if older_avg > 0:
            trend_ratio = recent_avg / older_avg
            if trend_ratio > 1.3:
                signals.append("近3日量能递增")
                score += 5 if price_chg > 0 else -3
            elif trend_ratio < 0.7:
                signals.append("近3日量能递减")

    signals.append(f"量比分位{pct_rank:.0f}%")
    score = max(0, min(100, score))
    conf = 0.55 if vol_ratio > 1.5 or vol_ratio < 0.5 else 0.35
    detail = " | ".join(signals)
    return SignalResult("量能", score, score_to_direction(score), conf, detail)


# ══════════════════════════════════════════════════════════════════
# 综合情绪信号
# ══════════════════════════════════════════════════════════════════

def emotion_signal(code: str, df: pd.DataFrame = None) -> SignalResult:
    """
    综合情绪信号

    融合涨跌停比 + 换手率异常 + 量能三个维度

    Args:
        code: 股票代码
        df: 日线DataFrame(可选)

    Returns:
        SignalResult: 综合情绪面分数
    """
    signals_list = []

    # 涨跌停比(市场层面)
    lr = limit_ratio_signal()
    signals_list.append(lr)

    # 换手率异常(个股层面)
    tr = turnover_signal(code, df)
    signals_list.append(tr)

    # 量能(个股层面)
    ve = volume_energy_signal(code, df)
    signals_list.append(ve)

    # 加权平均
    weights = [0.35, 0.30, 0.35]
    scores = [s.score for s in signals_list]
    confs = [s.confidence for s in signals_list]

    final_score = sum(s * w for s, w in zip(scores, weights))
    final_conf = sum(c * w for c, w in zip(confs, weights))

    details = [f"{sig.name}={sig.score}" for sig in signals_list]
    detail = " | ".join(details)

    return SignalResult(
        "情绪综合",
        int(final_score),
        score_to_direction(int(final_score)),
        round(final_conf, 2),
        detail,
    )


# 测试
if __name__ == "__main__":
    print("情绪信号测试")
    print("=" * 60)

    print("\n【1】涨跌停比信号")
    r = limit_ratio_signal()
    print(r)

    print("\n【2】换手率信号(茅台)")
    r = turnover_signal("600519")
    print(r)

    print("\n【3】量能信号(茅台)")
    r = volume_energy_signal("600519")
    print(r)

    print("\n【4】综合情绪信号(茅台)")
    r = emotion_signal("600519")
    print(r)
