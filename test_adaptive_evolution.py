#!/usr/bin/env python3
"""
自适应参数演进测试

构造近期低胜率样本，验证复盘后参数会自动收紧，
并且交易参数读取会叠加自适应状态。
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DECISION_BUY_THRESHOLD, TRADE_ADAPTIVE_MIN_SCORE_BASE
from data.database import Database
from strategy.adaptive import AdaptiveEngine
from strategy.regime_config import get_trade_params


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    review_dir = tempfile.mkdtemp(prefix="quant_adaptive_reviews_")
    temp_paths = []
    try:
        adaptive_file = tempfile.NamedTemporaryFile(suffix="_adaptive_state.json", delete=False)
        adaptive_path = adaptive_file.name
        adaptive_file.close()
        os.unlink(adaptive_path)
        temp_paths.append(adaptive_path)

        db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
        db_path = db_file.name
        db_file.close()
        os.unlink(db_path)
        temp_paths.append(db_path)

        today = datetime.now()
        losses = [-4.0, -3.5, -2.8]
        for i, pnl in enumerate(losses):
            date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            review = {
                "date": date,
                "trade_reviews": [{
                    "code": f"TEST{i}",
                    "name": f"测试股{i}",
                    "action": "SELL",
                    "price": 10,
                    "shares": 1000,
                    "reason": "自适应回放亏损卖出",
                    "result_pct": pnl,
                    "hit": False,
                    "source": "adaptive_replay",
                    "dimensions": {
                        "technical": 72,
                        "capital": 68,
                        "sentiment": 65,
                    },
                }],
            }
            with open(os.path.join(review_dir, f"review_{date}.json"), "w", encoding="utf-8") as f:
                json.dump(review, f, ensure_ascii=False, indent=2)

        engine = AdaptiveEngine(
            adaptive_file=adaptive_path,
            review_dir=review_dir,
            db_path=db_path,
            enable_versioning=False,
            enable_ab_testing=False,
        )
        loaded_trades = engine._load_recent_trades(days=5)
        assert_true(len(loaded_trades) == 3, "自适应回放样本加载3笔交易")
        report = engine.analyze_and_adjust(days=5)

        assert_true(report["status"] == "ok", "自适应分析成功")
        assert_true(report["overall"]["win_rate"] == 0, "低胜率样本被识别")

        params = engine.get_adjusted_params()
        assert_true(
            params["buy_threshold"] == DECISION_BUY_THRESHOLD + 2,
            "低胜率会提高买入阈值",
        )
        assert_true(
            params["min_score"] == TRADE_ADAPTIVE_MIN_SCORE_BASE + 2,
            "低胜率会提高最低选股分",
        )
        assert_true(
            params["top_k_delta"] == -1,
            "低胜率会减少下一轮候选数量",
        )
        assert_true(
            params["position_scale"] == 0.8,
            "低胜率会降低下一轮仓位缩放",
        )

        with Database(db_path=db_path) as db:
            saved_state = db.get_adaptive_state()
        assert_true(saved_state is not None, "自适应状态写入SQLite")
        assert_true(
            saved_state["current_min_score"] == TRADE_ADAPTIVE_MIN_SCORE_BASE + 2,
            "SQLite保存后的最低选股分正确",
        )
        assert_true(
            saved_state["current_top_k_delta"] == -1,
            "SQLite保存后的候选数量偏移正确",
        )
        assert_true(
            saved_state["current_position_scale"] == 0.8,
            "SQLite保存后的仓位缩放正确",
        )

        trade_params = get_trade_params("sideways", adaptive_state=saved_state)
        assert_true(
            trade_params["min_score"] == 60,
            "下一轮交易参数会叠加自适应门槛",
        )
        assert_true(
            trade_params["top_k"] == 2,
            "下一轮交易参数会减少TopK候选数量",
        )
        assert_true(
            abs(trade_params["max_weight"] - 0.096) < 0.000001,
            "下一轮交易参数会降低单票仓位上限",
        )

        print("自适应参数演进测试通过")

    finally:
        for path in temp_paths:
            for candidate in [path, f"{path}-wal", f"{path}-shm"]:
                if os.path.exists(candidate):
                    os.unlink(candidate)
        if os.path.isdir(review_dir):
            for name in os.listdir(review_dir):
                os.unlink(os.path.join(review_dir, name))
            os.rmdir(review_dir)


if __name__ == "__main__":
    main()
