"""
ML预测信号模块（Qlib风格）
使用LightGBM分类器预测次日涨跌方向，作为交易决策的补充信号。

特征工程:
    - 均线: MA5, MA10, MA20, MA60
    - RSI14
    - MACD (DIF, DEA, MACD柱)
    - ATR14
    - 成交量比率 (volume_ratio = 当日量 / MA20量)
    - 5日涨幅均值 (pctChg_5d_mean)
    - 10日涨幅标准差 (pctChg_10d_std)

模型:
    - LightGBM二分类（次日上涨概率）
    - 滚动窗口训练（默认120天训练，预测下一天）
    - 输出: 预测分数 0-100，置信度 0-1
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("strategy.qlib_signal")

# ── 默认参数 ──
DEFAULT_TRAIN_DAYS = 120   # 训练窗口天数
DEFAULT_LOOKBACK = 365     # 回溯自然日，覆盖约250个交易日（含指标预热和训练窗口）
FEATURE_COLS = [
    "ma5", "ma10", "ma20", "ma60",
    "rsi14",
    "macd_dif", "macd_dea", "macd_hist",
    "atr14",
    "volume_ratio",
    "pctChg_5d_mean", "pctChg_10d_std",
]


# ── 缓存: 避免同一股票短时间内重复训练 ──
_model_cache = {}   # key=(code, train_days) -> (model, train_time, accuracy)


@dataclass
class MLPrediction:
    """ML预测结果"""
    score: float        # 0-100，越高越看多
    confidence: float   # 0-1，基于近期预测准确率
    detail: str = ""    # 附加说明


# ════════════════════════════════════════════════════════════════
# 特征工程
# ════════════════════════════════════════════════════════════════

def _compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """计算RSI指标"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=period, min_periods=period).mean()
    avg_loss = loss.ewm(span=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _compute_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """计算MACD (DIF, DEA, MACD柱)"""
    ema_fast = close.ewm(span=fast, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, min_periods=slow).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, min_periods=signal).mean()
    hist = 2 * (dif - dea)
    return dif, dea, hist


