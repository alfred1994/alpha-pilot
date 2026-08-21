#!/usr/bin/env python3
"""pooled ML 影子模型质量门禁测试。"""
import json
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.pooled_ml import (
    get_pooled_model_status,
    load_pooled_model,
    predict_pooled,
    train_pooled_model,
)


class FakeModel:
    def fit(self, x, y):
        self.mean = float(np.mean(y))

    def predict_proba(self, x):
        p = np.full(len(x), self.mean)
        return np.column_stack([1 - p, p])


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = handle.name
    handle.close()
    os.unlink(db_path)
    model_path = f"{db_path}.model"
    metadata_path = f"{db_path}.json"
    universe_path = f"{db_path}.universe.json"
    try:
        with Database(db_path=db_path) as db:
            for code_index in range(5):
                code = f"6005{code_index:02d}"
                rows = []
                for day in range(180):
                    close = 10 + day * 0.01 + (day % 5) * 0.1 + code_index * 0.2
                    rows.append({
                        "code": code, "date": f"2024-{(day // 28) + 1:02d}-{(day % 28) + 1:02d}",
                        "open": close, "high": close + 0.1, "low": close - 0.1,
                        "close": close, "volume": 1000, "amount": 10000,
                        "turn": 1, "pctChg": 1 if day % 2 else -1,
                    })
                db.insert_k_daily(rows, source="test")
        with open(universe_path, "w", encoding="utf-8") as file:
            json.dump({
                "codes": [
                    {"code": f"6005{index:02d}", "name": "测试"} for index in range(5)
                ] + [
                    {"code": "000300", "name": "指数应排除"},
                    {"code": "510300", "name": "ETF应排除"},
                    {"code": "688001", "name": "科创板应排除"},
                    {"code": "920001", "name": "北交所应排除"},
                ],
            }, file)

        rejected = train_pooled_model(
            db_path=db_path, model_path=model_path, metadata_path=metadata_path,
            min_symbols=5, min_rows=500, model_factory=FakeModel,
            max_data_age_days=3650, universe_path=universe_path,
        )
        assert_true(rejected["status"] == "blocked" and "auc<" in rejected["reason"], "AUC无增益时阻止模型晋级")
        metadata = train_pooled_model(
            db_path=db_path, model_path=model_path, metadata_path=metadata_path,
            min_symbols=5, min_rows=500, model_factory=FakeModel,
            min_auc=0.49, min_balanced_accuracy=0.0,
            min_baseline_gain=-1.0, max_brier=1.0,
            max_data_age_days=3650, universe_path=universe_path,
        )
        assert_true(metadata["status"] == "ready", "样本覆盖达标后生成 pooled 模型")
        assert_true(metadata["coverage"]["universe_size"] == 5, "训练只使用研究池普通A股")
        assert_true(metadata.get("model_hash") and metadata.get("feature_schema_hash"), "模型记录artifact和特征schema哈希")
        assert_true(metadata["validation_rows"] >= 100, "保留足够样本外验证集")
        assert_true(
            metadata["purge_days"] == 1 and metadata["embargo_days"] == 1,
            "样本外验证显式记录purge和embargo",
        )
        status = get_pooled_model_status(
            metadata_path=metadata_path, model_path=model_path, max_data_age_days=3650,
        )
        assert_true(status["status"] == "ready" and status["artifact_usable"], "状态接口只报告可加载的ready artifact")
        with open(f"{model_path}.lock", "w", encoding="utf-8") as file:
            file.write("busy")
        loading, loading_metadata = load_pooled_model(model_path, metadata_path, max_data_age_days=3650)
        assert_true(loading is None and loading_metadata["status"] == "training", "训练期间影子模型不可加载")
        locked = train_pooled_model(
            db_path=db_path, model_path=model_path, metadata_path=metadata_path,
            universe_path=universe_path, model_factory=FakeModel,
        )
        assert_true(locked["reason"] == "training_locked", "pooled ML拒绝并发训练")
        os.unlink(f"{model_path}.lock")
        with open(f"{model_path}.lock", "w", encoding="utf-8") as file:
            json.dump({"pid": 999999999, "started_at": "2020-01-01T00:00:00"}, file)
        os.utime(f"{model_path}.lock", (1, 1))
        recovered_model, recovered_metadata = load_pooled_model(
            model_path, metadata_path, max_data_age_days=3650,
        )
        assert_true(
            recovered_model is not None and recovered_metadata["status"] == "ready"
            and not os.path.exists(f"{model_path}.lock"),
            "无主训练锁自动回收且不阻塞影子推理",
        )
        predictions = predict_pooled(
            ["600500"], db_path=db_path, model_path=model_path,
            metadata_path=metadata_path, max_data_age_days=3650,
        )
        assert_true(predictions["600500"]["status"] == "ok", "pooled 模型批量预测")
        stale_prediction = predict_pooled(
            ["600500"], db_path=db_path, model_path=model_path,
            metadata_path=metadata_path, max_data_age_days=3650,
            max_candidate_age_days=3,
        )
        assert_true(stale_prediction["600500"]["status"] == "stale_data", "候选K线过期时不生成影子分数")

        with open(model_path, "rb") as file:
            original_model = file.read()
        with open(metadata_path, "r", encoding="utf-8") as file:
            original_metadata = json.load(file)
        with open(model_path, "ab") as file:
            file.write(b"tampered")
        invalid_model, invalid_metadata = load_pooled_model(
            model_path, metadata_path, max_data_age_days=3650,
        )
        assert_true(invalid_model is None and invalid_metadata["reason"] == "artifact_hash_mismatch", "artifact篡改会被哈希校验拦截")
        with open(model_path, "wb") as file:
            file.write(original_model)
        with open(metadata_path, "w", encoding="utf-8") as file:
            json.dump(original_metadata, file)

        stale_metadata = dict(original_metadata)
        stale_metadata["data_cutoff"] = "2020-01-01"
        with open(metadata_path, "w", encoding="utf-8") as file:
            json.dump(stale_metadata, file)
        stale_model, stale_status = load_pooled_model(
            model_path, metadata_path, max_data_age_days=3,
        )
        assert_true(stale_model is None and stale_status["status"] == "stale", "过期artifact会持久化为stale并禁用")
        with open(metadata_path, "w", encoding="utf-8") as file:
            json.dump(original_metadata, file)
        blocked = train_pooled_model(
            db_path=db_path, model_path=model_path, metadata_path=metadata_path,
            min_symbols=20, model_factory=FakeModel,
            max_data_age_days=3650, universe_path=universe_path,
        )
        assert_true(blocked["status"] == "blocked", "覆盖不足时阻止模型晋级")
        loaded, blocked_metadata = load_pooled_model(model_path, metadata_path, max_data_age_days=3650)
        assert_true(loaded is not None and blocked_metadata.get("artifact_usable"), "blocked训练保留上一份合格artifact")
        assert_true(os.path.exists(f"{metadata_path}.last_attempt"), "失败训练另存质量报告")
        print("pooled ML 影子模型测试通过")
    finally:
        for path in (db_path, f"{db_path}-wal", f"{db_path}-shm", model_path, metadata_path, universe_path, f"{model_path}.lock", f"{metadata_path}.last_attempt"):
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    main()
