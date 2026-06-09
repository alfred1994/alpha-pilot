"""
同花顺风格指标公式库
=================================================================
兼容同花顺/通达信公式语法的Python实现
所有指标函数接受 pandas Series 输入，返回 Series 输出
=================================================================
"""
import numpy as np
import pandas as pd


# ════════════════════════════════════════════════════════════════
# 均线类
# ════════════════════════════════════════════════════════════════

def MA(series: pd.Series, n: int) -> pd.Series:
    """简单移动平均线 MA(C, N)"""
    return series.rolling(window=n, min_periods=1).mean()


def EMA(series: pd.Series, n: int) -> pd.Series:
    """指数移动平均线 EMA(C, N)"""
    return series.ewm(span=n, adjust=False).mean()


def SMA(series: pd.Series, n: int, m: int = 1) -> pd.Series:
    """同花顺SMA (递归移动平均) SMA(X, N, M)"""
    result = series.copy()
    for i in range(1, len(series)):
        result.iloc[i] = (series.iloc[i] * m + result.iloc[i - 1] * (n - m)) / n
    return result


# ════════════════════════════════════════════════════════════════
# MACD 指标
# ════════════════════════════════════════════════════════════════

def MACD(close: pd.Series, short: int = 12, long: int = 26, signal: int = 9):
    """
    MACD指标
    DIF:EMA(C,SHORT)-EMA(C,LONG)
    DEA:EMA(DIF,M)
    MACD:(DIF-DEA)*2,COLORSTICK
    """
    dif = EMA(close, short) - EMA(close, long)
    dea = EMA(dif, signal)
    macd = (dif - dea) * 2
    return dif, dea, macd


# ════════════════════════════════════════════════════════════════
# RSI 指标
# ════════════════════════════════════════════════════════════════

def RSI(close: pd.Series, n: int = 14) -> pd.Series:
    """RSI相对强弱指标"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = SMA(gain, n, 1)
    avg_loss = SMA(loss, n, 1)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


# ════════════════════════════════════════════════════════════════
# KDJ 指标
# ════════════════════════════════════════════════════════════════

def KDJ(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9, m1: int = 3, m2: int = 3):
    """
    KDJ随机指标
    RSV:=(C-LLV(L,N))/(HHV(H,N)-LLV(L,N))*100;
    K:SMA(RSV,M1,1);
    D:SMA(K,M2,1);
    J:3*K-2*D;
    """
    lowest = low.rolling(window=n, min_periods=1).min()
    highest = high.rolling(window=n, min_periods=1).max()
    rsv = (close - lowest) / (highest - lowest).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = SMA(rsv, m1, 1)
    d = SMA(k, m2, 1)
    j = 3 * k - 2 * d
    return k, d, j


# ════════════════════════════════════════════════════════════════
# 成交量指标
# ════════════════════════════════════════════════════════════════

def VOL_MA(volume: pd.Series, n: int) -> pd.Series:
    """成交量均线"""
    return MA(volume, n)


def VOL_RATIO(volume: pd.Series, n: int = 5) -> pd.Series:
    """量比: 当日成交量 / 过去N日平均成交量"""
    avg_vol = volume.rolling(window=n, min_periods=1).mean().shift(1)
    return volume / avg_vol.replace(0, np.nan)


def OBV(close: pd.Series, volume: pd.Series) -> pd.Series:
    """OBV能量潮"""
    sign = np.sign(close.diff())
    sign.iloc[0] = 0
    return (sign * volume).cumsum()


# ════════════════════════════════════════════════════════════════
# 布林带
# ════════════════════════════════════════════════════════════════

def BOLL(close: pd.Series, n: int = 20, k: float = 2.0):
    """布林带"""
    mid = MA(close, n)
    std = close.rolling(window=n, min_periods=1).std()
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower


# ════════════════════════════════════════════════════════════════
# ATR 真实波幅
# ════════════════════════════════════════════════════════════════

def ATR(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """ATR平均真实波幅"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return EMA(tr, n)


# ════════════════════════════════════════════════════════════════
# 偏离率
# ════════════════════════════════════════════════════════════════

def BIAS(close: pd.Series, n: int = 20) -> pd.Series:
    """乖离率 BIAS = (C - MA(C,N)) / MA(C,N) * 100"""
    ma = MA(close, n)
    return (close - ma) / ma * 100


# ════════════════════════════════════════════════════════════════
# 辅助判断函数
# ════════════════════════════════════════════════════════════════

def CROSS(series_a: pd.Series, series_b: pd.Series) -> pd.Series:
    """金叉: A上穿B"""
    return (series_a > series_b) & (series_a.shift(1) <= series_b.shift(1))


def CROSS_DOWN(series_a: pd.Series, series_b: pd.Series) -> pd.Series:
    """死叉: A下穿B"""
    return (series_a < series_b) & (series_a.shift(1) >= series_b.shift(1))


def REF(series: pd.Series, n: int = 1) -> pd.Series:
    """引用N日前的值"""
    return series.shift(n)


def HHV(series: pd.Series, n: int) -> pd.Series:
    """N日最高值"""
    return series.rolling(window=n, min_periods=1).max()


def LLV(series: pd.Series, n: int) -> pd.Series:
    """N日最低值"""
    return series.rolling(window=n, min_periods=1).min()


def COUNT(condition: pd.Series, n: int) -> pd.Series:
    """统计N日内满足条件的天数"""
    return condition.astype(int).rolling(window=n, min_periods=1).sum()


def EVERY(condition: pd.Series, n: int) -> pd.Series:
    """N日内每天都满足条件"""
    return condition.astype(int).rolling(window=n, min_periods=1).sum() == n


def EXIST(condition: pd.Series, n: int) -> pd.Series:
    """N日内至少有一天满足条件"""
    return condition.astype(int).rolling(window=n, min_periods=1).sum() > 0


def BARSLAST(condition: pd.Series) -> pd.Series:
    """上一次条件成立到现在的天数"""
    result = pd.Series(0, index=condition.index, dtype=float)
    count = 0
    for i in range(len(condition)):
        if condition.iloc[i]:
            count = 0
        else:
            count += 1
        result.iloc[i] = count
    return result


def HHVBARS(series: pd.Series, n: int) -> pd.Series:
    """N日内最高值距今天数"""
    result = pd.Series(0, index=series.index, dtype=float)
    for i in range(len(series)):
        start = max(0, i - n + 1)
        window = series.iloc[start:i + 1]
        result.iloc[i] = i - (start + window.argmax())
    return result
