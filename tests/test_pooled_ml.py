#!/usr/bin/env python3
"""pooled ML 影子模型质量门禁测试。"""
import json
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.pooled_ml import predict_pooled, train_pooled_model


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

        metadata = train_pooled_model(
            db_path=db_path, model_path=model_path, metadata_path=metadata_path,
            min_symbols=5, min_rows=500, model_factory=FakeModel,
        )
        assert_true(metadata["status"] == "ready", "样本覆盖达标后生成 pooled 模型")
        assert_true(metadata["validation_rows"] >= 100, "保留足够样本外验证集")
        predictions = predict_pooled(["600500"], db_path=db_path, model_path=model_path, metadata_path=metadata_path)
        assert_true(predictions["600500"]["status"] == "ok", "pooled 模型批量预测")
        blocked = train_pooled_model(db_path=db_path, model_path=f"{model_path}.blocked", metadata_path=f"{metadata_path}.blocked", min_symbols=20, model_factory=FakeModel)
        assert_true(blocked["status"] == "blocked", "覆盖不足时阻止模型晋级")
        print("pooled ML 影子模型测试通过")
    finally:
        for path in (db_path, f"{db_path}-wal", f"{db_path}-shm", model_path, metadata_path, f"{model_path}.blocked", f"{metadata_path}.blocked"):
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    main()
