"""宽股票池 pooled ML 影子模型。

该模块只负责盘后训练和盘中批量预测，不修改 TradePlan，也不绕过 LLM、T+1
或风险控制。训练使用按日期切分的样本外验证，artifact 带有数据覆盖和模型
版本元数据，供影子策略审计。
"""
import hashlib
import json
import logging
import os
import pickle
from datetime import datetime, timedelta
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
MIN_FRESH_COVERAGE = 0.8
MIN_VALIDATION_AUC = 0.52
MIN_BALANCED_ACCURACY = 0.50
MIN_BASELINE_GAIN = 0.0
MAX_BRIER_SCORE = 0.27
MAX_DATA_AGE_DAYS = 3
PURGE_DAYS = 1
EMBARGO_DAYS = 1
TRAINING_LOCK_STALE_SECONDS = int(os.environ.get("POOLED_ML_LOCK_STALE_SECONDS", "1800"))
MODEL_VERSION = "pooled-lgbm-shadow-v1"


def _ensure_parent_dir(path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def _write_metadata(metadata: Dict, metadata_path: str):
    _ensure_parent_dir(metadata_path)
    temp_meta = f"{metadata_path}.tmp"
    with open(temp_meta, "w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_meta, metadata_path)


def _ordinary_a_share(code: str) -> bool:
    return (
        len(code) == 6 and code.isdigit()
        and code.startswith(("0", "3", "6"))
        and not code.startswith("688")
        and code != "000300"
    )


def _load_research_codes(universe_path: str = None) -> List[str]:
    path = universe_path or os.path.join(DATA_DIR, "research_universe.json")
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        codes = []
        seen = set()
        for item in payload.get("codes") or []:
            code = str(item.get("code") or "")
            if _ordinary_a_share(code) and code not in seen:
                seen.add(code)
                codes.append(code)
        return codes
    except Exception:
        return []


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _training_lock_is_stale(lock_path: str) -> bool:
    try:
        age_seconds = max(0.0, datetime.now().timestamp() - os.path.getmtime(lock_path))
        with open(lock_path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        pid = int(payload.get("pid") or 0)
        if pid:
            return not _pid_is_running(pid)
        return age_seconds >= TRAINING_LOCK_STALE_SECONDS
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        try:
            return datetime.now().timestamp() - os.path.getmtime(lock_path) >= TRAINING_LOCK_STALE_SECONDS
        except OSError:
            return False


def _acquire_training_lock(lock_path: str) -> bool:
    """避免并发训练，并在被强制终止后回收无主锁。"""
    _ensure_parent_dir(lock_path)
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump({"pid": os.getpid(), "started_at": datetime.now().isoformat()}, file)
                file.flush()
                os.fsync(file.fileno())
            return True
        except FileExistsError:
            if not _training_lock_is_stale(lock_path):
                return False
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                continue
            except OSError:
                return False
    return False


def _release_training_lock(lock_path: str):
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass


def _feature_schema_hash() -> str:
    return hashlib.sha256("|".join(FEATURE_COLS).encode("utf-8")).hexdigest()


def _mark_artifact_unusable(metadata: Dict, metadata_path: str,
                            status: str, reason: str) -> Dict:
    metadata = dict(metadata)
    metadata.update({
        "status": status,
        "reason": reason,
        "artifact_usable": False,
        "invalidated_at": datetime.now().isoformat(),
    })
    try:
        _write_metadata(metadata, metadata_path)
    except OSError as exc:
        logger.warning("pooled ML artifact状态写入失败: %s", exc)
    return metadata


def _write_training_attempt(metadata: Dict, metadata_path: str):
    """记录未晋级训练的质量报告，而不破坏上一份可用影子模型。"""
    try:
        _write_metadata(metadata, f"{metadata_path}.last_attempt")
    except OSError as exc:
        logger.warning("pooled ML训练报告写入失败: %s", exc)


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


def _load_rows(db_path: str = None, min_rows: int = 90,
               max_age_days: int = MAX_DATA_AGE_DAYS,
               universe_path: str = None) -> Tuple[pd.DataFrame, Dict]:
    """从 SQLite 构建按股票独立计算技术特征的宽面板。"""
    from data.database import Database

    samples = []
    research_codes = _load_research_codes(universe_path)
    coverage = {
        "universe_size": len(research_codes), "symbols_seen": 0,
        "symbols_fresh": 0, "symbols_used": 0, "symbols_stale": 0,
        "symbols_insufficient_history": 0, "rows_raw": 0,
        "fresh_coverage": 0.0, "usable_coverage": 0.0,
        "latest_data_cutoff": "",
    }
    if not research_codes:
        return pd.DataFrame(columns=["code", "date"] + FEATURE_COLS + ["target"]), coverage
    with Database(db_path=db_path) as db:
        placeholders = ",".join("?" for _ in research_codes)
        rows = db.conn.execute(f"""
            SELECT code, date, open, high, low, close, volume, amount, turn, pctChg
            FROM k_daily
            WHERE code IN ({placeholders})
            ORDER BY code, date
        """, research_codes).fetchall()
        by_code: Dict[str, List[dict]] = {}
        for row in rows:
            by_code.setdefault(str(row["code"]), []).append(dict(row))
        coverage["symbols_seen"] = len(by_code)
        coverage["rows_raw"] = len(rows)

        for code, code_rows in by_code.items():
            latest = str(code_rows[-1].get("date") or "")
            try:
                age = (datetime.now() - datetime.strptime(latest, "%Y-%m-%d")).days
            except ValueError:
                age = max_age_days + 1
            if age > max_age_days:
                coverage["symbols_stale"] += 1
                continue
            coverage["symbols_fresh"] += 1
            coverage["latest_data_cutoff"] = max(coverage["latest_data_cutoff"], latest)
            if len(code_rows) < min_rows:
                coverage["symbols_insufficient_history"] += 1
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

    coverage["fresh_coverage"] = round(
        coverage["symbols_fresh"] / max(1, coverage["universe_size"]), 4
    )
    coverage["usable_coverage"] = round(
        coverage["symbols_used"] / max(1, coverage["universe_size"]), 4
    )

    if not samples:
        return pd.DataFrame(columns=["code", "date"] + FEATURE_COLS + ["target"]), coverage
    panel = pd.concat(samples, ignore_index=True)
    panel["date"] = panel["date"].astype(str)
    return panel, coverage


def train_pooled_model(db_path: str = None, model_path: str = None,
                       metadata_path: str = None, min_symbols: int = MIN_SYMBOLS,
                       min_rows: int = MIN_ROWS, model_factory=None,
                       min_fresh_coverage: float = MIN_FRESH_COVERAGE,
                       min_auc: float = MIN_VALIDATION_AUC,
                       min_balanced_accuracy: float = MIN_BALANCED_ACCURACY,
                       min_baseline_gain: float = MIN_BASELINE_GAIN,
                       max_brier: float = MAX_BRIER_SCORE,
                       max_data_age_days: int = MAX_DATA_AGE_DAYS,
                       universe_path: str = None) -> Dict:
    """训练 pooled 模型并返回质量报告；不足条件时不写入 artifact。"""
    model_path = model_path or MODEL_FILE
    metadata_path = metadata_path or METADATA_FILE
    lock_path = f"{model_path}.lock"
    if not _acquire_training_lock(lock_path):
        return {"model_version": MODEL_VERSION, "status": "blocked", "reason": "training_locked"}
    metadata = {
        "model_version": MODEL_VERSION,
        "trained_at": datetime.now().isoformat(),
        "features": FEATURE_COLS,
        "coverage": {},
        "status": "blocked",
        "reason": "",
    }

    def blocked(reason: str) -> Dict:
        metadata["reason"] = reason
        metadata["artifact_usable"] = False
        # 质量门禁失败只阻止这次晋级。不能以失败报告覆盖旧 artifact，
        # 否则一次临时的数据源波动会令仍新鲜、哈希完整的影子模型下线。
        _write_training_attempt(metadata, metadata_path)
        return metadata

    try:
        panel, coverage = _load_rows(
            db_path=db_path,
            min_rows=max(60, int(min_rows / max(1, min_symbols))),
            max_age_days=max_data_age_days,
            universe_path=universe_path,
        )
        metadata["coverage"] = coverage
        if not coverage["universe_size"]:
            return blocked("research_universe_missing_or_empty")
        if coverage["symbols_used"] < min_symbols:
            return blocked(f"symbols_used<{min_symbols}")
        if coverage["fresh_coverage"] < min_fresh_coverage:
            return blocked(f"fresh_coverage<{min_fresh_coverage:.2f}")
        if len(panel) < min_rows:
            return blocked(f"rows<{min_rows}")

        dates = sorted(panel["date"].unique())
        if len(dates) < 20:
            return blocked("too_few_dates")
        split_index = max(1, int(len(dates) * 0.8))
        train_end_index = max(0, split_index - PURGE_DAYS)
        validation_start_index = min(len(dates), split_index + EMBARGO_DAYS)
        train_dates = set(dates[:train_end_index])
        validation_dates = set(dates[validation_start_index:])
        train = panel[panel["date"].isin(train_dates)]
        validation = panel[panel["date"].isin(validation_dates)]
        if (
            len(validation) < MIN_VALIDATION_ROWS
            or train["target"].nunique() < 2
            or validation["target"].nunique() < 2
        ):
            return blocked("validation_or_class_coverage_insufficient")

        model = _build_model(model_factory=model_factory)
        if model is None:
            return blocked("lightgbm_or_sklearn_missing")
        X_train = train[FEATURE_COLS].to_numpy(dtype=float)
        y_train = train["target"].to_numpy(dtype=int)
        X_valid = validation[FEATURE_COLS].to_numpy(dtype=float)
        y_valid = validation["target"].to_numpy(dtype=int)
        model.fit(X_train, y_train)
        probabilities = model.predict_proba(X_valid)[:, 1]
        predictions = (probabilities >= 0.5).astype(int)
        accuracy = float(np.mean(predictions == y_valid))
        positive = y_valid == 1
        negative = y_valid == 0
        tpr = float(np.mean(predictions[positive] == 1)) if positive.any() else 0.5
        tnr = float(np.mean(predictions[negative] == 0)) if negative.any() else 0.5
        balanced_accuracy = (tpr + tnr) / 2
        baseline_accuracy = max(float(np.mean(y_valid)), 1 - float(np.mean(y_valid)))
        baseline_gain = accuracy - baseline_accuracy
        brier = float(np.mean((probabilities - y_valid) ** 2))
        try:
            from sklearn.metrics import roc_auc_score
            auc = float(roc_auc_score(y_valid, probabilities)) if len(np.unique(y_valid)) > 1 else 0.5
        except ImportError:
            auc = 0.5
        quality_failures = []
        if auc < min_auc:
            quality_failures.append(f"auc<{min_auc:.2f}")
        if balanced_accuracy < min_balanced_accuracy:
            quality_failures.append(f"balanced_accuracy<{min_balanced_accuracy:.2f}")
        if baseline_gain < min_baseline_gain:
            quality_failures.append(f"baseline_gain<{min_baseline_gain:.3f}")
        if brier > max_brier:
            quality_failures.append(f"brier>{max_brier:.3f}")
        if quality_failures:
            metadata.update({
                "validation_accuracy": round(accuracy, 4),
                "validation_auc": round(auc, 4),
                "balanced_accuracy": round(balanced_accuracy, 4),
                "baseline_accuracy": round(baseline_accuracy, 4),
                "baseline_gain": round(baseline_gain, 4),
                "brier_score": round(brier, 4),
            })
            return blocked(";".join(quality_failures))

        # 只有样本外质量和样本规模记录完成后才落 artifact。
        # 质量门禁已使用严格隔离的验证段；此处才将全部已标注样本用于次日推理。
        model.fit(panel[FEATURE_COLS].to_numpy(dtype=float), panel["target"].to_numpy(dtype=int))
        metadata.update({
            "status": "ready",
            "reason": "",
            "symbols": sorted(panel["code"].unique().tolist()),
            "rows": int(len(panel)),
            "train_rows": int(len(train)),
            "validation_rows": int(len(validation)),
            "train_end": str(max(train["date"])),
            "validation_start": str(min(validation["date"])),
            "purge_days": PURGE_DAYS,
            "embargo_days": EMBARGO_DAYS,
            "validation_accuracy": round(accuracy, 4),
            "validation_auc": round(auc, 4),
            "balanced_accuracy": round(balanced_accuracy, 4),
            "baseline_accuracy": round(baseline_accuracy, 4),
            "baseline_gain": round(baseline_gain, 4),
            "brier_score": round(brier, 4),
            "data_cutoff": coverage["latest_data_cutoff"],
            "fit_end": str(max(panel["date"])),
            "data_age_days": (
                datetime.now() - datetime.strptime(coverage["latest_data_cutoff"], "%Y-%m-%d")
            ).days,
            "feature_schema_hash": _feature_schema_hash(),
            "artifact_usable": True,
        })
        _ensure_parent_dir(model_path)
        temp_model = f"{model_path}.tmp"
        with open(temp_model, "wb") as file:
            pickle.dump(model, file, protocol=pickle.HIGHEST_PROTOCOL)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_model, model_path)
        with open(model_path, "rb") as file:
            metadata["model_hash"] = hashlib.sha256(file.read()).hexdigest()
        _write_metadata(metadata, metadata_path)
        logger.info("pooled ML训练完成: symbols=%s rows=%s valid_auc=%.3f", metadata["coverage"]["symbols_used"], metadata["rows"], auc)
        return metadata
    except Exception as exc:
        return blocked(f"fit_failed:{exc}"[:240])
    finally:
        _release_training_lock(lock_path)


def load_pooled_model(model_path: str = None, metadata_path: str = None,
                      max_data_age_days: int = MAX_DATA_AGE_DAYS):
    model_path = model_path or MODEL_FILE
    metadata_path = metadata_path or METADATA_FILE
    lock_path = f"{model_path}.lock"
    if os.path.exists(lock_path):
        if _training_lock_is_stale(lock_path):
            _release_training_lock(lock_path)
        else:
            return None, {"status": "training", "reason": "training_in_progress", "artifact_usable": False}
    if not os.path.exists(model_path) or not os.path.exists(metadata_path):
        return None, {"status": "missing", "reason": "artifact_not_found"}
    try:
        with open(metadata_path, "r", encoding="utf-8") as file:
            metadata = json.load(file)
        if metadata.get("status") != "ready" or not metadata.get("artifact_usable"):
            return None, metadata
        if metadata.get("model_version") != MODEL_VERSION:
            return None, _mark_artifact_unusable(
                metadata, metadata_path, "invalid", "model_version_mismatch",
            )
        if metadata.get("feature_schema_hash") != _feature_schema_hash():
            return None, _mark_artifact_unusable(
                metadata, metadata_path, "invalid", "feature_schema_mismatch",
            )
        cutoff = datetime.strptime(str(metadata.get("data_cutoff") or ""), "%Y-%m-%d")
        if datetime.now() - cutoff > timedelta(days=max_data_age_days):
            return None, _mark_artifact_unusable(
                metadata, metadata_path, "stale", "artifact_data_stale",
            )
        with open(model_path, "rb") as file:
            serialized_model = file.read()
        expected_hash = str(metadata.get("model_hash") or "")
        if not expected_hash:
            return None, _mark_artifact_unusable(
                metadata, metadata_path, "invalid", "artifact_hash_missing",
            )
        actual_hash = hashlib.sha256(serialized_model).hexdigest()
        if actual_hash != expected_hash:
            return None, _mark_artifact_unusable(
                metadata, metadata_path, "invalid", "artifact_hash_mismatch",
            )
        return pickle.loads(serialized_model), metadata
    except Exception as exc:
        return None, _mark_artifact_unusable(
            metadata if "metadata" in locals() else {},
            metadata_path,
            "invalid",
            f"artifact_load_failed:{exc}"[:240],
        )


def predict_pooled(codes: List[str], db_path: str = None,
                   model_path: str = None, metadata_path: str = None,
                   max_data_age_days: int = MAX_DATA_AGE_DAYS,
                   max_candidate_age_days: Optional[int] = None) -> Dict[str, Dict]:
    """批量读取最新K线并预测；模型不可用时返回明确的 shadow_unavailable。"""
    candidate_age_limit = (
        max_data_age_days if max_candidate_age_days is None
        else max(0, int(max_candidate_age_days))
    )
    model, metadata = load_pooled_model(
        model_path, metadata_path, max_data_age_days=max_data_age_days,
    )
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
            if frame.empty:
                result[code] = {"status": "insufficient_data"}
                continue
            latest_date = str(frame["date"].iloc[-1])
            try:
                data_age_days = (datetime.now() - datetime.strptime(latest_date, "%Y-%m-%d")).days
            except ValueError:
                data_age_days = max_data_age_days + 1
            if data_age_days > candidate_age_limit:
                result[code] = {
                    "status": "stale_data",
                    "reason": "candidate_data_stale",
                    "data_cutoff": latest_date,
                }
                continue
            features = build_features(frame)
            valid = features[FEATURE_COLS].notna().all(axis=1)
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
                "data_cutoff": latest_date,
            }
    return result


def get_pooled_model_status(metadata_path: str = None, model_path: str = None,
                            max_data_age_days: int = MAX_DATA_AGE_DAYS) -> Dict:
    """返回与加载器一致的artifact状态，避免过期模型被展示为 ready。"""
    _, metadata = load_pooled_model(
        model_path=model_path,
        metadata_path=metadata_path,
        max_data_age_days=max_data_age_days,
    )
    return metadata
