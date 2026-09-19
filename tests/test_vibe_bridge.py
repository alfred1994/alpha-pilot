#!/usr/bin/env python3
"""Vibe-Trading 研究层桥接契约测试（完全离线：不安装 vibe-trading 也能跑）。"""
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy.vibe_bridge as vibe_bridge
from scheduler.trader_brief import build_daily_facts
from strategy.vibe_bridge import (
    VibeUnavailable,
    call_tool,
    format_vibe_evidence_line,
    load_alpha_bench_summary,
)

_DRIVER_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "vibe", "vibe_tool_driver.py",
)
_spec = importlib.util.spec_from_file_location("vibe_tool_driver", _DRIVER_SRC)
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)


def ok(message):
    print(f"  OK {message}")


def _write_bench_payload(path, status="ok", top=None):
    payload = {
        "status": status,
        "report_path": str(path).replace(".json", ".html"),
        "n_alphas_tested": 191,
        "n_skipped": 3,
        "universe": "csi300",
        "zoo": "gtja191",
        "period": "2024-2026",
        "generated_at": "2026-09-19T08:00:00",
        "top": top if top is not None else [
            {"id": "GTJA#014", "ic_mean": 0.05123, "ir": 0.8123},
            {"id": "GTJA#050", "ic_mean": 0.04456, "ir": 0.7},
        ],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return payload


def test_summary_loader_offline():
    with tempfile.TemporaryDirectory() as tmp:
        assert load_alpha_bench_summary(os.path.join(tmp, "missing.json")) is None
        ok("基准文件缺失时返回 None")

        broken = os.path.join(tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{not-json")
        assert load_alpha_bench_summary(broken) is None
        payload = _write_bench_payload(broken)
        payload["status"] = "error"
        with open(broken, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        assert load_alpha_bench_summary(broken) is None
        ok("损坏或非 ok 状态的基准文件被拒绝")

        good = os.path.join(tmp, "good.json")
        _write_bench_payload(good, top=[
            {"id": "A", "ic_mean": 0.0512345, "ir": 0.81234},
            {"no_id": True},
            {"id": "B", "ic_mean": "bad", "ir": 1},
            {"id": "C", "ic_mean": -0.02, "ir": -0.3},
        ])
        summary = load_alpha_bench_summary(good)
        assert summary["universe"] == "csi300" and summary["zoo"] == "gtja191"
        assert [row["id"] for row in summary["top"]] == ["A", "C"]
        assert summary["top"][0]["ic_mean"] == 0.0512
        ok("基准摘要只保留有效因子并按 4 位小数收敛")

        line = format_vibe_evidence_line(summary)
        assert "gtja191@csi300" in line and "A(IC+0.051/IR+0.81)" in line
        ok("因子证据行可格式化供复盘 prompt 引用")


def test_call_tool_guards():
    with mock.patch.object(vibe_bridge, "VIBE_TRADING_ENABLED", False):
        try:
            call_tool("alpha_zoo", {})
            raise AssertionError("禁用时应抛 VibeUnavailable")
        except VibeUnavailable:
            pass
        ok("未启用时调用被拒绝且不产生子进程")

    with mock.patch.object(vibe_bridge, "VIBE_TRADING_ENABLED", True), \
            mock.patch.object(vibe_bridge, "vibe_python_path", return_value="python"):
        try:
            call_tool("refresh_strategy_evidence", {})
            raise AssertionError("白名单外工具应被拒绝")
        except VibeUnavailable as exc:
            assert "白名单" in str(exc)
        ok("MCP 工具白名单外的调用被拒绝")

        with mock.patch.object(vibe_bridge.subprocess, "run",
                               side_effect=vibe_bridge.subprocess.TimeoutExpired(cmd="x", timeout=1)):
            try:
                call_tool("alpha_zoo", {}, timeout=1)
                raise AssertionError("超时应抛 VibeUnavailable")
            except VibeUnavailable as exc:
                assert "超时" in str(exc)
        ok("子进程超时被转换为明确的不可用错误")

        failed = mock.Mock(returncode=1, stderr="boom\ntraceback line")
        with mock.patch.object(vibe_bridge.subprocess, "run", return_value=failed):
            try:
                call_tool("alpha_zoo", {})
                raise AssertionError("非零退出码应抛 VibeUnavailable")
            except VibeUnavailable as exc:
                assert "boom" in str(exc)
        ok("driver 失败退出会携带 stderr 尾部信息")

        good_run = mock.Mock(returncode=0, stdout=json.dumps({
            "ok": True, "content": ['{"status":"ok","top":[{"id":"GTJA#014"}]}'],
        }), stderr="")
        with mock.patch.object(vibe_bridge.subprocess, "run", return_value=good_run) as run:
            envelope = call_tool("alpha_zoo", {"action": "list_alphas"})
            assert envelope["ok"] is True
            payload = vibe_bridge._first_json(envelope)
            assert payload["top"][0]["id"] == "GTJA#014"
            passed_cmd = run.call_args[0][0]
            assert passed_cmd[2] == "alpha_zoo" and json.loads(passed_cmd[3])["action"] == "list_alphas"
        ok("driver 成功信封被解析，命令行参数契约正确")

        bad_json = mock.Mock(returncode=0, stdout="not-json-at-all", stderr="")
        with mock.patch.object(vibe_bridge.subprocess, "run", return_value=bad_json):
            try:
                call_tool("alpha_zoo", {})
                raise AssertionError("非 JSON 输出应抛 VibeUnavailable")
            except VibeUnavailable:
                pass
        ok("driver 异常输出不会被误判为成功")


class _FakeText:
    def __init__(self, text):
        self.text = text


class _FakeResult:
    def __init__(self, texts, data=None):
        self.content = [_FakeText(t) for t in texts]
        self.data = data


class _FakeClient:
    payload = None
    error = None

    def __init__(self, server):
        assert server is not None, "必须传入 mcp_server.mcp"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, tool, args):
        if _FakeClient.error:
            raise _FakeClient.error
        return _FakeClient.payload


def test_driver_envelope_contract():
    envelope = driver.build_success_envelope(["x"], structured={"a": 1})
    assert envelope == {"ok": True, "content": ["x"], "structured": {"a": 1}}
    envelope = driver.build_success_envelope(["x"], structured=object())
    assert envelope["structured"] is None
    ok("成功信封会剥离不可序列化的 structured 负载")

    assert driver.extract_texts(_FakeResult(["a", "b"])) == ["a", "b"]
    assert driver.extract_texts(_FakeResult([], data="raw")) == ["raw"]
    ok("文本块提取兼容 fastmcp 结果形态")

    fake_mcp_server = SimpleNamespace(mcp=object())
    fake_fastmcp = SimpleNamespace(Client=_FakeClient)

    _FakeClient.payload = _FakeResult(['{"status":"ok","n_alphas_tested":191}'], data=None)
    with mock.patch.dict(sys.modules, {"mcp_server": fake_mcp_server, "fastmcp": fake_fastmcp}):
        rc = driver.main(["driver", "alpha_bench", '{"universe":"csi300"}'])
    assert rc == 0
    ok("driver 在模拟 vibe 依赖下走通完整 MCP 调用路径")

    _FakeClient.error = RuntimeError("no tushare token")
    try:
        with mock.patch.dict(sys.modules, {"mcp_server": fake_mcp_server, "fastmcp": fake_fastmcp}):
            rc = driver.main(["driver", "alpha_bench", "{}"])
        assert rc == 1
    finally:
        _FakeClient.error = None
    ok("MCP 调用失败被信封化且退出码为 1")

    assert driver.main(["driver"]) == 1
    assert driver.main(["driver", "alpha_bench", "{bad"]) == 1
    ok("参数缺失或 JSON 非法时以失败信封退出")


def test_trader_brief_vibe_section():
    from data.database import Database

    with tempfile.TemporaryDirectory() as tmp:
        original_data_dir = vibe_bridge.VIBE_DATA_DIR
        vibe_bridge.VIBE_DATA_DIR = tmp
        handle = tempfile.NamedTemporaryFile(suffix="_vibe_brief.db", delete=False)
        db_path = handle.name
        handle.close()
        os.unlink(db_path)
        try:
            facts = build_daily_facts(
                date="2026-09-18", db_path=db_path,
                now=datetime(2026, 9, 18, 15, 10),
                market_status="盘后", trading_day=True,
            )
            assert facts["vibe_factors"] == {}
            ok("未运行因子基准时 daily_facts 的 vibe 段为空")

            _write_bench_payload(os.path.join(tmp, "alpha_bench_latest.json"))
            facts = build_daily_facts(
                date="2026-09-18", db_path=db_path,
                now=datetime(2026, 9, 18, 15, 10),
                market_status="盘后", trading_day=True,
            )
            vibe = facts["vibe_factors"]
            assert vibe and vibe["top"][0]["id"] == "GTJA#014"
            ok("基准摘要存在时 daily_facts 携带因子证据")
        finally:
            vibe_bridge.VIBE_DATA_DIR = original_data_dir
            for candidate in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
                if os.path.exists(candidate):
                    os.unlink(candidate)


def test_driver_alphapilot_universe_loader():
    import pandas as pd

    fake_tool = SimpleNamespace()
    fake_tool._parse_period = lambda p: ("2024-01-01", "2024-12-31")
    fake_tool._load_universe_panel = mock.Mock(return_value={"close": "ORIGINAL"})
    fake_src_tools = SimpleNamespace(alpha_bench_tool=fake_tool)

    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "panel_2024-01-01_2024-12-31.csv")
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03"] * 2,
            "code": ["600519", "600519", "000001", "000001"],
            "open": [10.0, 10.1, 3.3, 3.4],
            "high": [10.2, 10.3, 3.5, 3.6],
            "low": [9.9, 10.0, 3.2, 3.3],
            "close": [10.1, 10.2, 3.4, 3.5],
            "volume": [100.0, 110.0, 200.0, 210.0],
            "amount": [1010.0, 1122.0, 680.0, 735.0],
            "vwap": [10.1, 10.2, 3.4, 3.5],
        }).to_csv(csv_path, index=False)

        with mock.patch.dict(sys.modules, {
            "src": SimpleNamespace(tools=None),
            "src.tools": fake_src_tools,
            "src.tools.alpha_bench_tool": fake_tool,
        }):
            original = driver._install_alphapilot_universe_loader(csv_path)
            panel = fake_tool._load_universe_panel("alphapilot:pool", "2024-2026", use_cache=True)
            assert list(panel["close"].columns) == ["000001", "600519"]
            assert panel["close"].iloc[0, 0] == 3.4
            legacy = fake_tool._load_universe_panel("csi300", "2024-2026", use_cache=True)
            assert legacy == {"close": "ORIGINAL"}
        ok("driver 把 alphapilot:* 宇宙替换为本地 panel，其余 universe 透传 vibe 原始加载器")

        fake_mcp_server = SimpleNamespace(mcp=object())
        fake_fastmcp = SimpleNamespace(Client=_FakeClient)
        _FakeClient.payload = _FakeResult(['{"status":"ok"}'])
        with mock.patch.dict(sys.modules, {
            "mcp_server": fake_mcp_server, "fastmcp": fake_fastmcp,
            "src": SimpleNamespace(tools=None),
            "src.tools": fake_src_tools,
            "src.tools.alpha_bench_tool": fake_tool,
        }), mock.patch.dict(os.environ, {"ALPHAPILOT_VIBE_PANEL": csv_path}):
            assert driver.main(["driver", "alpha_bench",
                                '{"universe":"alphapilot:pool","period":"2024-2026"}']) == 0
        with mock.patch.dict(sys.modules, {
            "mcp_server": fake_mcp_server, "fastmcp": fake_fastmcp,
        }), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALPHAPILOT_VIBE_PANEL", None)
            rc = driver.main(["driver", "alpha_bench", '{"universe":"alphapilot:pool"}'])
            assert rc == 1
        ok("缺 ALPHAPILOT_VIBE_PANEL 时 driver 显式失败而非落入 Tushare 路径")


