"""价格信号：竞彩线偏离国际线时，按历史实测换算胜率并计算期望值。

点值表来源：19,385 场 NBA（SBR，2007-08 → 2022-23）的收盘让分与收盘总分。
对每场的残差 r = 实际分差 + 收盘主队让分（总分为 实际总分 - 收盘总分），统计
“拿到比收盘线好 d 分的线”时的赢/走盘比例（主客两侧取平均）。复现：
``python scripts/lottery_calibrate.py <SBR games.csv>``。

竞彩让分与总分都是 .5 结尾，不会走盘；因此用 ``赢 + 走/2`` 作为对半分线上的胜率。
这张表衡量的是相对“收盘线”的价值；我们比较的是抓取时点的国际线快照，噪声更大，
所以入选门槛用期望值 ≥ LOTTERY_MIN_EV（默认 +2%）留出安全边际。
"""
from __future__ import annotations

import math
import os
from typing import Dict, List, Optional

# (d, 赢, 走) — d 为相对收盘线的有利分差
SPREAD_TABLE = [
    (0.0, 0.4915, 0.0169), (0.5, 0.5085, 0.0176), (1.0, 0.5261, 0.0182), (1.5, 0.5443, 0.0190),
    (2.0, 0.5633, 0.0184), (2.5, 0.5816, 0.0180), (3.0, 0.5996, 0.0157), (3.5, 0.6153, 0.0164),
    (4.0, 0.6316, 0.0171), (4.5, 0.6488, 0.0157), (5.0, 0.6645, 0.0151), (5.5, 0.6796, 0.0141),
    (6.0, 0.6937, 0.0141), (6.5, 0.7079, 0.0138), (7.0, 0.7217, 0.0151), (7.5, 0.7368, 0.0127),
    (8.0, 0.7494, 0.0121), (8.5, 0.7615, 0.0123), (9.0, 0.7738, 0.0126), (9.5, 0.7864, 0.0109),
    (10.0, 0.7972, 0.0112),
]
TOTAL_TABLE = [
    (0.0, 0.4931, 0.0138), (0.5, 0.5069, 0.0112), (1.0, 0.5181, 0.0119), (1.5, 0.5300, 0.0120),
    (2.0, 0.5420, 0.0104), (2.5, 0.5524, 0.0121), (3.0, 0.5646, 0.0116), (3.5, 0.5762, 0.0110),
    (4.0, 0.5872, 0.0113), (4.5, 0.5985, 0.0104), (5.0, 0.6090, 0.0116), (5.5, 0.6205, 0.0120),
    (6.0, 0.6325, 0.0117), (6.5, 0.6442, 0.0105), (7.0, 0.6547, 0.0108), (7.5, 0.6655, 0.0099),
    (8.0, 0.6753, 0.0107), (8.5, 0.6861, 0.0111), (9.0, 0.6972, 0.0108), (9.5, 0.7080, 0.0093),
    (10.0, 0.7173, 0.0105),
]
TABLES = {"让分": SPREAD_TABLE, "大小分": TOTAL_TABLE}

MIN_EV = float(os.environ.get("LOTTERY_MIN_EV", "0.02"))
MAX_PICKS_PER_DAY = int(os.environ.get("LOTTERY_MAX_PICKS", "3"))
MAX_EDGE_POINTS = 8.0  # 偏离过大多半是数据错配（换线、伤停未同步），不入选
STRATEGY = "sharp"
FULL_CARD_STRATEGY = "all_games"  # 每场每玩法都出一个选择，不设期望值门槛
LATE_STRATEGY = "sharp_late"  # 与主信号同一规则，改在竞彩停售前最后一次采集时出单（影子对照）
LATE_WINDOW_MINUTES = float(os.environ.get("LOTTERY_LATE_WINDOW_MINUTES", "45"))


def win_probability(points: float, market: str) -> float:
    """在 .5 结尾的竞彩线上、相对国际线有利 points 分时的胜率（线性插值）。"""
    table = TABLES[market]
    if points < 0:
        return 1.0 - win_probability(-points, market)
    points = min(points, table[-1][0])
    for (d0, w0, p0), (d1, w1, p1) in zip(table, table[1:]):
        if d0 <= points <= d1:
            t = (points - d0) / (d1 - d0)
            win, push = w0 + t * (w1 - w0), p0 + t * (p1 - p0)
            return win + push / 2
    d, w, p = table[-1]
    return w + p / 2


def expected_value(probability: float, odds: float) -> float:
    return probability * (odds - 1.0) - (1.0 - probability)


def breakeven(odds: float) -> float:
    return 1.0 / odds


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def candidates(game: dict) -> List[dict]:
    """返回一场比赛在两个玩法上的“有利一侧”及其期望值（不论是否达标）。

    让分：d = 竞彩主队让分 - 国际主队让分；d>0 说明竞彩给主队更多分 → 主队一侧更便宜。
    大小分：d = 国际总分 - 竞彩总分；d>0 说明竞彩总分更低 → 买大更便宜（反之买小）。
    """
    out = []
    jc_line, ref_line = _num(game.get("jc_line")), _num(game.get("ref_line"))
    if jc_line is not None and ref_line is not None:
        d = jc_line - ref_line
        home = d > 0
        odds = _num(game.get("jc_sp_home" if home else "jc_sp_away"))
        if odds and odds > 1 and d != 0:
            p = win_probability(abs(d), "让分")
            out.append({"market": "让分", "side": "主" if home else "客", "line": jc_line,
                        "ref_line": ref_line, "edge": abs(d), "odds": odds, "p_win": p,
                        "ev": expected_value(p, odds), "single_open": game.get("single_spread")})
    jc_total, ref_total = _num(game.get("jc_total")), _num(game.get("ref_total"))
    if jc_total is not None and ref_total is not None:
        d = ref_total - jc_total
        over = d > 0
        odds = _num(game.get("jc_sp_over" if over else "jc_sp_under"))
        if odds and odds > 1 and d != 0:
            p = win_probability(abs(d), "大小分")
            out.append({"market": "大小分", "side": "大" if over else "小", "line": jc_total,
                        "ref_line": ref_total, "edge": abs(d), "odds": odds, "p_win": p,
                        "ev": expected_value(p, odds), "single_open": game.get("single_total")})
    return out


