#!/usr/bin/env python3
"""Laya 影子决策契约测试（完全离线：HTTP 与模型全部打桩）。"""
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy.laya_client as laya_client
from strategy.laya_client import (
    ACTION_ALIASES,
    LayaUnavailable,
    _laya_answer,
    build_candidate_state,
    run_shadow_comparison,
)


def ok(message):
    print(f"  OK {message}")


def _fake_laya_result():
    """按 laya 0.3.x 实测输出契约构造预测结果。"""
    return {
        "answers": {
            "action": {
                "type": "choice",
                "choice": "buy",
                "probabilities": {"buy": 0.6754, "hold": 0.1231, "sell": 0.2015},
                "confidence": 0.2301,
                "action": {"act_probability": 1.0},
            },
            "conviction": {
                "type": "score",
                "score": 1.5412,
                "legend": {
                    "0": "weak or conflicting",
                    "1": "moderate edge",
                    "2": "strong consistent edge",
                },
                "probabilities": {"0": 0.0717, "1": 0.3154, "2": 0.6129},
            },
        },
        "routing": {"model": "multilingual", "repo": "convaiinnovations/laya/multilingual"},
        "output_tokens": 0,
    }


def test_state_is_features_only():
    decision = {
        "code": "601123",
        "date": "2026-09-22",
        "action": "BUY",
        "confidence": 0.97,
        "reasoning": "MiMo 的判断理由不应泄漏",
        "dimensions": json.dumps({
            "technical": {"score": 70, "confidence": 0.8},
            "sentiment": {"score": 65, "confidence": 0.6},
            "broken": {"score": "nan", "confidence": None},
        }),
    }
    state = build_candidate_state(decision, "恒瑞医药")
    # 状态只含特征：MiMo 的答案/置信度/理由绝不进入 laya 的输入
    assert set(state.keys()) == {"code", "date", "name", "dimensions"}
    flat = json.dumps(state, ensure_ascii=False)
    assert "BUY" not in flat and "0.97" not in flat and "理由" not in flat
    assert state["code"] == "601123" and state["name"] == "恒瑞医药"
    assert state["dimensions"]["technical"] == {"score": 70.0, "confidence": 0.8}
    assert "broken" not in state["dimensions"]
    ok("候选状态只含 6 维特征，不含 MiMo 答案/置信度/理由，脏维度被丢弃")

    bare = build_candidate_state({"code": "000001", "date": "2026-09-22"})
    assert bare["dimensions"] == {} and "name" not in bare
    ok("缺少 dimensions 的行退化为空特征状态而不报错")


def test_answer_parsing():
    result = _fake_laya_result()
    action = _laya_answer(result, "action")
    assert action["choice"] == "buy"
    assert action["probability"] == 0.6754
    assert action["probabilities"]["sell"] == 0.2015
    assert action["confidence"] == 0.2301
    conviction = _laya_answer(result, "conviction")
    assert conviction["score"] == 1.5412
    assert conviction["legend"]["2"] == "strong consistent edge"
    assert conviction["probabilities"]["1"] == 0.3154
    ok("choice/score 答案按实测契约解析（probabilities 字典+连续 score）")

    assert _laya_answer({"answers": {}}, "action") is None
    assert _laya_answer(None, "action") is None
    ok("缺失或畸形答案返回 None 而不抛异常")


def test_action_aliases():
    assert ACTION_ALIASES["BUY"] == "buy"
    assert ACTION_ALIASES["SELL"] == "sell"
    assert ACTION_ALIASES["HOLD"] == "hold"
    ok("MiMo 动作到 laya 类别的映射")


def _seed_db(db_path, rows):
    from data.database import Database
    with Database(db_path=db_path) as db:
        for row in rows:
            db.insert_llm_decision(row)


