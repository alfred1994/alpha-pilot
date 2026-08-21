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
from typing import Dict, List, Optional, Tuple

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
_model_cache = {}   # key=(code, train_days, latest_bar_date) -> (model, train_time, accuracy)
VALIDATION_PURGE_DAYS = 1
VALIDATION_EMBARGO_DAYS = 1


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


def _time_split_indices(sample_count: int) -> Tuple[int, int]:
    """返回训练末端和验证起点，隔开标签依赖的边界日。"""
    split = max(45, int(sample_count * 0.75))
    split = min(split, sample_count - 12)
    train_end = max(0, split - VALIDATION_PURGE_DAYS)
    validation_start = min(sample_count, split + VALIDATION_EMBARGO_DAYS)
    return train_end, validation_start


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
            时间切分的样本外平衡准确率 (0-1)
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
        train_end, validation_start = _time_split_indices(len(X))
        # 训练区最后一条标签依赖下一交易日收盘。purge 排除该边界样本，
        # embargo 再留出一个交易日，使样本外置信度不跨越切分边界。
        validation_y = y[validation_start:]
        if (
            train_end > 0
            and len(validation_y)
            and len(np.unique(y[:train_end])) > 1
            and len(np.unique(validation_y)) > 1
        ):
            model.fit(X[:train_end], y[:train_end])
            validation_pred = model.predict(X[validation_start:])
            positive = validation_y == 1
            negative = validation_y == 0
            true_positive_rate = float(np.mean(validation_pred[positive] == 1))
            true_negative_rate = float(np.mean(validation_pred[negative] == 0))
            self._accuracy = (true_positive_rate + true_negative_rate) / 2
            model.fit(X, y)
        else:
            model.fit(X, y)
            self._accuracy = 0.0
        self._model = model
        logger.info(f"模型训练完成: 样本数={len(X)}, 样本外准确率={self._accuracy:.2%}")

        return self._accuracy

    def predict(self, code: str, date: str = None,
                lookback_days: int = None) -> Tuple[float, float]:
        """
        预测指定股票次日涨跌概率

        Args:
            code: 股票代码
            date: 预测基准日期（默认最新交易日）
            lookback_days: 获取日线的自然日回溯范围（默认 DEFAULT_LOOKBACK）

        Returns:
            (score: 0-100, confidence: 0-1)
        """
        # 获取历史数据
        from data.history import get_daily
        from datetime import datetime, timedelta

        end = date or datetime.now().strftime("%Y-%m-%d")
        lookback = int(lookback_days or DEFAULT_LOOKBACK)
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=lookback)).strftime("%Y-%m-%d")

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
        last_bar_date = str(df["date"].iloc[-1]) if "date" in df.columns and not df.empty else end
        cache_key = (code, effective_train_days, last_bar_date)
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

        # 置信度是样本外平衡准确率；验证集类别不足时为 0，禁止伪造下限。
        confidence = round(max(0.0, min(1.0, train_acc)), 2)

        logger.info(f"ML预测 {code}: score={score}, confidence={confidence}")
        return score, confidence


# ════════════════════════════════════════════════════════════════
# pooled 批量信号
# ════════════════════════════════════════════════════════════════

# 扫描级缓存：每次 prefetch_pooled_signals 全量替换，进程内各调用方共享。
_pooled_signal_cache: Dict[str, MLPrediction] = {}


def prefetch_pooled_signals(codes: List[str]) -> Dict[str, MLPrediction]:
    """
    用 pooled 影子模型为一批候选批量预取ML信号。

    数据来自本地K线库，不发起外部行情请求；模型不可用、数据不足或
    过期的代码不写入缓存，预测时自动回退逐股路径。每次调用清空上一
    轮缓存，保证信号与最近一次扫描对齐。

    Returns:
        实际通过 pooled 覆盖的 {code: MLPrediction}
    """
    _pooled_signal_cache.clear()
    unique_codes = [str(c) for c in dict.fromkeys(codes or []) if str(c)]
    if not unique_codes:
        return {}
    try:
        from strategy.pooled_ml import predict_pooled
        raw = predict_pooled(unique_codes)
    except Exception as exc:
        logger.warning(f"pooled 批量预测失败，全部回退逐股路径: {exc}")
        return {}
    result: Dict[str, MLPrediction] = {}
    for code in unique_codes:
        info = raw.get(code) or {}
        if info.get("status") != "ok":
            continue
        prediction = MLPrediction(
            score=float(info.get("score", 50.0)),
            confidence=max(0.0, min(1.0, float(info.get("confidence", 0.0)))),
            detail=(
                f"pooled模型={info.get('model_version', '')}"
                f" 数据截至={info.get('data_cutoff', '')}"
                f" 置信度=样本外AUC"
            ),
        )
        result[code] = prediction
        _pooled_signal_cache[code] = prediction
    logger.info(f"pooled 批量信号覆盖 {len(result)}/{len(unique_codes)} 只")
    return result


# ════════════════════════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════════════════════════

def get_ml_signal(code: str, lookback_days: int = DEFAULT_LOOKBACK) -> MLPrediction:
    """
    获取ML预测信号（一站式接口）

    优先返回本轮扫描预取的 pooled 批量信号；未预取或未覆盖时回退
    逐股临时训练路径。

    Args:
        code: 股票代码
        lookback_days: 回溯天数

    Returns:
        MLPrediction(score, confidence, detail)
    """
    cached = _pooled_signal_cache.get(str(code))
    if cached is not None:
        return cached
    try:
        predictor = QlibPredictor(train_days=min(DEFAULT_TRAIN_DAYS, lookback_days - 60))
        score, confidence = predictor.predict(code, lookback_days=lookback_days)
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
