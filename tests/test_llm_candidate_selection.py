#!/usr/bin/env python3
"""LLM 判断广度回归测试。

修复前行为：只有综合分排名前 top_k(默认3) 的候选进入 LLM 判断，
即使更多候选已过最低质量线。本测试锁定修复后契约：
所有过线候选（受可配置上限约束）都应进入判断，未过线候选给出拒绝原因。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler.pipeline import select_llm_candidates
from strategy.signal_stability import SignalStabilityTracker


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _scored(codes_scores, technical=75.0):
    """构造候选；技术面统一给到门槛以上，以便本用例专注于"判断广度"。

    技术面硬门槛与信号稳定性另有 tests/test_entry_and_risk_guards.py 专门覆盖。
    """
    return [
        {"code": code, "composite": score,
         "dimensions": {"technical": {"score": technical, "confidence": 0.5}}}
        for code, score in codes_scores
    ]


def main():
    with tempfile.TemporaryDirectory() as d:
        # required_rounds=1：本用例只验证广度契约，确认轮数语义由专门用例覆盖
        tracker = SignalStabilityTracker(
            required_rounds=1, state_file=os.path.join(d, "stability.json"))
        scored = _scored([
            ("600001", 70), ("600002", 68), ("600003", 65),
            ("600004", 62), ("600005", 60), ("600006", 59),
            ("600007", 40), ("600008", 35),
        ])

        def select(**kw):
            kw.setdefault("top_k", 3)
            kw.setdefault("min_score", 58)
            kw.setdefault("trading_date", "2026-09-24")
            kw.setdefault("stability_tracker", tracker)
            return select_llm_candidates(scored, **kw)

        # 旧逻辑复现：top_k=3 时仅 3 只过线候选进入判断
        selected, reasons = select(max_llm_candidates=3)
        assert_true(len(selected) == 3, f"上限3时仅判断3只（实际{len(selected)}）")
        assert_true([s["code"] for s in selected] == ["600001", "600002", "600003"],
                    "判断名单按综合分降序")
        assert_true("600004" in reasons and reasons["600004"].startswith("HOLD_NOT_TOP"),
                    "未入选候选记录拒绝原因")

        # 修复后默认：所有过线候选都进入判断
        selected, reasons = select(max_llm_candidates=10)
        assert_true(len(selected) == 6, f"6只过线候选全部进入判断（实际{len(selected)}）")
        assert_true(not any(code in reasons for code in
                            ("600001", "600002", "600003", "600004", "600005", "600006")),
                    "过线候选不产生拒绝原因")
        assert_true(reasons.get("600007", "").startswith("HOLD_SCORE_LOW"),
                    "未过线候选记录分数不足原因")
        assert_true(reasons.get("600008", "").startswith("HOLD_SCORE_LOW"),
                    "最低分候选同样记录分数不足原因")

        # 无上限配置时不过度限制（全部过线则全部判断）
        selected, _ = select(max_llm_candidates=None)
        assert_true(len(selected) == 6, f"无上限时6只全部判断（实际{len(selected)}）")

        # 技术面不达标时，即使综合分过线也不进入判断
        weak = _scored([("600009", 75)], technical=41.9)
        selected, reasons = select_llm_candidates(
            weak, top_k=3, min_score=58, max_llm_candidates=10,
            trading_date="2026-09-24", stability_tracker=tracker)
        assert_true(selected == [], "技术面不达标的候选不进入判断")
        assert_true(reasons["600009"].startswith("HOLD_TECH_LOW"),
                    "记录技术面拒绝原因")

        # 空输入安全
        selected, reasons = select_llm_candidates(
            [], top_k=3, min_score=58, max_llm_candidates=10,
            trading_date="2026-09-24", stability_tracker=tracker)
        assert_true(selected == [] and reasons == {}, "空候选列表安全返回")

    print("LLM判断广度测试通过")


if __name__ == "__main__":
    main()
