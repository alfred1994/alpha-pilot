"""风格池、因子 IC 权重与影子记录：无未来数据、方向由数据决定、可回放评估。"""
import os
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategy import style_pools as sp
from strategy import style_shadow as ss
from strategy.trend_channel import dtc_states, exposure_cap, hysteresis


def synthetic_panel(n_codes=60, n_days=520, seed=3):
    """低波动股票未来收益更高（植入的真实效应），用于验证 IC 权重方向。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days).strftime("%Y-%m-%d")
    codes = [f"{600000 + i:06d}" for i in range(n_codes)]
    vol = np.linspace(0.01, 0.05, n_codes)
    drift = 0.004 - 0.08 * vol  # 低波动 → 更高漂移
    rets = rng.normal(drift, vol, size=(n_days, n_codes))
    close = pd.DataFrame(50 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=codes)
    open_ = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.002, size=close.shape))
    high = np.maximum(close, open_) * 1.005
    low = np.minimum(close, open_) * 0.995
    return {"close": close, "open": open_, "high": high, "low": low, "raw_close": close, "raw_open": open_,
            "ref": close.shift(1), "amount": close * 0 + 2e8, "volume": close * 0 + 1e7, "flag": close * 0}


class TrendChannelTests(unittest.TestCase):
    def test_hysteresis_keeps_state_until_opposite_band(self):
        on = pd.Series([False, True, False, False, False])
        off = pd.Series([False, False, False, True, False])
        self.assertEqual(list(hysteresis(on, off)), [False, True, True, False, False])

    def test_exposure_cap_levels(self):
        idx = pd.RangeIndex(400)
        up = pd.Series(np.linspace(10, 30, 400), index=idx)
        cap = exposure_cap(up * 1.01, up * 0.99, up)
        self.assertEqual(cap.iloc[-1], 1.0)
        down = up[::-1].reset_index(drop=True)
        self.assertEqual(exposure_cap(down * 1.01, down * 0.99, down).iloc[-1], 0.0)


class StylePoolTests(unittest.TestCase):
    def setUp(self):
        self.panel = synthetic_panel()
        self.factors = sp.compute_factors(self.panel)
        self.universe = sp.universe_mask(self.panel, self.factors)
        self.dates = list(self.panel["close"].index)

    def test_factors_do_not_use_future_data(self):
        t = self.dates[300]
        changed = {k: v.copy() for k, v in self.panel.items()}
        for key in ("close", "open", "high", "low"):
            changed[key].loc[self.dates[301]:] *= 3
        again = sp.compute_factors(changed)
        for name in sp.SCORE_FACTORS + ("ret20", "vol20", "dtc_both"):
            pd.testing.assert_series_equal(self.factors[name].loc[t], again[name].loc[t], check_names=False)

    def test_universe_rules(self):
        self.assertFalse(self.universe.iloc[100].any(), "上市不满250日不入池")
        self.assertGreater(self.universe.iloc[-1].sum(), 40)
        thin = {**self.panel, "amount": self.panel["amount"] * 0 + 1e6}
        self.assertFalse(sp.universe_mask(thin, sp.compute_factors(thin)).iloc[-1].any())

    def test_ic_weights_use_only_visible_labels_and_find_planted_effect(self):
        fwd = sp.forward_returns(self.panel, 20)
        pos = len(self.dates) - 1
        weights = sp.ic_weights(self.factors, self.universe, fwd, pos)
        self.assertGreater(weights["low_vol20"]["weight"], 0, "植入的低波动效应应得到正权重")
        cutoff = pos - 20 - 1
        tampered = fwd.copy()
        tampered.iloc[cutoff + 1:] = np.random.default_rng(1).normal(size=tampered.iloc[cutoff + 1:].shape)
        again = sp.ic_weights(self.factors, self.universe, tampered, pos)
        self.assertEqual(weights["low_vol20"]["weight"], again["low_vol20"]["weight"])

    def test_pools_and_scores(self):
        date = self.dates[-1]
        weights = sp.ic_weights(self.factors, self.universe, sp.forward_returns(self.panel, 20), len(self.dates) - 1)
        scores = sp.factor_scores(self.factors, self.universe, date, weights)
        self.assertTrue(((scores > 0) & (scores <= 100)).all())
        pools = sp.select_pools(self.factors, self.universe, date, scores=scores, top_n=5)
        self.assertEqual(set(pools), set(sp.STYLES))
        low_vol = set(pools["low_vol_defensive"]["code"])
        self.assertTrue(low_vol, "低波池非空")
        self.assertTrue(low_vol <= set(self.panel["close"].columns[:20]), "低波池来自低波动股票")
        rebound = pools["oversold_rebound"]
        if not rebound.empty:
            drops = self.factors["ret20"].loc[date]
            self.assertTrue((drops[rebound["code"]] <= drops.quantile(0.2) + 1e-12).all(), "超跌池来自20日跌幅前20%")
        self.assertTrue(pools["quality_value"].empty, "无财务快照时基本面池为空而不是猜测")
        self.assertEqual(list(pools["factor_top"]["rank"]), [1, 2, 3, 4, 5])

    def test_quality_pool_requires_fundamentals(self):
        date = self.dates[-1]
        codes = self.panel["close"].columns
        fundamentals = pd.DataFrame({"roe": 15.0, "np_yoy": 10.0, "debt_ratio": 40.0, "cash_content": 100.0}, index=codes)
        fundamentals.loc[codes[:30], "roe"] = 5.0
        valuations = pd.DataFrame({"pe_ttm": 20.0, "name": "测试"}, index=codes)
        pools = sp.select_pools(self.factors, self.universe, date, fundamentals=fundamentals, valuations=valuations)
        self.assertTrue(set(pools["quality_value"]["code"]) <= set(codes[30:]))


class ShadowRunTests(unittest.TestCase):
    def test_run_daily_records_and_evaluate_reads_back(self):
        panel = synthetic_panel()
        dates = list(panel["close"].index)
        cut = {k: v.loc[:dates[-30]] for k, v in panel.items()}
        with tempfile.TemporaryDirectory() as folder:
            db = os.path.join(folder, "shadow.db")
            main_db = os.path.join(folder, "missing_main.db")
            summary = ss.run_daily(panel=cut, db_path=db, main_db=main_db, fetch_external=False,
                                   now=datetime(2025, 8, 1))
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["date"], dates[-30])
            self.assertIsNone(summary["market_exposure_cap"], "无指数数据时仓位上限为未知")
            again = ss.run_daily(panel=cut, db_path=db, main_db=main_db, fetch_external=False, now=datetime(2025, 8, 1))
            self.assertEqual(again["pools"], summary["pools"], "同日重跑幂等")
            report = ss.evaluate(db_path=db, main_db=main_db, panel=panel, horizons=(5,))
            self.assertEqual(report["status"], "ok")
            self.assertIn("low_vol_defensive", report["styles"])
            self.assertFalse(report["styles"]["low_vol_defensive"]["promotion_ready"], "单日样本不得晋级")
            self.assertIn("晋级门槛", ss.format_report(report))

    def test_ml_shadow_scores_rescale_skip_history_and_report_rank_ic(self):
        panel = synthetic_panel()
        panel["raw_close"] = panel["close"] / 3  # 后复权价 ≠ 不复权价
        dates = list(panel["close"].index)
        codes = list(panel["close"].columns)
        frames = ss._ml_frames(panel, dates[-1], codes[:2] + ["999999"])
        self.assertEqual(sorted(frames), codes[:2])
        self.assertAlmostEqual(frames[codes[0]]["close"].iloc[-1], panel["raw_close"][codes[0]].iloc[-1])
        self.assertEqual(len(frames[codes[0]]), ss.ML_HISTORY_BARS)

        with tempfile.TemporaryDirectory() as folder:
            db = os.path.join(folder, "shadow.db")
            seen = []

            def fake_predict(frames):
                seen.append(len(frames))
                return {code: float(i) for i, code in enumerate(frames)}, {"model_version": "v", "data_cutoff": dates[-1]}
            with closing(ss.connect(db)) as conn:
                self.assertEqual(ss.record_ml_scores(conn, panel, dates[-2], codes, fake_predict)["reason"],
                                 "not_latest_date")
                self.assertEqual(ss.record_ml_scores(conn, panel, dates[-1], codes, fake_predict)["scored"], len(codes))
                late = ss.record_ml_scores(conn, panel, dates[-1], codes,
                                           lambda f: ({"600000": 1.0}, {"data_cutoff": "2999-01-01"}))
                self.assertEqual(late["status"], "skipped")
                self.assertEqual(ss.record_ml_scores(conn, panel, dates[-1], codes,
                                                     lambda f: ({}, {"reason": "artifact_not_found"}))["status"],
                                 "unavailable")
                # 植入完美信号：ML 分 = 历史某日之后 5 日真实收益
                fwd = sp.forward_returns(panel, 5)
                rows = [(d, c, float(fwd.loc[d, c]), "v") for d in dates[-40:-20] for c in codes]
                conn.executemany("INSERT OR REPLACE INTO ml_scores VALUES (?,?,?,?)", rows)
                conn.execute("INSERT INTO style_candidates VALUES (?,?,?,?,?,?)",
                             (dates[-40], "factor_top", codes[0], 1, 1.0, "{}"))
                conn.commit()
            self.assertEqual(seen, [len(codes)] * 1)
            report = ss.evaluate(db_path=db, main_db=os.path.join(folder, "none.db"), panel=panel, horizons=(5,))
            self.assertEqual(report["ml"]["5d"]["signal_days"], 20)
            self.assertGreater(report["ml"]["5d"]["mean_rank_ic"], 0.9)
            self.assertTrue(report["ml"]["promotion_ready"])
            self.assertIn("池内 ML 分", ss.format_report(report))

    def test_latest_report_calendar(self):
        self.assertEqual(ss.latest_report(datetime(2026, 11, 2)), "2026-3")
        self.assertEqual(ss.latest_report(datetime(2026, 9, 1)), "2026-2")
        self.assertEqual(ss.latest_report(datetime(2026, 5, 6)), "2026-1")
        self.assertEqual(ss.latest_report(datetime(2026, 3, 1)), "2025-4")

    def test_fundamental_refresh_respects_budget_and_priority(self):
        class Client:
            available = True
            calls = []

            def get_indicators(self, code, report):
                self.calls.append((code, report))
                return {"abilities": [{"indicators": [{"index_id": "index_weighted_avg_roe", "value": "12.5"},
                                                      {"index_id": "assets_debt_ratio", "value": "30"}]}]}
        with tempfile.TemporaryDirectory() as folder:
            conn = ss.connect(os.path.join(folder, "s.db"))
            conn.execute("INSERT INTO fundamentals (code, report, fetched_at, roe) VALUES ('000001','2026-3','2026-11-01T00:00:00',9)")
            client = Client()
            done = ss.refresh_fundamentals(conn, client, ["000001", "000002", "000003"], datetime(2026, 11, 2), budget=1)
            self.assertEqual(done, 1)
            self.assertEqual(client.calls[0][0], "000002", "缺失的先补，已是最新报告期的不重复拉取")
            row = conn.execute("SELECT roe, debt_ratio, report FROM fundamentals WHERE code='000002'").fetchone()
            self.assertEqual(row, (12.5, 30.0, "2026-3"))
            conn.close()


if __name__ == "__main__":
    unittest.main()
