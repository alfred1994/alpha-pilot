"""风格名额/持有期自动调整（学习闭环）回归。

覆盖：默认均分、文件损坏回退、证据不足不动、每次最多一项调整、名额下限、
持有期只移动一档、检查间隔、开关关闭、通知与回滚。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategy import style_allocator as sa

STYLES = ("oversold_rebound", "low_vol_defensive", "factor_top")


def metric(net, excess, days=15):
    return {"signal_days": days, "picks": days * 15, "mean_net": net, "mean_excess": excess,
            "win_rate": 0.5, "days_beating_universe": 0.5}


def report(per_style):
    """per_style: {style: {horizon: (net, excess[, days])}}"""
    return {"status": "ok", "horizons": [5, 10, 20], "styles": {
        s: {f"{h}d": metric(*v) for h, v in hs.items()} for s, hs in per_style.items()}}


def state(slots=(4, 4, 4), hold=10):
    st = sa.default_allocation(STYLES, 12, hold)
    st["slots"] = dict(zip(STYLES, slots))
    return st


class DefaultsTest(unittest.TestCase):
    def test_even_split_with_remainder(self):
        self.assertEqual(sa.default_allocation(STYLES, 12, 10)["slots"], dict(zip(STYLES, (4, 4, 4))))
        self.assertEqual(sa.default_allocation(STYLES, 5, 10)["slots"], dict(zip(STYLES, (2, 2, 1))))

    def test_missing_or_corrupt_file_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "a.json")
            self.assertEqual(sa.load_allocation(path, STYLES)["history"], [])
            with open(path, "w", encoding="utf-8") as f:
                f.write("{broken")
            self.assertEqual(sa.load_allocation(path, STYLES), sa.default_allocation(STYLES))

    def test_normalizes_to_live_styles(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "a.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"slots": {"oversold_rebound": 6, "trend_strength": 3, "factor_top": 0}, "hold_days": 5}, f)
            st = sa.load_allocation(path, STYLES)
        self.assertEqual(st["slots"], {"oversold_rebound": 6, "low_vol_defensive": 1, "factor_top": 1})
        self.assertEqual(st["hold_days"], 5)


class ProposeTest(unittest.TestCase):
    def test_moves_one_slot_from_worst_to_best(self):
        r = report({"oversold_rebound": {10: (0.02, 0.010)}, "low_vol_defensive": {10: (0.0, -0.004)},
                    "factor_top": {10: (0.01, 0.002)}})
        change = sa.propose(state(), r)
        self.assertEqual((change["type"], change["from"], change["to"]), ("slot", "low_vol_defensive", "oversold_rebound"))
        new = sa.apply_change(state(), change, "2026-11-05")
        self.assertEqual(new["slots"], {"oversold_rebound": 5, "low_vol_defensive": 3, "factor_top": 4})
        self.assertEqual(new["history"][-1]["before"]["slots"]["low_vol_defensive"], 4)

    def test_insufficient_evidence_or_small_gap_does_nothing(self):
        few = report({"oversold_rebound": {10: (0.02, 0.01, 5)}, "low_vol_defensive": {10: (0.0, -0.01, 5)}})
        self.assertIsNone(sa.propose(state(), few))
        close = report({s: {10: (0.01, 0.001 * i)} for i, s in enumerate(STYLES)})
        self.assertIsNone(sa.propose(state(), close))
        self.assertIsNone(sa.propose(state(), {"status": "empty", "styles": {}}))

    def test_keeps_at_least_one_slot(self):
        r = report({"oversold_rebound": {10: (0.02, 0.01)}, "low_vol_defensive": {10: (0.0, -0.01)}})
        self.assertIsNone(sa.propose(state((10, 1, 1)), r))

    def test_hold_moves_one_step_when_slots_unchanged(self):
        # 各风格超额接近（不挪名额）；5 日口径日均净收益显著更好 → 10 → 5
        r = report({s: {5: (0.010, 0.001), 10: (0.008, 0.001), 20: (0.030, 0.001)} for s in STYLES})
        change = sa.propose(state(hold=10), r)
        self.assertEqual((change["type"], change["from"], change["to"]), ("hold", 10, 5))
        # 20 日看起来更好，但从 5 出发只能先到 10
        r = report({s: {5: (0.002, 0.001), 10: (0.008, 0.001), 20: (0.030, 0.001)} for s in STYLES})
        change = sa.propose(state(hold=5), r)
        self.assertEqual(change["to"], 10)

    def test_hold_unchanged_without_edge(self):
        r = report({s: {5: (0.005, 0.001), 10: (0.010, 0.001), 20: (0.020, 0.001)} for s in STYLES})
        self.assertIsNone(sa.propose(state(hold=10), r))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "allocation.json")
        self.styles = patch("config.STYLE_LIVE_STYLES", STYLES)
        self.styles.start()
        self.positions = patch("config.MAX_POSITIONS", 12)
        self.positions.start()

    def tearDown(self):
        self.positions.stop()
        self.styles.stop()
        self.folder.cleanup()

    def run_once(self, today, r, days_between=lambda a, b: 5):
        sent = []
        summary = sa.run(today, path=self.path, evaluate_fn=lambda **kw: r, days_between=days_between,
                         notify_fn=lambda text: sent.append(text) or True)
        return summary, sent

    def test_change_is_saved_notified_and_rolled_back(self):
        r = report({"oversold_rebound": {10: (0.02, 0.010)}, "low_vol_defensive": {10: (0.0, -0.004)}})
        summary, sent = self.run_once("2026-11-05", r)
        self.assertEqual(summary["status"], "changed")
        self.assertTrue(summary["notified"])
        self.assertIn("挪 1 个名额", sent[0])
        self.assertIn("--style-allocation-rollback", sent[0])
        saved = sa.load_allocation(self.path, STYLES)
        self.assertEqual(saved["slots"]["oversold_rebound"], 5)
        self.assertEqual(saved["last_checked"], "2026-11-05")
        restored = sa.rollback(self.path)
        self.assertEqual(restored["slots"], {s: 4 for s in STYLES})
        self.assertEqual(sa.load_allocation(self.path, STYLES)["slots"], {s: 4 for s in STYLES})
        self.assertIsNone(sa.rollback(self.path))

    def test_respects_check_interval_and_notifies_unchanged(self):
        r = report({})
        summary, sent = self.run_once("2026-11-05", r)
        self.assertEqual(summary["status"], "unchanged")
        self.assertIn("维持不变", sent[0])
        summary, sent = self.run_once("2026-11-06", r, days_between=lambda a, b: 1)
        self.assertEqual((summary["status"], sent), ("skipped", []))
        summary, _ = self.run_once("2026-11-06", r, days_between=lambda a, b: None)
        self.assertEqual(summary["status"], "skipped")

    def test_disabled_does_not_write(self):
        with patch("config.STYLE_AUTO_TUNE", False):
            summary, sent = self.run_once("2026-11-05", report({}))
        self.assertEqual((summary["status"], sent), ("disabled", []))
        self.assertFalse(os.path.exists(self.path))

    def test_notify_failure_keeps_change(self):
        r = report({"oversold_rebound": {10: (0.02, 0.010)}, "low_vol_defensive": {10: (0.0, -0.004)}})

        def boom(text):
            raise RuntimeError("telegram down")
        summary = sa.run("2026-11-05", path=self.path, evaluate_fn=lambda **kw: r, notify_fn=boom)
        self.assertEqual((summary["status"], summary["notified"]), ("changed", False))
        self.assertEqual(sa.load_allocation(self.path, STYLES)["slots"]["oversold_rebound"], 5)


if __name__ == "__main__":
    unittest.main()
