"""
高级技术信号模块
====================================================================
信号源:
  1. Ichimoku Cloud（一目均衡表）- 五条线 + 云图信号
  2. Volume Profile（成交量分布）- POC/VAH/VAL/HVN/LVN
  3. Market Structure（市场结构）- HH/HL/LH/LL + BOS/CHoCH
  4. Wyckoff 积累/派发检测 - Spring/UT 模式
  5. 蜡烛图形态识别 - 吞没/十字星/三白兵三黑鸦
  6. Fibonacci Clusters - 多重回撤汇聚区域

输入: DataFrame(date, open, high, low, close, volume)
输出: SignalResult(name, score, direction, confidence, detail)
====================================================================
"""
import pandas as pd
import numpy as np
from typing import List, Tuple, Optional
from signals import SignalResult, Direction, score_to_direction, logger


# ──────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────

def _ensure_columns(df: pd.DataFrame) -> bool:
    """检查必要列是否存在"""
    required = {"date", "open", "high", "low", "close", "volume"}
    return required.issubset(set(df.columns))


def _safe_float(s: pd.Series) -> pd.Series:
    """安全转 float，NaN 填前值"""
    return s.astype(float).ffill()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range"""
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float).shift(1)
    tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ══════════════════════════════════════════════════════════════════
# 1. Ichimoku Cloud（一目均衡表）
# ══════════════════════════════════════════════════════════════════
#
# 五条线:
#   Tenkan-sen  (转换线) = (9日最高 + 9日最低) / 2
#   Kijun-sen   (基准线) = (26日最高 + 26日最低) / 2
#   Senkou Span A (先行A) = (转换线 + 基准线) / 2，前移 26 日
#   Senkou Span B (先行B) = (52日最高 + 52日最低) / 2，前移 26 日
#   Chikou Span  (迟行线) = 收盘价后移 26 日
#
# 信号逻辑:
#   - 价格在云上 → 多头；价格在云下 → 空头
#   - 转换线金叉基准线 → 做多；死叉 → 做空
#   - 云由红转绿（A>B）→ 多头加强
#   - 迟行线在价格之上 → 多头确认
# ──────────────────────────────────────────────────────────────────

def ichimoku_signal(
    df: pd.DataFrame,
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
    displacement: int = 26,
) -> SignalResult:
    """
    一目均衡表信号

    Args:
        df: OHLCV DataFrame
        tenkan_period: 转换线周期，默认 9
        kijun_period:  基准线周期，默认 26
        senkou_b_period: 先行 B 周期，默认 52
        displacement: 云图位移，默认 26

    Returns:
        SignalResult: 综合云图位置、交叉、云色变化打分
    """
    min_len = max(senkou_b_period, displacement) + 10
    if not _ensure_columns(df) or len(df) < min_len:
        return SignalResult("一目均衡表", 50, Direction.HOLD, 0.0, "数据不足")

    high = _safe_float(df["high"])
    low = _safe_float(df["low"])
    close = _safe_float(df["close"])

    # --- 计算五条线 ---
    tenkan = (high.rolling(tenkan_period).max() + low.rolling(tenkan_period).min()) / 2
    kijun = (high.rolling(kijun_period).max() + low.rolling(kijun_period).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(displacement)
    senkou_b = ((high.rolling(senkou_b_period).max() + low.rolling(senkou_b_period).min()) / 2).shift(displacement)
    chikou = close.shift(-displacement)

    # 当前值
    cur_close = close.iloc[-1]
    cur_tenkan = tenkan.iloc[-1]
    cur_kijun = kijun.iloc[-1]
    cur_sa = senkou_a.iloc[-displacement] if len(df) > displacement else np.nan  # 当前 K 线对应的云
    cur_sb = senkou_b.iloc[-displacement] if len(df) > displacement else np.nan
    prev_tenkan = tenkan.iloc[-2]
    prev_kijun = kijun.iloc[-2]

    if pd.isna(cur_tenkan) or pd.isna(cur_kijun) or pd.isna(cur_sa) or pd.isna(cur_sb):
        return SignalResult("一目均衡表", 50, Direction.HOLD, 0.0, "指标计算中")

    # --- 评分逻辑 ---
    score = 50
    signals = []

    # (1) 价格 vs 云 → 最大 ±20 分
    cloud_top = max(cur_sa, cur_sb)
    cloud_bot = min(cur_sa, cur_sb)
    if cur_close > cloud_top:
        dist = (cur_close - cloud_top) / cloud_top * 100
        score += min(20, int(dist * 4))
        signals.append(f"价格在云上+{dist:.1f}%")
    elif cur_close < cloud_bot:
        dist = (cloud_bot - cur_close) / cloud_bot * 100
        score -= min(20, int(dist * 4))
        signals.append(f"价格在云下-{dist:.1f}%")
    else:
        signals.append("价格在云中(震荡)")

    # (2) 转换线/基准线交叉 → ±15 分
    if prev_tenkan <= prev_kijun and cur_tenkan > cur_kijun:
        score += 15
        signals.append("转换线金叉基准线")
    elif prev_tenkan >= prev_kijun and cur_tenkan < cur_kijun:
        score -= 15
        signals.append("转换线死叉基准线")
    elif cur_tenkan > cur_kijun:
        score += 5
        signals.append("转换线>基准线")
    else:
        score -= 5
        signals.append("转换线<基准线")

    # (3) 云色（先行 A vs B）→ ±10 分
    if cur_sa > cur_sb:
        score += 10
        signals.append("云色偏多(A>B)")
    else:
        score -= 10
        signals.append("云色偏空(B>A)")

    # (4) 迟行线确认 → ±5 分（用历史数据近似）
    if len(df) > displacement + 1:
        chikou_val = close.iloc[-displacement - 1]
        past_close = close.iloc[-2 * displacement - 1] if len(df) > 2 * displacement + 1 else np.nan
        if not pd.isna(past_close):
            if chikou_val > past_close:
                score += 5
                signals.append("迟行线确认多头")
            else:
                score -= 5
                signals.append("迟行线确认空头")

    score = max(0, min(100, score))
    conf = 0.65 if abs(score - 50) > 15 else 0.45
    detail = " | ".join(signals)
    return SignalResult("一目均衡表", score, score_to_direction(score), conf, detail)


# ══════════════════════════════════════════════════════════════════
# 2. Volume Profile（成交量分布）
# ══════════════════════════════════════════════════════════════════
#
# 将价格区间离散化为 N 个 bin，统计每个 bin 的累计成交量。
#   POC  (Point of Control)   = 成交量最大的价格
#   VAH  (Value Area High)    = 70% 成交量区间的上沿
#   VAL  (Value Area Low)     = 70% 成交量区间的下沿
#   HVN  (High Volume Node)   = 成交量密集区（支撑/阻力）
#   LVN  (Low Volume Node)    = 成交量稀疏区（价格快速穿越）
#
# 信号逻辑:
#   - 当前价在 POC 附近 → 震荡 → HOLD
#   - 当前价在 VAL 附近 → 支撑 → BUY
#   - 当前价在 VAH 附近 → 阻力 → SELL
#   - 当前价在 LVN 附近 → 可能快速突破 → 方向看趋势
# ──────────────────────────────────────────────────────────────────

def volume_profile_signal(
    df: pd.DataFrame,
    lookback: int = 60,
    bins: int = 40,
    value_pct: float = 0.70,
) -> SignalResult:
    """
    成交量分布信号

    Args:
        df: OHLCV DataFrame
        lookback: 回看天数，默认 60
        bins: 价格区间分箱数，默认 40
        value_pct: 核心成交量占比，默认 70%

    Returns:
        SignalResult: 基于 POC/VAH/VAL 位置关系打分
    """
    if not _ensure_columns(df) or len(df) < lookback:
        return SignalResult("成交量分布", 50, Direction.HOLD, 0.0, "数据不足")

    tail = df.tail(lookback).copy()
    close = _safe_float(tail["close"])
    high = _safe_float(tail["high"])
    low = _safe_float(tail["low"])
    volume = _safe_float(tail["volume"])

    cur_price = close.iloc[-1]

    # 用典型价格 (H+L+C)/3 作为每根 K 线的价格代表
    typical = (high + low + close) / 3

    # 构建成交量分布直方图
    price_min, price_max = low.min(), high.max()
    if price_max == price_min:
        return SignalResult("成交量分布", 50, Direction.HOLD, 0.0, "价格无波动")

    bin_edges = np.linspace(price_min, price_max, bins + 1)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    vol_hist = np.zeros(bins)

    # 将每根 K 线的成交量按典型价格分配到对应 bin
    for i in range(len(typical)):
        p = typical.iloc[i]
        v = volume.iloc[i]
        idx = int((p - price_min) / (price_max - price_min) * (bins - 1))
        idx = max(0, min(bins - 1, idx))
        vol_hist[idx] += v

    # POC: 成交量最大的 bin
    poc_idx = np.argmax(vol_hist)
    poc_price = bin_centers[poc_idx]

    # VAH / VAL: 累计 70% 成交量的上下沿
    total_vol = vol_hist.sum()
    target_vol = total_vol * value_pct
    sorted_idx = np.argsort(vol_hist)[::-1]  # 按成交量降序
    cum_vol = 0
    va_indices = []
    for idx in sorted_idx:
        cum_vol += vol_hist[idx]
        va_indices.append(idx)
        if cum_vol >= target_vol:
            break
    va_indices.sort()
    val_price = bin_centers[va_indices[0]]
    vah_price = bin_centers[va_indices[-1]]

    # HVN / LVN: 识别密集/稀疏区域
    threshold_high = np.percentile(vol_hist[vol_hist > 0], 75)
    threshold_low = np.percentile(vol_hist[vol_hist > 0], 25)
    hvn_prices = bin_centers[vol_hist >= threshold_high]
    lvn_prices = bin_centers[vol_hist <= threshold_low]

    # --- 评分逻辑 ---
    score = 50
    signals = []

    # 距离各关键位的百分比距离
    dist_poc = abs(cur_price - poc_price) / poc_price * 100
    dist_val = abs(cur_price - val_price) / val_price * 100
    dist_vah = abs(cur_price - vah_price) / vah_price * 100

    if dist_poc < 1.0:
        # 靠近 POC → 震荡区
        score = 50
        signals.append(f"价格贴近POC({poc_price:.2f}), 震荡")
    elif cur_price < val_price:
        # 低于 VAL → 超卖/突破下行
        score = max(20, 35 - int(dist_val * 3))
        signals.append(f"价格低于VAL({val_price:.2f}), 超卖区")
    elif cur_price > vah_price:
        # 高于 VAH → 超买/突破上行
        score = min(80, 65 + int(dist_vah * 3))
        signals.append(f"价格高于VAH({vah_price:.2f}), 超买区")
    elif cur_price < poc_price:
        # 在 VAL 和 POC 之间 → 偏多（支撑区）
        ratio = (cur_price - val_price) / (poc_price - val_price)
        score = 45 + int(ratio * 10)
        signals.append(f"VAL-POC之间, 偏支撑")
    else:
        # 在 POC 和 VAH 之间 → 偏空（阻力区）
        ratio = (cur_price - poc_price) / (vah_price - poc_price)
        score = 55 - int(ratio * 10)
        signals.append(f"POC-VAH之间, 偏阻力")

    # LVN 加成: 价格在 LVN 附近 → 可能快速穿越
    if len(lvn_prices) > 0:
        min_lvn_dist = min(abs(cur_price - lp) / lp * 100 for lp in lvn_prices)
        if min_lvn_dist < 1.5:
            signals.append(f"靠近LVN(低量区), 可能快速穿越")
            # LVN 本身不改变方向，但增加置信度

    signals.append(f"POC={poc_price:.2f} VAH={vah_price:.2f} VAL={val_price:.2f}")

    score = max(0, min(100, score))
    conf = 0.55
    detail = " | ".join(signals)
    return SignalResult("成交量分布", score, score_to_direction(score), conf, detail)


# ══════════════════════════════════════════════════════════════════
# 3. Market Structure（市场结构）
# ══════════════════════════════════════════════════════════════════
#
# 使用 ZigZag 算法提取显著高低点，然后识别:
#   HH (Higher High) / HL (Higher Low) → 上升趋势
#   LH (Lower High)  / LL (Lower Low)  → 下降趋势
#   BOS  (Break of Structure)  → 趋势延续
#   CHoCH (Change of Character) → 趋势反转
#
# 信号逻辑:
#   - 连续 HH+HL → 强多头
#   - 连续 LH+LL → 强空头
#   - CHoCH 出现 → 反转信号
#   - BOS 出现 → 延续信号
# ──────────────────────────────────────────────────────────────────

def market_structure_signal(
    df: pd.DataFrame,
    zigzag_pct: float = 3.0,
    lookback: int = 120,
) -> SignalResult:
    """
    市场结构信号

    Args:
        df: OHLCV DataFrame
        zigzag_pct: ZigZag 最小波动百分比，默认 3%
        lookback: 回看天数，默认 120

    Returns:
        SignalResult: 基于 HH/HL/LH/LL + BOS/CHoCH 打分
    """
    if not _ensure_columns(df) or len(df) < 30:
        return SignalResult("市场结构", 50, Direction.HOLD, 0.0, "数据不足")

    tail = df.tail(lookback).copy().reset_index(drop=True)
    high = _safe_float(tail["high"]).values
    low = _safe_float(tail["low"]).values
    close = _safe_float(tail["close"]).values
    n = len(tail)

    # --- ZigZag: 提取显著转折点 ---
    # direction: 1=上升段, -1=下降段
    pivots = []  # [(index, price, "H"/"L")]
    direction = 0
    last_pivot_price = close[0]
    last_pivot_idx = 0
    last_pivot_type = "L"  # 假设从低点开始

    for i in range(1, n):
        if direction == 0:
            # 初始化方向
            if high[i] - last_pivot_price >= last_pivot_price * zigzag_pct / 100:
                pivots.append((last_pivot_idx, last_pivot_price, "L"))
                direction = 1
                last_pivot_price = high[i]
                last_pivot_idx = i
                last_pivot_type = "H"
            elif last_pivot_price - low[i] >= last_pivot_price * zigzag_pct / 100:
                pivots.append((last_pivot_idx, last_pivot_price, "H"))
                direction = -1
                last_pivot_price = low[i]
                last_pivot_idx = i
                last_pivot_type = "L"
        elif direction == 1:
            # 上升段中，寻找更高高点
            if high[i] > last_pivot_price:
                last_pivot_price = high[i]
                last_pivot_idx = i
            # 反转: 跌幅达到阈值
            elif last_pivot_price - low[i] >= last_pivot_price * zigzag_pct / 100:
                pivots.append((last_pivot_idx, last_pivot_price, "H"))
                direction = -1
                last_pivot_price = low[i]
                last_pivot_idx = i
                last_pivot_type = "L"
        elif direction == -1:
            # 下降段中，寻找更低低点
            if low[i] < last_pivot_price:
                last_pivot_price = low[i]
                last_pivot_idx = i
            # 反转: 涨幅达到阈值
            elif high[i] - last_pivot_price >= last_pivot_price * zigzag_pct / 100:
                pivots.append((last_pivot_idx, last_pivot_price, "L"))
                direction = 1
                last_pivot_price = high[i]
                last_pivot_idx = i
                last_pivot_type = "H"

    # 加入最后一个转折点
    if last_pivot_type == "H":
        pivots.append((last_pivot_idx, last_pivot_price, "H"))
    else:
        pivots.append((last_pivot_idx, last_pivot_price, "L"))

    if len(pivots) < 4:
        return SignalResult("市场结构", 50, Direction.HOLD, 0.2, "转折点不足, 无法判断结构")

    # --- 分析最近的结构 ---
    recent = pivots[-6:]  # 最近 6 个转折点
    highs = [(i, p) for i, p, t in recent if t == "H"]
    lows = [(i, p) for i, p, t in recent if t == "L"]

    score = 50
    signals = []

    # 判断 HH/HL 或 LH/LL
    if len(highs) >= 2:
        if highs[-1][1] > highs[-2][1]:
            score += 12
            signals.append(f"HH({highs[-1][1]:.2f}>{highs[-2][1]:.2f})")
        else:
            score -= 12
            signals.append(f"LH({highs[-1][1]:.2f}<{highs[-2][1]:.2f})")

    if len(lows) >= 2:
        if lows[-1][1] > lows[-2][1]:
            score += 12
            signals.append(f"HL({lows[-1][1]:.2f}>{lows[-2][1]:.2f})")
        else:
            score -= 12
            signals.append(f"LL({lows[-1][1]:.2f}<{lows[-2][1]:.2f})")

    # BOS / CHoCH 检测
    cur_close = close[-1]
    if len(highs) >= 1 and len(lows) >= 1:
        last_high = highs[-1][1]
        last_low = lows[-1][1]

        # BOS (Break of Structure): 趋势延续
        # 上升趋势中突破前高 → Bullish BOS
        if score > 50 and cur_close > last_high:
            score += 10
            signals.append(f"BOS↑(突破{last_high:.2f})")
        # 下降趋势中跌破前低 → Bearish BOS
        elif score < 50 and cur_close < last_low:
            score -= 10
            signals.append(f"BOS↓(跌破{last_low:.2f})")

        # CHoCH (Change of Character): 趋势反转
        # 上升趋势中跌破前低 → Bearish CHoCH
        elif score > 50 and cur_close < last_low:
            score -= 15
            signals.append(f"CHoCH↓(跌破前低{last_low:.2f})")
        # 下降趋势中突破前高 → Bullish CHoCH
        elif score < 50 and cur_close > last_high:
            score += 15
            signals.append(f"CHoCH↑(突破前高{last_high:.2f})")

    score = max(0, min(100, score))
    conf = 0.6 if abs(score - 50) > 15 else 0.4
    detail = " | ".join(signals) if signals else "结构不明确"
    return SignalResult("市场结构", score, score_to_direction(score), conf, detail)


# ══════════════════════════════════════════════════════════════════
# 4. Wyckoff 积累/派发检测
# ══════════════════════════════════════════════════════════════════
#
# 积累模式 (Accumulation):
#   Phase A: 抛售停止 (Selling Climax → SC)
#   Phase B: 构建因果 (Trading Range)
#   Phase C: 测试 (Spring) — 价格短暂跌破支撑后快速拉回 → 买入信号
#   Phase D: 标记上涨 (Sign of Strength → SOS)
#   Phase E: 上涨离开区间
#
# 派发模式 (Distribution):
#   Phase A: 买入停止 (Buying Climax → BC)
#   Phase B: 构建因果
#   Phase C: 测试 (Upthrust → UT) — 价格短暂突破阻力后快速回落 → 卖出信号
#   Phase D: 标记下跌 (Sign of Weakness → SOW)
#   Phase E: 下跌离开区间
#
# 信号逻辑:
#   - 检测到 Spring → 强买入
#   - 检测到 UT   → 强卖出
#   - 量价配合确认积累/派发阶段
# ──────────────────────────────────────────────────────────────────

def wyckoff_signal(
    df: pd.DataFrame,
    lookback: int = 90,
    range_pct: float = 8.0,
) -> SignalResult:
    """
    Wyckoff 积累/派发信号

    Args:
        df: OHLCV DataFrame
        lookback: 回看天数，默认 90
        range_pct: 横盘区间幅度百分比阈值，默认 8%

    Returns:
        SignalResult: 基于 Spring/UT 模式打分
    """
    if not _ensure_columns(df) or len(df) < lookback:
        return SignalResult("Wyckoff", 50, Direction.HOLD, 0.0, "数据不足")

    tail = df.tail(lookback).copy().reset_index(drop=True)
    close = _safe_float(tail["close"]).values
    high = _safe_float(tail["high"]).values
    low = _safe_float(tail["low"]).values
    volume = _safe_float(tail["volume"]).values
    n = len(tail)

    cur_price = close[-1]

    # --- 识别横盘区间 (Trading Range) ---
    # 用最近 lookback 的最高/最低来定义区间
    range_high = np.max(high)
    range_low = np.min(low)
    range_mid = (range_high + range_low) / 2
    range_width_pct = (range_high - range_low) / range_mid * 100

    # 区间太窄或太宽都不符合 Wyckoff 模式
    if range_width_pct < 3 or range_width_pct > 25:
        return SignalResult("Wyckoff", 50, Direction.HOLD, 0.3, f"区间幅度{range_width_pct:.1f}%, 不符合积累/派发形态")

    score = 50
    signals = []

    # 将数据分为前半段和后半段，判断趋势方向
    half = n // 2
    first_half_trend = np.polyfit(np.arange(half), close[:half], 1)[0]
    second_half_trend = np.polyfit(np.arange(n - half), close[half:], 1)[0]

    # 前半段下跌 + 后半段横盘/微涨 → 可能是积累
    is_accumulation = first_half_trend < 0 and abs(second_half_trend) < abs(first_half_trend) * 0.5
    # 前半段上涨 + 后半段横盘/微跌 → 可能是派发
    is_distribution = first_half_trend > 0 and abs(second_half_trend) < abs(first_half_trend) * 0.5

    # --- Spring 检测 (积累 Phase C) ---
    # Spring: 价格短暂跌破区间低点后快速拉回
    # 寻找: 最近 N 天内，low 跌破 range_low，但 close 收回 range_low 之上
    spring_window = min(20, n // 3)
    spring_detected = False
    for i in range(n - spring_window, n):
        if i < 1:
            continue
        # 当日 low 跌破支撑，但收盘收回
        if low[i] < range_low and close[i] > range_low:
            spring_detected = True
            spring_idx = i
            break
        # 前一日跌破，当日收回
        if low[i - 1] < range_low and close[i] > range_low:
            spring_detected = True
            spring_idx = i
            break

    # --- Upthrust (UT) 检测 (派发 Phase C) ---
    # UT: 价格短暂突破区间高点后快速回落
    ut_detected = False
    for i in range(n - spring_window, n):
        if i < 1:
            continue
        if high[i] > range_high and close[i] < range_high:
            ut_detected = True
            ut_idx = i
            break
        if high[i - 1] > range_high and close[i] < range_high:
            ut_detected = True
            ut_idx = i
            break

    # --- 量价分析 ---
    # 积累后期: 下跌缩量，上涨放量
    recent_vol = volume[-10:]
    older_vol = volume[-30:-10]
    vol_ratio = np.mean(recent_vol) / np.mean(older_vol) if np.mean(older_vol) > 0 else 1.0

    # 最近 10 天上涨 K 线的量 vs 下跌 K 线的量
    up_days = [i for i in range(n - 10, n) if close[i] > close[i - 1]] if n > 10 else []
    dn_days = [i for i in range(n - 10, n) if close[i] < close[i - 1]] if n > 10 else []
    avg_up_vol = np.mean([volume[i] for i in up_days]) if up_days else 0
    avg_dn_vol = np.mean([volume[i] for i in dn_days]) if dn_days else 0

    # --- 综合评分 ---
    if spring_detected:
        score = 75
        signals.append(f"Spring! 价格跌破支撑({range_low:.2f})后收回")
        if avg_up_vol > avg_dn_vol * 1.3:
            score = 82
            signals.append("上涨放量确认")
        conf = 0.75
    elif ut_detected:
        score = 25
        signals.append(f"Upthrust! 价格突破阻力({range_high:.2f})后回落")
        if avg_dn_vol > avg_up_vol * 1.3:
            score = 18
            signals.append("下跌放量确认")
        conf = 0.75
    elif is_accumulation:
        # 积累阶段，未出现 Spring
        if cur_price < range_mid:
            score = 58
            signals.append("积累阶段, 价格在区间下半部")
        else:
            score = 55
            signals.append("积累阶段, 价格在区间上半部")
        if vol_ratio > 1.2:
            score += 5
            signals.append("近期放量")
        conf = 0.45
    elif is_distribution:
        # 派发阶段，未出现 UT
        if cur_price > range_mid:
            score = 42
            signals.append("派发阶段, 价格在区间上半部")
        else:
            score = 45
            signals.append("派发阶段, 价格在区间下半部")
        if vol_ratio > 1.2:
            score -= 5
            signals.append("近期放量")
        conf = 0.45
    else:
        score = 50
        signals.append("未识别明确积累/派发形态")
        conf = 0.3

    signals.append(f"区间[{range_low:.2f}-{range_high:.2f}] 幅度{range_width_pct:.1f}%")
    score = max(0, min(100, score))
    detail = " | ".join(signals)
    return SignalResult("Wyckoff", score, score_to_direction(score), conf, detail)


# ══════════════════════════════════════════════════════════════════
# 5. 蜡烛图形态识别
# ══════════════════════════════════════════════════════════════════
#
# 识别以下经典形态:
#   - 看涨吞没 (Bullish Engulfing)
#   - 看跌吞没 (Bearish Engulfing)
#   - 十字星 (Doji) — 多空犹豫
#   - 三白兵 (Three White Soldiers) — 强势上涨
#   - 三黑鸦 (Three Black Crows) — 强势下跌
#   - 锤子线 (Hammer) — 底部反转
#   - 射击之星 (Shooting Star) — 顶部反转
#
# 信号逻辑:
#   检测到形态后根据类型和上下文（趋势位置）打分
# ──────────────────────────────────────────────────────────────────

def candlestick_signal(
    df: pd.DataFrame,
    trend_window: int = 20,
) -> SignalResult:
    """
    蜡烛图形态识别信号

    Args:
        df: OHLCV DataFrame
        trend_window: 趋势判断窗口，默认 20

    Returns:
        SignalResult: 基于识别到的蜡烛形态打分
    """
    if not _ensure_columns(df) or len(df) < trend_window + 5:
        return SignalResult("蜡烛形态", 50, Direction.HOLD, 0.0, "数据不足")

    o = _safe_float(df["open"])
    h = _safe_float(df["high"])
    l = _safe_float(df["low"])
    c = _safe_float(df["close"])

    # 趋势判断: 最近 trend_window 天的线性回归斜率
    trend_close = c.tail(trend_window).values
    trend_slope = np.polyfit(np.arange(len(trend_close)), trend_close, 1)[0]
    trend_up = trend_slope > 0
    trend_down = trend_slope < 0

    # 最近 3 根 K 线
    def candle(i):
        """返回 (body, upper_shadow, lower_shadow, is_bull, range)"""
        body = c.iloc[i] - o.iloc[i]
        upper = h.iloc[i] - max(o.iloc[i], c.iloc[i])
        lower = min(o.iloc[i], c.iloc[i]) - l.iloc[i]
        rng = h.iloc[i] - l.iloc[i]
        return body, upper, lower, body > 0, rng

    # 形态检测
    patterns = []

    # --- (1) 看涨吞没 (Bullish Engulfing) ---
    # 前一根阴线，当前阳线完全包住前一根实体
    if len(df) >= 2:
        b0, u0, l0, bull0, r0 = candle(-2)
        b1, u1, l1, bull1, r1 = candle(-1)
        if (not bull0 and bull1 and
                o.iloc[-1] <= c.iloc[-2] and c.iloc[-1] >= o.iloc[-2] and
                abs(b1) > abs(b0) * 0.8):
            strength = "强" if trend_down else "弱"
            patterns.append(("看涨吞没", 70 if trend_down else 60, 0.7 if trend_down else 0.5,
                             f"{strength}势下看涨吞没, 实体包住前阴线"))

    # --- (2) 看跌吞没 (Bearish Engulfing) ---
    if len(df) >= 2:
        b0, u0, l0, bull0, r0 = candle(-2)
        b1, u1, l1, bull1, r1 = candle(-1)
        if (bull0 and not bull1 and
                o.iloc[-1] >= c.iloc[-2] and c.iloc[-1] <= o.iloc[-2] and
                abs(b1) > abs(b0) * 0.8):
            strength = "强" if trend_up else "弱"
            patterns.append(("看跌吞没", 30 if trend_up else 40, 0.7 if trend_up else 0.5,
                             f"{strength}势下看跌吞没, 实体包住前阳线"))

    # --- (3) 十字星 (Doji) ---
    b, u, l_shadow, _, rng = candle(-1)
    if rng > 0 and abs(b) / rng < 0.1:
        # 上下影线长度比较
        if u > l_shadow * 2:
            patterns.append(("墓碑十字星", 30, 0.5, "上影线长, 上方压力大"))
        elif l_shadow > u * 2:
            patterns.append(("蜻蜓十字星", 70, 0.5, "下影线长, 下方支撑强"))
        else:
            patterns.append(("十字星", 50, 0.4, "多空犹豫, 等待方向确认"))

    # --- (4) 三白兵 (Three White Soldiers) ---
    if len(df) >= 3:
        soldiers = True
        for i in range(-3, 0):
            bi, _, _, bulli, _ = candle(i)
            if not bulli or bi <= 0:
                soldiers = False
                break
        # 每根收盘价逐步升高
        if soldiers and c.iloc[-3] < c.iloc[-2] < c.iloc[-1]:
            patterns.append(("三白兵", 78, 0.75, "连续三根阳线, 强势上涨"))

    # --- (5) 三黑鸦 (Three Black Crows) ---
    if len(df) >= 3:
        crows = True
        for i in range(-3, 0):
            bi, _, _, bulli, _ = candle(i)
            if bulli or bi >= 0:
                crows = False
                break
        if crows and c.iloc[-3] > c.iloc[-2] > c.iloc[-1]:
            patterns.append(("三黑鸦", 22, 0.75, "连续三根阴线, 强势下跌"))

    # --- (6) 锤子线 (Hammer) ---
    b, u, l_shadow, _, rng = candle(-1)
    if rng > 0 and l_shadow > abs(b) * 2 and u < abs(b) * 0.5:
        if trend_down:
            patterns.append(("锤子线", 68, 0.65, "下跌趋势中锤子线, 底部反转信号"))
        else:
            patterns.append(("锤子线", 55, 0.4, "非下跌趋势中锤子线, 信号减弱"))

    # --- (7) 射击之星 (Shooting Star) ---
    b, u, l_shadow, _, rng = candle(-1)
    if rng > 0 and u > abs(b) * 2 and l_shadow < abs(b) * 0.5:
        if trend_up:
            patterns.append(("射击之星", 32, 0.65, "上涨趋势中射击之星, 顶部反转信号"))
        else:
            patterns.append(("射击之星", 45, 0.4, "非上涨趋势中射击之星, 信号减弱"))

    # --- 综合结果 ---
    if not patterns:
        return SignalResult("蜡烛形态", 50, Direction.HOLD, 0.2, "最近无明显蜡烛形态")

    # 取最强信号（置信度最高）
    best = max(patterns, key=lambda p: p[2])
    name, score, conf, detail = best
    score = max(0, min(100, score))
    return SignalResult("蜡烛形态", score, score_to_direction(score), conf, f"{name}: {detail}")


# ══════════════════════════════════════════════════════════════════
# 6. Fibonacci Clusters（多重回撤汇聚区域）
# ══════════════════════════════════════════════════════════════════
#
# 思路:
#   对多个不同时间窗口的高低点计算 Fibonacci 回撤位，
#   找到多个回撤位汇聚的区域 (Cluster)。
#   汇聚的回撤位越多，该区域的支撑/阻力越强。
#
# 常用回撤比率: 0.236, 0.382, 0.500, 0.618, 0.786
#
# 信号逻辑:
#   - 当前价接近多头汇聚区 → 强支撑 → BUY
#   - 当前价接近空头汇聚区 → 强阻力 → SELL
#   - 汇聚区强度 = 回撤位聚集数量
# ──────────────────────────────────────────────────────────────────

def fibonacci_cluster_signal(
    df: pd.DataFrame,
    windows: List[int] = None,
    ratios: List[float] = None,
    cluster_tolerance: float = 1.5,
) -> SignalResult:
    """
    Fibonacci Clusters 信号

    Args:
        df: OHLCV DataFrame
        windows: 多个回看窗口，默认 [20, 40, 60, 120]
        ratios: Fibonacci 回撤比率，默认 [0.236, 0.382, 0.5, 0.618, 0.786]
        cluster_tolerance: 汇聚判定容差百分比，默认 1.5%

    Returns:
        SignalResult: 基于当前价与最近汇聚区的关系打分
    """
    if windows is None:
        windows = [20, 40, 60, 120]
    if ratios is None:
        ratios = [0.236, 0.382, 0.5, 0.618, 0.786]

    if not _ensure_columns(df) or len(df) < max(windows) + 5:
        return SignalResult("Fibonacci", 50, Direction.HOLD, 0.0, "数据不足")

    close = _safe_float(df["close"])
    high = _safe_float(df["high"])
    low = _safe_float(df["low"])
    cur_price = close.iloc[-1]

    # --- 计算所有回撤位 ---
    all_levels = []  # (price, direction, window)

    for w in windows:
        if len(df) < w:
            continue
        window_high = high.tail(w).max()
        window_low = low.tail(w).min()
        window_range = window_high - window_low

        if window_range == 0:
            continue

        # 判断窗口内趋势方向
        trend = close.iloc[-1] - close.iloc[-w]
        is_uptrend = trend > 0

        for r in ratios:
            if is_uptrend:
                # 上升趋势: 回撤位 = 高点 - 范围 * 比率 (支撑位)
                level = window_high - window_range * r
                all_levels.append((level, "support", w))
            else:
                # 下降趋势: 回撤位 = 低点 + 范围 * 比率 (阻力位)
                level = window_low + window_range * r
                all_levels.append((level, "resistance", w))

    if not all_levels:
        return SignalResult("Fibonacci", 50, Direction.HOLD, 0.2, "无法计算回撤位")

    # --- 寻找 Clusters (汇聚区) ---
    # 将回撤位排序，找出聚集密度最高的区域
    sorted_levels = sorted(all_levels, key=lambda x: x[0])

    clusters = []  # [(center_price, count, support_count, resistance_count)]
    i = 0
    while i < len(sorted_levels):
        cluster_group = [sorted_levels[i]]
        j = i + 1
        while j < len(sorted_levels):
            # 在容差范围内
            dist_pct = abs(sorted_levels[j][0] - sorted_levels[i][0]) / sorted_levels[i][0] * 100
            if dist_pct <= cluster_tolerance:
                cluster_group.append(sorted_levels[j])
                j += 1
            else:
                break

        if len(cluster_group) >= 2:
            center = np.mean([l[0] for l in cluster_group])
            sup_count = sum(1 for l in cluster_group if l[1] == "support")
            res_count = sum(1 for l in cluster_group if l[1] == "resistance")
            clusters.append((center, len(cluster_group), sup_count, res_count))

        i = j

    if not clusters:
        # 没有找到汇聚区，用最强单个回撤位
        closest = min(all_levels, key=lambda x: abs(x[0] - cur_price))
        dist = abs(closest[0] - cur_price) / cur_price * 100
        direction = closest[1]
        if dist < 2:
            score = 65 if direction == "support" else 35
            conf = 0.4
        else:
            score = 50
            conf = 0.2
        detail = f"无汇聚区, 最近Fib位{closest[0]:.2f}({direction}) 距{dist:.1f}%"
        return SignalResult("Fibonacci", score, score_to_direction(score), conf, detail)

    # --- 评分 ---
    # 找到最近的汇聚区
    closest_cluster = min(clusters, key=lambda x: abs(x[0] - cur_price))
    center, count, sup_cnt, res_cnt = closest_cluster
    dist_pct = abs(center - cur_price) / center * 100

    score = 50
    signals = []

    if dist_pct < cluster_tolerance:
        # 价格在汇聚区内
        if sup_cnt > res_cnt:
            # 支撑型汇聚
            score = 50 + count * 8
            signals.append(f"价格在支撑汇聚区({center:.2f}), {count}个Fib重叠")
        elif res_cnt > sup_cnt:
            # 阻力型汇聚
            score = 50 - count * 8
            signals.append(f"价格在阻力汇聚区({center:.2f}), {count}个Fib重叠")
        else:
            signals.append(f"价格在中性汇聚区({center:.2f}), {count}个Fib重叠")
    elif cur_price < center:
        # 价格在汇聚区下方
        if sup_cnt > res_cnt:
            # 支撑区在上方 → 价格可能向支撑反弹
            score = 55 + min(count * 3, 15)
            signals.append(f"支撑汇聚区({center:.2f})在上方, {count}个Fib重叠, 距{dist_pct:.1f}%")
        else:
            score = 45
            signals.append(f"汇聚区({center:.2f})在上方, 距{dist_pct:.1f}%")
    else:
        # 价格在汇聚区上方
        if res_cnt > sup_cnt:
            # 阻力区在下方 → 价格可能向阻力回落
            score = 45 - min(count * 3, 15)
            signals.append(f"阻力汇聚区({center:.2f})在下方, {count}个Fib重叠, 距{dist_pct:.1f}%")
        else:
            score = 55
            signals.append(f"汇聚区({center:.2f})在下方, 距{dist_pct:.1f}%")

    # 汇聚强度加成
    if count >= 4:
        signals.append(f"强汇聚({count}个Fib重叠), 关键区域")
        conf = 0.7
    elif count >= 3:
        conf = 0.55
    else:
        conf = 0.4

    score = max(0, min(100, score))
    detail = " | ".join(signals)
    return SignalResult("Fibonacci", score, score_to_direction(score), conf, detail)


# ══════════════════════════════════════════════════════════════════
# 基础技术信号（向后兼容）
# ══════════════════════════════════════════════════════════════════

def ma_cross_signal(df: pd.DataFrame, short: int = 5, long: int = 20) -> SignalResult:
    """均线金叉/死叉信号（向后兼容）"""
    if not _ensure_columns(df) or len(df) < long + 5:
        return SignalResult("均线交叉", 50, Direction.HOLD, 0.0, "数据不足")

    close = _safe_float(df["close"])
    ma_short = close.rolling(short).mean()
    ma_long = close.rolling(long).mean()

    cur_short, cur_long = ma_short.iloc[-1], ma_long.iloc[-1]
    prev_short, prev_long = ma_short.iloc[-2], ma_long.iloc[-2]

    if pd.isna(cur_short) or pd.isna(cur_long):
        return SignalResult("均线交叉", 50, Direction.HOLD, 0.0, "均线计算中")

    golden = prev_short <= prev_long and cur_short > cur_long
    death = prev_short >= prev_long and cur_short < cur_long
    bull_align = cur_short > cur_long
    deviation = (cur_short - cur_long) / cur_long * 100

    if golden:
        score = min(80, 65 + int(abs(deviation) * 2))
        detail = f"金叉! MA{short}({cur_short:.2f})上穿MA{long}({cur_long:.2f})"
        conf = 0.8
    elif death:
        score = max(20, 35 - int(abs(deviation) * 2))
        detail = f"死叉! MA{short}({cur_short:.2f})下穿MA{long}({cur_long:.2f})"
        conf = 0.8
    elif bull_align:
        score = min(70, 55 + int(deviation))
        detail = f"多头排列 MA{short}>{long}, 偏离{deviation:.1f}%"
        conf = 0.5
    else:
        score = max(30, 45 + int(deviation))
        detail = f"空头排列 MA{short}<MA{long}, 偏离{deviation:.1f}%"
        conf = 0.5

    return SignalResult("均线交叉", score, score_to_direction(score), conf, detail)


def macd_signal(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal_period: int = 9) -> SignalResult:
    """MACD信号（向后兼容）"""
    if not _ensure_columns(df) or len(df) < slow + signal_period + 5:
        return SignalResult("MACD", 50, Direction.HOLD, 0.0, "数据不足")

    close = _safe_float(df["close"])
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal_period, adjust=False).mean()
    macd_hist = (dif - dea) * 2

    cur_dif, cur_dea, cur_hist = dif.iloc[-1], dea.iloc[-1], macd_hist.iloc[-1]
    prev_hist = macd_hist.iloc[-2]

    if pd.isna(cur_dif):
        return SignalResult("MACD", 50, Direction.HOLD, 0.0, "MACD计算中")

    dif_cross_up = dif.iloc[-2] <= dea.iloc[-2] and cur_dif > cur_dea
    dif_cross_down = dif.iloc[-2] >= dea.iloc[-2] and cur_dif < cur_dea
    hist_rising = cur_hist > prev_hist
    above_zero = cur_dif > 0

    if dif_cross_up:
        score = 75 if above_zero else 65
        detail = f"MACD金叉 DIF({cur_dif:.3f})上穿DEA({cur_dea:.3f})"
        conf = 0.75
    elif dif_cross_down:
        score = 25 if not above_zero else 35
        detail = f"MACD死叉 DIF({cur_dif:.3f})下穿DEA({cur_dea:.3f})"
        conf = 0.75
    elif hist_rising and above_zero:
        score = 60
        detail = f"MACD柱放大+零轴上, DIF={cur_dif:.3f}"
        conf = 0.5
    elif not hist_rising and not above_zero:
        score = 40
        detail = f"MACD柱缩小+零轴下, DIF={cur_dif:.3f}"
        conf = 0.5
    elif hist_rising:
        score = 55
        detail = f"MACD柱放大, DIF={cur_dif:.3f}"
        conf = 0.4
    else:
        score = 45
        detail = f"MACD柱缩小, DIF={cur_dif:.3f}"
        conf = 0.4

    return SignalResult("MACD", score, score_to_direction(score), conf, detail)


def volume_price_signal(df: pd.DataFrame, window: int = 10) -> SignalResult:
    """量价背离信号（向后兼容）"""
    if not _ensure_columns(df) or len(df) < window + 5:
        return SignalResult("量价背离", 50, Direction.HOLD, 0.0, "数据不足")

    close = _safe_float(df["close"]).tail(window)
    volume = _safe_float(df["volume"]).tail(window)

    x = np.arange(len(close))
    price_slope = np.polyfit(x, close.values, 1)[0]
    vol_slope = np.polyfit(x, volume.values, 1)[0]

    price_chg = price_slope / close.mean() * 100
    vol_chg = vol_slope / volume.mean() * 100

    if price_chg > 0.5 and vol_chg < -0.5:
        score = max(20, 35 - int(abs(vol_chg)))
        detail = f"顶背离! 价格↑{price_chg:.1f}% 量↓{vol_chg:.1f}%"
        conf = 0.7
    elif price_chg < -0.5 and vol_chg > 0.5:
        score = min(80, 65 + int(vol_chg))
        detail = f"底背离! 价格↓{price_chg:.1f}% 量↑{vol_chg:.1f}%"
        conf = 0.6
    elif price_chg > 0.5 and vol_chg > 0.5:
        score = min(75, 60 + int(min(price_chg, vol_chg)))
        detail = f"量价齐升 价格↑{price_chg:.1f}% 量↑{vol_chg:.1f}%"
        conf = 0.65
    elif price_chg < -0.5 and vol_chg < -0.5:
        score = 55
        detail = f"缩量下跌 价格↓{price_chg:.1f}% 量↓{vol_chg:.1f}%"
        conf = 0.4
    else:
        score = 50
        detail = f"量价正常 价格{price_chg:+.1f}% 量{vol_chg:+.1f}%"
        conf = 0.3

    return SignalResult("量价背离", score, score_to_direction(score), conf, detail)


# ──────────────────────────────────────────────────────────────────
# 便捷函数: 一次性运行所有信号
# ──────────────────────────────────────────────────────────────────

def all_technical_signals(df: pd.DataFrame) -> list:
    """
    运行所有高级技术信号，返回 SignalResult 列表
    """
    return [
        ma_cross_signal(df),
        macd_signal(df),
        volume_price_signal(df),
        ichimoku_signal(df),
        volume_profile_signal(df),
        market_structure_signal(df),
        wyckoff_signal(df),
        candlestick_signal(df),
        fibonacci_cluster_signal(df),
    ]


# ──────────────────────────────────────────────────────────────────
# 测试入口
# ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from data.history import get_daily

    print("高级技术信号测试 - 贵州茅台(600519)")
    print("=" * 70)

    df = get_daily("600519", start_date="20240101")
    if len(df) == 0:
        print("获取数据失败")
    else:
        print(f"数据: {len(df)}条 ({df['date'].iloc[0]} ~ {df['date'].iloc[-1]})")
        print()

        for signal_func in [
            ichimoku_signal,
            volume_profile_signal,
            market_structure_signal,
            wyckoff_signal,
            candlestick_signal,
            fibonacci_cluster_signal,
        ]:
            result = signal_func(df)
            print(result)
