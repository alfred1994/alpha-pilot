#!/usr/bin/env python3
"""Vibe-Trading MCP 工具驱动（在 vibe venv 内运行，由 strategy/vibe_bridge.py 调用）。

用法:
    python vibe_tool_driver.py <tool> '<json-args>'

在 vibe venv 内 import 已安装的 mcp_server（vibe-trading-ai 的 MCP 服务器），
用 fastmcp 的 in-process Client 走 MCP 公开契约调用工具，结果以 JSON 信封
写到 stdout：

    {"ok": true, "content": ["<text>", ...], "structured": <Any|None>}
    {"ok": false, "error": "<reason>"}

约定：
  - stdout 只承载本信封；所有库日志强制走 stderr；
  - 模块级只 import 标准库，重依赖在 main() 内延迟导入——这样 AlphaPilot
    的离线测试可以直接 import 本模块做契约测试，无需安装 vibe-trading；
  - 本驱动不判断工具语义，白名单由调用方（strategy/vibe_bridge.py）把关；
  - alpha_bench 且 universe=alphapilot:* 时，用环境变量 ALPHAPILOT_VIBE_PANEL
    指向的本地长表 CSV（data/vibe_panel.py 导出）替换 vibe 的宇宙加载器，
    数据完全来自 AlphaPilot 本地日线（k_daily/同花顺），不触 Tushare。
"""
import asyncio
import json
import logging
import os
import re
import sys

_PERIOD_YEAR = re.compile(r"^(\d{4})-(\d{4})$")
_PERIOD_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})$")


def _period_bounds(period):
    """解析 MCP 工具公开的 period 格式（与 vibe _parse_period 语义一致）。"""
    period = str(period or "").strip()
    m = _PERIOD_DATE.match(period)
    if m:
        return m.group(1), m.group(2)
    m = _PERIOD_YEAR.match(period)
    if m:
        return f"{m.group(1)}-01-01", f"{m.group(2)}-12-31"
    raise RuntimeError(f"period 格式不支持: {period!r}")


def build_success_envelope(content, structured=None):
    """构造成功信封；structured 只保留可 JSON 序列化的对象。"""
    if structured is not None:
        try:
            json.dumps(structured, ensure_ascii=False)
        except (TypeError, ValueError):
            structured = None
    return {"ok": True, "content": content, "structured": structured}


def build_error_envelope(error):
    return {"ok": False, "error": str(error)}


def extract_texts(result):
    """从 MCP CallToolResult 提取文本块，兼容 fastmcp 2.x/3.x 的返回形态。"""
    texts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            texts.append(text)
    if not texts and isinstance(getattr(result, "data", None), str):
        texts.append(result.data)
    return texts


def _panel_from_long_csv(path, period=None):
    """把 AlphaPilot 导出的长表 CSV pivot 成 alpha_bench 需要的宽表 panel。

    panel 约定（vibe 0.1.x）：dict[str, DataFrame]，键为
    open/high/low/close/volume/amount/vwap，值是 date×code 宽表。
    code 必须按字符串读入，否则 000001 会被吃掉前导零。
    """
    import pandas as pd

    frame = pd.read_csv(path, dtype={"code": str})
    required = {"date", "code", "open", "high", "low", "close", "volume", "amount"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"panel CSV 缺少列: {sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    if period:
        start_s, end_s = _period_bounds(period)
        frame = frame[
            (frame["date"] >= pd.Timestamp(start_s)) & (frame["date"] <= pd.Timestamp(end_s))
        ]
    if frame.empty:
        raise RuntimeError(f"panel CSV 在 period={period} 内没有数据: {path}")
    panel = {}
    for col in ("open", "high", "low", "close", "volume", "amount", "vwap"):
        if col not in frame.columns:
            continue
        panel[col] = frame.pivot_table(
            index="date", columns="code", values=col, aggfunc="last",
        ).sort_index()
    if panel.get("close") is None or panel["close"].empty:
        raise RuntimeError(f"panel CSV 缺少有效 close 数据: {path}")
    return panel


def _install_alphapilot_universe_loader(panel_csv):
    """把 universe=alphapilot:* 的加载替换为本地 panel CSV（唯一 pin 的内部缝）。

    其余 universe（csi300 等）仍走 vibe 原始加载器。缝不存在说明 pin 的
    vibe-trading 版本已变，必须显式失败而不是静默走 Tushare。
    """
    from src.tools import alpha_bench_tool

    original = getattr(alpha_bench_tool, "_load_universe_panel", None)
    if original is None:
        raise RuntimeError(
            "vibe-trading alpha_bench_tool 缺少 _load_universe_panel，"
            "pin 的版本契约已变化；请核对 scripts/setup_vibe_trading.sh 固定的版本"
        )

    def _loader(universe, period, *, use_cache=True):
        if str(universe).startswith("alphapilot:"):
            return _panel_from_long_csv(panel_csv, period)
        return original(universe, period, use_cache=use_cache)

    alpha_bench_tool._load_universe_panel = _loader
    return original


def call_mcp_tool(tool, args):
    """在 vibe venv 内走 MCP 契约调用工具；重依赖在此延迟导入。"""
    import mcp_server  # vibe-trading-ai 安装后为顶层模块（py-modules）
    from fastmcp import Client

    universe = str((args or {}).get("universe") or "")
    if tool == "alpha_bench" and universe.startswith("alphapilot:"):
        panel_csv = os.environ.get("ALPHAPILOT_VIBE_PANEL", "").strip()
        if not panel_csv:
            raise RuntimeError(
                "universe=alphapilot:* 需要 ALPHAPILOT_VIBE_PANEL 指向 AlphaPilot 导出的 panel CSV"
            )
        if not os.path.isfile(panel_csv):
            raise RuntimeError(f"ALPHAPILOT_VIBE_PANEL 指向的文件不存在: {panel_csv}")
        _install_alphapilot_universe_loader(panel_csv)

    server = getattr(mcp_server, "mcp", None)
    if server is None:
        raise RuntimeError("mcp_server.mcp 不存在，vibe-trading 版本可能不兼容")

    async def _run():
        async with Client(server) as client:
            return await client.call_tool(tool, args)

    result = asyncio.run(_run())
    texts = extract_texts(result)
    if not texts:
        raise RuntimeError("MCP 工具未返回任何文本内容")
    structured = getattr(result, "data", None)
    if structured is not None and not isinstance(structured, (dict, list, str, int, float, bool)):
        structured = None
    return build_success_envelope(texts, structured)


def main(argv):
    if len(argv) < 2:
        envelope = build_error_envelope("用法: vibe_tool_driver.py <tool> <json-args>")
        print(json.dumps(envelope, ensure_ascii=False))
        return 1
    tool = argv[1]
    try:
        args = json.loads(argv[2]) if len(argv) > 2 else {}
    except json.JSONDecodeError as exc:
        envelope = build_error_envelope(f"参数不是合法 JSON: {exc}")
        print(json.dumps(envelope, ensure_ascii=False))
        return 1
    try:
        envelope = call_mcp_tool(tool, args)
    except Exception as exc:  # noqa: BLE001 —— 信封化一切失败，交给调用方降级
        envelope = build_error_envelope(f"{type(exc).__name__}: {exc}")
        print(json.dumps(envelope, ensure_ascii=False))
        return 1
    print(json.dumps(envelope, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    # 库日志（fastmcp/mcp_server）全部走 stderr，保持 stdout 只含结果信封。
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    sys.exit(main(sys.argv))
