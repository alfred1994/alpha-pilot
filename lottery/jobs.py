"""定时任务入口：出单（picks）、盘口与比分采集（capture，含竞彩跟踪、停售前影子出单与结算）、历史账本导入。"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from lottery import injuries, signals, sources, store

logger = logging.getLogger("lottery.jobs")

# 两次竞彩抓取的最小间隔：16:00 出单与同一时刻的采集任务不重复抓竞彩
TRACK_MIN_GAP = timedelta(minutes=10)


def _sale_day(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _stamp(now: datetime) -> str:
    """快照与伤停的记录时间用任务时钟，保证时间线与停售/开赛时间可比。"""
    return now.isoformat(timespec="seconds")


def _merge_ref(games: List[dict], espn: List[dict]) -> List[dict]:
    by_teams = {(item["home"], item["away"]): item for item in espn}
    merged = []
    for game in games:
        ref = by_teams.get((game["home"], game["away"]), {})
        merged.append({**game, **{key: ref.get(key) for key in
                                  ("ref_provider", "ref_line", "ref_total", "ref_details", "state")}})
    return merged


def _still_open(game: dict, now: datetime) -> bool:
    end = injuries.parse_time(game.get("buy_end"))
    return end is not None and end > now


def _in_late_window(game: dict, now: datetime) -> bool:
    end = injuries.parse_time(game.get("buy_end"))
    return end is not None and timedelta(0) < end - now <= timedelta(minutes=signals.LATE_WINDOW_MINUTES)


def _not_started(game: dict, now: datetime) -> bool:
    if game.get("state") not in (None, "", "pre"):
        return False
    tip = injuries.parse_time(game.get("tip_at"))
    return tip is None or tip > now


def _fetch_injuries(fetch: Callable) -> Optional[List[dict]]:
    """伤停名单只用于标签与展示；抓取失败或返回空名单都按“不可用”处理，不影响出单。"""
    try:
        return fetch() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("伤停名单获取失败: %s", type(exc).__name__)
        return None


def _round(value, digits):
    return None if value is None else round(value, digits)


def _record(conn, day: str, chosen: List[tuple], injury_items: Optional[List[dict]], now: datetime) -> Dict[str, int]:
    """写入纸面注单（只记第一次），附出单时的伤停标签与双方名单；返回各策略新增数。"""
    added: Dict[str, int] = {}
    for strategy, item in chosen:
        game = item["game"]
        ctx = injuries.context(injury_items, game["home"], game["away"], now)
        new = store.add_pick(conn, {
            "sale_date": day, "matchnum": game["matchnum"], "home": game["home"], "away": game["away"],
            "home_cn": game["home_cn"], "away_cn": game["away_cn"], "strategy": strategy,
            "market": item["market"], "side": item["side"], "line": item["line"], "odds": item["odds"],
            "ref_line": item["ref_line"], "edge": _round(item["edge"], 2),
            "p_win": _round(item["p_win"], 4), "ev": _round(item["ev"], 4),
            "single_open": None if item["single_open"] is None else int(bool(item["single_open"])),
            "tip_at": game["tip_at"],
            "injury_tags": None if ctx is None else ",".join(ctx["tags"]),
            "injury_detail": None if ctx is None else json.dumps({"home": ctx["home"], "away": ctx["away"]},
                                                                 ensure_ascii=False),
        })
        added[strategy] = added.get(strategy, 0) + int(new)
    return added


def run_picks(*, day: str = None, now: datetime = None, db_path: str = None,
              fetch_jc: Callable = sources.fetch_jc, fetch_espn: Callable = sources.fetch_espn,
              fetch_injuries: Callable = sources.fetch_injuries) -> dict:
    """抓当日竞彩与国际线，记录快照与伤停名单；在销售截止前按价格信号记录纸面注单。"""
    now = now or sources.now_beijing()
    day = day or _sale_day(now)
    with store.session(db_path) as conn:
        run_id = store.start_run(conn, "picks")
        try:
            games = sources.merge_jc(fetch_jc(day, sources.PLAY_SPREAD), fetch_jc(day, sources.PLAY_TOTAL))
            if not games:
                store.finish_run(conn, run_id, "ok", f"{day} 竞彩无 NBA 场次")
                return {"status": "ok", "date": day, "games": 0, "new_picks": 0}
            try:
                espn = fetch_espn(day)
            except Exception as exc:  # noqa: BLE001 - 国际线缺失时只记录快照，不出单
                logger.warning("ESPN 获取失败: %s", type(exc).__name__)
                espn = []
            games = _merge_ref(games, espn)
            captured = _stamp(now)
            injury_items = _fetch_injuries(fetch_injuries)
            injury_changed = store.save_injuries(conn, injury_items, captured) if injury_items else 0
            for game in games:
                store.add_snapshot(conn, day, game, "picks", captured_at=captured)
                store.upsert_game(conn, day, game)

            open_games = [g for g in games if _still_open(g, now) and (g.get("state") in (None, "", "pre"))]
            already = store.count_picks(conn, day, signals.STRATEGY)
            chosen = [(signals.STRATEGY, item) for item in signals.select_picks(open_games, already=already)]
            chosen += [(signals.FULL_CARD_STRATEGY, {**item, "game": game})
                       for game in open_games for item in signals.full_card(game)]
            added = _record(conn, day, chosen, injury_items, now)
            new, new_full = added.get(signals.STRATEGY, 0), added.get(signals.FULL_CARD_STRATEGY, 0)
            summary = {"status": "ok", "date": day, "games": len(games), "with_ref": sum(g.get("ref_line") is not None for g in games),
                       "open_games": len(open_games), "new_picks": new, "picks_today": already + new,
                       "new_full_card": new_full, "full_card_today": store.count_picks(conn, day, signals.FULL_CARD_STRATEGY),
                       "injuries_available": injury_items is not None, "injury_teams_changed": injury_changed}
            store.finish_run(conn, run_id, "ok", json.dumps(summary, ensure_ascii=False))
            return summary
        except Exception as exc:
            store.finish_run(conn, run_id, "failed", type(exc).__name__)
            raise


def _track_due(conn, day: str, now: datetime) -> bool:
    last = injuries.parse_time(conn.execute(
        "SELECT MAX(captured_at) FROM snapshots WHERE sale_date=? AND source IN ('picks','track')", (day,)).fetchone()[0])
    return last is None or last > now or now - last >= TRACK_MIN_GAP


def run_capture(*, now: datetime = None, db_path: str = None, days: List[str] = None,
                fetch_espn: Callable = sources.fetch_espn, fetch_jc: Callable = sources.fetch_jc,
                fetch_injuries: Callable = sources.fetch_injuries) -> dict:
    """每 30 分钟一次：

    1. 竞彩仍在销售的场次重新抓竞彩让分/大小分（``track`` 快照），得到竞彩调线时间线；
    2. 抓开赛前国际线（收盘近似）与比分；有未开赛场次时同步伤停名单；
    3. 停售前最后一个采集窗口内，按主信号同一规则记录影子策略 ``sharp_late``；
    4. 结算已完赛注单。默认处理今天与昨天的销售日。
    """
    now = now or sources.now_beijing()
    days = days or [_sale_day(now - timedelta(days=1)), _sale_day(now)]
    with store.session(db_path) as conn:
        run_id = store.start_run(conn, "capture")
        try:
            known = {(row["sale_date"], row["home"], row["away"]): dict(row) for row in
                     conn.execute("SELECT sale_date, home, away, tip_at, buy_end, state FROM games WHERE sale_date IN (%s)" %
                                  ",".join("?" * len(days)), days)}
            captured = _stamp(now)
            injury_items = None
            injury_changed = 0
            if any(_not_started(game, now) for game in known.values()):
                injury_items = _fetch_injuries(fetch_injuries)
                injury_changed = store.save_injuries(conn, injury_items, captured) if injury_items else 0
            touched = closed = tracked = jc_errors = new_late = 0
            for day in days:
                day_games = {(key[1], key[2]): game for key, game in known.items() if key[0] == day}
                if not day_games:
                    continue
                espn = fetch_espn(day)
                track: Dict[tuple, dict] = {}
                if any(_still_open(game, now) for game in day_games.values()) and _track_due(conn, day, now):
                    try:
                        jc = sources.merge_jc(fetch_jc(day, sources.PLAY_SPREAD), fetch_jc(day, sources.PLAY_TOTAL))
                        track = {(g["home"], g["away"]): g for g in _merge_ref(jc, espn) if (g["home"], g["away"]) in day_games}
                    except Exception as exc:  # noqa: BLE001 - 竞彩页面异常不影响收盘线与结算
                        jc_errors += 1
                        logger.warning("竞彩跟踪获取失败: %s", type(exc).__name__)
                for key, game in track.items():
                    store.add_snapshot(conn, day, game, "track", captured_at=captured)
                    store.upsert_game(conn, day, {k: game.get(k) for k in ("home", "away", "matchnum", "tip_at", "buy_end")})
                    tracked += 1
                for item in espn:
                    key = (item["home"], item["away"])
                    if key not in day_games:
                        continue
                    update = {"home": item["home"], "away": item["away"], "state": item["state"]}
                    if item["state"] == "pre" and (item["ref_line"] is not None or item["ref_total"] is not None):
                        if key not in track:  # 跟踪快照已含同一时刻的国际线
                            store.add_snapshot(conn, day, item, "capture", captured_at=captured)
                        update.update(close_ref_line=item["ref_line"], close_ref_total=item["ref_total"],
                                      close_captured_at=captured)
                        closed += 1
                    if item["completed"]:
                        update.update(home_score=item["home_score"], away_score=item["away_score"])
                    store.upsert_game(conn, day, update)
                    touched += 1
                late = [g for g in track.values() if _in_late_window(g, now) and g.get("state") in (None, "", "pre")]
                if late:
                    already = store.count_picks(conn, day, signals.LATE_STRATEGY)
                    chosen = [(signals.LATE_STRATEGY, item) for item in signals.select_picks(late, already=already)]
                    new_late += _record(conn, day, chosen, injury_items, now).get(signals.LATE_STRATEGY, 0)
            settled = settle(conn)
            summary = {"status": "ok", "days": days, "games_updated": touched, "close_lines": closed, "settled": settled,
                       "tracked": tracked, "jc_errors": jc_errors, "new_late_picks": new_late,
                       "injuries_available": injury_items is not None, "injury_teams_changed": injury_changed}
            store.finish_run(conn, run_id, "ok", json.dumps(summary, ensure_ascii=False))
            return summary
        except Exception as exc:
            store.finish_run(conn, run_id, "failed", type(exc).__name__)
            raise


def settle(conn) -> int:
    """为已完赛比赛的未结算注单补结果、盈亏与收盘价差。"""
    rows = conn.execute(
        """SELECT p.id, p.market, p.side, p.line, p.odds, g.home_score, g.away_score,
                  g.close_ref_line, g.close_ref_total
           FROM picks p JOIN games g ON g.sale_date=p.sale_date AND g.home=p.home AND g.away=p.away
           WHERE p.result IS NULL AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL""").fetchall()
    for row in rows:
        result, pnl = signals.grade(row["market"], row["side"], row["line"], row["home_score"], row["away_score"], row["odds"])
        close_ref = row["close_ref_line"] if row["market"] == "让分" else row["close_ref_total"]
        clv = signals.clv_points(row["market"], row["side"], row["line"], close_ref)
        conn.execute("UPDATE picks SET result=?, pnl=?, home_score=?, away_score=?, close_ref=?, clv=?, settled_at=? WHERE id=?",
                     (result, pnl, row["home_score"], row["away_score"], close_ref, clv, store._now(), row["id"]))
    return len(rows)


def import_ledger(path: str, *, db_path: str = None) -> dict:
    """导入 nba-prediction-lab 的 ledger.csv（已结算的历史纸面注单），重复导入不产生重复。"""
    strategy_map = {"sharp": "sharp", "model": "model_legacy"}
    added = skipped = 0
    pending_days = set()
    with open(path, encoding="utf-8-sig") as handle, store.session(db_path) as conn:
        for row in csv.DictReader(handle):
            strategy = strategy_map.get(row.get("strategy", ""))
            if not strategy or row.get("market") not in ("让分", "大小分"):
                skipped += 1
                continue

            def num(name):
                try:
                    return float(row[name]) if row.get(name) not in (None, "") else None
                except ValueError:
                    return None
            # 原账本的 jc_line 对让分是主队让分、对大小分是总分，与本库 line 口径一致
            pick = {
                "created_at": f"{row['date']}T21:00:00+08:00", "sale_date": row["date"], "matchnum": row.get("matchnum"),
                "home": row["home"], "away": row["away"], "home_cn": row.get("home_cn"), "away_cn": row.get("away_cn"),
                "strategy": strategy, "market": row["market"], "side": row["pick_side"], "line": num("jc_line"),
                "odds": num("pick_odds"), "ref_line": num("ref_line"), "edge": num("edge"), "stake": num("stake") or 1.0,
                "tip_at": f"{row['date']}T23:59:00+08:00", "result": row.get("result") or None, "pnl": num("pnl"),
                "home_score": int(num("home_score")) if num("home_score") is not None else None,
                "away_score": int(num("away_score")) if num("away_score") is not None else None,
                "settled_at": f"{row['date']}T23:59:00+08:00" if row.get("result") else None,
                "source": "nba-prediction-lab",
            }
            if pick["line"] is None or pick["odds"] is None:
                skipped += 1
                continue
            # 已结算注单带比分：同步写入比赛，场次页才会按"已完赛"展示，而不是"未开赛"
            settled_score = pick["result"] and pick["home_score"] is not None and pick["away_score"] is not None
            store.upsert_game(conn, row["date"], {"home": row["home"], "away": row["away"],
                                                  "matchnum": row.get("matchnum"), "home_cn": row.get("home_cn"),
                                                  "away_cn": row.get("away_cn"),
                                                  "state": "post" if settled_score else None,
                                                  "home_score": pick["home_score"] if settled_score else None,
                                                  "away_score": pick["away_score"] if settled_score else None})
            if not pick["result"]:
                pending_days.add(row["date"])
            if store.add_pick(conn, pick):
                added += 1
            else:
                skipped += 1
    return {"added": added, "skipped": skipped, "pending_days": sorted(pending_days)}
