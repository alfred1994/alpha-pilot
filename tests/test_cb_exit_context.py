"""可转债退出上下文与无副作用止损评估回归测试。"""
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.paper_account import PaperAccount
from execution.broker import PaperBrokerAdapter
from data.convertible_bond import _optional_float as source_optional_float, get_cb_list
from scheduler.auto_trader import check_stops_once
from scheduler.market_calendar import _now_bj
from strategy.cb_t0_strategy import (
    _EXIT_CONTEXT_CACHE,
    get_cb_exit_market_context,
    should_sell,
)


def _new_account(directory, name):
    account = PaperAccount(
        filepath=os.path.join(directory, f"{name}.json"),
        db_path=os.path.join(directory, f"{name}.db"),
    )
    trade = account.buy(
        "113000", "测试转债", price=100.0, shares=100,
        allow_t0=True, trade_unit=10,
    )
    assert trade
    return account


def _wait_for_context(code="113000"):
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        context = get_cb_exit_market_context([code], max_age_seconds=60.0)
        if code in context:
            return context
        time.sleep(0.01)
    raise AssertionError("可转债退出上下文未在后台刷新完成")


def test_should_sell_preserves_zero_and_missing_semantics():
    bomb = should_sell(
        "113000", {"cb_price": 99.0, "stock_change_pct": 0.0, "premium_rate": 2.0}, 100.0,
    )
    assert bomb["sell"] is True and "炸板" in bomb["reason"]

    missing_context = should_sell("113000", {"cb_price": 99.0}, 100.0)
    assert missing_context["sell"] is False

    premium = should_sell(
        "113000", {"cb_price": 101.0, "stock_change_pct": 8.0, "premium_rate": 31.0}, 100.0,
    )
    assert premium["sell"] is True and "溢价扩大" in premium["reason"]

    price_stop = should_sell("113000", {"cb_price": 96.5}, 100.0)
    assert price_stop["sell"] is True and "止损" in price_stop["reason"]


def test_data_source_marks_missing_exit_fields_without_changing_numeric_contract():
    class FakeFrame:
        empty = False

        def iterrows(self):
            yield 0, {"代码": "113000", "正股涨跌": "-", "转股溢价率": None}

    fake_akshare = type("FakeAkshare", (), {"bond_cb_jsl": staticmethod(lambda: FakeFrame())})
    with patch.dict(sys.modules, {"akshare": fake_akshare}):
        record = get_cb_list()[0]
    assert record["stock_change_pct"] == 0.0
    assert record["premium_rate"] == 0.0
    assert record["stock_change_pct_valid"] is False
    assert record["premium_rate_valid"] is False
    assert source_optional_float(True) is None


def test_exit_context_is_batched_and_short_lived_cached():
    _EXIT_CONTEXT_CACHE.update({"fetched_at": 0.0, "by_code": {}})
    records = [{
        "cb_code": "113000", "cb_price": 100.0,
        "stock_change_pct": 0.0, "premium_rate": 2.0,
    }]
    with patch("data.convertible_bond.get_cb_list", return_value=records) as get_list:
        get_cb_exit_market_context(["113000"])
        first = _wait_for_context()
        second = get_cb_exit_market_context(["113000"], max_age_seconds=60.0)

    assert get_list.call_count == 1
    assert first["113000"]["stock_change_pct"] == 0.0
    assert second == first

    _EXIT_CONTEXT_CACHE.update({"fetched_at": 0.0, "by_code": {}})
    invalid_records = [{
        "cb_code": "113000", "cb_price": 100.0,
        "stock_change_pct": 0.0, "stock_change_pct_valid": False,
        "premium_rate": 0.0, "premium_rate_valid": False,
    }]
    with patch("data.convertible_bond.get_cb_list", return_value=invalid_records):
        get_cb_exit_market_context(["113000"])
        invalid_context = _wait_for_context()
    assert "stock_change_pct" not in invalid_context["113000"]
    assert "premium_rate" not in invalid_context["113000"]

    _EXIT_CONTEXT_CACHE.update({"fetched_at": 0.0, "by_code": {}})
    with patch("data.convertible_bond.get_cb_list") as get_list:
        assert get_cb_exit_market_context(["600519"]) == {}
    assert get_list.call_count == 0


