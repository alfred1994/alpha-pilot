"""
基本面信号模块
====================================================================
信号源:
  1. PE分位数 - 市盈率在历史区间中的位置
  2. PB分位数 - 市净率在历史区间中的位置
  3. 综合估值 - PE+PB加权分位

估值逻辑:
  PE/PB处于历史低位(20%以下) → 低估 → 偏多
  PE/PB处于历史高位(80%以上) → 高估 → 偏空
  PE为负(亏损) → 基本面差 → 偏空
  PB<1(破净) → 可能低估或基本面恶化

输入: 股票代码 / 日线数据(含peTTM/pbMRQ)
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
# 1. PE分位数信号
# ══════════════════════════════════════════════════════════════════
# 使用baostock历史PE数据计算分位数
# PE分位 < 20% → 低估 → 高分
# PE分位 > 80% → 高估 → 低分
# PE < 0 (亏损) → 特殊处理
# ──────────────────────────────────────────────────────────────────

def pe_percentile_signal(code: str, df: pd.DataFrame = None) -> SignalResult:
    """
    PE分位数信号

    Args:
        code: 股票代码
        df: 日线DataFrame(含peTTM列), 为None则自动获取

    Returns:
        SignalResult: 基于PE历史分位打分
    """
    if df is None or "peTTM" not in df.columns:
        # 从baostock获取含PE的历史数据
        from data.history import get_daily
        df = get_daily(code, start_date=(datetime.now() - timedelta(days=730)).strftime("%Y%m%d"), simple=False)

    if df is None or len(df) < 60 or "peTTM" not in df.columns:
        return SignalResult("PE分位", 50, Direction.HOLD, 0.0, "PE数据不足")

    pe = pd.to_numeric(df["peTTM"], errors="coerce").dropna()
    pe = pe[pe > 0]  # 排除负值(亏损)

    if len(pe) < 30:
        return SignalResult("PE分位", 50, Direction.HOLD, 0.0, "有效PE数据不足")

    cur_pe = pe.iloc[-1]
    percentile = (pe < cur_pe).mean() * 100

    score = 50
    signals = []

    # 分位映射到分数: 低分位=高分(低估), 高分位=低分(高估)
    if percentile < 10:
        score = 80
        signals.append(f"PE={cur_pe:.1f} 处于{percentile:.0f}%分位(极度低估)")
    elif percentile < 20:
        score = 72
        signals.append(f"PE={cur_pe:.1f} 处于{percentile:.0f}%分位(低估)")
    elif percentile < 40:
        score = 60
        signals.append(f"PE={cur_pe:.1f} 处于{percentile:.0f}%分位(偏低)")
    elif percentile < 60:
        score = 50
        signals.append(f"PE={cur_pe:.1f} 处于{percentile:.0f}%分位(合理)")
    elif percentile < 80:
        score = 40
        signals.append(f"PE={cur_pe:.1f} 处于{percentile:.0f}%分位(偏高)")
    elif percentile < 90:
        score = 30
        signals.append(f"PE={cur_pe:.1f} 处于{percentile:.0f}%分位(高估)")
    else:
        score = 22
        signals.append(f"PE={cur_pe:.1f} 处于{percentile:.0f}%分位(极度高估)")

    # PE绝对值修正
    if cur_pe > 200:
        score = max(20, score - 10)
        signals.append(f"PE>{200}, 估值过高")
    elif 0 < cur_pe < 10:
        score = min(85, score + 5)
        signals.append(f"PE<10, 估值极低")

    score = max(0, min(100, score))
    conf = 0.55 if len(pe) >= 120 else 0.4
    detail = " | ".join(signals)
    return SignalResult("PE分位", score, score_to_direction(score), conf, detail)


# ══════════════════════════════════════════════════════════════════
# 2. PB分位数信号
# ══════════════════════════════════════════════════════════════════
# PB<1 → 破净, 可能低估(银行)或基本面恶化(地产)
# PB分位 < 20% → 低估
# PB分位 > 80% → 高估
# ──────────────────────────────────────────────────────────────────

def pb_percentile_signal(code: str, df: pd.DataFrame = None) -> SignalResult:
    """
    PB分位数信号

    Args:
        code: 股票代码
        df: 日线DataFrame(含pbMRQ列), 为None则自动获取

    Returns:
        SignalResult: 基于PB历史分位打分
    """
    if df is None or "pbMRQ" not in df.columns:
        from data.history import get_daily
        df = get_daily(code, start_date=(datetime.now() - timedelta(days=730)).strftime("%Y%m%d"), simple=False)

    if df is None or len(df) < 60 or "pbMRQ" not in df.columns:
        return SignalResult("PB分位", 50, Direction.HOLD, 0.0, "PB数据不足")

    pb = pd.to_numeric(df["pbMRQ"], errors="coerce").dropna()
    pb = pb[pb > 0]

    if len(pb) < 30:
        return SignalResult("PB分位", 50, Direction.HOLD, 0.0, "有效PB数据不足")

    cur_pb = pb.iloc[-1]
    percentile = (pb < cur_pb).mean() * 100

    score = 50
    signals = []

    if percentile < 10:
        score = 80
        signals.append(f"PB={cur_pb:.2f} 处于{percentile:.0f}%分位(极度低估)")
    elif percentile < 20:
        score = 72
        signals.append(f"PB={cur_pb:.2f} 处于{percentile:.0f}%分位(低估)")
    elif percentile < 40:
        score = 60
        signals.append(f"PB={cur_pb:.2f} 处于{percentile:.0f}%分位(偏低)")
    elif percentile < 60:
        score = 50
        signals.append(f"PB={cur_pb:.2f} 处于{percentile:.0f}%分位(合理)")
    elif percentile < 80:
        score = 40
        signals.append(f"PB={cur_pb:.2f} 处于{percentile:.0f}%分位(偏高)")
    elif percentile < 90:
        score = 30
        signals.append(f"PB={cur_pb:.2f} 处于{percentile:.0f}%分位(高估)")
    else:
        score = 22
        signals.append(f"PB={cur_pb:.2f} 处于{percentile:.0f}%分位(极度高估)")

    # 破净修正
    if cur_pb < 1.0:
        score = min(85, score + 8)
        signals.append(f"PB<1(破净), 资产折价")

    score = max(0, min(100, score))
    conf = 0.5 if len(pb) >= 120 else 0.35
    detail = " | ".join(signals)
    return SignalResult("PB分位", score, score_to_direction(score), conf, detail)


# ══════════════════════════════════════════════════════════════════
# 3. 综合基本面信号
# ══════════════════════════════════════════════════════════════════

def fundamental_signal(code: str, df: pd.DataFrame = None) -> SignalResult:
    """
    综合基本面信号

    融合PE分位 + PB分位, 加权计算基本面分数

    Args:
        code: 股票代码
        df: 日线DataFrame(含peTTM/pbMRQ列), 为None则自动获取

    Returns:
        SignalResult: 综合基本面分数
    """
    # 获取含PE/PB的数据(一次获取, 两个信号共用)
    if df is None or "peTTM" not in df.columns or "pbMRQ" not in df.columns:
        from data.history import get_daily
        df = get_daily(code, start_date=(datetime.now() - timedelta(days=730)).strftime("%Y%m%d"), simple=False)

    pe_sig = pe_percentile_signal(code, df)
    pb_sig = pb_percentile_signal(code, df)

    # PE权重60%, PB权重40%
    final_score = pe_sig.score * 0.6 + pb_sig.score * 0.4
    final_conf = pe_sig.confidence * 0.6 + pb_sig.confidence * 0.4

    detail = f"{pe_sig.name}={pe_sig.score} | {pb_sig.name}={pb_sig.score}"

    return SignalResult(
        "基本面",
        int(final_score),
        score_to_direction(int(final_score)),
        round(final_conf, 2),
        detail,
    )


# 测试
if __name__ == "__main__":
    print("基本面信号测试")
    print("=" * 60)

    code = "600519"
    print(f"\n【1】PE分位信号({code})")
    r = pe_percentile_signal(code)
    print(r)

    print(f"\n【2】PB分位信号({code})")
    r = pb_percentile_signal(code)
    print(r)

    print(f"\n【3】综合基本面信号({code})")
    r = fundamental_signal(code)
    print(r)
