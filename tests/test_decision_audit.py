"""机会成本评估必须区分空仓观望、约束、失败和未成熟样本。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategy.decision_audit import audit_flat_holds, select_flat_holds


def decision(code, action="HOLD", identifier=1, confidence=0.6):
    return {"id": identifier, "code": code, "date": "2026-09-01", "created_at": "2026-09-01T10:00:00",
            "action": action, "confidence": confidence, "reasoning": "等待突破"}


class DecisionAuditTest(unittest.TestCase):
    def test_dedup_excludes_trade_signal_held_and_failures(self):
        rows = [decision("flat"), decision("flat", identifier=2), decision("signal"),
                decision("signal", "BUY"), decision("held"), decision("traded"),
                decision("failed", confidence=0), dict(decision("legacy"), llm_prompt="持仓卖出分析")]
        trades = [{"code": "held", "action": "BUY", "shares": 100, "created_at": "2026-08-31T10:00:00"},
                  {"code": "traded", "action": "BUY", "shares": 100, "created_at": "2026-09-01T11:00:00"}]
        selected, excluded = select_flat_holds(rows, trades)
        self.assertEqual([r["code"] for r in selected], ["flat"])
        self.assertEqual(selected[0]["scan_count"], 2)
        self.assertEqual(excluded["invalid_decisions"], 1)

    def test_peak_is_not_close_profit_or_t0_fill(self):
        calendar = [f"2026-09-{i:02}" for i in range(1, 7)]
        prices = [{"date": day, "open": 100, "high": 105, "low": 90, "close": 95} for day in calendar[1:]]
        prices[0]["high"] = 150  # 买入当天的冲高不能算T+1可卖空间
        prices[1]["high"] = 110
        result = audit_flat_holds([decision("flat")], [], {"flat": prices}, calendar, calendar[-1])
        outcome = result["samples"][0]["horizons"]["5"]
        self.assertAlmostEqual(outcome["mfe"], 0.0979)
        self.assertAlmostEqual(outcome["net_return"], -0.0521)
        self.assertEqual(result["summary"]["5"]["peak_at_least_5pct"], 1)
        self.assertEqual(result["summary"]["5"]["positive_net"], 0)

    def test_missing_session_does_not_shift_horizon(self):
        calendar = [f"2026-09-{i:02}" for i in range(1, 8)]
        prices = [{"date": day, "open": 100, "high": 105, "low": 90, "close": 95}
                  for day in calendar[1:] if day != "2026-09-03"]
        result = audit_flat_holds([decision("flat")], [], {"flat": prices}, calendar, calendar[-1])
        self.assertEqual(result["summary"]["5"]["matured"], 0)
        self.assertEqual(result["summary"]["5"]["missing_prices"], 1)

    def test_as_of_cuts_off_future_and_nonfinite_prices(self):
        calendar = [f"2026-09-{i:02}" for i in range(1, 7)]
        result = audit_flat_holds([decision("flat")], [], {}, calendar, "2026-09-02")
        self.assertEqual(result["summary"]["3"]["pending"], 1)
        prices = [{"date": day, "open": float("nan"), "high": 105, "low": 90, "close": 95}
                  for day in calendar[1:]]
        result = audit_flat_holds([decision("flat")], [], {"flat": prices}, calendar, calendar[-1])
        self.assertEqual(result["summary"]["5"]["missing_prices"], 1)


if __name__ == "__main__":
    unittest.main()
