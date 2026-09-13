#!/usr/bin/env python3
"""执行链路风控护栏回归测试。

覆盖三类修复:
1. PaperAccount.buy 对显式传入股数的路径强制执行单票仓位上限(MAX_SINGLE_PCT)。
2. SystemRiskController.update 同日多次调用只刷新当日记录(同日去重)，
   连续亏损天数从日记录幂等重算，不再被盘中多次执行虚增。
3. PositionManager 组合级仓位检查(持仓数上限/重复开仓拦截)。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import MAX_SINGLE_PCT
from execution.paper_account import PaperAccount
from risk.position import PositionManager
from risk.system_risk import SystemRiskController


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def temp_path(suffix):
    item = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    path = item.name
    item.close()
    os.unlink(path)
    return path


def cleanup(*paths):
    for path in paths:
        for candidate in (path, f"{path}-wal", f"{path}-shm"):
            if os.path.exists(candidate):
                os.unlink(candidate)


def test_single_position_cap():
    print("测试1: 单票仓位上限对显式股数强制生效")
    account_path = temp_path("_cap_account.json")
    db_path = temp_path("_cap_quant.db")
    try:
        account = PaperAccount(filepath=account_path, db_path=db_path)
        total_assets = account.total_assets()
        cap_value = total_assets * MAX_SINGLE_PCT

        # 初始资金100万, 20%上限=20万; 价格50元 → 上限内4000股
        trade = account.buy("600519", "贵州茅台", 50.0, shares=6000)
        assert_true(trade is not None, "买入订单可执行")
        assert_true(
            account.positions["600519"]["shares"] == int(cap_value / 50.0 / 100) * 100,
            f"股数被钳制到上限内({account.positions['600519']['shares']}股)",
        )
        assert_true(
            account.positions["600519"]["shares"] * 50.0 <= cap_value,
            "成交金额不超过单票上限",
        )
    finally:
        cleanup(account_path, db_path)


def test_single_position_cap_small_lot():
    print("测试2: 上限内不足1手时拒绝买入")
    account_path = temp_path("_cap2_account.json")
    db_path = temp_path("_cap2_quant.db")
    try:
        account = PaperAccount(filepath=account_path, db_path=db_path)
        # 20%上限约20万, 请求单价5000元/股 → 上限内不足1手(100股=50万)
        trade = account.buy("603001", "高价股", 5000.0, shares=100)
        assert_true(trade is None, "上限内不足1手买入被拒绝")
        assert_true(not account.positions, "账户无持仓")
    finally:
        cleanup(account_path, db_path)


def test_system_risk_same_day_dedup():
    print("测试3: SystemRisk 同日多次调用不重复计数")
    state_file = temp_path("_sysrisk.json")
    try:
        controller = SystemRiskController(state_file=state_file)
        # 基线日
        controller.update(1_000_000.0, date="2026-09-09")
        # 第一天收盘: 亏损6% → 触发单日熔断
        controller.update(940_000.0, date="2026-09-10")
        assert_true(controller.state.forbid_new_buy, "单日亏损触发禁止开新仓")
        assert_true(controller.state.consecutive_loss_days == 1, "连亏1天")

        # 同日盘中多次执行(资产估值来回波动) → 不新增记录、不重复计数
        for assets in (920_000.0, 950_000.0, 930_000.0):
            controller.update(assets, date="2026-09-10")
        assert_true(len(controller.state.daily_records) == 2, "同日不新增记录")
        assert_true(controller.state.daily_records[-1]["total_assets"] == 930_000.0, "当日记录被刷新")
        assert_true(controller.state.consecutive_loss_days == 1, "同日重复执行连亏不虚增")

        # 第二天开始: 亏损标记被新一天记录刷新(幂等重算)
        controller.update(925_000.0, date="2026-09-11")
        assert_true(len(controller.state.daily_records) == 3, "新交易日新增记录")
        assert_true(controller.state.consecutive_loss_days == 2, "连亏天数=2(两天都亏损)")

        # 第三天反弹: 连亏清零、降仓解除
        controller.update(960_000.0, date="2026-09-14")
        assert_true(controller.state.consecutive_loss_days == 0, "盈利日连亏清零")
        assert_true(not controller.state.reduce_position, "降仓解除")

        # 幂等性: 同日再调用不改变规则状态
        before = (controller.state.consecutive_loss_days, controller.state.reduce_position)
        controller.update(970_000.0, date="2026-09-14")
        after = (controller.state.consecutive_loss_days, controller.state.reduce_position)
        assert_true(before == after, "同日重复调用不改变连亏/降仓状态")
    finally:
        cleanup(state_file)


def test_system_risk_intraday_not_false_trigger():
    print("测试4: 盘中估值波动不虚增连亏天数")
    state_file = temp_path("_sysrisk2.json")
    try:
        controller = SystemRiskController(state_file=state_file)
        # 基线日 + 某交易日盘中估值连续下跌, 旧实现会按执行次数累加连亏天数
        controller.update(1_000_000.0, date="2026-09-09")
        for assets in (990_000.0, 970_000.0, 950_000.0, 940_000.0):
            controller.update(assets, date="2026-09-10")
        assert_true(controller.state.consecutive_loss_days == 1, "同日多次执行连亏仍为1天")
        assert_true(not controller.state.reduce_position, "不因盘中波动触发降仓")
        assert_true(len(controller.state.daily_records) == 2, "不新增盘中记录")
    finally:
        cleanup(state_file)


def test_position_manager_checks():
    print("测试5: PositionManager 组合仓位检查")
    pm = PositionManager()
    positions = {"600519": {"shares": 100}}
    check = pm.check_position_limit("600519", positions, 1_000_000)
    assert_true(not check["allowed"], "重复开仓被拦截")

    full = {f"60{i:04d}": {"shares": 100} for i in range(5)}
    check = pm.check_position_limit("000001", full, 1_000_000)
    assert_true(not check["allowed"], "持仓已满被拦截")

    check = pm.check_position_limit("000001", {"600519": {"shares": 100}}, 1_000_000)
    assert_true(check["allowed"], "正常开仓放行")
    assert_true(
        abs(check["max_amount"] - 1_000_000 * MAX_SINGLE_PCT) < 1e-6,
        "返回单票金额上限",
    )


def main():
    test_single_position_cap()
    test_single_position_cap_small_lot()
    test_system_risk_same_day_dedup()
    test_system_risk_intraday_not_false_trigger()
    test_position_manager_checks()
    print("\n全部风控护栏测试通过")


if __name__ == "__main__":
    main()
