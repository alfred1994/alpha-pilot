"""多市场领域层回归：离线行情、真实交易日历、精度和已结束日线。"""
import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.market_quotes import ALPACA_DATA_URL, COINBASE_DATA_URL, QuoteService
from services.market_types import (
    Market, MarketError, Quote, QuoteStatus, estimate_fill, get_instrument,
    get_market_spec, list_instruments, market_session, settlement_available_at,
    validate_quantity, validate_quote,
)


UTC = timezone.utc
US_NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
CN_NOW = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)
CRYPTO_NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
CREDENTIALS = {"ALPACA_DATA_KEY_ID": "test-key", "ALPACA_DATA_SECRET_KEY": "test-secret"}


def response(payload, status=200):
    result = Mock()
    result.status_code = status
    result.json.return_value = payload
    return result


def product(**overrides):
    payload = {
        "id": "BTC-USD", "base_currency": "BTC", "quote_currency": "USD",
        "status": "online", "trading_disabled": False, "cancel_only": False,
        "post_only": False, "limit_only": False, "base_increment": "0.00000001",
        "quote_increment": "0.01", "min_market_funds": "1",
    }
    payload.update(overrides)
    return payload


def ticker(stamp=CRYPTO_NOW, **overrides):
    payload = {"price": "50000", "bid": "49999", "ask": "50001", "time": stamp.isoformat()}
    payload.update(overrides)
    return payload


def alpaca_quote(stamp=US_NOW, **overrides):
    payload = {"symbol": "SPY", "quote": {"bp": "99.99", "ap": "100.01", "bs": 100, "as": 100, "t": stamp.isoformat()}}
    payload["quote"].update(overrides)
    return payload


def make_quote(market="US", symbol="SPY", stamp=US_NOW, **overrides):
    defaults = dict(instrument=get_instrument(market, symbol), price=Decimal("100"),
                    as_of=stamp, received_at=stamp, source="fixture",
                    bid=Decimal("99.99"), ask=Decimal("100.01"))
    defaults.update(overrides)
    return Quote(**defaults)


def candle(day, close="50000"):
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return [int(start.timestamp()), "49000", "51000", "50000", close, "10"]


