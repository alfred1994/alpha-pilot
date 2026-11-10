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
STUDY_TYPES = ("单关", "2串1", "3串1")
RESEARCH_STRATEGIES = (STRATEGY, LATE_STRATEGY)


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
    """全天按期望值降序取前 N 注（已出单注数计入上限）。

    不按单关开售过滤。没开单关的腿留在这里，研究时由 ``recommend_tickets`` 合成串关。
    """
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


def _match_key(pick: dict):
    """同一场比赛的身份。有场次号就用场次号，否则用主客队；两边都空则无法组票。"""
    if pick.get("matchnum"):
        return ("num", str(pick["matchnum"]))
    if pick.get("home") and pick.get("away"):
        return ("teams", pick["home"], pick["away"])
    return None


def _selection(pick: dict) -> str:
    if pick.get("selection"):
        return pick["selection"]
    line = _num(pick.get("line"))
    if pick.get("market") and pick.get("side") and line is not None:
        home = pick.get("home_cn") or pick.get("home") or ""
        away = pick.get("away_cn") or pick.get("away") or ""
        return describe(pick["market"], pick["side"], line, home, away)
    return ""


def _leg_view(pick: dict, *, companion: bool = False) -> dict:
    opened = pick.get("single_open")
    return {
        "matchnum": pick.get("matchnum"), "home": pick.get("home"), "away": pick.get("away"),
        "market": pick.get("market"), "side": pick.get("side"), "line": _num(pick.get("line")),
        "selection": _selection(pick), "odds": _num(pick.get("odds")), "p_win": _num(pick.get("p_win")),
        "ev": _num(pick.get("ev")), "single_open": None if opened is None else bool(opened),
        "result": pick.get("result"), "clv": pick.get("clv"), "companion": companion,
        "strategy": pick.get("strategy"),
    }


def _price(legs: List[dict]) -> Optional[dict]:
    """组合赔率、胜率、期望、保本胜率。各场按独立估算，胜率和赔率都是连乘。"""
    odds = p = 1.0
    for leg in legs:
        leg_odds, leg_p = leg.get("odds"), leg.get("p_win")
        if leg_odds is None or leg_p is None or leg_odds <= 1:
            return None
        odds *= leg_odds
        p *= leg_p
    return {"odds": round(odds, 4), "p_win": round(p, 4), "ev": round(p * odds - 1.0, 4),
            "breakeven": round(1.0 / odds, 4)}


def grade_ticket(legs: List[dict]):
    """整张票的输赢。任一腿未结束则待结算；任一腿输则整张 -1；走盘的腿按赔率 1 继续。"""
    if not legs:
        return None, None
    results = [leg.get("result") for leg in legs]
    if any(result not in ("赢", "输", "走") for result in results):
        return None, None
    if any(result == "输" for result in results):
        return "输", -1.0
    if all(result == "走" for result in results):
        return "走", 0.0
    effect = 1.0
    for leg in legs:
        if leg["result"] != "赢":
            continue
        leg_odds = _num(leg.get("odds"))
        if leg_odds is None or leg_odds <= 1:
            return None, None
        effect *= leg_odds
    return "赢", round(effect - 1.0, 4)


def _label(legs: List[dict]) -> str:
    return " × ".join(f"{leg.get('matchnum') or ''} {leg.get('selection') or ''}".strip() for leg in legs)


def _assemble(kind: str, role: str, picks: List[dict], note: str, companions: List[bool]) -> Optional[dict]:
    legs = [_leg_view(pick, companion=flag) for pick, flag in zip(picks, companions)]
    priced = _price(legs)
    if priced is None:
        return None
    result, pnl = grade_ticket(legs)
    return {"type": kind, "role": role, "label": _label(legs), "legs": legs, "note": note,
            "result": result, "pnl": pnl, **priced}


def _unplaced(pick: dict, note: str) -> dict:
    leg = _leg_view(pick)
    return {"type": "无法出票", "role": "研究", "label": _label([leg]), "legs": [leg], "note": note,
            "odds": None, "p_win": leg.get("p_win"), "ev": None, "breakeven": None,
            "result": None, "pnl": None}


def _ev_of(pick: dict) -> float:
    value = _num(pick.get("ev"))
    return value if value is not None else -999.0


