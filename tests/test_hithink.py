"""Fuyao 官方 REST 合同的离线回归；不调用付费或真实数据服务。"""
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import requests
from data.hithink import HiThinkClient, HiThinkError, HiThinkUnavailable, normalize_code, _date_ms, get_client


def response(data=None, *, code=0, status=200, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = {"code": code, "data": data if data is not None else {"item": []}}
    return result


class HiThinkTests(unittest.TestCase):
    def setUp(self):
        self.client = HiThinkClient("secret-test-key", min_interval=0)
        self.net = patch("data.hithink.requests.get").start()
        self.sleep = patch("data.hithink.time.sleep").start()
        self.addCleanup(patch.stopall)

    def test_missing_key_never_calls_network(self):
        with patch.dict(os.environ, {}, clear=True):
            client = HiThinkClient()
        self.assertFalse(client.available)
        with self.assertRaises(HiThinkUnavailable):
            client.get_tickers()
        self.net.assert_not_called()

    def test_alias_key(self):
        with patch.dict(os.environ, {"FUYAO_API_KEY": "key"}, clear=True):
            self.assertTrue(HiThinkClient().available)

    def test_explicit_feature_gate(self):
        with patch.dict(os.environ, {"FUYAO_API_KEY": "key"}, clear=True):
            self.assertIsNone(get_client())
            os.environ["HITHINK_ENABLED"] = "1"
            self.assertIsNotNone(get_client())
            del os.environ["FUYAO_API_KEY"]
            self.assertIsNone(get_client())
        self.net.assert_not_called()

    def test_snapshot_page_retains_batch_timestamp_only(self):
        self.net.return_value = response({"timestamp": 42, "total": 5000, "item": []})
        result = self.client.get_snapshot_page(limit=100, offset=100)
        self.assertEqual(result["total"], 5000)
        self.assertFalse(result["per_symbol_timestamp_available"])
        self.assertEqual(self.net.call_args.kwargs["params"], {"limit": 100, "offset": 100})

    def test_normalization(self):
        for raw, expected in [("sh.600000", "600000.SH"), ("000001", "000001.SZ"),
                              ("920002", "920002.BJ"), ("000001.SH", "000001.SH")]:
            self.assertEqual(normalize_code(raw), expected)
        for bad in ("abc600000xyz", "60000", "600000.SH,000001.SZ"):
            with self.assertRaises(ValueError):
                normalize_code(bad)

    def test_http_and_business_auth_no_retry(self):
        for code, status in [(2001, 200), (2003, 200), (0, 401), (0, 302)]:
            self.net.reset_mock()
            self.net.return_value = response(code=code, status=status)
            with self.assertRaises(HiThinkError):
                self.client.get_tickers()
            self.assertEqual(self.net.call_count, 1)
            self.net.return_value.close.assert_called_once()

    def test_rate_limit_both_forms_and_bounded(self):
        self.net.side_effect = [response(status=429), response(code=4001), response()]
        self.assertEqual(self.client.get_tickers()["item"], [])
        self.assertEqual(self.net.call_count, 3)
        self.net.side_effect = [response(status=429)] * 3
        with self.assertRaisesRegex(HiThinkError, "rate limit exhausted"):
            self.client.get_tickers()

    def test_retry_after_respected_or_deferred(self):
        self.net.side_effect = [response(status=429, headers={"Retry-After": "10"}), response()]
        self.client.get_tickers()
        self.sleep.assert_any_call(10)
        self.net.side_effect = [response(status=429, headers={"Retry-After": "120"})]
        with self.assertRaisesRegex(HiThinkError, "later retry"):
            self.client.get_tickers()

    def test_safe_errors(self):
        self.net.side_effect = requests.ConnectionError("secret-test-key")
        with self.assertRaises(HiThinkError) as error:
            self.client.get_tickers()
        self.assertNotIn("secret-test-key", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_envelope_validation(self):
        for invalid in ([], {}, {"code": False}, {"code": 0, "data": None},
                        {"code": 0, "data": {"item": "bad"}}):
            self.net.return_value = response()
            self.net.return_value.json.return_value = invalid
            with self.assertRaises(HiThinkError):
                self.client.get_tickers()

    def test_daily_units_timezone_adjust_missing_values(self):
        self.net.return_value = response({"timestamp": 42, "item": [{
            "date_ms": _date_ms("2026-09-04"), "open_price": 10, "high_price": 12,
            "low_price": 9, "close_price": 11, "volume": 123400, "turnover": None}]})
        frame = self.client.get_daily("600000", "2026-09-01", "2026-09-05", "hfq")
        self.assertEqual(frame.iloc[0]["volume"], 123400)
        self.assertTrue(pd.isna(frame.iloc[0]["amount"]))
        self.assertEqual(frame.iloc[0]["date"], "2026-09-04")
        self.assertEqual(frame.attrs["adjust"], "backward")
        options = self.net.call_args.kwargs
        self.assertEqual(options["params"]["adjust"], "backward")
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(options["headers"], {"X-api-key": "secret-test-key"})
        self.assertEqual(self.net.call_args.args[0], "https://fuyao.aicubes.cn/api/a-share/prices/historical")

    def test_bad_bars_rejected_empty_schema(self):
        self.net.return_value = response({"item": [{"date_ms": "bad"}, {
            "date_ms": _date_ms("2026-09-04"), "open_price": float("nan"),
            "high_price": 12, "low_price": 9, "close_price": 11}]})
        frame = self.client.get_daily("600000", "2026-09-01", "2026-09-05")
        self.assertTrue(frame.empty)
        self.assertEqual(frame.attrs["rejected_rows"], 2)
        self.assertEqual(list(frame), ["date", "open", "high", "low", "close", "volume", "amount"])

    def test_window_and_adjust_validation_before_request(self):
        for start, end, adjust in [("2000-01-01", "2026-01-01", "qfq"),
                                   ("2026-09-05", "2026-09-01", "qfq"),
                                   ("2026-09-01", "2026-09-05", "oops")]:
            with self.assertRaises(ValueError):
                self.client.get_daily("600000", start, end, adjust)
        self.net.assert_not_called()

    def test_batch_missing_and_timestamp_scope(self):
        codes = [f"{600000+i}.SH" for i in range(101)]
        self.net.side_effect = [response({"timestamp": 123, "item": [{"thscode": codes[0], "volume": 100}]}),
                                response({"timestamp": 456, "item": []})]
        data = self.client.get_snapshot(codes)
        self.assertEqual(self.net.call_count, 2)
        self.assertEqual(len(data["missing_codes"]), 100)
        self.assertNotIn("timestamp", data["item"][0])
        self.assertFalse(data["per_symbol_timestamp_available"])
        self.assertEqual(data["batches"][0]["latest_upstream_timestamp_ms"], 123)

    def test_financial_contract_preserves_null_disclosure(self):
        raw = {"item": [{"report_date_ms": 123, "period_end_ms": 100, "net_profit": None}]}
        self.net.return_value = response(raw)
        self.assertEqual(self.client.get_financials("600000"), raw)
        self.assertEqual(self.net.call_args.kwargs["params"]["limit"], 4)
        self.client.get_financials("600000", "balance-sheets", start_date="2025-01-01", end_date="2025-12-31")
        self.assertNotIn("limit", self.net.call_args.kwargs["params"])
        with self.assertRaises(ValueError):
            self.client.get_financials("600000", limit=4, start_date="2025-01-01", end_date="2025-12-31")

    def test_other_endpoints(self):
        self.net.return_value = response()
        self.client.get_indicators("600000", "2025-4")
        self.assertEqual(self.net.call_args.kwargs["params"]["report"], "2025-4")
        self.client.get_valuations(["600000"])
        self.assertTrue(self.net.call_args.args[0].endswith("/valuations/snapshot"))
        self.client.get_market_dump()
        self.assertTrue(self.net.call_args.args[0].endswith("/api/dump/market-dumps/daily-k-10d/download-url"))
        self.assertEqual(self.net.call_count, 3)


if __name__ == "__main__":
    unittest.main()
