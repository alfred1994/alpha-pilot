"""
Vibe-Trading 研究层桥接
====================================================================
把外置 vibe-trading venv（HKUDS/Vibe-Trading，MIT）作为只读研究工具接入：

  - 因子库（GTJA191 / Alpha101 / academic）清单与元数据  -> alpha_zoo
  - 因子 IC/IR 基准（默认宇宙=AlphaPilot 研究池，数据来自本地
    k_daily/同花顺日线，不触 Tushare）                   -> alpha_bench

隔离原则：
  - vibe-trading-ai 及其重依赖（langchain/langgraph/fastmcp）只存在于独立
    venv，主依赖树零新增；
  - 调用走 MCP 服务器公开契约（scripts/vibe/vibe_tool_driver.py 在 vibe
    venv 内以 in-process MCP client 调用工具），不用其内部 API；
  - 工具白名单只含只读研究工具；vibe 侧 shell 工具默认关闭，broker 工具
    从不调用；
  - 任何失败（未安装/禁用/超时/输出异常）对交易主链路都是静默降级。

使用方法:
    from strategy.vibe_bridge import (
        VibeUnavailable, alpha_bench, list_alphas,
        load_alpha_bench_summary, format_vibe_evidence_line,
    )
====================================================================
"""
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from config import VIBE_DATA_DIR, VIBE_PYTHON, VIBE_TOOL_TIMEOUT_SECONDS, VIBE_TRADING_ENABLED

DRIVER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "vibe", "vibe_tool_driver.py",
)

# MCP 工具白名单：只暴露 vibe-trading 中已核实为只读的研究工具。
# 扩展前先核对 mcp_server.py 的工具文档，禁止加入任何下单/撤单/写缓存类工具。
ALLOWED_TOOLS = {"alpha_zoo", "alpha_bench"}

# alpha_bench 输出信封（vibe 侧契约）：{"status", "report_path", "n_alphas_tested",
# "n_skipped", "top": [{"id", "ic_mean", "ir", ...}]}
DEFAULT_TOP_N = 8


class VibeUnavailable(Exception):
    """vibe-trading 研究层不可用（禁用/未安装/超时/输出异常）。"""


def vibe_python_path() -> str:
    """返回 vibe venv 的解释器路径；找不到时返回空串。"""
    if VIBE_PYTHON:
        return VIBE_PYTHON
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".vibe-trading-venv", "Scripts", "python.exe"),  # Windows
        os.path.join(home, ".vibe-trading-venv", "bin", "python"),          # Linux
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


def is_available() -> bool:
    """研究层是否可用（启用且 venv 已安装）。不发起子进程。"""
    return bool(VIBE_TRADING_ENABLED and vibe_python_path())