def _best_cover(pool: List[dict]) -> List[dict]:
    """2 场出一张 2 串 1；3 场出一张 3 串 1，并给出期望最高的 2 串 1 作备选。"""
    pool = sorted(pool, key=_ev_of, reverse=True)
    extra, pool = pool[3:], pool[:3]
    tickets = []
    if len(pool) == 2:
        made = _assemble("2串1", "研究", pool, "两场都未开单关，按 2 串 1 研究。胜率与赔率均按两腿相乘。", [False, False])
        if made:
            tickets.append(made)
    elif len(pool) == 3:
        made = _assemble("3串1", "研究", pool,
                         "三场都未开单关，按 3 串 1 研究。旁边的 2 串 1 是备选，不要和这张一起计入研究。",
                         [False, False, False])
        if made:
            tickets.append(made)
        pairs = [(pool[i], pool[j]) for i in range(3) for j in range(i + 1, 3)]

        def pair_key(pair):
            priced = _price([_leg_view(pick) for pick in pair])
            names = tuple(sorted(str(pick.get("matchnum") or "") for pick in pair))
            return (-(priced["ev"] if priced else -999), names)

        best = min(pairs, key=pair_key)
        alt = _assemble("2串1", "备选", list(best), "备选 2 串 1：命中率更高，有一场主信号不在这张票里。", [False, False])
        if alt:
            tickets.append(alt)
    for pick in extra:
        tickets.append(_unplaced(pick, "同一策略当天最多把 3 场编进一张研究票"))
    return tickets


def _filler(leg: dict, companions: List[dict], min_ev: float, blocked=()) -> Optional[dict]:
    """只有一场不能单买时，用其他场的全场预测凑票。组合期望达不到门槛就不出。

    已经单独研究的单关场次不再拿来搭配，避免同一场同时出现在两张研究票里。
    """
    used = {_match_key(leg), *blocked}
    cands = []
    for pick in companions:
        key = _match_key(pick)
        if key is None or key in used or pick.get("p_win") is None or not _num(pick.get("odds")):
            continue
        cands.append(pick)
    best = None
    for cand in cands:
        ticket = _assemble("2串1", "研究", [leg, cand],
                           "只有一场未开单关。搭配另一场后，组合期望仍达到门槛，按 2 串 1 研究。",
                           [False, True])
        if ticket and ticket["ev"] >= min_ev and (best is None or ticket["ev"] > best["ev"]):
            best = ticket
    if best:
        return best
    best3 = None
    for i, first in enumerate(cands):
        for second in cands[i + 1:]:
            if _match_key(first) == _match_key(second):
                continue
            ticket = _assemble("3串1", "研究", [leg, first, second],
                               "2 串 1 达不到期望门槛。三场组合期望仍达到门槛，按 3 串 1 研究。",
                               [False, True, True])
            if ticket and ticket["ev"] >= min_ev and (best3 is None or ticket["ev"] > best3["ev"]):
                best3 = ticket
    return best3


def recommend_tickets(sharp: List[dict], companions: List[dict] = None, *, min_ev: float = None) -> List[dict]:
    """把一天的主信号变成可研究的票。

    开了单关的，各自一张单关。没开单关的，同一场只留期望更高的玩法，再按场数合成
    2 串 1 或 3 串 1。组合胜率 = 各腿胜率相乘，组合赔率 = 各腿 SP 相乘，
    组合期望 = 组合胜率 × 组合赔率 − 1。凑不出达到门槛的票时标成「无法出票」，
    不按单场冒充研究结果。单关是否开售未知的历史注单不编入。
    """
    min_ev = MIN_EV if min_ev is None else min_ev
    companions = companions or []
    singles, parlay_pool, dropped = [], {}, []
    single_matches = set()
    for pick in sharp:
        if pick.get("single_open") is None:
            continue
        if bool(pick.get("single_open")):
            singles.append(pick)
            key = _match_key(pick)
            if key:
                single_matches.add(key)
            continue
        key = _match_key(pick)
        if key is None:
            dropped.append((pick, "缺少场次身份，不能编入串关"))
            continue
        current = parlay_pool.get(key)
        if current is None or _ev_of(pick) > _ev_of(current):
            if current is not None:
                dropped.append((current, "同一场的另一玩法不能互串，未编入研究票"))
            parlay_pool[key] = pick
        else:
            dropped.append((pick, "同一场的另一玩法不能互串，未编入研究票"))
    tickets = []
    for pick in singles:
        made = _assemble("单关", "研究", [pick], "本场此玩法开了单关，按单场研究。", [False])
        if made:
            tickets.append(made)
    pool = list(parlay_pool.values())
    if len(pool) >= 2:
        tickets.extend(_best_cover(pool))
    elif len(pool) == 1:
        made = _filler(pool[0], companions, min_ev, single_matches)
        tickets.append(made or _unplaced(pool[0], "未开单关，且找不到组合期望仍达到门槛的搭配"))
    tickets.extend(_unplaced(pick, note) for pick, note in dropped)
    return tickets


def ticket_leg_key(ticket: dict) -> str:
    parts = []
    for leg in ticket.get("legs") or []:
        parts.append("|".join((leg.get("matchnum") or "", leg.get("home") or "", leg.get("away") or "", leg.get("market") or "")))
    return ",".join(sorted(parts))


