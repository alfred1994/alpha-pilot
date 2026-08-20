"""宽股票池 pooled ML 影子模型。

该模块只负责盘后训练和盘中批量预测，不修改 TradePlan，也不绕过 LLM、T+1
或风险控制。训练使用按日期切分的样本外验证，artifact 带有数据覆盖和模型
版本元数据，供影子策略审计。
"""
import json
import logging
import os
import pickle
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import DATA_DIR
from strategy.qlib_signal import FEATURE_COLS, build_features, build_target

logger = logging.getLogger("strategy.pooled_ml")

MODEL_DIR = os.path.join(DATA_DIR, "ml")
MODEL_FILE = os.path.join(MODEL_DIR, "pooled_model.pkl")
METADATA_FILE = os.path.join(MODEL_DIR, "pooled_model.json")
MIN_SYMBOLS = 5
MIN_ROWS = 500
MIN_VALIDATION_ROWS = 100
MODEL_VERSION = "pooled-lgbm-shadow-v1"


def _build_model(model_factory=None):
    if model_factory is not None:
        return model_factory()
    try:
        from lightgbm import LGBMClassifier
        return LGBMClassifier(
            n_estimators=160,
            max_depth=4,
            learning_rate=0.04,
            num_leaves=15,
            min_child_samples=20,
            subsample=0.85,
            colsample_bytree=0.85,
            reg_alpha=0.1,
            reg_lambda=0.1,
            verbose=-1,
            random_state=42,
        )
    except ImportError:
        try:
            from sklearn.ensemble import HistGradientBoostingClassifier
            return HistGradientBoostingClassifier(
                max_iter=160, learning_rate=0.04, max_leaf_nodes=15,
                l2_regularization=0.1, random_state=42,
            )
        except ImportError:
            return None


def _load_rows(db_path: str = None, min_rows: int = 90) -> Tuple[pd.DataFrame, Dict]:
    """从 SQLite 构建按股票独立计算技术特征的宽面板。"""
    from data.database import Database

    samples = []
    coverage = {"symbols_seen": 0, "symbols_used": 0, "symbols_stale": 0, "rows_raw": 0}
    with Database(db_path=db_path) as db:
        rows = db.conn.execute("""
            SELECT code, date, open, high, low, close, volume, amount, turn, pctChg
            FROM k_daily
            WHERE code GLOB '[0-9]*' AND length(code)=6
            ORDER BY code, date
        """).fetchall()
        by_code: Dict[str, List[dict]] = {}
        for row in rows:
            by_code.setdefault(str(row["code"]), []).append(dict(row))
        coverage["symbols_seen"] = len(by_code)
        coverage["rows_raw"] = len(rows)

        for code, code_rows in by_code.items():
            if len(code_rows) < min_rows:
                continue
            frame = pd.DataFrame(code_rows)
            features = build_features(frame)
            target = build_target(frame)
            valid = features[FEATURE_COLS].notna().all(axis=1) & target.notna()
            if int(valid.sum()) < 60:
                continue
            usable = features.loc[valid, ["date"] + FEATURE_COLS].copy()
            usable["target"] = target.loc[valid].astype(int).values
            usable["code"] = code
            samples.append(usable)
            coverage["symbols_used"] += 1

    if not samples:
        return pd.DataFrame(columns=["code", "date"] + FEATURE_COLS + ["target"]), coverage
    panel = pd.concat(samples, ignore_index=True)
    panel["date"] = panel["date"].astype(str)
    return panel, coverage


