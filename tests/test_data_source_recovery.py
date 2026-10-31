"""数据源恢复：指数路由、龙虎榜单位、活跃股过滤及研究续跑预算。"""
import json
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.hithink import HiThinkClient, HiThinkError


def test_index_daily_uses_index_endpoint():
    client = HiThinkClient(api_key="test")
    body = {"timestamp": 0, "item": [{"date_ms": int(pd.Timestamp("2026-10-12", tz="Asia/Shanghai").timestamp()*1000),
             "open_price": 100, "high_price": 102, "low_price": 99, "close_price": 101,
             "volume": 1000, "turnover": 100000}]}
    with patch.object(client, "_request", return_value=body) as req:
        frame = client.get_daily("000300", "2026-10-09", "2026-10-12", asset_type="index")
        assert req.call_args.args[0] == "/api/a-share-index/prices/historical"
        assert req.call_args.args[1]["thscode"] == "000300.SH"
        assert req.call_args.args[1]["adjust"] == "none"
        assert frame["date"].tolist() == ["2026-10-12"]
    from data.history import _try_hithink
    with patch("data.hithink.get_client", return_value=client), patch.object(client, "_request", return_value=body):
        routed = _try_hithink("000300.SH", "2026-10-09", "2026-10-12")
        assert routed is not None and routed.attrs["adjust"] == "qfq"
    with patch.object(client, "_request", return_value=body) as req:
        client.get_daily("000001", "2026-10-09", "2026-10-12")
        assert req.call_args.args[0] == "/api/a-share/prices/historical"
        assert req.call_args.args[1]["thscode"] == "000001.SZ"


def test_lhb_preserves_yuan_and_prior_publication_date():
    from data.market import get_dragon_tiger
    client = SimpleNamespace(get_dragon_tiger=lambda date: {
        "trade_date": "2026-10-09", "stock_items": [{"thscode": "600521.SH", "name": "样本",
            "change": .05, "net_value": 25000000, "limit_reason": "业绩"}]})
    with patch("data.hithink.get_client", return_value=client), \
            patch("scheduler.market_calendar._now_bj", return_value=datetime(2026, 10, 12, 10)):
        frame = get_dragon_tiger("20261012")
    assert frame.iloc[0]["龙虎榜净买额"] == 25000000
    assert frame.iloc[0]["涨跌幅"] == 5
    assert frame.iloc[0]["上榜日"] == "2026-10-09"


def test_liquidity_fallback_keeps_filters_and_market_completeness():
    import strategy.stock_picker as picker
    price_rows = [{"thscode": c, "turnover": a} for c,a in [
        ("600521.SH", 100000000), ("688001.SH", 200000000), ("000002.SZ", 100000000),
        ("000003.SZ", 100000000), ("000004.SZ", 1), ("000005.SZ", 100000000)]]
    values = [{"thscode": c, "name": n, "pe_ttm": pe} for c,n,pe in [
        ("600521.SH", "正常", 20), ("688001.SH", "科创", 20), ("000002.SZ", "ST样本", 20),
        ("000003.SZ", "贵", 10000), ("000004.SZ", "低流动", 20), ("000005.SZ", "缺估值", None)]]
    client = HiThinkClient(api_key="test")
    def data(path, params):
        rows = price_rows if "prices" in path else values
        return {"total": len(rows), "item": rows}
    picker._HITHINK_MARKET_CACHE.clear()
    with patch("data.hithink.get_client", return_value=client), patch.object(client, "_request", side_effect=data), \
            patch.object(client, "get_valuations", return_value={"item": values}):
        assert picker._hithink_liquidity_fallback(3000, 100) == {"600521": "正常"}
        picker._hithink_liquidity_fallback(5000, 50)
        assert picker._HITHINK_MARKET_CACHE["key"] == (5000, 50)
    picker._HITHINK_MARKET_CACHE.clear()
    with patch("data.hithink.get_client", return_value=client), patch.object(client, "_request", return_value={"total": 10, "item": price_rows}):
        assert not picker._hithink_liquidity_fallback(3000, 100)


def test_research_prioritizes_stale_and_persists_budget_progress():
    import data.research_universe as research
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "universe.json")
        today = datetime.now().strftime("%Y-%m-%d")
        payload = {"codes": [{"code": "600001"}, {"code": "600002"}, {"code": "600003"}], "cursor": 0}
        with open(path, "w") as f:json.dump(payload, f)
        class FakeDatabase:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            conn = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchall=lambda: [
                {"code": "600001", "latest": today}, {"code": "600002", "latest": "2026-09-30"},
                {"code": "600003", "latest": "2026-09-11"}]))
        calls=[]
        def sync(item,*a):
            calls.append(item["code"])
            return {"code": item["code"], "status": "ok", "rows": 100}
        with patch("data.database.Database", side_effect=lambda **kw: FakeDatabase()), \
                patch.object(research, "_sync_one_bounded", side_effect=sync), \
                patch.object(research.time, "monotonic", side_effect=[0, 0, 800]):
            result = research.sync_research_universe(path=path)
        assert calls == ["600003"]
        assert result["fresh_before"] == 1 and result["requested"] == 2 and result["attempted"] == 1
        assert result["budget_exhausted"] and result["status"] == "partial"
        with open(path) as f:persisted=json.load(f)
        assert persisted["last_sync"]["attempted"] == 1


