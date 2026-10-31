"""前向冻结、实际记录时间、严格成交门禁和诊断证据回归。"""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.ml_diagnostics import audit_stale_marks, daily_rank_ic, parse_calendar
from research.ml_forward import BJ, advance, initialize, read_status
from strategy.ml_research import MLResearchConfig


def fixture():
    dates = pd.bdate_range("2025-01-02", periods=155)
    rows = []
    for i, code in enumerate([f"60000{i}" for i in range(1, 9)] + ["510300", "510500"]):
        rng = np.random.default_rng(i+70)
        prices = (10+i)*np.exp(np.cumsum(rng.normal(0.001, .003, len(dates))))
        for j, day in enumerate(dates):
            price = prices[j]
            rows.append(dict(date=day, code=code, open=price*.999, high=price*1.01, low=price*.99,
                             close=price, volume=float(100000+rng.integers(1, 100000))))
    frame = pd.DataFrame(rows).sort_values(["date", "code"]).reset_index(drop=True)
    calendar = {"error_code": "0", "data": [[day.strftime("%Y-%m-%d"), "1" if day.weekday()<5 else "0"]
                 for day in pd.date_range(dates[0], dates[-1])]}
    return frame, dates, calendar


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"forward.db"
        self.panel, self.dates, self.calendar = fixture()
        self.config = MLResearchConfig(train_days=40, valid_days=20, horizon=3, top_k=3, min_stocks=5, threads=1)

    def now(self, index, hour=16):
        return self.dates[index].to_pydatetime().replace(hour=hour, tzinfo=BJ)

    def data(self, index, history=True):
        frame = self.panel[self.panel.date <= self.dates[index]] if history else self.panel[self.panel.date == self.dates[index]]
        return frame[frame.code.str.startswith("6")].copy(), frame[frame.code.str.startswith("5")].copy()

    def execution(self, end, missing=False):
        frame = self.panel[(self.panel.date >= self.dates[120]) & (self.panel.date <= self.dates[end])].copy()
        frame["raw_open"], frame["raw_close"] = frame.open, frame.close
        frame["limit_up"], frame["limit_down"] = frame.close*1.1, frame.close*.9
        frame["tradable"], frame["eligible"], frame["corporate_action"] = True, True, False
        frame["source"] = "fixture"
        frame["date"] = frame.date.dt.strftime("%Y-%m-%d")
        if missing:
            frame = frame.drop(columns="raw_open")
        return {"bars": frame.to_dict(orient="records"), "metadata": {
            "source": "fixture", "calendar_source": "fixture", "adjustment": "raw",
            "universe": "forward_observed", "corporate_actions_verified": True}}

    def init(self, **kwargs):
        return initialize(self.path, *self.data(120), self.calendar, config=self.config, now=self.now(120), **kwargs)

    def step(self, index, **kwargs):
        return advance(self.path, *self.data(index), self.calendar, now=self.now(index), **kwargs)

    def test_initialize_has_no_historical_profit_and_immutable_model(self):
        result = self.init()
        status = read_status(self.path)
        self.assertEqual(result["observed_days"], 0)
        self.assertEqual(result["mature_label_rows"], 0)
        self.assertIsNone(result["execution"]["accounts"])
        self.assertFalse(result["promotion_allowed"])
        self.assertLess(status["manifest"]["train_label_end"], status["manifest"]["initial_date"])
        self.assertEqual(status["manifest"]["model_policy"], "fixed_until_observation_end_no_retraining")
        with self.assertRaises(FileExistsError):
            self.init()

    def test_next_day_records_prediction_without_inventing_pnl_and_is_idempotent(self):
        self.init()
        result = self.step(121)
        self.assertEqual(result["observed_days"], 1)
        self.assertEqual(result["execution"]["status"], "waiting_execution_data")
        self.assertIsNone(result["execution"]["accounts"])
        self.assertEqual(self.step(121)["status"], "unchanged")
        self.assertEqual(read_status(self.path)["latest"], result)

    def test_strict_account_needs_data_and_replays_frozen_signals(self):
        self.init(execution_payload=self.execution(120))
        first = self.step(121, execution_payload=self.execution(121))
        self.assertEqual(first["execution"]["status"], "strict_simulation")
        accounts = first["execution"]["accounts"]
        self.assertGreater(accounts["lightgbm"]["metrics"]["trade_count"], 0)
        self.assertEqual(accounts["510300"]["metrics"]["stamp_tax"], 0)
        later = self.step(122, execution_payload=self.execution(122))
        for name, account in accounts.items():
            self.assertEqual(later["execution"]["accounts"][name]["curve"][:-1], account["curve"])
            for trade in account["trades"]:
                self.assertGreater(trade["date"], trade["signal_date"])
        self.assertEqual(later["mature_label_rows"], 0)

    def test_missing_strict_metadata_never_becomes_cash_return(self):
        self.init()
        result = self.step(121, execution_payload=self.execution(121, missing=True))
        self.assertEqual(result["execution"]["status"], "waiting_execution_data")
        self.assertIn("incomplete_raw_open", result["execution"]["reasons"])
        self.assertIsNone(result["execution"]["accounts"])
        repaired = self.step(122, execution_payload=self.execution(122))
        self.assertEqual(repaired["execution"]["status"], "strict_simulation")

    def test_future_or_stale_history_and_late_initial_signal_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "未来"):
            initialize(self.path, *self.data(121), self.calendar, config=self.config, now=self.now(120))
        with self.assertRaisesRegex(ValueError, "开盘"):
            initialize(self.path, *self.data(120), self.calendar, config=self.config, now=self.now(121,10))
        with self.assertRaisesRegex(ValueError, "覆盖不足"):
            initialize(self.path, *self.data(119), self.calendar, config=self.config, now=self.now(120))
        self.assertFalse(self.path.exists())

    def test_no_skipped_day_or_intraday_advance(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "遗漏"):
            self.step(122)
        with self.assertRaisesRegex(ValueError, "未来"):
            advance(self.path, *self.data(121), self.calendar, now=self.now(121,14))
        self.assertEqual(read_status(self.path)["latest"]["observed_days"], 0)

    def test_frozen_model_code_and_calendar_are_checked(self):
        self.init()
        with patch("research.ml_forward.implementation", return_value={"changed": True}):
            with self.assertRaisesRegex(ValueError, "指纹"):
                self.step(121)
        calendar = json.loads(json.dumps(self.calendar))
        for row in calendar["data"]:
            if row[0] == self.dates[130].strftime("%Y-%m-%d"):
                row[1] = "0"
        with self.assertRaisesRegex(ValueError, "日历修订"):
            advance(self.path, *self.data(121), calendar, now=self.now(121))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE seed SET model=model || ' changed'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "校验"):
            self.step(121)

    def test_history_and_execution_revisions_are_rejected(self):
        self.init(execution_payload=self.execution(120))
        stocks, etfs = self.data(121)
        stocks.loc[stocks.date == self.dates[119], "volume"] *= 2
        with self.assertRaisesRegex(ValueError, "特征行情发生修订"):
            advance(self.path, stocks, etfs, self.calendar, now=self.now(121))
        revised = self.execution(121)
        revised["bars"][0]["raw_open"] += .0001
        with self.assertRaisesRegex(ValueError, "执行行情发生修订"):
            self.step(121, execution_payload=revised)
        self.assertEqual(read_status(self.path)["latest"]["observed_days"], 0)

    def test_same_day_revision_is_not_hidden_by_idempotence(self):
        self.init()
        stocks, etfs = self.data(120)
        stocks.loc[stocks.date == self.dates[120], "volume"] *= 2
        with self.assertRaisesRegex(ValueError, "当日行情发生修订"):
            advance(self.path, stocks, etfs, self.calendar, now=self.now(120))

    def test_daily_only_input_and_mature_labels(self):
        self.init()
        for index in range(121,126):
            result = advance(self.path, *self.data(index, history=False), self.calendar, now=self.now(index))
        self.assertGreater(result["mature_label_rows"], 0)
        with closing(sqlite3.connect(self.path)) as db:
            days = [(d, json.loads(w)) for d,w in db.execute("SELECT day,weights FROM days ORDER BY day")]
        self.assertTrue(days[0][1])
        self.assertFalse(days[1][1])
        self.assertTrue(days[3][1])
        self.assertFalse(result["promotion_allowed"])

    def test_terminal_observation_does_not_refit_or_extend(self):
        self.init(evaluation_days=2)
        self.step(121)
        result = self.step(122)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.step(123)["observed_days"], 2)
        self.assertEqual(read_status(self.path)["latest"], result)

    def test_raw_company_action_cannot_be_ignored(self):
        self.init()
        raw = self.execution(121)
        raw["bars"][-1]["corporate_action"] = True
        with self.assertRaisesRegex(ValueError, "corporate_action"):
            self.step(121, execution_payload=raw)

    def test_missing_current_stock_blocks_capture(self):
        self.init()
        stocks, etfs = self.data(121)
        stocks = stocks[~(stocks.date.eq(self.dates[121]) & stocks.code.eq("600001"))]
        with self.assertRaisesRegex(ValueError, "覆盖不足"):
            advance(self.path, stocks, etfs, self.calendar, now=self.now(121))
        self.assertEqual(read_status(self.path)["latest"]["observed_days"], 0)

    def test_concurrent_daily_calls_only_append_once(self):
        self.init()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _:self.step(121), range(2)))
        self.assertEqual(sorted(r["status"] for r in results), ["advanced", "unchanged"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM days").fetchone()[0], 2)

    def test_calendar_extension_is_saved_and_cannot_be_removed_later(self):
        self.init()
        calendar = json.loads(json.dumps(self.calendar))
        last = pd.Timestamp(calendar["data"][-1][0])
        calendar["data"] += [[d.strftime("%Y-%m-%d"), "1" if d.weekday()<5 else "0"]
                             for d in pd.date_range(last + pd.Timedelta(days=1), periods=7)]
        advance(self.path, *self.data(121), calendar, now=self.now(121))
        with self.assertRaisesRegex(ValueError, "日历修订"):
            self.step(122)


class DiagnosticTests(unittest.TestCase):
    def test_independent_calendar_rejects_truncation_duplicate_and_failure(self):
        _,dates,calendar = fixture()
        start,end = dates[0].strftime("%Y-%m-%d"),dates[-1].strftime("%Y-%m-%d")
        self.assertEqual(len(parse_calendar(calendar,start,end)),len(dates))
        for bad in ({"error_code":"1","data":[]}, {"error_code":"0","data":calendar["data"][:-1]},
                    {"error_code":"0","data":calendar["data"]+calendar["data"][:1]}):
            with self.assertRaises(ValueError):parse_calendar(bad,start,end)

    def test_suspension_evidence_does_not_replace_unresolved_prices(self):
        accounts = {"model":{"curve":[{"date":"2025-01-03","stale_marks":["600001","600002"]}]}}
        evidence = [{"error_code":"0","fields":["date","code","tradestatus"],"data":[["2025-01-03","sh.600001","0"]]}]
        result = audit_stale_marks(accounts,evidence)["model"]
        self.assertEqual(result["confirmed_suspended_events"],1)
        self.assertEqual(result["unresolved_events"],1)
        self.assertTrue(result["valuation_still_approximate"])
        evidence.append({"error_code":"0","fields":["date","code","tradestatus"],"data":[["2025-01-03","sh.600001","1"]]})
        with self.assertRaisesRegex(ValueError,"冲突"):audit_stale_marks(accounts,evidence)

    def test_rank_ic_keeps_negative_signal_and_ignores_unmatured_labels(self):
        frame = pd.DataFrame([dict(date="2025-01-03",fold=1,code=str(i),score=-i,target=float(i)) for i in range(8)])
        frame.loc[len(frame)] = dict(date="2025-01-03",fold=1,code="future",score=100,target=np.nan)
        result = daily_rank_ic(frame,["score"])
        self.assertAlmostEqual(result.iloc[0].score,-1)
        self.assertEqual(result.iloc[0].stocks,8)


if __name__ == "__main__":
    unittest.main()