def train_pooled_model(db_path: str = None, model_path: str = None,
                       metadata_path: str = None, min_symbols: int = MIN_SYMBOLS,
                       min_rows: int = MIN_ROWS, model_factory=None) -> Dict:
    """训练 pooled 模型并返回质量报告；不足条件时不写入 artifact。"""
    model_path = model_path or MODEL_FILE
    metadata_path = metadata_path or METADATA_FILE
    panel, coverage = _load_rows(db_path=db_path, min_rows=max(60, int(min_rows / max(1, min_symbols))))
    metadata = {
        "model_version": MODEL_VERSION,
        "trained_at": datetime.now().isoformat(),
        "features": FEATURE_COLS,
        "coverage": coverage,
        "status": "blocked",
        "reason": "",
    }
    if coverage["symbols_used"] < min_symbols:
        metadata["reason"] = f"symbols_used<{min_symbols}"
        return metadata
    if len(panel) < min_rows:
        metadata["reason"] = f"rows<{min_rows}"
        return metadata

    dates = sorted(panel["date"].unique())
    if len(dates) < 20:
        metadata["reason"] = "too_few_dates"
        return metadata
    split_at = dates[max(1, int(len(dates) * 0.8))]
    purge_index = max(0, dates.index(split_at) - 1)
    train_dates = set(dates[:purge_index])
    train = panel[panel["date"].isin(train_dates)]
    validation = panel[panel["date"] >= split_at]
    if len(validation) < MIN_VALIDATION_ROWS or train["target"].nunique() < 2:
        metadata["reason"] = "validation_or_class_coverage_insufficient"
        return metadata

    model = _build_model(model_factory=model_factory)
    if model is None:
        metadata["reason"] = "lightgbm_or_sklearn_missing"
        return metadata
    X_train = train[FEATURE_COLS].to_numpy(dtype=float)
    y_train = train["target"].to_numpy(dtype=int)
    X_valid = validation[FEATURE_COLS].to_numpy(dtype=float)
    y_valid = validation["target"].to_numpy(dtype=int)
    try:
        model.fit(X_train, y_train)
        probabilities = model.predict_proba(X_valid)[:, 1]
        predictions = (probabilities >= 0.5).astype(int)
        accuracy = float(np.mean(predictions == y_valid))
        try:
            from sklearn.metrics import roc_auc_score
            auc = float(roc_auc_score(y_valid, probabilities)) if len(np.unique(y_valid)) > 1 else 0.5
        except ImportError:
            auc = 0.5
    except Exception as exc:
        metadata["reason"] = f"fit_failed:{exc}"[:240]
        return metadata

    # 只有样本外质量和样本规模记录完成后才落 artifact。
    metadata.update({
        "status": "ready",
        "reason": "",
        "symbols": sorted(panel["code"].unique().tolist()),
        "rows": int(len(panel)),
        "train_rows": int(len(train)),
        "validation_rows": int(len(validation)),
        "train_end": str(max(train["date"])),
        "validation_start": str(min(validation["date"])),
        "validation_accuracy": round(accuracy, 4),
        "validation_auc": round(auc, 4),
    })
    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    temp_model = f"{model_path}.tmp"
    with open(temp_model, "wb") as file:
        pickle.dump(model, file, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(temp_model, model_path)
    temp_meta = f"{metadata_path}.tmp"
    with open(temp_meta, "w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)
    os.replace(temp_meta, metadata_path)
    logger.info("pooled ML训练完成: symbols=%s rows=%s valid_auc=%.3f", metadata["coverage"]["symbols_used"], metadata["rows"], auc)
    return metadata


def load_pooled_model(model_path: str = None, metadata_path: str = None):
    model_path = model_path or MODEL_FILE
    metadata_path = metadata_path or METADATA_FILE
    if not os.path.exists(model_path) or not os.path.exists(metadata_path):
        return None, {"status": "missing", "reason": "artifact_not_found"}
    try:
        with open(metadata_path, "r", encoding="utf-8") as file:
            metadata = json.load(file)
        if metadata.get("status") != "ready":
            return None, metadata
        with open(model_path, "rb") as file:
            return pickle.load(file), metadata
    except Exception as exc:
        return None, {"status": "invalid", "reason": str(exc)[:240]}


def predict_pooled(codes: List[str], db_path: str = None,
                   model_path: str = None, metadata_path: str = None) -> Dict[str, Dict]:
    """批量读取最新K线并预测；模型不可用时返回明确的 shadow_unavailable。"""
    model, metadata = load_pooled_model(model_path, metadata_path)
    if model is None:
        return {str(code): {"status": "shadow_unavailable", "reason": metadata.get("reason", "")} for code in codes}
    from data.database import Database
    result = {}
    with Database(db_path=db_path) as db:
        for raw_code in codes:
            code = str(raw_code)
            rows = db.conn.execute("""
                SELECT code,date,open,high,low,close,volume,amount,turn,pctChg
                FROM k_daily WHERE code=? ORDER BY date
            """, (code,)).fetchall()
            frame = pd.DataFrame([dict(row) for row in rows])
            features = build_features(frame) if not frame.empty else frame
            valid = features[FEATURE_COLS].notna().all(axis=1) if not frame.empty else pd.Series(dtype=bool)
            if not valid.any():
                result[code] = {"status": "insufficient_data"}
                continue
            row = features.loc[valid, FEATURE_COLS].tail(1).to_numpy(dtype=float)
            probability = float(model.predict_proba(row)[0][1])
            result[code] = {
                "status": "ok",
                "score": round(probability * 100, 2),
                "confidence": round(float(metadata.get("validation_auc", 0.5)), 4),
                "model_version": metadata.get("model_version", MODEL_VERSION),
            }
    return result


def get_pooled_model_status(metadata_path: str = None) -> Dict:
    metadata_path = metadata_path or METADATA_FILE
    if not os.path.exists(metadata_path):
        return {"status": "missing", "reason": "artifact_not_found"}
    try:
        with open(metadata_path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as exc:
        return {"status": "invalid", "reason": str(exc)[:240]}
