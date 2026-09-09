#!/usr/bin/env python3
"""
KT实时行情适配器单元测试（离线，不访问网络）

覆盖:
  - normalize_security 代码归一化与冲突拒绝
  - 新浪快照解析（五档盘口/时间校验/涨跌幅）
  - 腾讯分钟K线解析（OHLC合理性过滤）
  - get_realtime_quotes 适配层映射（无效快照剔除/NaN处理/单位换算）
  - pipeline._collect_position_prices 批量优先+逐个回退
  - _default_realtime_func 双源回退顺序
"""
import os
import sys
import urllib.request
from datetime import datetime
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.kt_realtime as kt
from data.kt_realtime import KTRealtimeClient, get_realtime_quotes
from data.quote_validation import BEIJING_TZ
from scheduler.pipeline import _collect_position_prices, _default_realtime_func


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


class _FakeResponse:
    """模拟 urlopen 返回的上下文管理器响应。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _sina_payload(date_text=None, time_text=None) -> bytes:
    """构造 sh600519 新浪快照: 价格1330, 五档盘口, 有效时间戳。"""
    now = datetime.now(BEIJING_TZ)
    date_text = date_text or now.strftime("%Y-%m-%d")
    time_text = time_text or now.strftime("%H:%M:%S")
    fields = [
        "贵州茅台", "1321.00", "1321.00", "1330.00", "1335.00", "1325.00",
        "1329.90", "1330.00", "4748700", "630000000",
        "100", "1329.82", "200", "1329.50", "300", "1329.00", "400", "1328.00", "500", "1327.00",
        "4626", "1330.00", "200", "1330.50", "300", "1331.00", "400", "1332.00", "500", "1333.00",
        date_text, time_text,
    ]
    line = 'var hq_str_sh600519="' + ",".join(fields) + '";\n'
    return line.encode("gbk")


def _kline_payload() -> bytes:
    """构造 sh600519 1分钟K线: 3根有效 + 1根 high<open 被过滤。"""
    import json
    payload = {
        "code": 0, "msg": "",
        "data": {"sh600519": {"m1": [
            ["202609041458", "1330.100", "1330.100", "1330.100", "1330.100", "3.0"],
            ["202609041459", "1330.100", "1330.100", "1330.100", "1330.100", "0"],
            ["202609041500", "1330.100", "1330.000", "1330.100", "1330.000", "384.0"],
            ["202609041501", "10.0", "5.0", "1.0", "2.0", "10"],
        ]}},
    }
    return json.dumps(payload).encode("utf-8")


def test_normalize_security():
    print("[1] normalize_security 代码归一化")
    client = KTRealtimeClient()
    market, code, symbol, sec_type = client.normalize_security("600519")
    assert_true((market, code, symbol, sec_type) == ("SH", "600519", "sh600519", "stock"), "裸六位沪市股票")
    market, code, symbol, sec_type = client.normalize_security("000001")
    assert_true((market, sec_type) == ("SZ", "stock"), "裸000001是深市股票")
    _, _, symbol, sec_type = client.normalize_security("sh000001")
    assert_true((symbol, sec_type) == ("sh000001", "index"), "sh000001上证指数例外")
    _, _, _, sec_type = client.normalize_security("920002")
    assert_true(sec_type == "stock" and client.normalize_security("920002")[0] == "BJ", "北交所代码")
    _, _, _, sec_type = client.normalize_security("113042")
    assert_true(sec_type == "convertible", "沪市可转债")
    _, _, _, sec_type = client.normalize_security("600000.SH")
    assert_true(sec_type == "stock", "后缀格式600000.SH")
    for bad in ("sz600519", "12345", "abc123", "1234567"):
        try:
            client.normalize_security(bad)
            raise AssertionError(f"{bad} 应被拒绝")
        except ValueError:
            pass
    print("  OK 非法/冲突代码全部拒绝")


def test_sina_snapshot_parse():
    print("[2] 新浪快照解析")
    original = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: _FakeResponse(_sina_payload())
    try:
        client = KTRealtimeClient()
        frame = client.get_realtime_tick(["600519"])
    finally:
        urllib.request.urlopen = original
    assert_true(len(frame) == 1, f"返回1行 (实际{len(frame)})")
    row = frame.iloc[0]
    assert_true(row["code"] == "600519" and row["sec_type"] == "stock", "代码与类型")
    assert_true(abs(row["price"] - 1330.00) < 1e-9, "现价")
    assert_true(abs(row["pre_close"] - 1321.00) < 1e-9, "昨收")
    assert_true(abs(row["pct_change"] - (1330.00 - 1321.00) / 1321.00 * 100) < 1e-6, "涨跌幅")
    assert_true(abs(row["bid1_p"] - 1329.82) < 1e-9 and abs(row["bid1_v"] - 100) < 1e-9, "买一档")
    assert_true(abs(row["ask1_p"] - 1330.00) < 1e-9 and abs(row["ask1_v"] - 4626) < 1e-9, "卖一档")
    assert_true(bool(row["data_valid"]), "时间戳有效")
    assert_true(row["amount_status"] == "provider_field_9", "成交额状态标注")
    assert_true(row["volume_hand"] == 4748700 / 100, "成交量换算为手")


def test_stale_sina_snapshot_rejected():
    print("[3] 新浪过期快照拒绝")
    original = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: _FakeResponse(
        _sina_payload("2000-01-01", "09:30:00")
    )
    try:
        frame = KTRealtimeClient().get_realtime_tick(["600519"])
    finally:
        urllib.request.urlopen = original
    assert_true(len(frame) == 1 and not bool(frame.iloc[0]["data_valid"]), "过期时间戳不得标为有效")
    assert_true("过期" in str(frame.iloc[0]["invalid_reason"]), "过期原因可审计")


def test_kline_parse():
    print("[4] 腾讯分钟K线解析")
    original = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: _FakeResponse(_kline_payload())
    try:
        client = KTRealtimeClient()
        frame = client.get_kline("600519", "1m", count=10)
    finally:
        urllib.request.urlopen = original
    assert_true(len(frame) == 3, f"high<open的异常K线被过滤 (实际{len(frame)})")
    assert_true(list(frame["datetime"])[:2] == ["2026-09-04 14:58", "2026-09-04 14:59"], "分钟时间解析")
    assert_true(frame.iloc[0]["volume_hand"] == 3.0, "成交量(手)")
    assert_true((frame["amount_yuan"].isna()).all(), "未验证的成交额字段保持NaN不伪造")


def test_adapter_mapping():
    print("[5] get_realtime_quotes 适配层")
    columns = KTRealtimeClient.TICK_COLUMNS
    valid = {c: None for c in columns}
    valid.update({
        "code": "600519", "symbol": "sh600519", "sec_type": "stock", "name": "贵州茅台",
        "price": 1330.0, "change": float("nan"), "pct_change": 0.68, "open": 1321.0,
        "high": 1335.0, "low": 1325.0, "pre_close": 1321.0,
        "volume_raw": 4748700, "volume_hand": 47487.0, "amount_yuan": 6.3e8,
        "time": "2026-09-04 15:00:03", "data_valid": True,
    })
    invalid = dict(valid, code="000001", symbol="sz000001", data_valid=False)
    frame = pd.DataFrame([valid, invalid], columns=columns)
    frame.attrs.update({"source": "sina_hq", "missing_symbols": [], "source_errors": []})

    class _StubClient:
        def get_realtime_tick(self, codes, **kwargs):
            return frame

    original = kt.get_kt_client
    kt.get_kt_client = lambda: _StubClient()
    try:
        quotes = get_realtime_quotes(["600519", "000001"])
    finally:
        kt.get_kt_client = original
    assert_true(len(quotes) == 1, f"data_valid=False的快照被剔除 (实际{len(quotes)})")
    q = quotes[0]
    assert_true(q.code == "600519" and q.price == 1330.0 and q.close_prev == 1321.0, "价格字段映射")
    assert_true(q.volume == 47487, "volume为手数整数")
    assert_true(abs(q.amount - 6.3e8 / 10000.0) < 1e-6, "成交额元→万元")
    assert_true(q.change == 0.0 and q.pe == 0.0 and q.turnover == 0.0, "NaN与缺失字段填0")


def test_collect_position_prices():
    print("[6] pipeline._collect_position_prices")
    calls = []

    def fake_with_code(codes):
        calls.append(list(codes))
        if len(codes) > 1:
            return [SimpleNamespace(code="600519", price=10.0)]
        return [SimpleNamespace(code="000001", price=3.5)]

    prices = _collect_position_prices(["600519", "000001"], fake_with_code, allow_historical=True)
    assert_true(prices == {"600519": 10.0, "000001": 3.5}, f"批量+回退补齐: {prices}")
    assert_true(calls[0] == ["600519", "000001"], "第一次是批量调用")
    assert_true(calls[1] == ["000001"], "已覆盖的代码不再逐个查询")

    calls.clear()

    def fake_legacy(codes):
        calls.append(list(codes))
        return [SimpleNamespace(price=7.7, close_prev=7.0)]

    prices = _collect_position_prices(["600519", "000001"], fake_legacy, allow_historical=True)
    assert_true(prices == {"600519": 7.7, "000001": 7.7}, f"无.code的fake回退逐个路径: {prices}")
    assert_true(len(calls) == 3, "批量1次+逐个2次")

    def fake_explode(codes):
        raise RuntimeError("network down")

    prices = _collect_position_prices(["600519"], fake_explode, allow_historical=True)
    assert_true(prices == {}, "批量异常不外泄")

    assert_true(_collect_position_prices([], fake_legacy, allow_historical=True) == {}, "空持仓直接返回")

    stale_quote = lambda codes: [SimpleNamespace(
        code=codes[0], price=7.7, timestamp="2000-01-01 09:30:00",
    )]
    assert_true(
        _collect_position_prices(["600519"], stale_quote, allow_historical=False) == {},
        "自动执行收集器拒绝过期行情",
    )


def test_default_realtime_func_fallback():
    print("[7] _default_realtime_func 双源回退")
    import data.realtime as rt_module

    original_kt = kt.get_realtime_quotes
    original_tencent = rt_module.get_realtime
    sentinel = [SimpleNamespace(price=1.0, close_prev=1.0)]
    try:
        provider = _default_realtime_func()

        kt.get_realtime_quotes = lambda codes: (_ for _ in ()).throw(RuntimeError("kt down"))
        rt_module.get_realtime = lambda codes: sentinel
        assert_true(provider(["600519"]) is sentinel, "KT异常回退腾讯源")

        kt.get_realtime_quotes = lambda codes: []
        assert_true(provider(["600519"]) is sentinel, "KT空结果回退腾讯源")

        kt_quotes = [SimpleNamespace(code="600519", price=9.9)]
        kt.get_realtime_quotes = lambda codes: kt_quotes
        tencent_called = []

        def _tencent(codes):
            tencent_called.append(codes)
            return sentinel

        rt_module.get_realtime = _tencent
        assert_true(provider(["600519"]) is kt_quotes, "KT可用时直接返回")
        assert_true(tencent_called == [], "腾讯源未被调用")
    finally:
        kt.get_realtime_quotes = original_kt
        rt_module.get_realtime = original_tencent


def main():
    print("=" * 60)
    print("KT实时行情适配器单元测试（离线）")
    print("=" * 60)
    test_normalize_security()
    test_sina_snapshot_parse()
    test_stale_sina_snapshot_rejected()
    test_kline_parse()
    test_adapter_mapping()
    test_collect_position_prices()
    test_default_realtime_func_fallback()
    print("=" * 60)
    print("全部通过")


if __name__ == "__main__":
    main()
