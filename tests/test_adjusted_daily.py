"""等比复权日线库：除权参考价、停牌除权、增量链条与读取口径。"""
import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data import adjusted_daily as ad


def bars(code, closes, start="2026-01-05"):
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"code": code, "date": dates, "open": closes, "high": [c * 1.01 for c in closes],
                         "low": [c * 0.99 for c in closes], "close": closes, "volume": 1e6, "amount": 1e8})


def event(code, date, dividend=0.0, bonus=0.0, allot_ratio=0.0, allot_price=0.0):
    return pd.DataFrame([{"code": code, "ex_date": date, "dividend": dividend, "bonus": bonus,
                          "allot_ratio": allot_ratio, "allot_price": allot_price}])


class AdjustedDailyTests(unittest.TestCase):
    def test_cash_dividend_and_bonus_use_exchange_reference_price(self):
        raw = pd.concat([bars("600001", [10, 10, 9.6]), bars("000002", [20, 20, 10.2])])
        events = pd.concat([event("600001", "2026-01-07", dividend=0.5), event("000002", "2026-01-07", bonus=1.0)])
        out = ad.compute_adjusted(ad.normalize_raw(raw), ad.normalize_events(events)).set_index(["code", "date"])
        self.assertAlmostEqual(out.loc[("600001", "2026-01-07"), "ref"], 9.5)
        self.assertAlmostEqual(out.loc[("600001", "2026-01-07"), "ret"], 9.6 / 9.5 - 1)
        self.assertAlmostEqual(out.loc[("000002", "2026-01-07"), "ret"], 0.02)
        self.assertEqual(int(out["flag"].sum()), 0)
        # 后复权：首日因子为1，除权日后因子放大，复权价比值等于链式收益
        adj = out["close"] * out["factor"]
        self.assertAlmostEqual(adj.loc[("000002", "2026-01-07")] / adj.loc[("000002", "2026-01-06")], 1.02)

    def test_event_during_suspension_applies_on_resume_day(self):
        raw = bars("600001", [10, 10, 9.6]).drop(index=1)  # 01-06 停牌
        out = ad.compute_adjusted(ad.normalize_raw(raw), ad.normalize_events(event("600001", "2026-01-06", dividend=0.5)))
        resumed = out[out["date"] == "2026-01-07"].iloc[0]
        self.assertAlmostEqual(resumed["ret"], 9.6 / 9.5 - 1)

    def test_outlier_beyond_price_limit_is_neutralized_and_flagged(self):
        out = ad.compute_adjusted(ad.normalize_raw(bars("600001", [10, 10, 15])), ad.normalize_events(None))
        last = out.iloc[-1]
        self.assertEqual((last["ret"], last["flag"]), (0.0, 1))
        chinext = ad.compute_adjusted(ad.normalize_raw(bars("300001", [10, 10, 11.9], start="2026-03-02")),
                                      ad.normalize_events(None))
        self.assertEqual(int(chinext["flag"].sum()), 0)

    def test_ineligible_boards_are_excluded(self):
        raw = pd.concat([bars("688001", [10, 11]), bars("830001", [10, 11]), bars("600001", [10, 11])])
        self.assertEqual(set(ad.normalize_raw(raw)["code"]), {"600001"})

    def test_incremental_chain_matches_full_build(self):
        closes = [10, 10.5, 10.2, 9.8, 10.1, 10.6, 10.9, 10.4]
        raw = bars("600001", closes)
        events = event("600001", "2026-01-12", dividend=0.3)
        with tempfile.TemporaryDirectory() as folder:
            full_db, inc_db = os.path.join(folder, "full.db"), os.path.join(folder, "inc.db")
            ad.store(raw, events, db_path=full_db, full=True)
            ad.store(raw.iloc[:4], events.iloc[0:0], db_path=inc_db, full=True)
            summary = ad.store(raw.iloc[3:], events, db_path=inc_db, full=False)
            self.assertEqual(summary["mode"], "incremental")
            a = ad.load_panel("2026-01-01", db_path=full_db)["close"]["600001"]
            b = ad.load_panel("2026-01-01", db_path=inc_db)["close"]["600001"]
            np.testing.assert_allclose(a.values, b.values, rtol=1e-12)
            self.assertEqual(ad.status(db_path=inc_db)["max_date"], raw["date"].iloc[-1])

    def test_panel_exposes_raw_and_adjusted_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "p.db")
            ad.store(bars("600001", [10, 10, 9.6]), event("600001", "2026-01-07", dividend=0.5), db_path=path, full=True)
            panel = ad.load_panel("2026-01-01", db_path=path)
            self.assertEqual(panel["raw_close"]["600001"].iloc[-1], 9.6)
            close = panel["close"]["600001"]
            self.assertAlmostEqual(close.iloc[-1] / close.iloc[-2], 9.6 / 9.5)
            self.assertFalse(ad.status(db_path=os.path.join(folder, "missing.db"))["available"])

    def test_dump_download_only_from_trusted_https_host(self):
        for url in ("http://o.thsi.cn/x.parquet", "https://evil.example.com/x.parquet", ""):
            with self.assertRaises(ValueError):
                ad._download(url, os.path.join(tempfile.gettempdir(), "never.parquet"))


if __name__ == "__main__":
    unittest.main()