def select_picks(games: List[dict], *, min_ev: float = None, max_picks: int = None,
                 already: int = 0) -> List[dict]:
    """全天按期望值排序，取达标且偏离不过大的前 N 注（已出的注单计入上限）。"""
    min_ev = MIN_EV if min_ev is None else min_ev
    limit = (MAX_PICKS_PER_DAY if max_picks is None else max_picks) - already
    pool = []
    for game in games:
        for cand in candidates(game):
            if cand["ev"] >= min_ev and cand["edge"] <= MAX_EDGE_POINTS:
                pool.append({**cand, "game": game})
    pool.sort(key=lambda item: item["ev"], reverse=True)
    return pool[:max(0, limit)]


# (玩法, 竞彩线字段, 国际线字段, 第一侧, 第一侧赔率字段, 第二侧, 第二侧赔率字段, 单关字段)
_MARKETS = (
    ("让分", "jc_line", "ref_line", "主", "jc_sp_home", "客", "jc_sp_away", "single_spread"),
    ("大小分", "jc_total", "ref_total", "大", "jc_sp_over", "小", "jc_sp_under", "single_total"),
)


def _first_side_points(market: str, jc: float, ref: float) -> float:
    """第一侧（主 / 大）相对国际线的有利分数；负数表示第二侧（客 / 小）更便宜。"""
    return jc - ref if market == "让分" else ref - jc


def full_card(game: dict) -> List[dict]:
    """每场比赛每个玩法都给出一个选择：两侧中期望值更高的一侧，不设门槛。

    有国际线时按点值表估算两侧胜率（偏离为 0 时两侧各约 50%，于是选赔率更高的一侧）；
    缺国际线时无法估算胜率，只按赔率更高的一侧记录，胜率与期望值留空。
    竞彩线或赔率缺失（未开售）的玩法跳过。
    """
    out = []
    for market, jc_key, ref_key, side_a, odds_a_key, side_b, odds_b_key, single_key in _MARKETS:
        jc = _num(game.get(jc_key))
        odds_a, odds_b = _num(game.get(odds_a_key)), _num(game.get(odds_b_key))
        if jc is None or not odds_a or not odds_b or odds_a <= 1 or odds_b <= 1:
            continue
        ref = _num(game.get(ref_key))
        if ref is None:
            side, odds, p, ev, edge = (side_a, odds_a, None, None, None) if odds_a >= odds_b else (side_b, odds_b, None, None, None)
        else:
            points = _first_side_points(market, jc, ref)
            p_a = win_probability(points, market)
            options = [(side_a, odds_a, p_a, points), (side_b, odds_b, 1.0 - p_a, -points)]
            side, odds, p, edge = max(options, key=lambda o: (expected_value(o[2], o[1]), o[1]))
            ev = expected_value(p, odds)
        out.append({"market": market, "side": side, "line": jc, "ref_line": ref, "edge": edge,
                    "odds": odds, "p_win": p, "ev": ev, "single_open": game.get(single_key)})
    return out


def required_edge(odds: float, market: str, min_ev: float = None) -> Optional[float]:
    """给定赔率下达到期望值门槛所需的最小偏离分数（用于页面说明）。"""
    min_ev = MIN_EV if min_ev is None else min_ev
    d = 0.0
    while d <= MAX_EDGE_POINTS:
        if expected_value(win_probability(d, market), odds) >= min_ev:
            return round(d, 2)
        d += 0.1
    return None


def describe(market: str, side: str, line: float, home_cn: str, away_cn: str) -> str:
    if market == "让分":
        if side == "主":
            return f"{home_cn} {line:+.1f}"
        return f"{away_cn} {-line:+.1f}"
    return f"{'大' if side == '大' else '小'} {line:.1f}"


def clv_points(market: str, side: str, line: float, close_ref: Optional[float]) -> Optional[float]:
    """收盘价差：我们拿到的竞彩线相对国际收盘线好多少分（正=更好）。"""
    close_ref = _num(close_ref)
    if close_ref is None:
        return None
    if market == "让分":
        return (line - close_ref) if side == "主" else (close_ref - line)
    return (close_ref - line) if side == "大" else (line - close_ref)


def grade(market: str, side: str, line: float, home_score: int, away_score: int, odds: float):
    """返回 (结果, 盈亏单位)。让分/大小分均含加时。"""
    margin = home_score - away_score
    if market == "让分":
        value = margin + line if side == "主" else -(margin + line)
    else:
        total = home_score + away_score
        value = total - line if side == "大" else line - total
    if value > 0:
        return "赢", round(odds - 1.0, 4)
    if value < 0:
        return "输", -1.0
    return "走", 0.0