def test_research_refresh_does_not_accept_old_cache_as_fresh():
    from data.history import get_daily
    stale = pd.DataFrame({"date": ["2026-10-09"], "close": [10]})
    fresh = pd.DataFrame({"date": ["2026-10-12"], "close": [11]})
    with patch("data.history._try_cache", return_value=stale), \
            patch("data.history._try_hithink", return_value=fresh) as provider, \
            patch("data.history._assess_daily_coverage", side_effect=lambda df,*a: df), \
            patch("data.history._coverage_status", return_value="ok"), \
            patch("data.history._save_to_cache"):
        assert get_daily("600521", "2026-10-09", "2026-10-12").iloc[0]["close"] == 10
        assert get_daily("600521", "2026-10-09", "2026-10-12", refresh=True).iloc[0]["close"] == 11
        assert provider.call_count == 1


def test_weibo_rejected_cookie_skips_until_file_changes():
    import data.sentiment as sentiment
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "cookies.json")
        with open(path, "w") as file:
            file.write("test=old")
        login = SimpleNamespace(headers={"content-type": "text/html"}, text="<html>login</html>")
        good = SimpleNamespace(headers={"content-type": "application/json"}, text="{}",
                               json=lambda: {"statuses": [{"text_raw": "财经新闻"}]})
        with patch.object(sentiment, "WEIBO_COOKIES_FILE", path), \
                patch.object(sentiment, "_WEIBO_REJECTED_VERSION", None), \
                patch.object(sentiment.requests, "get", side_effect=[login, good]) as req:
            assert not sentiment.get_weibo_finance()
            assert not sentiment.get_weibo_finance()
            assert req.call_count == 1
            with open(path, "w") as file:
                file.write("test=new-longer")
            assert len(sentiment.get_weibo_finance()) == 1
            assert req.call_count == 2


def test_news_transport_retry_is_bounded():
    import data.sentiment as sentiment
    import requests
    body = SimpleNamespace(text='jQuery({"result":{"cmsArticleWebOld":[{"title":"新闻"}]}})',
                           raise_for_status=lambda: None)
    with patch.object(sentiment.requests, "get", side_effect=[requests.Timeout(), body]) as req:
        assert sentiment.get_stock_news("600521")[0]["title"] == "新闻"
        assert req.call_count == 2 and req.call_args.kwargs["timeout"] == 4
    with patch.object(sentiment.requests, "get", side_effect=requests.Timeout()) as req:
        assert not sentiment.get_stock_news("600521")
        assert req.call_count == 2


def test_limit_down_fallback_preserves_pct_units():
    from data.market import get_limit_down
    client = HiThinkClient(api_key="test")
    data = {"pagination": {"total": 1, "pages": 1},
            "item": [{"thscode": "600521.SH", "name": "样本", "last_price": 9.1,
                      "price_change_ratio_pct": -10, "turnover_ratio_pct": 2}]}
    with patch("data.market._get_limit_down_akshare", return_value=pd.DataFrame()), \
            patch("data.hithink.get_client", return_value=client), \
            patch.object(client, "_request", return_value=data) as req:
        frame = get_limit_down("20261012")
        assert frame.iloc[0]["代码"] == "600521" and frame.iloc[0]["涨跌幅"] == -10
        assert frame.attrs["source"] == "hithink"
        assert "date" not in req.call_args.args[1]
        assert pd.Timestamp(req.call_args.args[1]["date_ms"], unit="ms", tz="UTC").tz_convert("Asia/Shanghai").strftime("%Y-%m-%d") == "2026-10-12"


def test_limit_down_never_counts_partial_pool_as_complete():
    client = HiThinkClient(api_key="test")
    row = {"thscode": "600521.SH"}
    first = {"pagination": {"total": 2, "pages": 2}, "item": [row]}
    last = {"pagination": {"total": 2, "pages": 2}, "item": [{"thscode": "600522.SH"}]}
    with patch.object(client, "_request", side_effect=[first, last]) as req:
        assert len(client.get_limit_down("20261012")["item"]) == 2
        assert req.call_args.args[1]["page"] == 2
    with patch.object(client, "_request", side_effect=[first, dict(last, item=[])]):
        try:
            client.get_limit_down("20261012")
        except HiThinkError as exc:
            assert "incomplete" in str(exc)
        else:
            raise AssertionError("partial pool was accepted")


def test_research_budget_progress_does_not_hide_item_failures():
    from main import cmd_research_sync
    args = SimpleNamespace(research_universe_size=800, research_batch_size=None,
                           research_workers=1, research_history_days=730)
    data = {"status": "partial", "budget_exhausted": True, "attempted": 20, "ok": 20}
    with patch("data.research_universe.refresh_research_universe", return_value={"codes": []}), \
            patch("data.research_universe.sync_research_universe", return_value=data):
        assert cmd_research_sync(args)["errors"] == []
        data["ok"] = 19
        assert cmd_research_sync(args)["errors"] == ["research_sync"]


def test_same_day_research_resumes_without_refetching_market():
    import data.research_universe as research
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "universe.json")
        data = {"generated_at": datetime.now().isoformat(),
                "codes": [{"code": f"{600000+i}"} for i in range(20)]}
        with open(path, "w") as file:
            json.dump(data, file)
        with patch.object(research, "_hithink_active_stocks") as provider:
            assert research.refresh_research_universe(limit=20, path=path, reuse_today=True) == data
            provider.assert_not_called()


def test_volume_candidates_use_ratio_not_previous_close():
    import strategy.stock_picker as picker
    rows = [{"f12": "600521", "f14": "样本", "f2": 20, "f3": 3, "f18": 19.5, "f10": 1},
            {"f12": "000017", "f14": "放量", "f2": 4, "f3": 2, "f18": 3.9, "f10": 5}]
    resp = SimpleNamespace(json=lambda: {"data": {"diff": rows}})
    with patch.object(picker.requests, "get", return_value=resp) as req:
        assert set(picker._get_volume_candidates()) == {"000017"}
        assert "fid=f10" in req.call_args.args[0]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn();print("OK",name)
