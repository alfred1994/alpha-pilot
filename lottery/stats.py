"""纸面账本统计：命中率置信区间、收益率、收盘价差、预注册停机规则与累计曲线。"""
from __future__ import annotations

import math
from collections import OrderedDict
from typing import Dict, Iterable, List

STRATEGY_LABELS = {"sharp": "价格偏离（主信号）", "all_games": "全场预测（每场必出）",
                   "model_legacy": "模型对照组（已停用）"}
BANKROLL_UNITS = 100.0          # 1 注 = 1 单位 = 1% 本金
STOP_RULES = (
    {"at": 50, "metric": "hit_rate", "threshold": 0.55, "label": "累计 50 注时命中率 < 55% → 停"},
    {"at": 150, "metric": "roi", "threshold": -0.05, "label": "累计 150 注时收益率 < -5% → 停"},
)
MAX_DRAWDOWN_UNITS = 20.0       # 任一时点回撤达到本金 20% → 停


def wilson(wins: int, n: int, z: float = 1.96):
    if n <= 0:
        return None, None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def summarize(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    settled = [r for r in rows if r.get("result") in ("赢", "输", "走")]
    wins = sum(r["result"] == "赢" for r in settled)
    losses = sum(r["result"] == "输" for r in settled)
    pushes = sum(r["result"] == "走" for r in settled)
    decided = wins + losses
    pnl = sum(float(r.get("pnl") or 0) for r in settled)
    low, high = wilson(wins, decided)
    clvs = [float(r["clv"]) for r in settled if r.get("clv") is not None]
    odds = [float(r["odds"]) for r in settled if r.get("odds")]
    avg_odds = sum(odds) / len(odds) if odds else None
    return {
        "bets": len(rows), "settled": len(settled), "pending": len(rows) - len(settled),
        "wins": wins, "losses": losses, "pushes": pushes,
        "hit_rate": wins / decided if decided else None, "hit_rate_ci95": [low, high] if decided else None,
        "pnl_units": round(pnl, 4), "roi": pnl / len(settled) if settled else None,
        "avg_odds": avg_odds, "breakeven_hit_rate": 1 / avg_odds if avg_odds else None,
        "avg_clv_points": sum(clvs) / len(clvs) if clvs else None, "clv_samples": len(clvs),
        "positive_clv_ratio": sum(c > 0 for c in clvs) / len(clvs) if clvs else None,
    }


def stop_status(rows: List[dict]) -> dict:
    settled = sorted((r for r in rows if r.get("result") in ("赢", "输", "走")),
                     key=lambda r: (r.get("settled_at") or "", r.get("id") or 0))
    checks = []
    for rule in STOP_RULES:
        if len(settled) < rule["at"]:
            checks.append({**rule, "status": "pending", "progress": len(settled)})
            continue
        sample = summarize(settled[:rule["at"]])
        value = sample[rule["metric"]]
        failed = value is None or value < rule["threshold"]
        checks.append({**rule, "status": "stop" if failed else "pass", "value": value, "progress": rule["at"]})
    peak = equity = drawdown = 0.0
    for r in settled:
        equity += float(r.get("pnl") or 0)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    drawdown_hit = drawdown >= MAX_DRAWDOWN_UNITS
    triggered = drawdown_hit or any(c["status"] == "stop" for c in checks)
    return {"checks": checks, "max_drawdown_units": round(drawdown, 4),
            "drawdown_limit_units": MAX_DRAWDOWN_UNITS, "drawdown_triggered": drawdown_hit,
            "triggered": triggered, "settled": len(settled)}


def curve(rows: List[dict]) -> List[dict]:
    """按结算对应的销售日累计盈亏，每个策略一条线。"""
    by_day: Dict[str, Dict[str, float]] = OrderedDict()
    for r in sorted((r for r in rows if r.get("result") in ("赢", "输", "走")), key=lambda r: r["sale_date"]):
        by_day.setdefault(r["sale_date"], {})
        by_day[r["sale_date"]][r["strategy"]] = by_day[r["sale_date"]].get(r["strategy"], 0.0) + float(r.get("pnl") or 0)
    points, running = [], {}
    for day, values in by_day.items():
        for strategy, pnl in values.items():
            running[strategy] = running.get(strategy, 0.0) + pnl
        points.append({"date": day, **{k: round(v, 4) for k, v in running.items()}})
    return points
