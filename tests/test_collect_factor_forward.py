"""公开行情采集的固定股票池、缺失拒绝和不可覆盖快照。"""
from datetime import datetime
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.ml_forward import BJ
from scripts.collect_factor_forward import collect, fetch_verified, identity_stock_adjustments


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "snapshot"
        self.now = datetime(2026, 10, 13, 18, tzinfo=BJ)
        self.calendar = {"error_code": "0", "data": [["2026-10-12", "1"], ["2026-10-13", "1"], ["2026-10-14", "1"]]}
        self.seed = {"expected_codes": ["600001", "600002"], "bars": [
            {"date": "2026-10-12", "code": code, "open": 10., "high": 11., "low": 9., "close": 10., "volume": 1000.}
            for code in ("600001", "600002")]}

    def fetch(self, code):
        frame = pd.DataFrame({"data_date": ["2026-10-12", "2026-10-13", "2026-10-14"],
                              "open": [10., 10., 10.], "high": [11., 11., 11.], "low": [9., 9., 9.],
                              "close": [10., 10., 10.], "volume_raw": [1000., 1000., 1000.]})
        frame.attrs["adjustment"] = "qfq"
        return frame

    def test_complete_snapshot_keeps_expected_pool_and_excludes_future(self):
        result = collect(self.seed, self.calendar, self.output, fetch=self.fetch, now=self.now)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["rows"], 8)
        with gzip.open(self.output / "stocks.json.gz", "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        self.assertEqual(data["expected_codes"], self.seed["expected_codes"])
        self.assertEqual(len(data["bars"]), 4)
        self.assertFalse(data["metadata"]["corporate_actions_verified"])
        with self.assertRaises(FileExistsError):
            collect(self.seed, self.calendar, self.output, fetch=self.fetch, now=self.now)

    def test_missing_suspended_security_is_never_silently_deleted(self):
        def fetch(code):
            result = self.fetch(code)
            return result.iloc[:1] if code == "600001" else result
        result = collect(self.seed, self.calendar, self.output, fetch=fetch, now=self.now)
        self.assertEqual(result["status"], "waiting_data")
        self.assertIn("suspension_evidence", result["failures"][0]["reason"])
        with gzip.open(self.output / "stocks.json.gz", "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        self.assertEqual(data["expected_codes"], self.seed["expected_codes"])
        self.assertEqual({r["code"] for r in data["bars"]}, {"600002"})

    def test_unknown_adjustment_and_network_errors_remain_visible(self):
        def fetch(code):
            if code == "600001":
                raise TimeoutError("network unavailable")
            result = self.fetch(code)
            if code == "600002":
                result.attrs["adjustment"] = "unadjusted"
            return result
        result = collect(self.seed, self.calendar, self.output, fetch=fetch, now=self.now)
        self.assertEqual({r["reason"] for r in result["failures"]}, {"TimeoutError", "unverified_adjustment"})

    def test_bad_budget_or_seed_rejected_before_network(self):
        def forbidden(code):
            raise AssertionError("must not fetch")
        for workers in (0, 5, True):
            with self.assertRaises(ValueError):
                collect(self.seed, self.calendar, self.output, workers=workers, fetch=forbidden, now=self.now)
        self.seed["expected_codes"].append("600003")
        with self.assertRaisesRegex(ValueError, "股票池"):
            collect(self.seed, self.calendar, self.output, fetch=forbidden, now=self.now)
        self.assertFalse(self.output.exists())

    def test_stock_identity_requires_complete_finite_unit_factors(self):
        payload = {"total": 2, "data": [{"d": "1900-01-01", "f": "1"}, {"d": "2026-10-12", "f": "1"}]}
        valid = "var qfq=" + json.dumps(payload)
        self.assertEqual(identity_stock_adjustments(valid, "2026-10-13"), payload)
        raw = self.fetch("600001")
        raw.attrs["adjustment"] = "unadjusted"
        result = fetch_verified("600001", "2026-10-13", kline=lambda c: raw, events=lambda kind: valid)
        self.assertEqual(result.attrs["adjustment"], "qfq")
        self.assertEqual(set(result.attrs["verification_evidence"]["identity_adjustments"]), {"qfq", "hfq"})
        for value in ("1.1", "nan", "inf"):
            payload["data"][0]["f"] = value
            with self.assertRaises(ValueError):
                identity_stock_adjustments(json.dumps(payload), "2026-10-13")
        with self.assertRaises(ValueError):
            identity_stock_adjustments('{"total":0,"data":[]}', "2026-10-13")

    def test_missing_day_only_accepts_explicit_provider_suspension(self):
        raw = self.fetch("600001").iloc[:1]
        payload = {"error_code": "0", "fields": ["date", "code", "open", "high", "low", "close", "volume", "tradestatus"],
                   "data": [["2026-10-13", "sh.600001", "10", "10", "10", "10", "", "0"]]}
        result = fetch_verified("600001", "2026-10-13", kline=lambda c: raw, suspension=lambda: payload)
        self.assertEqual(result.iloc[-1].volume_raw, 0)
        self.assertEqual(result.iloc[-1].data_date, "2026-10-13")
        self.assertIn("suspension", result.attrs["verification_evidence"])
        payload["data"][0][-1] = "1"
        unchanged = fetch_verified("600001", "2026-10-13", kline=lambda c: raw, suspension=lambda: payload)
        self.assertEqual(len(unchanged), 1)

    def test_retry_only_missing_codes_without_changing_previous_snapshot(self):
        def missing(code):
            return pd.DataFrame() if code == "600001" else self.fetch(code)
        collect(self.seed, self.calendar, self.output, fetch=missing, now=self.now)
        original = (self.output / "stocks.json.gz").read_bytes()
        fetched = []
        def record(code):
            fetched.append(code)
            return self.fetch(code)
        dest = Path(self.temp.name) / "retry"
        result = collect(self.seed, self.calendar, dest, fetch=record, now=self.now, reuse=self.output)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(fetched, ["600001"])
        self.assertEqual(original, (self.output / "stocks.json.gz").read_bytes())
        self.assertIn("reused_files", json.loads((dest / "manifest.json").read_text(encoding="utf-8")))

    def test_baostock_fallback_records_source_and_explicit_suspension(self):
        payload = {"error_code": "0", "fields": ["date", "code", "open", "high", "low", "close", "volume", "tradestatus"],
                   "data": [["2026-10-12", "sh.600001", "10", "11", "9", "10", "1000", "1"],
                            ["2026-10-13", "sh.600001", "10", "10", "10", "10", "", "0"]]}
        result = fetch_verified("600001", "2026-10-13", start="2026-10-12", kline=lambda c: pd.DataFrame(), fallback=lambda: payload)
        self.assertEqual(result.attrs["adjustment"], "qfq")
        self.assertEqual(result.iloc[-1].volume_raw, 0)
        self.assertEqual(result.attrs["verification_evidence"]["fallback_history"]["raw"], payload)
        payload["data"][1][-1] = "1"
        with self.assertRaises(ValueError):
            fetch_verified("600001", "2026-10-13", start="2026-10-12", kline=lambda c: pd.DataFrame(), fallback=lambda: payload)


if __name__ == "__main__":
    unittest.main()
