import os
import sys
import unittest
from dataclasses import replace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategy.diversified_research import DiversifiedConfig, build_targets, prepare_features


def fixture():
    rows = []
    for i in range(12):
        for j, date in enumerate(pd.bdate_range("2024-01-01", periods=250)):
            price = 10 * np.exp((i + 1) * j * 0.00015 + 0.001 * np.sin(j))
            rows.append(dict(code=f"600{i+1:03d}", date=date, open=price, close=price,
                             high=price * 1.01, low=price * 0.99, volume=1e5))
    return pd.DataFrame(rows).sort_values(["date", "code"]).reset_index(drop=True)


class DiversifiedTests(unittest.TestCase):
    def setUp(self):
        self.panel = fixture()
        self.sectors = {f"600{i+1:03d}": str(i // 4) for i in range(12)}
        self.config = DiversifiedConfig(top_k=6, retention_rank=9, sector_slots=2, max_weight=1/6)
        self.start = sorted(self.panel.date.unique())[140]

    def test_weights_frequency_caps_and_buffer(self):
        targets, audit = build_targets(self.panel, self.sectors, self.start, replace(self.config, mode="momentum", retention_rank=12))
        self.assertEqual(len(targets), 6)
        for entry in audit:
            self.assertEqual(len(entry["selected"]), 6)
            self.assertTrue(all(n <= 2 for n in entry["sector_counts"].values()))
            self.assertAlmostEqual(sum(entry["weights"].values()), 1)
        self.assertEqual(set(audit[1]["retained"]), set(audit[0]["selected"]))

    def test_future_perturbation_leaves_previous_signals_unchanged(self):
        before, _ = build_targets(self.panel, self.sectors, self.start, self.config)
        modified = self.panel.copy()
        cutoff = sorted(modified.date.unique())[185]
        modified.loc[modified.date > cutoff, ["open", "close", "high", "low"]] *= 0.6
        after, _ = build_targets(modified, self.sectors, self.start, self.config)
        self.assertEqual({d:w for d,w in before.items() if d<=cutoff},
                         {d:w for d,w in after.items() if d<=cutoff})

    def test_downtrend_stays_cash_and_missing_quotes_not_filled(self):
        modified = self.panel.copy()
        for col in ("open", "close", "high", "low"):
            modified[col] = 100 / self.panel[col]
        modified["high"], modified["low"] = modified["low"].copy(), modified["high"].copy()
        targets, _ = build_targets(modified, self.sectors, self.start, self.config)
        self.assertTrue(all(not w for w in targets.values()))
        removed = self.panel[~((self.panel.code == "600001") & (self.panel.date == self.start))]
        features = prepare_features(removed)
        self.assertFalse(features["available"].loc[self.start, "600001"])

    def test_missing_sector_and_bad_configuration_fail(self):
        with self.assertRaises(ValueError):
            build_targets(self.panel, {}, self.start, self.config)
        with self.assertRaises(ValueError):
            replace(self.config, retention_rank=1).validate()

    def test_full_universe_control_and_no_future_label_dependency(self):
        config = replace(self.config, mode="equal", trend_filter=False, top_k=12,
                         retention_rank=12, sector_slots=4, max_weight=1/12)
        targets, _ = build_targets(self.panel, self.sectors, self.start, config)
        self.assertTrue(all(len(w)==12 for w in targets.values()))
        # 截短未来依然保留相同的已经产生且有执行日的目标。
        cut = self.panel[self.panel.date <= sorted(self.panel.date.unique())[181]]
        partial, _ = build_targets(cut, self.sectors, self.start, config)
        self.assertEqual(partial, {date: targets[date] for date in partial})


if __name__ == "__main__":
    unittest.main()
