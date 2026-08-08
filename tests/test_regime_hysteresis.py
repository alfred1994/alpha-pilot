"""市场环境单日反转迟滞测试。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.market_regime import _apply_regime_hysteresis


def main():
    with tempfile.TemporaryDirectory(prefix="quant_regime_") as d:
        db_path = os.path.join(d, "quant.db")
        with Database(db_path=db_path) as db:
            db.insert_market_regime({"date": "2026-08-07", "regime": "bull", "confidence": 0.9})
        result = _apply_regime_hysteresis("2026-08-08", "bear", 0.5, "原始判断", db_path=db_path)
        assert result[0] == "bull" and result[3] is True
        assert "迟滞沿用前日bull" in result[2]
    print("市场环境迟滞测试通过")


if __name__ == "__main__":
    main()