def call_tool(tool: str, args: Optional[Dict[str, Any]] = None,
              timeout: int = None, env_extra: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    通过 driver 子进程调用 vibe-trading MCP 工具，返回解析后的信封。

    返回: {"ok": True, "content": [str...], "structured": Any|None}
    失败: 抛 VibeUnavailable（禁用/未安装/白名单外/超时/非JSON输出）。
    """
    if not VIBE_TRADING_ENABLED:
        raise VibeUnavailable("VIBE_TRADING_ENABLED 未开启")
    if tool not in ALLOWED_TOOLS:
        raise VibeUnavailable(f"工具 {tool!r} 不在白名单内")
    python = vibe_python_path()
    if not python:
        raise VibeUnavailable("未找到 vibe venv 解释器（先运行 scripts/setup_vibe_trading.sh）")
    if not os.path.isfile(DRIVER_PATH):
        raise VibeUnavailable(f"driver 不存在: {DRIVER_PATH}")

    cmd = [python, DRIVER_PATH, tool, json.dumps(args or {}, ensure_ascii=False)]
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.update(env_extra or {})
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout or VIBE_TOOL_TIMEOUT_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired:
        raise VibeUnavailable(f"vibe 工具 {tool} 超时（>{timeout or VIBE_TOOL_TIMEOUT_SECONDS}s）")
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise VibeUnavailable(
            f"vibe driver 退出码 {proc.returncode}: {' | '.join(stderr_tail) or '无stderr'}"
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise VibeUnavailable("vibe driver 输出不是 JSON")
    if not envelope.get("ok"):
        raise VibeUnavailable(str(envelope.get("error") or "vibe driver 报告失败"))
    return envelope


def _first_json(envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 MCP 文本内容里取出第一段可解析为 JSON 对象的负载（容忍围栏等杂质）。"""
    decoder = json.JSONDecoder()
    for text in envelope.get("content") or []:
        if not isinstance(text, str):
            continue
        start = text.find("{")
        if start < 0:
            continue
        try:
            payload, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    structured = envelope.get("structured")
    if isinstance(structured, dict):
        return structured
    return None


def list_alphas(zoo: str = "gtja191", limit: int = 20) -> Optional[Dict[str, Any]]:
    """列出因子库中的因子清单。不可用时返回 None（不阻塞调用方）。"""
    try:
        envelope = call_tool("alpha_zoo", {"action": "list_alphas", "zoo": zoo, "limit": limit})
    except VibeUnavailable:
        return None
    return _first_json(envelope)


def alpha_bench(universe: str = "alphapilot:pool", zoo: str = "gtja191",
                period: str = "2024-2026", top: int = 20,
                output_dir: str = None, panel_csv: str = None,
                timeout: int = None) -> Optional[Dict[str, Any]]:
    """
    运行因子 IC/IR 基准。

    - universe 以 ``alphapilot:`` 开头（默认 alphapilot:pool）时，数据来自
      AlphaPilot 本地日线（data/vibe_panel.py 导出的 panel CSV，经环境变量
      传给 driver），不触 Tushare；panel_csv 必须存在。
    - 其他 universe（csi300 等）走 vibe 内置加载器，需要其自身的行情源
      （csi300 需 TUSHARE_TOKEN，默认不用）。

    返回 vibe 侧 JSON 信封（含 status/report_path/top），不可用时返回 None。
    """
    args: Dict[str, Any] = {"universe": universe, "zoo": zoo, "period": period, "top": top}
    if output_dir:
        args["output_dir"] = output_dir
    env_extra: Optional[Dict[str, str]] = None
    try:
        if universe.startswith("alphapilot:"):
            if not panel_csv or not os.path.isfile(panel_csv):
                raise VibeUnavailable(
                    f"universe={universe} 需要有效的 panel_csv（由 data.vibe_panel.export_panel_csv 生成）"
                )
            env_extra = {"ALPHAPILOT_VIBE_PANEL": panel_csv}
        envelope = call_tool("alpha_bench", args, timeout=timeout, env_extra=env_extra)
    except VibeUnavailable:
        return None
    return _first_json(envelope)


def load_alpha_bench_summary(path: str = None,
                             top_n: int = DEFAULT_TOP_N) -> Optional[Dict[str, Any]]:
    """
    读取 scripts/vibe_alpha_screen.py 落盘的最新基准摘要（纯文件读取，
    不发起子进程、不触网）。文件缺失或损坏返回 None。
    """
    path = path or os.path.join(VIBE_DATA_DIR, "alpha_bench_latest.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    rows = []
    for item in payload.get("top") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        try:
            rows.append({
                "id": str(item["id"]),
                "ic_mean": round(float(item.get("ic_mean") or 0.0), 4),
                "ir": round(float(item.get("ir") or 0.0), 4),
            })
        except (TypeError, ValueError):
            continue
        if len(rows) >= top_n:
            break
    if not rows:
        return None
    return {
        "universe": str(payload.get("universe") or ""),
        "zoo": str(payload.get("zoo") or ""),
        "period": str(payload.get("period") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "n_alphas_tested": int(payload.get("n_alphas_tested") or 0),
        "top": rows,
    }


def format_vibe_evidence_line(summary: Dict[str, Any]) -> str:
    """把基准摘要压成一行复盘 prompt 证据文本。"""
    if not summary:
        return ""
    factors = "，".join(
        f"{row['id']}(IC{row['ic_mean']:+.3f}/IR{row['ir']:+.2f})" for row in summary["top"]
    )
    return (
        f"{summary.get('zoo')}@{summary.get('universe')} "
        f"[{summary.get('period')}] 高IC因子: {factors}"
    )


if __name__ == "__main__":
    # 手工自检：python -m strategy.vibe_bridge
    print(f"enabled={VIBE_TRADING_ENABLED} python={vibe_python_path()!r} "
          f"driver={os.path.isfile(DRIVER_PATH)}")
    summary = load_alpha_bench_summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2) if summary
          else "alpha_bench_latest.json 不存在或无效")
    sys.exit(0)