class MarketRulesTests(unittest.TestCase):
    def assert_market_error(self, code, fn, *args, **kwargs):
        with self.assertRaises(MarketError) as raised:
            fn(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_allowlist_and_currency(self):
        self.assertEqual(get_instrument("crypto", "BTC/USD").instrument_id, "CRYPTO:BTC-USD")
        self.assertEqual(get_market_spec("US").currency, "USD")
        self.assertEqual(get_market_spec("CRYPTO").currency, "USD")
        self.assertEqual(get_market_spec("CN").currency, "CNY")
        self.assertEqual(len(list_instruments("CRYPTO")), 2)
        self.assert_market_error("unsupported_instrument", get_instrument, "US", "../../orders")
        self.assert_market_error("unsupported_instrument", get_instrument, "US", "BTC-USD")
        self.assert_market_error("unsupported_market", get_instrument, "FOREX", "USD")

    def test_quantity_and_numeric_input_validation(self):
        us = get_instrument("US", "SPY")
        cn = get_instrument("CN", "510300")
        crypto = get_instrument("CRYPTO", "BTC-USD")
        self.assertEqual(validate_quantity(us, "1"), Decimal("1"))
        self.assertEqual(validate_quantity(cn, 200), Decimal("200"))
        self.assertEqual(validate_quantity(crypto, "0.00000001"), Decimal("0.00000001"))
        for item, value in ((us, "0.1"), (cn, "101"), (crypto, "0.000000001")):
            self.assert_market_error("invalid_quantity", validate_quantity, item, value)
        for value in (True, "NaN", "Infinity", "-1", "0", "1e9999999", "1e-9999999", None):
            self.assert_market_error("invalid_number", validate_quantity, crypto, value)

    def test_nyse_holiday_dst_and_early_close(self):
        self.assertFalse(market_session("US", datetime(2026, 1, 19, 15, tzinfo=UTC)).is_open)
        winter = market_session("US", datetime(2026, 3, 6, 14, 29, tzinfo=UTC))
        self.assertFalse(winter.is_open)
        self.assertEqual(winter.opens_at.hour, 14)
        self.assertTrue(market_session("US", datetime(2026, 3, 6, 14, 30, tzinfo=UTC)).is_open)
        summer = market_session("US", datetime(2026, 3, 9, 13, 30, tzinfo=UTC))
        self.assertTrue(summer.is_open)
        self.assertEqual(summer.opens_at.hour, 13)
        early = market_session("US", datetime(2026, 11, 27, 17, 59, tzinfo=UTC))
        self.assertTrue(early.is_open)
        self.assertEqual(early.closes_at.hour, 18)
        self.assertFalse(market_session("US", datetime(2026, 11, 27, 18, tzinfo=UTC)).is_open)

    def test_missing_calendar_fails_closed(self):
        with patch("services.market_types._us_calendar", side_effect=MarketError("calendar_unavailable", "日历缺失")):
            session = market_session("US", US_NOW)
            self.assertFalse(session.is_open)
            self.assertEqual(session.status, "unavailable")
            self.assert_market_error("calendar_unavailable", estimate_fill,
                                     get_instrument("US", "SPY"), "BUY", "1", make_quote(), US_NOW)

    def test_cn_lunch_holiday_and_unknown_calendar(self):
        with patch("scheduler.market_calendar._trading_dates_cache", None):
            self.assertTrue(market_session("CN", CN_NOW).is_open)
            self.assertFalse(market_session("CN", CN_NOW.replace(hour=4)).is_open)
            self.assertTrue(market_session("CN", CN_NOW.replace(hour=5)).is_open)
            self.assertFalse(market_session("CN", CN_NOW.replace(day=1)).is_open)
            self.assertEqual(market_session("CN", CN_NOW.replace(year=2032)).status, "unavailable")

    def test_crypto_opens_on_weekend(self):
        self.assertTrue(market_session("CRYPTO", datetime(2026, 10, 11, 1, tzinfo=UTC)).is_open)

    def test_quote_timestamps_are_not_replaced_by_fetch_time(self):
        quote = make_quote()
        self.assertIs(validate_quote(quote, now=US_NOW), quote)
        self.assert_market_error("stale_quote", validate_quote,
                                 replace(quote, as_of=US_NOW - timedelta(seconds=61)), now=US_NOW)
        self.assert_market_error("future_quote", validate_quote,
                                 replace(quote, as_of=US_NOW + timedelta(seconds=1, milliseconds=1)), now=US_NOW)
        self.assert_market_error("future_quote", validate_quote,
                                 replace(quote, received_at=US_NOW + timedelta(seconds=1, milliseconds=1)), now=US_NOW)
        self.assert_market_error("invalid_timestamp", validate_quote,
                                 replace(quote, as_of=US_NOW.replace(tzinfo=None)), now=US_NOW)
        self.assert_market_error("invalid_timestamp", validate_quote,
                                 replace(quote, as_of=None), now=US_NOW)
        self.assert_market_error("invalid_timestamp", validate_quote,
                                 replace(quote, received_at=None), now=US_NOW)
        self.assert_market_error("quote_unavailable", validate_quote,
                                 replace(quote, status=QuoteStatus.STALE), now=US_NOW)

    def test_quote_clock_skew_is_bounded_and_does_not_replace_source_time(self):
        quote = make_quote()
        for delta in (timedelta(milliseconds=999), timedelta(seconds=1)):
            source_ahead = replace(quote, as_of=US_NOW + delta)
            self.assertIs(validate_quote(source_ahead, now=US_NOW), source_ahead)
            self.assertEqual(source_ahead.as_of, US_NOW + delta)
            self.assertEqual(source_ahead.to_dict()["clock_skew_seconds"], delta.total_seconds())
            received_ahead = replace(quote, received_at=US_NOW + delta)
            self.assertIs(validate_quote(received_ahead, now=US_NOW), received_ahead)
        # 来源与接收时间各自在 now 的一秒范围内，二者偏差也不能叠加成两秒。
        combined = replace(quote, as_of=US_NOW + timedelta(milliseconds=999),
                           received_at=US_NOW - timedelta(milliseconds=999))
        self.assert_market_error("future_quote", validate_quote, combined, now=US_NOW)
        self.assert_market_error("future_quote", validate_quote,
                                 replace(quote, received_at=US_NOW - timedelta(seconds=1, milliseconds=1)), now=US_NOW)

    def test_clock_tolerance_never_extends_quote_freshness(self):
        for market, symbol in (("CN", "510300"), ("US", "SPY"), ("CRYPTO", "BTC-USD")):
            limit = get_market_spec(market).max_quote_age_seconds
            quote = make_quote(market, symbol, US_NOW, as_of=US_NOW - timedelta(seconds=limit))
            self.assertIs(validate_quote(quote, now=US_NOW), quote)
            self.assert_market_error("stale_quote", validate_quote,
                                     replace(quote, as_of=quote.as_of - timedelta(milliseconds=1)), now=US_NOW)

    def test_bad_quote_values_and_wrong_symbol(self):
        quote = make_quote()
        self.assert_market_error("quote_mismatch", validate_quote, quote,
                                 instrument=get_instrument("US", "QQQ"), now=US_NOW)
        self.assert_market_error("invalid_quote", validate_quote,
                                 replace(quote, bid=Decimal("101")), now=US_NOW)
        self.assert_market_error("invalid_number", validate_quote,
                                 replace(quote, price=Decimal("NaN")), now=US_NOW)
        self.assert_market_error("no_liquidity", validate_quote,
                                 replace(quote, volume=Decimal("0")), now=US_NOW)

    def test_fill_slippage_fees_and_rounding_are_conservative(self):
        quote = make_quote()
        buy = estimate_fill(quote.instrument, "BUY", "1", quote, US_NOW)
        sell = estimate_fill(quote.instrument, "SELL", "1", quote, US_NOW)
        self.assertEqual(buy.price, Decimal("100.07"))
        self.assertEqual(buy.fee, Decimal("0.06"))
        self.assertEqual(buy.total, Decimal("100.13"))
        self.assertEqual(sell.price, Decimal("99.94"))
        self.assertEqual(sell.fee, Decimal("0.05"))
        self.assertEqual(sell.total, Decimal("99.89"))
        self.assertEqual(buy.to_dict()["quantity"], "1")
        self.assertEqual(quote.to_dict()["as_of"], "2026-10-12T15:00:00Z")

    def test_crypto_precision_minimum_and_product_constraints(self):
        quote = make_quote("CRYPTO", "BTC-USD", CRYPTO_NOW, price=Decimal("50000"),
                           bid=Decimal("49999"), ask=Decimal("50001"), minimum_notional=Decimal("10"))
        fill = estimate_fill(quote.instrument, "BUY", "0.001", quote, CRYPTO_NOW)
        self.assertEqual(fill.notional, Decimal("50.06"))
        self.assertEqual(fill.total, Decimal("50.37"))
        self.assert_market_error("minimum_notional", estimate_fill, quote.instrument,
                                 "BUY", "0.0001", quote, CRYPTO_NOW)
        self.assert_market_error("invalid_quantity", estimate_fill, quote.instrument,
                                 "BUY", "0.001", replace(quote, quantity_increment=Decimal("0.01")), CRYPTO_NOW)

    def test_cn_price_limits_and_minimum_commission(self):
        quote = make_quote("CN", "510300", CN_NOW, price=Decimal("4"), bid=None, ask=None,
                           limit_up=Decimal("4.4"), limit_down=Decimal("3.6"))
        fill = estimate_fill(quote.instrument, "BUY", "100", quote, CN_NOW)
        self.assertEqual(fill.price, Decimal("4.002"))
        self.assertEqual(fill.fee, Decimal("5.00"))
        self.assertEqual(fill.total, Decimal("405.20"))
        self.assert_market_error("price_limit", estimate_fill, quote.instrument, "BUY", "100",
                                 replace(quote, price=Decimal("4.4")), CN_NOW)
        self.assert_market_error("price_limit", estimate_fill, quote.instrument, "BUY", "100",
                                 replace(quote, price=Decimal("4.399")), CN_NOW)
        self.assert_market_error("price_limit", estimate_fill, quote.instrument, "SELL", "100",
                                 replace(quote, price=Decimal("3.6")), CN_NOW)

    def test_after_hours_fill_is_rejected(self):
        after = US_NOW.replace(hour=22)
        quote = make_quote(stamp=after)
        self.assert_market_error("market_closed", estimate_fill, quote.instrument, "BUY", "1", quote, after)

    def test_us_cash_settlement_skips_holidays_and_weekends(self):
        instrument = get_instrument("US", "SPY")
        thursday = datetime(2026, 7, 2, 15, tzinfo=UTC)
        self.assertEqual(settlement_available_at(instrument, thursday), datetime(2026, 7, 6, 13, 30, tzinfo=UTC))
        self.assertEqual(settlement_available_at(get_instrument("CN", "510300"), CN_NOW), CN_NOW)
        self.assertEqual(settlement_available_at(get_instrument("CRYPTO", "BTC-USD"), CRYPTO_NOW), CRYPTO_NOW)


class ProviderTests(unittest.TestCase):
    assert_market_error = MarketRulesTests.assert_market_error

    def service(self, payloads=(), *, environ=None, now=US_NOW):
        transport = Mock()
        transport.get.side_effect = [response(payload) for payload in payloads]
        return QuoteService(session=transport, environ={} if environ is None else environ,
                            clock=lambda: now), transport

    def test_missing_credentials_does_not_call_network(self):
        service, transport = self.service()
        self.assert_market_error("provider_not_configured", service.get_quote, "US", "SPY")
        transport.get.assert_not_called()

    def test_alpaca_uses_only_read_only_data_endpoint(self):
        service, transport = self.service([alpaca_quote()], environ=CREDENTIALS)
        quote = service.get_quote("US", "SPY")
        self.assertEqual(quote.price, Decimal("100.00"))
        args, kwargs = transport.get.call_args
        self.assertEqual(args[0], f"{ALPACA_DATA_URL}/SPY/quotes/latest")
        self.assertEqual(kwargs["params"], {"feed": "iex"})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (3.0, 8.0))
        self.assertNotIn("test-secret", str(quote.to_dict()))

    def test_future_stale_and_mismatched_provider_quotes(self):
        for stamp, code in ((US_NOW + timedelta(seconds=1, milliseconds=1), "future_quote"),
                            (US_NOW - timedelta(minutes=2), "stale_quote")):
            service, _ = self.service([alpaca_quote(stamp)], environ=CREDENTIALS)
            self.assert_market_error(code, service.get_quote, "US", "SPY")
        payload = alpaca_quote()
        payload["symbol"] = "QQQ"
        service, _ = self.service([payload], environ=CREDENTIALS)
        self.assert_market_error("quote_mismatch", service.get_quote, "US", "SPY")

    def test_provider_failure_redaction_and_no_retries(self):
        for status, code in ((401, "provider_unauthorized"), (403, "provider_unauthorized"),
                             (429, "provider_rate_limited"), (500, "provider_unavailable"),
                             (302, "provider_unavailable")):
            service, transport = self.service(environ=CREDENTIALS)
            transport.get.side_effect = None
            transport.get.return_value = response({"message": "test-secret"}, status)
            error = self.assert_market_error(code, service.get_quote, "US", "SPY")
            self.assertNotIn("test-secret", error.message)
            self.assertEqual(transport.get.call_count, 1)
        transport.get.side_effect = requests.Timeout("test-secret")
        error = self.assert_market_error("provider_unavailable", service.get_quote, "US", "SPY")
        self.assertNotIn("test-secret", error.message)

    def test_unknown_symbol_is_rejected_before_network(self):
        service, transport = self.service()
        self.assert_market_error("unsupported_instrument", service.get_quote, "CRYPTO", "https://evil.invalid")
        transport.get.assert_not_called()

    def test_coinbase_product_rules_and_cache(self):
        service, transport = self.service([product(), ticker(), ticker()], now=CRYPTO_NOW)
        first = service.get_quote("CRYPTO", "BTC-USD")
        second = service.get_quote("CRYPTO", "BTC/USD")
        self.assertEqual(first.instrument.currency, "USD")
        self.assertEqual(first.minimum_notional, Decimal("1"))
        self.assertEqual(first.quantity_increment, Decimal("0.00000001"))
        self.assertEqual(first.source, second.source)
        self.assertEqual(transport.get.call_count, 3)
        self.assertEqual(transport.get.call_args_list[0].args[0], f"{COINBASE_DATA_URL}/BTC-USD")
        self.assertEqual(transport.get.call_args_list[1].args[0], f"{COINBASE_DATA_URL}/BTC-USD/ticker")

    def test_coinbase_offline_or_mismatched_product_is_rejected(self):
        for metadata, code in ((product(status="offline"), "instrument_unavailable"),
                               (product(quote_currency="USDC"), "quote_mismatch"),
                               (product(limit_only=True), "instrument_unavailable"),
                               (product(base_increment="0.000000015"), "instrument_unavailable")):
            service, transport = self.service([metadata], now=CRYPTO_NOW)
            self.assert_market_error(code, service.get_quote, "CRYPTO", "BTC-USD")
            self.assertEqual(transport.get.call_count, 1)

    def test_coinbase_stale_quote_keeps_exchange_time(self):
        service, _ = self.service([product(), ticker(CRYPTO_NOW - timedelta(seconds=31))], now=CRYPTO_NOW)
        self.assert_market_error("stale_quote", service.get_quote, "CRYPTO", "BTC-USD")

    def test_coinbase_subsecond_clock_skew_is_reported(self):
        for delta in (timedelta(milliseconds=999), timedelta(seconds=1, milliseconds=1)):
            stamp = CRYPTO_NOW + delta
            service, _ = self.service([product(), ticker(stamp)], now=CRYPTO_NOW)
            if delta > timedelta(seconds=1):
                self.assert_market_error("future_quote", service.get_quote, "CRYPTO", "BTC-USD")
            else:
                quote = service.get_quote("CRYPTO", "BTC-USD")
                self.assertEqual(quote.as_of, stamp)
                self.assertEqual(quote.received_at, CRYPTO_NOW)
                self.assertEqual(quote.to_dict()["clock_skew_seconds"], 0.999)

    def test_cn_source_mapping_is_read_only_and_preserves_timestamp(self):
        item = SimpleNamespace(code="510300", price=4, close_prev=4, volume=1000, timestamp="20261012100000")
        service, transport = self.service(now=CN_NOW)
        with patch("data.realtime.get_realtime", return_value=[item]) as fetch:
            quote = service.get_quote("CN", "510300")
        self.assertEqual(quote.as_of, CN_NOW)
        self.assertEqual(quote.limit_up, Decimal("4.400"))
        self.assertEqual(quote.limit_down, Decimal("3.600"))
        fetch.assert_called_once_with(["510300"])
        transport.get.assert_not_called()

    def test_us_bars_exclude_unfinished_session_and_adjust_splits(self):
        payload = {"symbol": "SPY", "bars": [
            {"t": "2026-10-12T04:00:00Z", "c": "103"},
            {"t": "2026-10-09T04:00:00Z", "c": "102"},
            {"t": "2026-10-08T04:00:00Z", "c": "101"},
        ]}
        service, transport = self.service([payload], environ=CREDENTIALS)
        bars = service.get_bars("US", "SPY")
        self.assertEqual([bar.close for bar in bars], [Decimal("101"), Decimal("102")])
        self.assertEqual(bars[-1].as_of, datetime(2026, 10, 9, 20, tzinfo=UTC))
        self.assertEqual(transport.get.call_args.kwargs["params"]["adjustment"], "split")

    def test_us_bars_early_close_and_dst(self):
        now = datetime(2026, 11, 27, 18, 1, tzinfo=UTC)
        payload = {"symbol": "SPY", "bars": [{"t": "2026-11-27T05:00:00Z", "c": "103"}]}
        service, _ = self.service([payload], environ=CREDENTIALS, now=now)
        self.assertEqual(service.get_bars("US", "SPY")[-1].as_of, datetime(2026, 11, 27, 18, tzinfo=UTC))

    def test_us_bars_follow_short_pages_until_completed_limit(self):
        first = {"symbol": "SPY", "bars": [{"t": "2026-10-12T04:00:00Z", "c": "103"}], "next_page_token": "page-2"}
        second = {"symbol": "SPY", "bars": [{"t": "2026-10-09T04:00:00Z", "c": "102"}], "next_page_token": "page-3"}
        third = {"symbol": "SPY", "bars": [{"t": "2026-10-08T04:00:00Z", "c": "101"}], "next_page_token": "unused-page"}
        service, transport = self.service([first, second, third], environ=CREDENTIALS)
        bars = service.get_bars("US", "SPY", limit=2)
        self.assertEqual([bar.close for bar in bars], [Decimal("101"), Decimal("102")])
        self.assertEqual(transport.get.call_count, 3)
        calls = [call.kwargs["params"] for call in transport.get.call_args_list]
        self.assertNotIn("page_token", calls[0])
        self.assertEqual(calls[1]["page_token"], "page-2")
        self.assertEqual(calls[2]["page_token"], "page-3")
        for params in calls:
            self.assertTrue(1 <= params["limit"] <= 10000)
            self.assertEqual(params["start"], calls[0]["start"])
            self.assertEqual(params["end"], calls[0]["end"])
            self.assertEqual(params["currency"], "USD")
            self.assertEqual(params["sort"], "desc")

    def test_us_bars_pagination_rejects_repetition_and_has_bounded_requests(self):
        payload = {"symbol": "SPY", "bars": [], "next_page_token": "repeat"}
        service, transport = self.service([payload, payload], environ=CREDENTIALS)
        self.assert_market_error("invalid_history", service.get_bars, "US", "SPY")
        self.assertEqual(transport.get.call_count, 2)
        pages = [{"symbol": "SPY", "bars": [], "next_page_token": f"page-{i}"} for i in range(5)]
        service, transport = self.service(pages, environ=CREDENTIALS)
        self.assert_market_error("incomplete_history", service.get_bars, "US", "SPY")
        self.assertEqual(transport.get.call_count, 5)

    def test_crypto_bars_use_completed_utc_days(self):
        service, _ = self.service([product(), [candle("2026-10-12"), candle("2026-10-11"), candle("2026-10-10")]], now=CRYPTO_NOW)
        bars = service.get_bars("CRYPTO", "BTC-USD")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1].as_of, datetime(2026, 10, 12, tzinfo=UTC))

    def test_stale_and_gapped_history_cannot_drive_strategies(self):
        service, _ = self.service([product(), [candle("2026-10-10")]], now=CRYPTO_NOW)
        self.assert_market_error("stale_history", service.get_bars, "CRYPTO", "BTC-USD")
        service, _ = self.service([product(), [candle("2026-10-11"), candle("2026-10-09")]], now=CRYPTO_NOW)
        self.assert_market_error("incomplete_history", service.get_bars, "CRYPTO", "BTC-USD")
        payload = {"symbol": "SPY", "bars": [{"t": "2026-10-08T04:00:00Z", "c": "101"}]}
        service, _ = self.service([payload], environ=CREDENTIALS)
        self.assert_market_error("stale_history", service.get_bars, "US", "SPY")

    def test_cn_history_and_invalid_limits_are_explicit(self):
        service, transport = self.service()
        self.assert_market_error("history_unavailable", service.get_bars, "CN", "510300")
        for value in (True, 0, 201, "10"):
            self.assert_market_error("invalid_limit", service.get_bars, "CRYPTO", "BTC-USD", value)
        transport.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
