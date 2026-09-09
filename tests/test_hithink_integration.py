"""同花顺历史/研究接入离线回归，不调用真实行情。"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data import history, research_universe
from data.hithink_research import build_research_context


class HiThinkIntegrationTests(unittest.TestCase):
    def frame(self, adjust="forward"):
        df = pd.DataFrame([dict(date="2026-09-01", open=10, high=11, low=9,
                                close=10.5, volume=1000, amount=10500)])
        df.attrs.update(adjust=adjust, source="hithink")
        return df

    def test_disabled_and_missing_key_zero_http(self):
        for env in ({}, {"HITHINK_ENABLED": "1"}, {"HITHINK_FINANCE_API_KEY": "test"}):
            with patch.dict(os.environ, env, clear=True), patch("requests.get") as request:
                self.assertIsNone(history._try_hithink("600519", "2026-09-01", "2026-09-02"))
                self.assertEqual(research_universe._hithink_active_stocks(20), {})
                self.assertTrue(build_research_context(["600519"], "2026-09-01")["stocks"][0]["missing"])
                request.assert_not_called()

    def test_daily_cache_source_and_units(self):
        client = Mock()
        client.get_daily.return_value = self.frame()
        with patch("data.hithink.get_client", return_value=client), patch.object(history, "_try_cache", return_value=None), patch.object(history, "_save_to_cache") as save, patch.object(history, "_try_longbridge") as fallback:
            df = history.get_daily("600519", "20260901", "20260902")
            self.assertEqual(df.iloc[0].volume, 1000)
            self.assertEqual(df.iloc[0].amount, 10500)
            self.assertEqual(df.attrs["adjust"], "qfq")
            self.assertEqual(save.call_args.kwargs["source"], "hithink")
            client.get_daily.assert_called_once_with("600519", "2026-09-01", "2026-09-02", adjust="forward")
            fallback.assert_not_called()

    def test_wrong_adjust_and_failure_fallback(self):
        client = Mock()
        for response in (self.frame("none"), RuntimeError("failed")):
            client.get_daily.side_effect = response if isinstance(response, Exception) else None
            client.get_daily.return_value = response
            legacy = self.frame()
            with patch("data.hithink.get_client", return_value=client), patch.object(history, "_try_cache", return_value=None), patch.object(history, "_save_to_cache"), patch.object(history, "_try_longbridge", return_value=legacy) as fallback:
                result = history.get_daily("600519", "20260901", "20260902")
                self.assertEqual(result.attrs["source"], "longport")
                self.assertEqual(result.iloc[0].close, legacy.iloc[0].close)
                fallback.assert_called_once()

    def test_non_qfq_never_uses_shared_cache(self):
        with patch("data.database.Database") as database:
            self.assertIsNone(history._try_cache("600519", "2026-09-01", "2026-09-02", ""))
            history._save_to_cache("600519", self.frame("none"), adjust="", source="hithink")
            database.assert_not_called()

    def test_full_fields_bypass_hithink(self):
        with patch.object(history, "_try_hithink") as source, patch.object(history, "_fetch_baostock", return_value=self.frame()):
            history.get_daily("600519", simple=False)
            source.assert_not_called()

    def test_universe_amount_eligibility_and_source(self):
        client = Mock()
        client.get_snapshot_page.return_value = {"item": [
            {"thscode": "600519.SH", "turnover": 30_000_000},
            {"thscode": "000001.SZ", "turnover": 60_000_000},
            {"thscode": "688001.SH", "turnover": 90_000_000},
            {"thscode": "300750.SZ", "turnover": 29999999},
        ], "total": 4}
        with patch("data.hithink.get_client", return_value=client), tempfile.TemporaryDirectory() as temp, patch("strategy.stock_picker._get_active_stocks") as fallback:
            result = research_universe.refresh_research_universe(path=os.path.join(temp, "pool.json"))
            self.assertEqual([r["code"] for r in result["codes"]], ["000001", "600519"])
            self.assertEqual(result["source"], "hithink_liquidity")
            fallback.assert_not_called()

    def test_pagination_failure_and_budget_fall_back(self):
        client = Mock()
        first = {"item": [{"thscode": "600519.SH", "turnover": 50_000_000}] * 100, "total": 5000}
        with patch("data.hithink.get_client", return_value=client), patch.dict(os.environ, {"HITHINK_UNIVERSE_MAX_PAGES": "2"}):
            client.get_snapshot_page.side_effect = [first, RuntimeError("partial")]
            self.assertEqual(research_universe._hithink_active_stocks(20), {})
            client.get_snapshot_page.side_effect = [first, first]
            self.assertEqual(research_universe._hithink_active_stocks(20), {})

    def test_financial_disclosure_cutoff_and_no_historical_valuation(self):
        def stamp(day):
            return int(datetime.fromisoformat(day).replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
        rows = [
            dict(thscode="600519.SH", report_date_ms=stamp("2026-08-31"), period_end_ms=stamp("2026-06-30")),
            dict(thscode="600519.SH", report_date_ms=stamp("2026-09-02"), period_end_ms=stamp("2026-06-30")),
            dict(thscode="600519.SH", period_end_ms=stamp("2026-06-30")),
            dict(thscode="000001.SZ", report_date_ms=stamp("2026-08-31"), period_end_ms=stamp("2026-06-30")),
        ]
        client = Mock()
        client.get_financials.return_value = {"item": rows}
        result = build_research_context(["600519"], "2026-09-01", client)
        item = result["stocks"][0]
        self.assertEqual(item["financials"]["income-statements"], rows[:1])
        self.assertEqual(item["missing"]["valuation"], "historical_valuation_not_supported")
        client.get_valuations.assert_not_called()


if __name__ == "__main__":
    unittest.main()
