#!/usr/bin/env python3
"""Laya 文本情绪契约测试（完全离线：HTTP 与数据源全部打桩）。"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy.laya_text as laya_text
from strategy.laya_text import (
    SENTIMENTS,
    _collect_stock_news,
    _tally,
    format_text_sentiment_line,
    load_text_sentiment_summary,
    run_text_sentiment,
    score_texts,
)


def ok(message):
    print(f"  OK {message}")


def _answer(choice, p):
    return {"sentiment": {
        "type": "choice",
        "choice": choice,
        "probabilities": p,
    }}


def _fake_result(choice):
    p = {k: 0.05 for k in SENTIMENTS}
    p[choice] = 0.9
    return {"answers": _answer(choice, p),
            "routing": {"model": "multilingual"}}


def _http_response(status, payload):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def test_score_texts_chunks():
    """25 条文本按 batch_size=10 分 3 批，顺序保持；畸形答案返回 None。"""
    texts = [f"新闻{i}" for i in range(25)]

    def fake_post(url, json=None, timeout=None):
        assert url.endswith("/predict")
        chunk = json["states"]
        assert all(set(s.keys()) == {"text"} for s in chunk)
        return _http_response(200, {
            "ok": True,
            "results": [_fake_result(SENTIMENTS[i % 3]) for i in range(len(chunk))],
        })

    with mock.patch.object(laya_text.httpx, "post", side_effect=fake_post) as post:
        results = score_texts(texts)
    assert results is not None and len(results) == 25
    assert post.call_count == 3
    assert results[0]["choice"] in SENTIMENTS
    assert set(results[0]["probabilities"].keys()) == set(SENTIMENTS)
    ok("文本分批情绪分类：3 次请求、顺序保持、契约解析")

    def bad_post(url, json=None, timeout=None):
        return _http_response(200, {"ok": True, "results": [
            {"answers": _answer("unknown", {})},
        ]})

    with mock.patch.object(laya_text.httpx, "post", side_effect=bad_post):
        assert score_texts(["一条文本"]) is None
    ok("choice 不在三分类内时整批返回 None")

    with mock.patch.object(laya_text.httpx, "post",
                           return_value=_http_response(500, {"ok": False})):
        assert score_texts(["一条文本"]) is None
    assert score_texts([]) == []
    ok("服务 500 返回 None；空输入不发起请求")


def test_tally():
    assert _tally([]) == {"positive": 0, "neutral": 0, "negative": 0}
    assert _tally([{"choice": "positive"}, {"choice": "negative"},
                   {"choice": "negative"}]) == {
        "positive": 1, "neutral": 0, "negative": 2}
    ok("情绪计数聚合")


def test_stock_news_collector_degrades():
    with mock.patch("data.rate_limit"), \
            mock.patch("data.sentiment.get_stock_news", side_effect=Exception("boom")):
        texts, code_of_text, counts = _collect_stock_news(["600000", "600001"])
    assert texts == [] and code_of_text == []
    assert counts == {"600000": 0, "600001": 0}
    ok("新闻源全部失败时返回空而不抛异常")

    news = {
        "600000": [{"title": "公司获大单", "content": "金额约12亿元",
                    "source": "东财"}, {"title": "", "content": "无标题跳过",
                              "source": "东财"}],
        "600001": [],
    }

    def fake_get_stock_news(code):
        return news[code]

    with mock.patch("data.rate_limit"), \
            mock.patch("data.sentiment.get_stock_news", side_effect=fake_get_stock_news):
        texts, code_of_text, counts = _collect_stock_news(["600000", "600001"])
    assert counts == {"600000": 1, "600001": 0}
    assert len(texts) == 1 and code_of_text == ["600000"]
    assert "公司获大单" in texts[0] and "12亿元" in texts[0]
    ok("个股新闻文本构建：空标题跳过、code 与文本对齐")


def _seed_db(db_path, rows):
    from data.database import Database
    with Database(db_path=db_path) as db:
        for row in rows:
            db.insert_llm_decision(row)


def test_run_text_sentiment_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        handle = tempfile.NamedTemporaryFile(suffix="_layatext.db", delete=False)
        db_path = handle.name
        handle.close()
        os.unlink(db_path)
        try:
            _seed_db(db_path, [
                {"code": "601123", "date": "2026-09-23", "action": "BUY",
                 "llm_response": "{}", "confidence": 0.8, "dimensions": "{}"},
                {"code": "000001", "date": "2026-09-23", "action": "HOLD",
                 "llm_response": "{}", "confidence": 0.5, "dimensions": "{}"},
            ])

            headlines = {"headlines": [
                {"source": "同花顺", "title": "央行降准0.5个百分点"},
                {"source": "新浪财经", "title": ""},
            ], "sector_heat": []}
            news = {"601123": [{"title": "公司业绩大增", "content": "净利+80%",
                                "source": "东财"}],
                    "000001": []}

            def fake_post(url, json=None, timeout=None):
                chunk = json["states"]
                # 市场批（头条1条）答利好；个股批（601123 1条）答利好
                choice = "positive" if any(
                    "央行" in s["text"] or "业绩" in s["text"] for s in chunk) else "neutral"
                return _http_response(200, {"ok": True,
                                            "results": [_fake_result(choice)
                                                        for _ in chunk]})

            with mock.patch.object(laya_text, "is_available", return_value=True), \
                    mock.patch("data.news_aggregator.get_all_news",
                               return_value=headlines), \
                    mock.patch("data.sentiment.get_weibo_finance", return_value=[]), \
                    mock.patch("data.rate_limit"), \
                    mock.patch("data.sentiment.get_stock_news",
                               side_effect=lambda code: news[code]), \
                    mock.patch.object(laya_text, "LAYA_DATA_DIR", tmp), \
                    mock.patch.object(laya_text.httpx, "post", side_effect=fake_post):
                summary = run_text_sentiment("2026-09-23", db_path=db_path)

            assert summary is not None
            assert summary["market"]["positive"] == 1 and summary["market"]["total"] == 1
            assert summary["per_stock"]["601123"]["positive"] == 1
            assert summary["per_stock"]["601123"]["news_count"] == 1
            assert summary["per_stock"]["000001"]["news_count"] == 0
            assert summary["stats"]["n_codes"] == 2
            assert os.path.isfile(os.path.join(tmp, "text_sentiment_2026-09-23.json"))
            assert os.path.isfile(os.path.join(tmp, "text_sentiment_latest.json"))

            with mock.patch.object(laya_text, "LAYA_DATA_DIR", tmp):
                loaded = load_text_sentiment_summary()
            assert loaded["date"] == "2026-09-23"
            line = format_text_sentiment_line(loaded)
            assert "601123" in line and "文本情绪" in line
            ok("端到端：市场+个股文本情绪分类、落盘双文件、latest 加载、证据行")

            from data.database import Database
            with Database(db_path=db_path) as db:
                events = [dict(r) for r in db.conn.execute(
                    "SELECT details FROM auto_events "
                    "WHERE event_type='laya_text_sentiment'"
                ).fetchall()]
            assert events and json.loads(events[0]["details"])["n_texts_scored"] == 2
            ok("laya_text_sentiment 事件落库")
        finally:
            for candidate in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
                if os.path.exists(candidate):
                    os.unlink(candidate)


def test_degradation():
    with tempfile.TemporaryDirectory() as tmp:
        handle = tempfile.NamedTemporaryFile(suffix="_layatext2.db", delete=False)
        db_path = handle.name
        handle.close()
        os.unlink(db_path)
        try:
            with mock.patch.object(laya_text, "is_available", return_value=False):
                assert run_text_sentiment("2026-09-23", db_path=db_path) is None
            ok("服务不可用时静默返回 None")

            _seed_db(db_path, [
                {"code": "601123", "date": "2026-09-24", "action": "BUY",
                 "llm_response": "{}", "confidence": 0.8, "dimensions": "{}"},
            ])
            with mock.patch.object(laya_text, "is_available", return_value=True), \
                    mock.patch.object(laya_text, "LAYA_DATA_DIR", tmp), \
                    mock.patch.object(laya_text.httpx, "post") as post:
                assert run_text_sentiment("2099-01-01", db_path=db_path) is None
            assert post.call_count == 0
            ok("当日无候选时不发起请求并返回 None")
        finally:
            for candidate in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
                if os.path.exists(candidate):
                    os.unlink(candidate)


def test_load_degrades():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(laya_text, "LAYA_DATA_DIR", tmp):
            assert load_text_sentiment_summary() == {}
            ok("latest 文件缺失时返回空")

        with open(os.path.join(tmp, "text_sentiment_latest.json"),
                  "w", encoding="utf-8") as fh:
            fh.write("{broken json")
        with mock.patch.object(laya_text, "LAYA_DATA_DIR", tmp):
            assert load_text_sentiment_summary() == {}
        ok("latest 文件损坏时返回空")

    assert format_text_sentiment_line({}) == ""
    ok("空摘要的证据行为空串")


def test_trader_brief_integration():
    from scheduler.trader_brief import build_daily_facts
    handle = tempfile.NamedTemporaryFile(suffix="_layatext3.db", delete=False)
    db_path = handle.name
    handle.close()
    os.unlink(db_path)
    try:
        from data.database import Database
        with Database(db_path=db_path) as db:
            pass
        # latest 文件不存在 → 空 dict（不报错、不阻塞 brief）
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("strategy.laya_text.LAYA_DATA_DIR", tmp):
                facts = build_daily_facts("2026-09-23", db_path=db_path,
                                          market_status="closed")
        assert facts.get("text_sentiment") == {}
        ok("trader brief 含 text_sentiment 字段，无文件时静默为空")
    finally:
        for candidate in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
            if os.path.exists(candidate):
                os.unlink(candidate)


def main():
    print("== Laya 文本情绪契约测试 ==")
    test_score_texts_chunks()
    test_tally()
    test_stock_news_collector_degrades()
    test_run_text_sentiment_end_to_end()
    test_degradation()
    test_load_degrades()
    test_trader_brief_integration()
    print("全部通过")


if __name__ == "__main__":
    main()