def _http_response(status, payload):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def test_shadow_comparison_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = os.path.join(tmp, "laya")
        handle = tempfile.NamedTemporaryFile(suffix="_laya.db", delete=False)
        db_path = handle.name
        handle.close()
        os.unlink(db_path)
        try:
            _seed_db(db_path, [
                {"code": "601123", "date": "2026-09-22", "action": "BUY",
                 "llm_response": "{}", "confidence": 0.8,
                 "dimensions": json.dumps({"technical": {"score": 70, "confidence": 0.8}})},
                {"code": "000001", "date": "2026-09-22", "action": "HOLD",
                 "llm_response": "{}", "confidence": 0.5,
                 "dimensions": json.dumps({"technical": {"score": 50, "confidence": 0.0}})},
            ])

            with mock.patch.object(laya_client, "LAYA_DATA_DIR", data_dir), \
                    mock.patch.object(laya_client, "is_available", return_value=True), \
                    mock.patch.object(laya_client.httpx, "post",
                                      return_value=_http_response(200, {
                                          "ok": True,
                                          "results": [_fake_laya_result(), _fake_laya_result()],
                                      })) as post:
                summary = run_shadow_comparison("2026-09-22", db_path=db_path)
            assert summary is not None
            assert summary["n_candidates"] == 2
            # 601123 MiMo=BUY laya=buy 一致；000001 MiMo=HOLD laya=buy 不一致
            assert summary["agreement"] == "1/2"
            assert summary["confusion"]["buy->buy"] == 1
            assert summary["confusion"]["hold->buy"] == 1
            assert os.path.isfile(os.path.join(data_dir, "shadow_2026-09-22.json"))
            # 请求体：状态只含特征，questions 原样透传
            request = post.call_args.kwargs["json"]
            assert request["states"][0]["code"] == "601123"
            assert "action" not in request["states"][0]
            assert request["questions"] is laya_client.SHADOW_QUESTIONS

            from data.database import Database
            with Database(db_path=db_path) as db:
                events = [dict(r) for r in db.conn.execute(
                    "SELECT details FROM auto_events WHERE event_type='laya_shadow'"
                ).fetchall()]
            assert events and json.loads(events[0]["details"])["agreement"] == "1/2"
            ok("影子对照端到端：逐只对齐、混淆矩阵、落盘 JSON、laya_shadow 事件")
        finally:
            for candidate in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
                if os.path.exists(candidate):
                    os.unlink(candidate)


def test_shadow_degradation():
    with tempfile.TemporaryDirectory() as tmp:
        handle = tempfile.NamedTemporaryFile(suffix="_laya2.db", delete=False)
        db_path = handle.name
        handle.close()
        os.unlink(db_path)
        try:
            with mock.patch.object(laya_client, "is_available", return_value=False):
                assert run_shadow_comparison("2026-09-22", db_path=db_path) is None
            ok("服务不可用时影子对照静默返回 None")

            _seed_db(db_path, [
                {"code": "601123", "date": "2026-09-23", "action": "BUY",
                 "llm_response": "{}", "confidence": 0.8, "dimensions": "{}"},
            ])
            with mock.patch.object(laya_client, "LAYA_DATA_DIR", tmp), \
                    mock.patch.object(laya_client, "is_available", return_value=True), \
                    mock.patch.object(laya_client.httpx, "post",
                                      return_value=_http_response(500, {"ok": False})):
                assert run_shadow_comparison("2026-09-23", db_path=db_path) is None
            ok("服务 500 时影子对照静默返回 None")

            with mock.patch.object(laya_client, "LAYA_DATA_DIR", tmp), \
                    mock.patch.object(laya_client, "is_available", return_value=True), \
                    mock.patch.object(laya_client.httpx, "post",
                                      return_value=_http_response(200, {
                                          "ok": True, "results": [_fake_laya_result()],
                                      })):
                assert run_shadow_comparison("2099-01-01", db_path=db_path) is None
            ok("当日无候选时不发起请求并返回 None")
        finally:
            for candidate in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
                if os.path.exists(candidate):
                    os.unlink(candidate)


def test_server_module_contract():
    server_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "laya", "laya_server.py",
    )
    spec = importlib.util.spec_from_file_location("laya_server_test", server_path)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    envelope = server.build_ok_envelope([{"a": 1}], 12.345)
    assert envelope == {"ok": True, "results": [{"a": 1}], "latency_ms": 12.35}
    assert server.build_error_envelope("boom") == {"ok": False, "error": "boom"}
    ok("服务信封契约（成功/失败）")

    handler_cls = server.make_handler(router=mock.Mock())
    assert handler_cls is not None
    ok("handler 可在无真实模型的情况下构造（模型加载延迟到 main）")


def main():
    print("== Laya 影子决策契约测试 ==")
    test_state_is_features_only()
    test_answer_parsing()
    test_action_aliases()
    test_shadow_comparison_end_to_end()
    test_shadow_degradation()
    test_server_module_contract()
    print("全部通过")


if __name__ == "__main__":
    main()
