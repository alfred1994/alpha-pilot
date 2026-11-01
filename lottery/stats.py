"""纸面账本统计：命中率置信区间、收益率、收盘价差、预注册停机规则与累计曲线。"""
from __future__ import annotations

import math
import statistics
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from lottery import injuries

STRATEGY_LABELS = {"sharp": "价格偏离（主信号）", "sharp_late": "价格偏离（停售前，影子）",
                   "all_games": "全场预测（每场必出）", "model_legacy": "模型对照组（已停用）"}
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


def by_injury_tag(rows: Iterable[dict]) -> List[dict]:
    """按出单时的伤停标签分组；伤停未记录的注单（历史注单、抓取失败）单独计数，不归入“无”。"""
    rows = list(rows)
    out = []
    for tag in injuries.TAGS:
        groups = {True: [], False: []}
        unknown = 0
        for row in rows:
            flag = injuries.tag_group(row.get("injury_tags"), tag)
            if flag is None:
                unknown += 1
            else:
                groups[flag].append(row)
        out.append({"tag": tag, "with": summarize(groups[True]), "without": summarize(groups[False]),
                    "unrecorded": unknown})
    return out


# (玩法, 竞彩线字段, 国际线字段, 赔率字段)
_MOVEMENT_MARKETS = (
    ("让分", "jc_line", "ref_line", ("jc_sp_home", "jc_sp_away")),
    ("大小分", "jc_total", "ref_total", ("jc_sp_over", "jc_sp_under")),
)


def _at(value) -> Optional[datetime]:
    return injuries.parse_time(value)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def jc_movement(games: Iterable[List[dict]]) -> Dict[str, dict]:
    """竞彩在首次抓取（16:00 出单）之后到停售前有没有调线、朝哪个方向调。

    输入每场一组竞彩快照（含同一时刻的国际线），按时间排序。偏离 = |竞彩线 - 国际线|；
    “偏离缩小”说明竞彩在向国际线靠拢，也就是 16:00 看到的便宜价格之后被收回了。
    """
    out = {}
    games = [sorted(g, key=lambda s: _at(s.get("captured_at")) or _EPOCH) for g in games]
    for market, jc_key, ref_key, sp_keys in _MOVEMENT_MARKETS:
        tracked = changed = sp_changed = narrowed = widened = 0
        first_devs, last_devs, minutes = [], [], []
        for snaps in games:
            rows = [s for s in snaps if s.get(jc_key) is not None]
            if len(rows) < 2:
                continue
            tracked += 1
            first, last = rows[0], rows[-1]
            if any(r.get(jc_key) != first[jc_key] for r in rows[1:]):
                changed += 1
                moved = next(r for r in rows[1:] if r.get(jc_key) != first[jc_key])
                start, when = _at(first.get("captured_at")), _at(moved.get("captured_at"))
                if start and when:
                    minutes.append((when - start).total_seconds() / 60)
            if any(tuple(r.get(k) for k in sp_keys) != tuple(first.get(k) for k in sp_keys) for r in rows[1:]):
                sp_changed += 1
            if first.get(ref_key) is not None and last.get(ref_key) is not None:
                d0 = abs(first[jc_key] - first[ref_key])
                d1 = abs(last[jc_key] - last[ref_key])
                first_devs.append(d0)
                last_devs.append(d1)
                narrowed += d1 < d0
                widened += d1 > d0
        out[market] = {
            "games": tracked, "line_changed": changed, "odds_changed": sp_changed,
            "deviation_narrowed": narrowed, "deviation_widened": widened, "deviation_pairs": len(first_devs),
            "avg_deviation_first": sum(first_devs) / len(first_devs) if first_devs else None,
            "avg_deviation_last": sum(last_devs) / len(last_devs) if last_devs else None,
            "median_minutes_to_change": statistics.median(minutes) if minutes else None,
        }
    return out
