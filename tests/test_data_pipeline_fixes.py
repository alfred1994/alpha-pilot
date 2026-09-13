#!/usr/bin/env python3
"""数据链路修复回归测试。

覆盖三类修复:
1. 腾讯实时行情缺失时间戳时不再用当前时间顶替(fail-closed)。
2. validate_quote 对缺失时间戳的行情拒绝通过。
3. _get_active_stocks 过滤 ST/高PE, min_amount 缺省接通 PICKER_MIN_AMOUNT。
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from config import PICKER_MAX_PE, PICKER_MIN_AMOUNT
from data.quote_validation import validate_quote
from data.realtime import _parse_tencent_line


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


# 腾讯行情样例: parts[30] 为时间戳位
def _tencent_line(ts="20260911150003"):
    fields = ["0"] * 50
    fields[1] = "贵州茅台"
    fields[2] = "600519"
    fields[3] = "1500.00"   # 现价
    fields[4] = "1490.00"   # 昨收
    fields[5] = "1495.00"   # 开盘
    fields[30] = ts
    fields[31] = "10.00"
    fields[32] = "0.67"
    fields[33] = "1505.00"
    fields[34] = "1488.00"
    fields[36] = "47487"
    fields[37] = "711234"
    fields[38] = "0.31"
    fields[39] = "22.5"
    fields[45] = "18800"
    return f'v_sh600519="{"~".join(fields)}"'


def test_tencent_missing_timestamp_not_fabricated():
    print("测试1: 腾讯行情缺失时间戳不再伪造")
    quote = _parse_tencent_line(_tencent_line(ts=""))
    assert_true(quote is not None, "行情可解析")
    assert_true(quote.timestamp == "", f"时间戳留空(实际={quote.timestamp!r})")

    quote_ok = _parse_tencent_line(_tencent_line())
    assert_true(quote_ok.timestamp == "20260911150003", "正常时间戳保留")


def test_validate_quote_rejects_missing_timestamp():
    print("测试2: 校验器拒绝缺失时间戳的行情")
    validation = validate_quote(
        {"code": "600519", "price": 1500.0, "timestamp": ""},
        expected_code="600519",
    )
    assert_true(not validation.valid, f"空时间戳被拒绝: {validation.reason}")
    assert_true("时间戳" in validation.reason, "拒绝原因为时间戳缺失")


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _fake_em_response():
    return _FakeResp({
        "data": {
            "diff": [
                {"f12": "600001", "f14": "正常股", "f6": str(PICKER_MIN_AMOUNT * 10000 * 10), "f9": 30.0},
                {"f12": "600002", "f14": "ST风险", "f6": str(PICKER_MIN_AMOUNT * 10000 * 10), "f9": 30.0},
                {"f12": "600003", "f14": "退市股", "f6": str(PICKER_MIN_AMOUNT * 10000 * 10), "f9": 30.0},
                {"f12": "600004", "f14": "高PE股", "f6": str(PICKER_MIN_AMOUNT * 10000 * 10), "f9": str(PICKER_MAX_PE + 50)},
                {"f12": "600005", "f14": "低额股", "f6": str(PICKER_MIN_AMOUNT * 10000 * 0.5), "f9": 30.0},
                {"f12": "688001", "f14": "科创股", "f6": str(PICKER_MIN_AMOUNT * 10000 * 10), "f9": 30.0},
            ]
        }
    })


def test_active_stocks_filters():
    print("测试3: 活跃股池过滤 ST/退市/高PE/低成交额")
    import strategy.stock_picker as sp

    with mock.patch.object(sp.requests, "get", return_value=_fake_em_response()):
        stocks = sp._get_active_stocks()

    assert_true("600001" in stocks, "正常股保留")
    assert_true("600002" not in stocks, "ST股被过滤")
    assert_true("600003" not in stocks, "退市股被过滤")
    assert_true("600004" not in stocks, f"PE>{PICKER_MAX_PE} 被过滤")
    assert_true("600005" not in stocks, "低于PICKER_MIN_AMOUNT被过滤")
    assert_true("688001" not in stocks, "科创板被过滤")


def main():
    test_tencent_missing_timestamp_not_fabricated()
    test_validate_quote_rejects_missing_timestamp()
    test_active_stocks_filters()
    print("\n全部数据链路修复测试通过")


if __name__ == "__main__":
    main()
