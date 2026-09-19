#!/usr/bin/env python3
"""运行 Vibe-Trading 因子 IC/IR 基准并落盘摘要。

实际计算在 vibe venv 内完成（见 strategy/vibe_bridge.py），本脚本只负责
调用、落盘和展示。结果供盘后复盘 prompt 引用：

    data/vibe/alpha_bench_latest.json          # 复盘读取的固定入口
    data/vibe/alpha_bench_<universe>_<zoo>.json

用法:
    python scripts/vibe_alpha_screen.py --universe csi300 --zoo gtja191 \
        --period 2024-2026 --top 20

注意: csi300 宇宙需要 vibe venv 侧能访问 Tushare（免费档即可，
TUSHARE_TOKEN 配在 vibe venv 的环境里），300 只股票全量抓取约需数分钟。
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
    parser = argparse.ArgumentParser(description="Vibe-Trading 因子 IC/IR 基准")
    parser.add_argument("--universe", default="csi300", help="csi300 | sp500 | btc-usdt")
    parser.add_argument("--zoo", default="gtja191", help="gtja191 | alpha101 | academic")
    parser.add_argument("--period", default="2024-2026", help="YYYY-YYYY 或 YYYY-MM-DD/YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=20, help="报告保留的头部因子数")
    parser.add_argument("--timeout", type=int, default=None, help="覆盖 VIBE_TOOL_TIMEOUT_SECONDS")
    args = parser.parse_args()

    if not os.path.isdir(VIBE_DATA_DIR):
        os.makedirs(VIBE_DATA_DIR, exist_ok=True)

    print(f">> 运行 alpha_bench: universe={args.universe} zoo={args.zoo} "
          f"period={args.period} top={args.top}（首次运行含行情抓取，可能耗时数分钟）")
    payload = alpha_bench(
        universe=args.universe, zoo=args.zoo, period=args.period,
        top=args.top, output_dir=VIBE_DATA_DIR, timeout=args.timeout,
    )
    if not payload:
        print("ERROR: 研究层不可用或调用失败。检查 VIBE_TRADING_ENABLED=1、"
              "venv 已安装（scripts/setup_vibe_trading.sh）与行情源可达。", file=sys.stderr)
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
    })

    canonical = os.path.join(VIBE_DATA_DIR, "alpha_bench_latest.json")
    named = os.path.join(VIBE_DATA_DIR, f"alpha_bench_{args.universe}_{args.zoo}.json")
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