def _compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """计算ATR (Average True Range)"""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(span=period, min_periods=period).mean()
    return atr


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    从日线DataFrame构建特征矩阵

    输入列: date, open, high, low, close, volume, amount, turn, pctChg
    输出: 添加特征列后的DataFrame（前若干行为NaN，因为需要回溯窗口）
    """
    feat = df.copy()

    # 确保数值类型
    for col in ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]:
        if col in feat.columns:
            feat[col] = pd.to_numeric(feat[col], errors="coerce")

    close = feat["close"]
    high = feat["high"]
    low = feat["low"]
    vol = feat["volume"]
    pct = feat.get("pctChg", close.pct_change() * 100)

    # 均线
    feat["ma5"] = close.rolling(5).mean()
    feat["ma10"] = close.rolling(10).mean()
    feat["ma20"] = close.rolling(20).mean()
    feat["ma60"] = close.rolling(60).mean()

    # RSI14
    feat["rsi14"] = _compute_rsi(close, 14)

    # MACD
    feat["macd_dif"], feat["macd_dea"], feat["macd_hist"] = _compute_macd(close)

    # ATR14
    feat["atr14"] = _compute_atr(high, low, close, 14)

    # 成交量比率 (当日量 / 20日均量)
    vol_ma20 = vol.rolling(20).mean()
    feat["volume_ratio"] = vol / vol_ma20.replace(0, np.nan)

    # 5日涨幅均值
    feat["pctChg_5d_mean"] = pct.rolling(5).mean()

    # 10日涨幅标准差（波动率）
    feat["pctChg_10d_std"] = pct.rolling(10).std()

    return feat


def build_target(df: pd.DataFrame) -> pd.Series:
    """
    构建标签: 次日收盘 > 当日收盘 → 1（上涨），否则 0
    """
    close = pd.to_numeric(df["close"], errors="coerce")
    target = (close.shift(-1) > close).astype(int)
    # 最后一根K线没有“下一日”可作为标签，不能被隐式标成下跌样本。
    if len(target):
        target.iloc[-1] = np.nan
    return target


# ════════════════════════════════════════════════════════════════
# 预测器
# ════════════════════════════════════════════════════════════════

class QlibPredictor:
    """
    LightGBM预测器（Qlib风格接口）

    用法:
        predictor = QlibPredictor()
        score, confidence = predictor.predict("600519")
    """

    def __init__(self, train_days: int = DEFAULT_TRAIN_DAYS):
        self.train_days = train_days
        self._model = None
        self._accuracy = 0.0

    def train(self, df: pd.DataFrame, train_days: int = None) -> float:
        """
        训练模型

        Args:
            df: 日线DataFrame（至少 train_days + 60 行）

        Returns:
            训练集准确率 (0-1)
        """
        try:
            from lightgbm import LGBMClassifier
        except ImportError:
            logger.error("lightgbm 未安装，请执行: pip install lightgbm")
            return 0.0

        train_days = int(train_days or self.train_days)
        if df is None or len(df) < train_days + 30:
            logger.warning(f"数据不足: 需要至少 {train_days + 30} 行，实际 {len(df) if df is not None else 0}")
            return 0.0

        # 构建特征和标签
        feat_df = build_features(df)
        target = build_target(df)

        # 丢弃NaN行
        valid_mask = feat_df[FEATURE_COLS].notna().all(axis=1) & target.notna()
        feat_df = feat_df.loc[valid_mask].copy()
        target = target.loc[valid_mask]

        if len(feat_df) < 60:
            logger.warning(f"有效样本不足: {len(feat_df)}")
            return 0.0

        # 使用最近 train_days 天的数据训练
        train_df = feat_df.tail(train_days)
        train_target = target.loc[train_df.index]

        X = train_df[FEATURE_COLS].values
        y = train_target.values

        # LightGBM分类器（保守参数，避免过拟合）
        model = LGBMClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.05,
            num_leaves=15,
            min_child_samples=10,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,
            reg_lambda=0.1,
            verbose=-1,
            random_state=42,
        )

        # 用时间顺序保留末段作为样本外校准，禁止把训练准确率伪装成置信度。
        split = max(45, int(len(X) * 0.75))
        split = min(split, len(X) - 12)
        if split > 0 and len(np.unique(y[:split])) > 1:
            model.fit(X[:split], y[:split])
            validation_pred = model.predict(X[split:])
            self._accuracy = float(np.mean(validation_pred == y[split:]))
            model.fit(X, y)
        else:
            model.fit(X, y)
            self._accuracy = 0.5
        self._model = model
        logger.info(f"模型训练完成: 样本数={len(X)}, 样本外准确率={self._accuracy:.2%}")

        return self._accuracy

    def predict(self, code: str, date: str = None) -> Tuple[float, float]:
        """
        预测指定股票次日涨跌概率

        Args:
            code: 股票代码
            date: 预测基准日期（默认最新交易日）

        Returns:
            (score: 0-100, confidence: 0-1)
        """
        # 获取历史数据
        from data.history import get_daily
        from datetime import datetime, timedelta

        end = date or datetime.now().strftime("%Y-%m-%d")
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=DEFAULT_LOOKBACK)).strftime("%Y-%m-%d")

        try:
            df = get_daily(code, start_date=start, end_date=end)
        except Exception as e:
            logger.warning(f"获取历史数据失败 {code}: {e}")
            return 50.0, 0.0

        if df is None or len(df) < self.train_days + 30:
            logger.warning(f"数据不足 {code}: {len(df) if df is not None else 0} 行")
            return 50.0, 0.0

        # 构建特征
        feat_df = build_features(df)
        valid_mask = feat_df[FEATURE_COLS].notna().all(axis=1)
        feat_df = feat_df.loc[valid_mask]

        if len(feat_df) < 60:
            logger.warning(f"有效特征不足 {code}: {len(feat_df)} 行（需至少60）")
            return 50.0, 0.0

        effective_train_days = min(self.train_days, len(feat_df) - 10)
        if effective_train_days < 60:
            logger.warning(f"训练窗口不足 {code}: {effective_train_days} 天")
            return 50.0, 0.0

        # 检查缓存（同一股票5分钟内复用模型）
        cache_key = (code, effective_train_days)
        cached = _model_cache.get(cache_key)
        if cached and time.time() - cached[1] < 300:
            model, _, train_acc = cached
        else:
            # 训练模型
            train_acc = self.train(df, train_days=effective_train_days)
            if self._model is None:
                return 50.0, 0.0
            model = self._model
            _model_cache[cache_key] = (model, time.time(), train_acc)

        # 取最后一行特征预测
        last_features = feat_df[FEATURE_COLS].iloc[[-1]].values
        proba = model.predict_proba(last_features)[0]

        # proba[1] = 上涨概率
        up_prob = float(proba[1])
        score = round(up_prob * 100, 1)

        # 置信度 = 训练准确率（适度缩放到 0.3-0.8 区间，避免过度自信）
        confidence = round(max(0.3, min(0.8, train_acc)), 2)

        logger.info(f"ML预测 {code}: score={score}, confidence={confidence}")
        return score, confidence


# ════════════════════════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════════════════════════

def get_ml_signal(code: str, lookback_days: int = DEFAULT_LOOKBACK) -> MLPrediction:
    """
    获取ML预测信号（一站式接口）

    Args:
        code: 股票代码
        lookback_days: 回溯天数

    Returns:
        MLPrediction(score, confidence, detail)
    """
    try:
        predictor = QlibPredictor(train_days=min(DEFAULT_TRAIN_DAYS, lookback_days - 60))
        score, confidence = predictor.predict(code)
        detail = f"ML预测分数={score:.1f}, 置信度={confidence:.0%}"
        return MLPrediction(score=score, confidence=confidence, detail=detail)
    except Exception as e:
        logger.error(f"ML信号获取失败 {code}: {e}")
        return MLPrediction(score=50.0, confidence=0.0, detail=f"ML信号异常: {e}")


# ════════════════════════════════════════════════════════════════
# 测试
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    print(f"=== ML信号测试: {code} ===")

    result = get_ml_signal(code)
    print(f"  分数: {result.score}")
    print(f"  置信度: {result.confidence}")
    print(f"  详情: {result.detail}")