def _find_leg(picks: List[dict], ticket: dict, leg: dict) -> Optional[dict]:
    strategy = leg.get("strategy") or ticket.get("strategy")
    for pick in picks:
        if ticket.get("sale_date") and pick.get("sale_date") not in (None, ticket.get("sale_date")):
            continue
        if strategy and pick.get("strategy") not in (None, strategy):
            continue
        if pick.get("market") != leg.get("market"):
            continue
        if leg.get("home") and (pick.get("home"), pick.get("away")) != (leg.get("home"), leg.get("away")):
            continue
        if not leg.get("home") and leg.get("matchnum") and pick.get("matchnum") != leg.get("matchnum"):
            continue
        return pick
    return None


def resolve_tickets(tickets: List[dict], picks: List[dict]) -> List[dict]:
    """用各腿当前的赛果给整张票结算。赔率仍用出票时锁定的 SP。"""
    out = []
    for ticket in tickets:
        legs = []
        for leg in ticket.get("legs") or []:
            merged = dict(leg)
            pick = _find_leg(picks, ticket, leg)
            if pick is not None:
                merged["result"] = pick.get("result")
                if pick.get("clv") is not None:
                    merged["clv"] = pick.get("clv")
            legs.append(merged)
        item = {**ticket, "legs": legs}
        if ticket.get("type") in STUDY_TYPES:
            result, pnl = grade_ticket(legs)
            item["result"], item["pnl"] = result, pnl
        out.append(item)
    return out


def merge_day_tickets(sharp: List[dict], companions: List[dict], stored: List[dict]) -> List[dict]:
    """已落库的研究票优先，避免之后改规则改写样本；无法出票仍按当前规则说明。"""
    derived = recommend_tickets(sharp, companions)
    stored_study = [t for t in stored if t.get("role") == "研究" and t.get("type") in STUDY_TYPES]
    if not stored_study:
        return derived
    chosen = [t for t in stored if t.get("type") in STUDY_TYPES]
    covered = set()
    for ticket in chosen:
        if ticket.get("role") != "研究":
            continue
        for leg in ticket.get("legs") or []:
            covered.add(_match_key(leg))
    for ticket in derived:
        if ticket["type"] != "无法出票":
            continue
        key = _match_key(ticket["legs"][0]) if ticket.get("legs") else None
        if key not in covered:
            chosen.append(ticket)
    return chosen


def collect_tickets(picks: List[dict], stored: List[dict]) -> List[dict]:
    """按销售日、策略把主信号收成研究票，并按各腿赛果结算。"""
    by_day: Dict[str, Dict[str, List[dict]]] = {}
    for pick in picks:
        if pick.get("strategy") not in (*RESEARCH_STRATEGIES, FULL_CARD_STRATEGY):
            continue
        by_day.setdefault(pick["sale_date"], {STRATEGY: [], LATE_STRATEGY: [], FULL_CARD_STRATEGY: []})
        by_day[pick["sale_date"]][pick["strategy"]].append(pick)
    stored_by: Dict[tuple, List[dict]] = {}
    for ticket in stored:
        stored_by.setdefault((ticket.get("sale_date"), ticket.get("strategy")), []).append(ticket)
    out = []
    seen = set()
    for day, bucket in by_day.items():
        day_picks = [pick for rows in bucket.values() for pick in rows]
        for strategy in RESEARCH_STRATEGIES:
            if not bucket[strategy]:
                continue
            chosen = merge_day_tickets(bucket[strategy], bucket[FULL_CARD_STRATEGY], stored_by.get((day, strategy), []))
            stamped = [{**ticket, "sale_date": day, "strategy": strategy} for ticket in chosen]
            out.extend(resolve_tickets(stamped, day_picks))
            seen.add((day, strategy))
    for key, tickets in stored_by.items():
        if key in seen or key[0] is None:
            continue
        out.extend(resolve_tickets(tickets, picks))
    return out


def research_records(tickets: List[dict], picks: List[dict]) -> List[dict]:
    """研究样本：单关票、串关票，以及单关状态未知的历史腿。

    已经收成票的那一天，不再把主信号腿重复计入。没开单关又凑不出票的腿不计入。
    """
    study = [t for t in tickets if t.get("role") == "研究" and t.get("type") in STUDY_TYPES]
    covered = {(t.get("sale_date"), t.get("strategy")) for t in study}
    rows = []
    for ticket in study:
        market, clv = "串关", None
        if ticket.get("type") == "单关" and ticket.get("legs"):
            market = ticket["legs"][0].get("market") or "让分"
            clv = ticket["legs"][0].get("clv")
        rows.append({
            "sale_date": ticket.get("sale_date"), "strategy": ticket.get("strategy"), "market": market,
            "result": ticket.get("result"), "pnl": ticket.get("pnl"), "odds": ticket.get("odds"), "clv": clv,
            "id": ticket.get("id") or 0, "settled_at": ticket.get("settled_at") or "",
        })
    for pick in picks:
        if pick.get("strategy") not in RESEARCH_STRATEGIES:
            continue
        if (pick.get("sale_date"), pick.get("strategy")) in covered:
            continue
        if pick.get("single_open") is None or bool(pick.get("single_open")):
            rows.append(pick)
    return rows

