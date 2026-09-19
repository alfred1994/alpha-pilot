#!/usr/bin/env python3
"""运行 Vibe-Trading 因子 IC/IR 基准并落盘摘要。

数据与计算分工：
  - 数据面：AlphaPilot 本地日线（k_daily 缓存 → 同花顺 → 长桥 → Baostock），
    由 data/vibe_panel.py 构建研究池 panel，不使用 Tushare；
  - 计算面：vibe venv 内的 alpha_bench（见 strategy/vibe_bridge.py）。

结果供盘后复盘 prompt 引用：

    data/vibe/alpha_bench_latest.json          # 复盘读取的固定入口
    data/vibe/alpha_bench_<universe>_<zoo>.json

用法:
    # 默认：研究池前 100 只（universe 规格见 data.vibe_panel.resolve_universe_codes）
    python scripts/vibe_alpha_screen.py --period 2024-2026

    # 自定义代码文件
    python scripts/vibe_alpha_screen.py --universe alphapilot:file:data/my_codes.txt

    # vibe 内置宇宙（需要 TUSHARE_TOKEN，默认不用）
    python scripts/vibe_alpha_screen.py --universe csi300
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import VIBE_DATA_DIR
from strategy.vibe_bridge import VibeUnavailable, alpha_bench


def main():
    parser = argparse.ArgumentParser(description="Vibe-Trading 因子 IC/IR 基准（AlphaPilot 本地数据）")
    parser.add_argument("--universe", default="alphapilot:pool",
                        help="alphapilot:pool | alphapilot:file:<path> | csi300/sp500/btc-usdt")
    parser.add_argument("--pool-limit", type=int, default=100,
                        help="universe=alphapilot:pool 时截取研究池前 N 只（0=全部）")
    parser.add_argument("--min-rows", type=int, default=60,
                        help="面板内单只代码最少日线行数，不足则跳过")
    parser.add_argument("--zoo", default="gtja191", help="gtja191 | alpha101 | academic")
    parser.add_argument("--period", default="2024-2026", help="YYYY-YYYY 或 YYYY-MM-DD/YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=20, help="报告保留的头部因子数")
    parser.add_argument("--timeout", type=int, default=None, help="覆盖 VIBE_TOOL_TIMEOUT_SECONDS")
    args = parser.parse_args()

    if not os.path.isdir(VIBE_DATA_DIR):
        os.makedirs(VIBE_DATA_DIR, exist_ok=True)

    panel_csv = None
    if args.universe.startswith("alphapilot:"):
        from data.vibe_panel import export_panel_csv, resolve_universe_codes
        try:
            codes = resolve_universe_codes(args.universe.split(":", 1)[1], pool_limit=args.pool_limit)
        except ValueError as exc:
            print(f"ERROR: 解析宇宙失败: {exc}", file=sys.stderr)
            return 1
        print(f">> 构建本地日线面板: {len(codes)} 只代码, period={args.period}"
              f"（k_daily 缓存 → 同花顺 → 兜底源）")
        try:
            panel_csv, panel_stats = export_panel_csv(
                codes, args.period, min_rows=args.min_rows,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f">> 面板完成: 使用 {panel_stats['used']}/{panel_stats['requested']} 只, "
              f"{panel_stats['rows']} 行 → {panel_csv}")
        if panel_stats["skipped"]:
            preview = list(panel_stats["skipped"].items())[:5]
            print(f">> 跳过 {len(panel_stats['skipped'])} 只（如 {preview}…）")

    print(f">> 运行 alpha_bench: universe={args.universe} zoo={args.zoo} "
          f"period={args.period} top={args.top}")
    payload = alpha_bench(
        universe=args.universe, zoo=args.zoo, period=args.period,
        top=args.top, output_dir=VIBE_DATA_DIR, panel_csv=panel_csv,
        timeout=args.timeout,
    )
    if not payload:
        print("ERROR: 研究层不可用或调用失败。检查 VIBE_TRADING_ENABLED=1、"
              "venv 已安装（scripts/setup_vibe_trading.sh）与本地日线数据可用。",
              file=sys.stderr)
        return 1
    if payload.get("status") != "ok":
        print(f"ERROR: vibe alpha_bench 报告失败: {payload}", file=sys.stderr)
        return 1

    payload = dict(payload)
    payload.update({
        "universe": args.universe,
        "zoo": args.zoo,
        "period": args.period,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "data_source": "alphapilot_k_daily_hithink" if panel_csv else "vibe_builtin",
    })
    if panel_csv:
        payload["panel_csv"] = panel_csv

    canonical = os.path.join(VIBE_DATA_DIR, "alpha_bench_latest.json")
    named = os.path.join(VIBE_DATA_DIR, f"alpha_bench_{args.universe.replace(':', '_')}_{args.zoo}.json")
    for path in (canonical, named):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    print(f">> 已写入 {canonical}")
    print(f">> 测试 {payload.get('n_alphas_tested', '?')} 个因子，"
          f"跳过 {payload.get('n_skipped', '?')} 个；头部因子:")
    rows = payload.get("top") or []
    for row in rows[:10]:
        print(f"   {row.get('id'):<16} IC={row.get('ic_mean', 0):+.4f}  IR={row.get('ir', 0):+.3f}")
    report = payload.get("report_path")
    if report:
        print(f">> 完整 HTML 报告: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
