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
  - 本驱动不判断工具语义，白名单由调用方（strategy/vibe_bridge.py）把关。
"""
import asyncio
import json
import logging
import sys


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


def call_mcp_tool(tool, args):
    """在 vibe venv 内走 MCP 契约调用工具；重依赖在此延迟导入。"""
    import mcp_server  # vibe-trading-ai 安装后为顶层模块（py-modules）
    from fastmcp import Client

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
