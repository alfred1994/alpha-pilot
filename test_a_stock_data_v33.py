#!/usr/bin/env python3
"""
a-stock-data v3.3集成回归测试
验证:
    1. mootdx K线调用使用 frequency 参数
    2. 打板情绪指标计算正确
    3. 东方财富涨跌停读取优先使用直连路径
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _install_fake_requests_if_needed():
    """当前运行时未安装requests时，提供足够导入模块的假实现"""
    try:
        import requests  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def mount(self, *args, **kwargs):
            return None

        def get(self, *args, **kwargs):
            raise RuntimeError("network disabled")

    requests = types.ModuleType("requests")
    requests.Session = FakeSession
    adapters = types.ModuleType("requests.adapters")
    adapters.HTTPAdapter = lambda *args, **kwargs: object()
    requests.adapters = adapters
    sys.modules["requests"] = requests
    sys.modules["requests.adapters"] = adapters


def test_mootdx_frequency_param():
    """mootdx bars必须传frequency，不能传category"""
    _install_fake_requests_if_needed()
    import data.a_stock_data as m

    class FakeBars:
        empty = False

        def iterrows(self):
            yield 0, {
                "datetime": "2026-06-10 15:00:00",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "vol": 1000,
                "amount": 10500,
            }

    class FakeClient:
        def __init__(self):
            self.kwargs = None

        def bars(self, **kwargs):
            self.kwargs = kwargs
            return FakeBars()

    client = FakeClient()
    m.tdx_client = lambda: client
    rows = m.get_kline_mootdx("sh600519", period="weekly")

    assert rows and rows[0]["date"] == "2026-06-10"
    assert client.kwargs["frequency"] == 5
    assert "category" not in client.kwargs
    assert client.kwargs["symbol"] == "600519"
    assert client.kwargs["market"] == 1


def test_limit_up_sentiment():
    """打板情绪应正确计算炸板率、连板梯队和晋级率"""
    _install_fake_requests_if_needed()
    import data.a_stock_data as m

    def fake_api(endpoint, sort, date, page_size=10000):
        if endpoint == "getTopicZTPool":
            return [
                {"c": "000001", "n": "A", "p": 10000, "zdp": 10.0, "amount": 1,
                 "ltsz": 2, "hs": 3, "lbc": 3, "fbt": 92500, "lbt": 145500,
                 "fund": 4, "zbc": 0, "hybk": "银行", "zttj": {"days": 3, "ct": 3}},
                {"c": "000002", "n": "B", "p": 20000, "zdp": 10.0, "amount": 1,
                 "ltsz": 2, "hs": 3, "lbc": 1, "fbt": 93000, "lbt": 145500,
                 "fund": 4, "zbc": 1, "hybk": "地产", "zttj": {"days": 1, "ct": 1}},
            ]
        if endpoint == "getTopicZBPool":
            return [{"c": "000003", "n": "C", "p": 15000, "ztp": 16000, "zdp": 6.0,
                     "hs": 5, "fbt": 100000, "zbc": 2, "zf": 8, "zs": 1,
                     "hybk": "科技", "zttj": {"days": 1, "ct": 1}}]
        if endpoint == "getTopicDTPool":
            return [{"c": "000004", "n": "D", "p": 8000, "zdp": -10.0, "hs": 4,
                     "pe": 10, "fund": 5, "lbt": 140000, "fba": 6, "days": 1,
                     "oc": 0, "hybk": "煤炭"}]
        if endpoint == "getYesterdayZTPool":
            return [
                {"c": "000001", "n": "A", "p": 10000, "zdp": 10.0, "hs": 3,
                 "zf": 1, "zs": 1, "yfbt": 92500, "ylbc": 2, "hybk": "银行",
                 "zttj": {"days": 3, "ct": 3}},
                {"c": "000005", "n": "E", "p": 10000, "zdp": 2.0, "hs": 3,
                 "zf": 1, "zs": 1, "yfbt": 92500, "ylbc": 1, "hybk": "消费",
                 "zttj": {"days": 1, "ct": 1}},
            ]
        return []

    m._em_zt_api = fake_api
    sentiment = m.get_limit_up_sentiment("20260626")

    assert sentiment["zt_count"] == 2
    assert sentiment["zb_count"] == 1
    assert sentiment["dt_count"] == 1
    assert sentiment["break_rate"] == 33.3
    assert sentiment["max_height"] == 3
    assert sentiment["ladder"] == {1: 1, 3: 1}
    assert sentiment["promotion_rate"] == 50.0


def test_eastmoney_direct_limit_path():
    """data.eastmoney应优先使用a_stock_data直连结果并保持原字段结构"""
    fake = types.ModuleType("data.a_stock_data")
    fake.get_limit_up_pool = lambda date: [{
        "code": "000001", "name": "平安银行", "pct": 10.0, "price": 10.0,
        "amount": 200000000, "limit_days": 2, "first_seal": "09:30:00",
        "industry": "银行", "float_cap": 10000000000, "turnover": 3.2,
        "seal_fund": 30000000, "break_times": 1, "zt_stat": "2天2板",
    }]
    fake.get_limit_down_pool = lambda date: [{
        "code": "000002", "name": "万科A", "pct": -10.0, "price": 8.0,
        "board_amount": 50000000, "industry": "地产", "turnover": 4.1,
        "seal_fund": 20000000, "dt_days": 1, "open_times": 0,
    }]
    sys.modules["data.a_stock_data"] = fake

    import data.eastmoney as em
    up = em.get_limit_up("20260626")
    down = em.get_limit_down("20260626")

    assert up[0]["consecutive"] == 2
    assert up[0]["first_seal_time"] == "09:30"
    assert up[0]["amount"] == 2.0
    assert down[0]["seal_amount"] == 2000.0


if __name__ == "__main__":
    test_mootdx_frequency_param()
    test_limit_up_sentiment()
    test_eastmoney_direct_limit_path()
    print("a-stock-data v3.3回归测试通过")