def test_slow_context_source_never_blocks_stop_path_or_duplicates_refresh():
    _EXIT_CONTEXT_CACHE.update({"fetched_at": 0.0, "by_code": {}, "refreshing": False})
    started = threading.Event()
    release = threading.Event()

    def slow_get_list():
        started.set()
        release.wait(1.0)
        return []

    with patch("data.convertible_bond.get_cb_list", side_effect=slow_get_list) as get_list:
        started_at = time.monotonic()
        assert get_cb_exit_market_context(["113000"]) == {}
        assert time.monotonic() - started_at < 0.1
        assert started.wait(0.5)
        assert get_cb_exit_market_context(["113000"], max_age_seconds=0.0) == {}
        assert get_list.call_count == 1
        release.set()
        deadline = time.monotonic() + 1.0
        while _EXIT_CONTEXT_CACHE["refreshing"] and time.monotonic() < deadline:
            time.sleep(0.01)
    assert _EXIT_CONTEXT_CACHE["refreshing"] is False


def test_expired_context_cannot_trigger_extended_exit_while_refreshing():
    _EXIT_CONTEXT_CACHE.update({
        "fetched_at": 0.0,
        "by_code": {"113000": {"stock_change_pct": 0.0, "premium_rate": 2.0}},
        "refreshing": False,
    })
    started = threading.Event()
    release = threading.Event()

    def slow_get_list():
        started.set()
        release.wait(1.0)
        return []

    with patch("data.convertible_bond.get_cb_list", side_effect=slow_get_list):
        context = get_cb_exit_market_context(["113000"])
        assert context == {}
        assert started.wait(0.5)
        with tempfile.TemporaryDirectory(prefix="test_cb_expired_context_") as directory:
            account = _new_account(directory, "expired")
            signals = account.evaluate_stop_conditions({"113000": 99.0}, market_context=context)
            assert signals == [], "过期正股上下文不能触发炸板退出"
        release.set()
        deadline = time.monotonic() + 1.0
        while _EXIT_CONTEXT_CACHE["refreshing"] and time.monotonic() < deadline:
            time.sleep(0.01)
    assert _EXIT_CONTEXT_CACHE["refreshing"] is False


def test_pure_evaluation_does_not_sell_but_execution_uses_full_context():
    with tempfile.TemporaryDirectory(prefix="test_cb_exit_context_") as directory:
        account = _new_account(directory, "bomb")
        signals = account.evaluate_stop_conditions(
            {"113000": 99.0},
            market_context={"113000": {"stock_change_pct": 0.0, "premium_rate": 2.0}},
        )
        assert len(signals) == 1 and "炸板" in signals[0]["reason"]
        assert "113000" in account.positions
        assert len(account.trades) == 1

        trades = account.check_stop_conditions(
            {"113000": 99.0},
            market_context={"113000": {"stock_change_pct": 0.0, "premium_rate": 2.0}},
        )
        assert len(trades) == 1 and "炸板" in trades[0]["reason"]
        assert "113000" not in account.positions

        price_only_account = _new_account(directory, "price_only")
        trades = price_only_account.check_stop_conditions({"113000": 96.5})
        assert len(trades) == 1 and "止损" in trades[0]["reason"]


def test_scheduled_stop_uses_context_but_falls_back_to_price_stop():
    def run_once(account, price, context):
        broker = PaperBrokerAdapter(account=account)

        def quote(codes):
            return [SimpleNamespace(
                code=codes[0], price=price,
                timestamp=_now_bj().strftime("%Y-%m-%d %H:%M:%S"),
            )]

        with patch("execution.broker.get_broker_adapter", return_value=broker), \
                patch("scheduler.pipeline._default_realtime_func", return_value=quote), \
                patch("strategy.cb_t0_strategy.get_cb_exit_market_context", return_value=context):
            return check_stops_once()

    with tempfile.TemporaryDirectory(prefix="test_cb_scheduled_stop_") as directory:
        bomb_account = _new_account(directory, "scheduled_bomb")
        result = run_once(
            bomb_account, 99.0,
            {"113000": {"stock_change_pct": 0.0, "premium_rate": 2.0}},
        )
        assert result["sold"] == 1 and "113000" not in bomb_account.positions

        missing_account = _new_account(directory, "scheduled_missing")
        result = run_once(missing_account, 99.0, {})
        assert result["sold"] == 0 and "113000" in missing_account.positions

        price_account = _new_account(directory, "scheduled_price")
        result = run_once(price_account, 96.5, {})
        assert result["sold"] == 1 and "113000" not in price_account.positions


def main():
    test_should_sell_preserves_zero_and_missing_semantics()
    test_data_source_marks_missing_exit_fields_without_changing_numeric_contract()
    test_exit_context_is_batched_and_short_lived_cached()
    test_slow_context_source_never_blocks_stop_path_or_duplicates_refresh()
    test_expired_context_cannot_trigger_extended_exit_while_refreshing()
    test_pure_evaluation_does_not_sell_but_execution_uses_full_context()
    test_scheduled_stop_uses_context_but_falls_back_to_price_stop()
    print("可转债退出上下文测试通过")


if __name__ == "__main__":
    main()