def test_alpha_bench_panel_env_passthrough():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "panel.csv")
        with open(csv_path, "w", encoding="utf-8") as fh:
            fh.write("date,code,open,high,low,close,volume,amount,vwap\n")
            fh.write("2024-01-02,600519,10,10.2,9.9,10.1,100,1010,10.1\n")

        good_run = mock.Mock(returncode=0, stdout=json.dumps({
            "ok": True, "content": ['{"status":"ok","n_alphas_tested":191,"top":[]}'],
        }), stderr="")
        with mock.patch.object(vibe_bridge, "VIBE_TRADING_ENABLED", True), \
                mock.patch.object(vibe_bridge, "vibe_python_path", return_value="python"), \
                mock.patch.object(vibe_bridge.subprocess, "run", return_value=good_run) as run:
            payload = vibe_bridge.alpha_bench(universe="alphapilot:pool", panel_csv=csv_path)
            assert payload == {"status": "ok", "n_alphas_tested": 191, "top": []}
            env_used = run.call_args.kwargs["env"]
            assert env_used["ALPHAPILOT_VIBE_PANEL"] == csv_path
        ok("alpha_bench 把 panel 路径经环境变量传给 driver")

        with mock.patch.object(vibe_bridge, "VIBE_TRADING_ENABLED", True), \
                mock.patch.object(vibe_bridge, "vibe_python_path", return_value="python"), \
                mock.patch.object(vibe_bridge.subprocess, "run") as run:
            assert vibe_bridge.alpha_bench(universe="alphapilot:pool", panel_csv="missing.csv") is None
            assert not run.called
            assert vibe_bridge.alpha_bench(universe="alphapilot:pool") is None
            assert not run.called
        ok("panel 文件缺失时不发起子进程并静默降级")


def main():
    print("== Vibe-Trading 桥接契约测试 ==")
    test_summary_loader_offline()
    test_call_tool_guards()
    test_driver_envelope_contract()
    test_driver_alphapilot_universe_loader()
    test_alpha_bench_panel_env_passthrough()
    test_trader_brief_vibe_section()
    print("全部通过")


if __name__ == "__main__":
    main()
