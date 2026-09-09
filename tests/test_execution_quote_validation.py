"""自动执行必须拒绝过期、错标的或无时间戳的行情。"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.quote_validation import validate_quote
from execution.broker import PaperBrokerAdapter
from risk.drawdown import DrawdownController
from risk.system_risk import SystemRiskController
from scheduler.market_calendar import _now_bj
from scheduler.pipeline import execute_trade_plan


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _temp_path(suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    os.unlink(path)
    return path


def _plan(code, reason):
    return {
        "date": _now_bj().strftime("%Y-%m-%d"),
        "orders": [{
            "code": code, "name": "行情校验测试", "action": "BUY",
            "target_weight": 0.10, "max_price": 11.0,
            "reason": reason, "score": 80, "conviction": 0.8,
        }],
    }


def test_validation_contract():
    fresh = SimpleNamespace(
        code="600519", price=10.0,
        timestamp=_now_bj().strftime("%Y-%m-%d %H:%M:%S"),
    )
    assert_true(validate_quote(fresh, expected_code="600519").valid, "当前匹配行情可用于自动执行")
    assert_true(
        not validate_quote(fresh, expected_code="000001").valid,
        "错标的行情被拒绝",
    )
    stale = SimpleNamespace(code="600519", price=10.0, timestamp="2000-01-01 09:30:00")
    assert_true(not validate_quote(stale, expected_code="600519").valid, "过期行情被拒绝")
    assert_true(
        validate_quote(stale, expected_code="600519", allow_historical=True).valid,
        "显式历史回放仍可使用固定旧行情",
    )
    assert_true(
        not validate_quote(SimpleNamespace(code="600519", price=True, timestamp=fresh.timestamp)).valid,
        "布尔值不能作为价格",
    )
    assert_true(
        not validate_quote(fresh, max_age_seconds=float("inf")).valid,
        "无限行情新鲜度阈值被拒绝",
    )


def test_automatic_execution_rejects_stale_quote():
    paths = [_temp_path("_paper.json"), _temp_path("_quant.db"), _temp_path("_drawdown.json"), _temp_path("_risk.json")]
    try:
        broker = PaperBrokerAdapter(account_file=paths[0], db_path=paths[1])

        def stale_quote(codes):
            return [SimpleNamespace(code=codes[0], price=10.0, timestamp="2000-01-01 09:30:00")]

        result = execute_trade_plan(
            _plan("600519", "过期行情不可成交"), broker=broker,
            realtime_func=stale_quote, market_status="盘中",
            drawdown_controller=DrawdownController(state_file=paths[2]),
            system_risk_controller=SystemRiskController(state_file=paths[3]),
            update_memory=False, allow_historical_plan=False,
        )
        assert_true(not result.executed_orders, "自动执行不会按过期行情成交")
        assert_true(result.order_audit[0]["status"] == "blocked", "过期行情写入阻断审计")
        assert_true("过期" in result.order_audit[0]["reason"], "审计包含过期原因")
        assert_true(not broker.get_positions(), "过期行情不会创建持仓")
    finally:
        for path in paths:
            for candidate in (path, f"{path}-wal", f"{path}-shm"):
                if os.path.exists(candidate):
                    os.unlink(candidate)


def test_automatic_sell_rejects_stale_quote():
    paths = [_temp_path("_paper.json"), _temp_path("_quant.db"), _temp_path("_drawdown.json"), _temp_path("_risk.json")]
    try:
        broker = PaperBrokerAdapter(account_file=paths[0], db_path=paths[1])
        previous_day = (_now_bj().date() - timedelta(days=1)).isoformat()
        assert_true(
            broker.buy("600519", "行情校验测试", 10.0, 1000, trade_date=previous_day),
            "构造可卖持仓",
        )

        def stale_quote(codes):
            return [SimpleNamespace(code=codes[0], price=9.0, timestamp="2000-01-01 09:30:00")]

        result = execute_trade_plan(
            {
                "date": _now_bj().strftime("%Y-%m-%d"),
                "orders": [{"code": "600519", "name": "行情校验测试", "action": "SELL", "reason": "旧价不得卖出"}],
            },
            broker=broker, realtime_func=stale_quote, market_status="盘中",
            drawdown_controller=DrawdownController(state_file=paths[2]),
            system_risk_controller=SystemRiskController(state_file=paths[3]),
            update_memory=False, allow_historical_plan=False,
        )
        assert_true(not result.executed_orders, "自动执行不会按过期行情卖出")
        assert_true(result.order_audit[0]["status"] == "blocked", "卖出过期行情写入阻断审计")
        assert_true(broker.has_position("600519"), "过期卖出行情不会清空持仓")
    finally:
        for path in paths:
            for candidate in (path, f"{path}-wal", f"{path}-shm"):
                if os.path.exists(candidate):
                    os.unlink(candidate)


def test_historical_cb_replay_does_not_request_realtime_context():
    """历史回放不可把今天的正股/溢价数据混入历史止损判断。"""
    paths = [_temp_path("_paper.json"), _temp_path("_quant.db"), _temp_path("_drawdown.json"), _temp_path("_risk.json")]
    try:
        broker = PaperBrokerAdapter(account_file=paths[0], db_path=paths[1])
        assert_true(
            broker.buy("113000", "历史转债", 100.0, 100, allow_t0=True, trade_unit=10, trade_date="2000-01-01"),
            "构造历史可转债持仓",
        )

        def historical_quote(codes):
            return [SimpleNamespace(code=codes[0], price=99.0, timestamp="2000-01-01 09:30:00")]

        historical_plan = {"date": "2000-01-01", "orders": []}
        with patch(
            "strategy.cb_t0_strategy.get_cb_exit_market_context",
            side_effect=AssertionError("历史回放不得调用实时上下文"),
        ):
            result = execute_trade_plan(
                historical_plan, broker=broker, realtime_func=historical_quote,
                market_status="盘中", update_memory=False,
                drawdown_controller=DrawdownController(state_file=paths[2]),
                system_risk_controller=SystemRiskController(state_file=paths[3]),
                allow_historical_plan=True,
            )
        assert_true(not result.errors, f"历史转债回放不混入实时上下文: {result.errors}")
        assert_true(broker.has_position("113000"), "无历史退出上下文时99元不会误触发附加退出")
    finally:
        for path in paths:
            for candidate in (path, f"{path}-wal", f"{path}-shm"):
                if os.path.exists(candidate):
                    os.unlink(candidate)


def main():
    print("自动执行行情校验测试")
    test_validation_contract()
    test_automatic_execution_rejects_stale_quote()
    test_automatic_sell_rejects_stale_quote()
    test_historical_cb_replay_does_not_request_realtime_context()
    print("自动执行行情校验测试通过")


if __name__ == "__main__":
    main()
