"""体彩 NBA 纸面验证：解析、点值换算、出单只记一次、全场预测、结算与收盘价差、公开接口。"""
import csv
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lottery import injuries, jobs, signals, sources, stats, store

BJ = ZoneInfo("Asia/Shanghai")
ROW = ('<tr class="bet-tb-tr  bet-tb-end" data-homesxname="爵士" data-awaysxname="开拓者" data-matchdate="2026-11-01" '
       'data-matchtime="03:00" data-simpleleague="美职篮" data-buyendtime="2026-10-31 23:00:00" '
       'data-subactive="dxfdg:0,dxfgg:1,rfsfdg:1,rfsfgg:1" data-matchnum="周六308" data-yszf="234.5">'
       '<td><p class="betbtn " data-type="rfsf" data-value="2" data-sp="1.70"><span>1.70</span></p>'
       '<p class="betmsg"><span class="red" title="客队让8.5分">+8.5</span></p>'
       '<p class="betbtn betbtn2 " data-type="rfsf" data-value="1" data-sp="1.75"><span>1.75</span></p>'
       '<p data-type="dxf" data-value="1" data-sp="1.65"></p><p data-type="dxf" data-value="2" data-sp="1.75"></p></td></tr>'
       '<tr class="bet-tb-tr" data-homesxname="山猫" data-awaysxname="自由人" data-simpleleague="美职篮"></tr>')


def espn(state="pre", line="UTAH -1.5", total=234.5, scores=None):
    comp = {"date": "2026-10-31T19:00Z", "status": {"type": {"state": state, "completed": scores is not None}},
            "competitors": [{"homeAway": "home", "team": {"abbreviation": "UTAH"}, "score": str(scores[0]) if scores else "0"},
                            {"homeAway": "away", "team": {"abbreviation": "POR"}, "score": str(scores[1]) if scores else "0"}],
            "odds": [{"details": line, "overUnder": total, "provider": {"name": "Draft Kings"}}]}
    return {"events": [{"competitions": [comp]}]}


def injury_payload(*rows):
    """rows: (ESPN 缩写, 球员, 状态, 部位, 更新时间 UTC)"""
    return {"injuries": [{"displayName": "ignored", "injuries": [
        {"status": status, "date": updated, "athlete": {"displayName": player, "team": {"abbreviation": abbr}},
         "details": {"type": kind, "returnDate": None}}
        for abbr, player, status, kind, updated in rows]}]}


def no_injuries():
    return []


class ParsingTests(unittest.TestCase):
    def test_jc_page_parses_lines_odds_and_single_bet_flags(self):
        rows = sources.parse_jc_page(ROW)
        self.assertEqual(len(rows), 1, "非 NBA 的美职赛事被过滤")
        game = sources.merge_jc(rows, rows)[0]
        self.assertEqual(game["jc_line"], 8.5)
        self.assertEqual((game["jc_sp_home"], game["jc_sp_away"]), (1.75, 1.70))
        self.assertEqual((game["jc_total"], game["jc_sp_over"], game["jc_sp_under"]), (234.5, 1.65, 1.75))
        self.assertEqual((game["single_spread"], game["single_total"]), (True, False))
        self.assertEqual(game["buy_end"], "2026-10-31T23:00:00+08:00")
        self.assertEqual(game["tip_at"], "2026-11-01T03:00:00+08:00")

    def test_home_line_signs(self):
        self.assertEqual(sources.parse_home_line("主队让3.5分"), -3.5)
        self.assertEqual(sources.parse_home_line("主队受让2.5分"), 2.5)
        self.assertEqual(sources.parse_home_line("客队让1.5分"), 1.5)
        self.assertEqual(sources.parse_home_line("客队受让4.5分"), -4.5)
        self.assertIsNone(sources.parse_home_line("未开售"))

    def test_espn_reference_line_and_scores(self):
        item = sources.parse_espn(espn(line="POR -2.5", scores=(110, 100)))[0]
        self.assertEqual((item["home"], item["away"], item["ref_line"], item["ref_total"]), ("Utah", "Portland", 2.5, 234.5))
        self.assertEqual((item["home_score"], item["away_score"], item["completed"]), (110, 100, True))
        self.assertEqual(sources.parse_espn(espn(line="PK"))[0]["ref_line"], 0.0)

    def test_injuries_parse_and_map_teams(self):
        items = sources.parse_injuries(injury_payload(
            ("UTAH", "Lauri Markkanen", "Out", "Back", "2026-10-31T02:00Z"),
            ("POR", "Shaedon Sharpe", "Day-To-Day", "Ankle", "2026-10-30T20:00Z"),
            ("XXX", "Nobody", "Out", None, None)))
        self.assertEqual([(i["team"], i["player"], i["status"], i["type"]) for i in items],
                         [("Utah", "Lauri Markkanen", "Out", "Back"), ("Portland", "Shaedon Sharpe", "Day-To-Day", "Ankle")],
                         "无法映射的球队被丢弃")


    def test_fetch_retries_timeouts_then_gives_up(self):
        import requests

        class Response:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return espn(line="POR -2.5")

        class Session:
            def __init__(self, failures):
                self.failures, self.calls = failures, 0
            def get(self, *args, **kwargs):
                self.calls += 1
                if self.calls <= self.failures:
                    raise requests.Timeout("slow")
                return Response()

        with patch("lottery.sources.time.sleep"):
            flaky = Session(failures=1)
            self.assertEqual(len(sources.fetch_espn("2026-10-31", session=flaky)), 1, "一次超时后重试成功")
            self.assertEqual(flaky.calls, 2)
            down = Session(failures=5)
            with self.assertRaises(requests.Timeout):
                sources.fetch_espn("2026-10-31", session=down)
            self.assertEqual(down.calls, sources.RETRY_ATTEMPTS, "有限重试，不无限等待")


class SignalTests(unittest.TestCase):
    def test_point_value_is_monotonic_and_symmetric(self):
        values = [signals.win_probability(d / 2, "让分") for d in range(0, 21)]
        self.assertTrue(all(b > a for a, b in zip(values, values[1:])))
        self.assertAlmostEqual(signals.win_probability(0, "让分"), 0.5, places=3)
        self.assertAlmostEqual(signals.win_probability(-3, "让分"), 1 - signals.win_probability(3, "让分"))
        self.assertLess(signals.win_probability(3, "大小分"), signals.win_probability(3, "让分"), "总分波动更大，同样偏离价值更低")

    def test_thresholds_follow_odds(self):
        spread = signals.required_edge(1.70, "让分")
        self.assertTrue(2.5 < spread < 3.2, spread)
        self.assertGreater(signals.required_edge(1.70, "大小分"), 3.9, "大小分 2.5 分偏离不足以覆盖抽水")
        self.assertLess(signals.required_edge(1.81, "让分"), spread, "赔率越高所需偏离越小")

    def test_candidate_sides(self):
        game = {"jc_line": 1.5, "ref_line": -1.5, "jc_sp_home": 1.70, "jc_sp_away": 1.70,
                "jc_total": 226.5, "ref_total": 231.5, "jc_sp_over": 1.70, "jc_sp_under": 1.70}
        by_market = {c["market"]: c for c in signals.candidates(game)}
        self.assertEqual((by_market["让分"]["side"], by_market["让分"]["edge"]), ("主", 3.0))
        self.assertEqual((by_market["大小分"]["side"], by_market["大小分"]["edge"]), ("大", 5.0))
        game.update(jc_total=233.5, ref_total=228.5)
        self.assertEqual({c["market"]: c for c in signals.candidates(game)}["大小分"]["side"], "小")

    def test_full_card_picks_every_open_market(self):
        game = {"jc_line": 1.5, "ref_line": -1.5, "jc_sp_home": 1.70, "jc_sp_away": 1.75,
                "jc_total": 230.5, "ref_total": 230.5, "jc_sp_over": 1.70, "jc_sp_under": 1.80}
        by_market = {c["market"]: c for c in signals.full_card(game)}
        self.assertEqual(set(by_market), {"让分", "大小分"}, "每个开售玩法都出一个选择")
        self.assertEqual((by_market["让分"]["side"], by_market["让分"]["edge"]), ("主", 3.0), "有偏离时选便宜的一侧")
        self.assertEqual(by_market["大小分"]["side"], "小", "无偏离时选赔率更高的一侧")
        self.assertLess(by_market["大小分"]["ev"], 0, "不设门槛：负期望值也记录")
        no_ref = signals.full_card({"jc_line": -3.5, "jc_sp_home": 1.65, "jc_sp_away": 1.80})
        self.assertEqual([(c["market"], c["side"], c["p_win"], c["ev"]) for c in no_ref], [("让分", "客", None, None)],
                         "缺国际线按赔率选边，胜率留空；未开售的大小分跳过")

    def test_two_parlay_only_legs_become_one_recalculated_ticket(self):
        picks = [
            {"matchnum": "周一303", "market": "让分", "selection": "黄蜂 +10.5", "odds": 1.81, "p_win": 0.5725, "ev": 0.0362, "single_open": False},
            {"matchnum": "周一306", "market": "让分", "selection": "步行者 +6.5", "odds": 1.70, "p_win": 0.6075, "ev": 0.0327, "single_open": False},
        ]
        study = [t for t in signals.recommend_tickets(picks) if t["role"] == "研究"]
        self.assertEqual([t["type"] for t in study], ["2串1"])
        self.assertEqual(study[0]["odds"], 3.077)
        self.assertEqual(study[0]["p_win"], 0.3478)
        self.assertEqual(study[0]["ev"], 0.0702)
        self.assertEqual(study[0]["breakeven"], 0.325)
        self.assertEqual(signals.grade_ticket([
            {"result": "赢", "odds": 1.81}, {"result": "赢", "odds": 1.70},
        ]), ("赢", 2.077))
        self.assertEqual(signals.grade_ticket([
            {"result": "赢", "odds": 1.81}, {"result": "输", "odds": 1.70},
        ]), ("输", -1.0))

    def test_single_stays_single_and_same_game_cannot_parlay(self):
        picks = [
            {"matchnum": "301", "odds": 1.70, "p_win": 0.60, "ev": 0.02, "selection": "A -1.5", "single_open": False, "market": "让分"},
            {"matchnum": "302", "odds": 1.75, "p_win": 0.58, "ev": 0.015, "selection": "C +3.5", "single_open": True, "market": "让分"},
            {"matchnum": "301", "odds": 1.65, "p_win": 0.55, "ev": -0.09, "selection": "大 220.5", "single_open": False, "market": "大小分"},
        ]
        tickets = signals.recommend_tickets(picks)
        self.assertEqual([t["type"] for t in tickets if t["type"] == "单关"], ["单关"])
        self.assertEqual(tickets[0]["label"], "302 C +3.5")
        unplaced = [t["label"] for t in tickets if t["type"] == "无法出票"]
        self.assertEqual(len(unplaced), 2, "没开单关的一场凑不出票；同场另一玩法也不能互串")
        self.assertTrue(any("A -1.5" in label for label in unplaced))
        self.assertTrue(any("大 220.5" in label for label in unplaced))
        self.assertFalse(any("C +3.5" in label for label in unplaced))

    def test_three_parlay_legs_study_as_one_three_leg_ticket(self):
        picks = [
            {"matchnum": str(300 + i), "odds": 1.80, "p_win": 0.60, "ev": 0.08, "selection": f"L{i}", "single_open": False}
            for i in range(3)
        ]
        tickets = signals.recommend_tickets(picks)
        study = [t for t in tickets if t["role"] == "研究"]
        alt = [t for t in tickets if t["role"] == "备选"]
        self.assertEqual([t["type"] for t in study], ["3串1"])
        self.assertEqual(study[0]["odds"], 5.832)
        self.assertEqual(study[0]["p_win"], 0.216)
        self.assertEqual(study[0]["ev"], 0.2597)
        self.assertEqual([t["type"] for t in alt], ["2串1"])
        self.assertEqual(len(alt[0]["legs"]), 2)

    def test_lone_parlay_leg_needs_a_companion_above_the_threshold(self):
        sharp = [{"matchnum": "301", "odds": 1.70, "p_win": 0.60, "ev": 0.02, "selection": "A +1.5", "single_open": False}]
        weak = [{"matchnum": "302", "odds": 1.70, "p_win": 0.50, "ev": -0.15, "selection": "弱", "single_open": False, "strategy": "all_games"}]
        strong = [{"matchnum": "302", "odds": 1.80, "p_win": 0.58, "ev": 0.044, "selection": "强", "single_open": False, "strategy": "all_games"}]
        self.assertEqual(signals.recommend_tickets(sharp, weak)[0]["type"], "无法出票")
        made = signals.recommend_tickets(sharp, strong)[0]
        self.assertEqual(made["type"], "2串1")
        self.assertEqual(made["odds"], 3.06)
        self.assertGreaterEqual(made["ev"], signals.MIN_EV)
        self.assertTrue(made["legs"][1]["companion"])

    def test_selection_keeps_games_that_are_parlay_only(self):
        base = {"jc_sp_home": 1.70, "jc_sp_away": 1.70}
        g_nosingle = {**base, "jc_line": 4.5, "ref_line": 0.5, "single_spread": False}
        g_single = {**base, "jc_line": 3.5, "ref_line": 0.5, "single_spread": True}
        with patch.dict(os.environ, {"LOTTERY_SINGLE_ONLY": "true"}):
            chosen = signals.select_picks([g_nosingle, g_single], max_picks=3)
        self.assertEqual([c["edge"] for c in chosen], [4.0, 3.0], "没有单关开关，没开单关的场次仍入选")
        self.assertIs(chosen[0]["single_open"], False)

    def test_selection_caps_and_ignores_suspicious_gaps(self):
        base = {"jc_sp_home": 1.70, "jc_sp_away": 1.70}
        games = [{**base, "jc_line": 4.5, "ref_line": 0.5}, {**base, "jc_line": 3.5, "ref_line": 0.5},
                 {**base, "jc_line": 12.5, "ref_line": 0.5}, {**base, "jc_line": 1.5, "ref_line": 0.5}]
        chosen = signals.select_picks(games, max_picks=3)
        self.assertEqual([c["edge"] for c in chosen], [4.0, 3.0], "偏离 12 分视为错配，偏离 1 分不达标")
        self.assertEqual(len(signals.select_picks(games, max_picks=3, already=2)), 1)

    def test_grade_and_clv(self):
        self.assertEqual(signals.grade("让分", "主", 1.5, 100, 101, 1.7), ("赢", 0.7))
        self.assertEqual(signals.grade("让分", "客", 1.5, 100, 101, 1.7), ("输", -1.0))
        self.assertEqual(signals.grade("大小分", "大", 200.5, 100, 101, 1.8), ("赢", 0.8))
        self.assertEqual(signals.clv_points("让分", "主", 1.5, -1.0), 2.5)
        self.assertEqual(signals.clv_points("让分", "客", 1.5, -1.0), -2.5)
        self.assertEqual(signals.clv_points("大小分", "小", 230.5, 228.0), 2.5)
        self.assertIsNone(signals.clv_points("让分", "主", 1.5, None))


class JobTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.folder.name, "lottery.db")

    def tearDown(self):
        self.folder.cleanup()

    def fake_jc(self, jc_home_line_title):
        html = ROW.replace("客队让8.5分", jc_home_line_title)
        return lambda day, playid: sources.parse_jc_page(html)

    def run_picks(self, title, ref="UTAH -1.5", hour=21, fetch_injuries=no_injuries):
        now = datetime(2026, 10, 31, hour, 40, tzinfo=BJ)
        return jobs.run_picks(day="2026-10-31", now=now, db_path=self.db, fetch_jc=self.fake_jc(title),
                              fetch_espn=lambda day: sources.parse_espn(espn(line=ref)), fetch_injuries=fetch_injuries)

    def test_pick_recorded_once_and_never_rewritten(self):
        first = self.run_picks("客队受让1.5分")  # 竞彩主队 -1.5，与国际线相同
        self.assertEqual(first["new_picks"], 0, "竞彩与国际线一致时主信号不出单")
        self.assertEqual(first["new_full_card"], 2, "全场预测不设门槛：让分、大小分各一条")
        summary = self.run_picks("客队让1.5分")  # 竞彩主队 +1.5，国际主队 -1.5 → 偏离 3 分
        self.assertEqual((summary["new_picks"], summary["new_full_card"]), (1, 0), "全场预测同样只记第一次")
        again = self.run_picks("客队让4.5分")
        self.assertEqual(again["new_picks"], 0, "同场同玩法只记第一次决定")
        with store.session(self.db) as conn:
            rows = conn.execute("SELECT side, line, edge FROM picks WHERE strategy='sharp'").fetchall()
            full = conn.execute("SELECT market, line FROM picks WHERE strategy='all_games' ORDER BY market").fetchall()
            snaps = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        self.assertEqual([tuple(r) for r in rows], [("主", 1.5, 3.0)])
        self.assertEqual([tuple(r) for r in full], [("大小分", 234.5), ("让分", -1.5)], "全场预测保留首次出单时的线")
        self.assertEqual(snaps, 3, "每次抓取都追加快照")

    def test_no_pick_after_sales_close(self):
        result = self.run_picks("客队让1.5分", hour=23)
        self.assertEqual((result["new_picks"], result["new_full_card"]), (0, 0))

    def test_capture_records_close_line_and_settles_with_clv(self):
        self.run_picks("客队让1.5分")
        jobs.run_capture(now=datetime(2026, 11, 1, 2, 30, tzinfo=BJ), db_path=self.db, fetch_injuries=no_injuries,
                         fetch_espn=lambda day: sources.parse_espn(espn(line="UTAH -2.5")) if day == "2026-10-31" else [])
        jobs.run_capture(now=datetime(2026, 11, 1, 6, 0, tzinfo=BJ), db_path=self.db, fetch_injuries=no_injuries,
                         fetch_espn=lambda day: sources.parse_espn(espn(state="post", scores=(100, 105))) if day == "2026-10-31" else [])
        with store.session(self.db) as conn:
            pick = dict(conn.execute("SELECT result, pnl, close_ref, clv FROM picks WHERE strategy='sharp'").fetchone())
        self.assertEqual(pick, {"result": "输", "pnl": -1.0, "close_ref": -2.5, "clv": 4.0})

    def test_parlay_only_legs_settle_as_one_ticket(self):
        with store.session(self.db) as conn:
            store.upsert_game(conn, "2026-11-09", {"home": "A", "away": "B", "matchnum": "周一303", "home_cn": "黄蜂",
                                                   "away_cn": "骑士", "home_score": 110, "away_score": 100, "state": "post"})
            store.upsert_game(conn, "2026-11-09", {"home": "C", "away": "D", "matchnum": "周一306", "home_cn": "独行侠",
                                                   "away_cn": "步行者", "home_score": 90, "away_score": 110, "state": "post"})
            for home, away, num, side in (("A", "B", "周一303", "主"), ("C", "D", "周一306", "客")):
                store.add_pick(conn, {
                    "sale_date": "2026-11-09", "matchnum": num, "home": home, "away": away,
                    "home_cn": "黄蜂" if home == "A" else "独行侠", "away_cn": "骑士" if away == "B" else "步行者",
                    "strategy": "sharp", "market": "让分", "side": side, "line": 10.5 if side == "主" else 6.5,
                    "odds": 1.81 if side == "主" else 1.70, "p_win": 0.5725 if side == "主" else 0.6075,
                    "ev": 0.0362 if side == "主" else 0.0327, "single_open": 0,
                })
            jobs.settle(conn)
            ticket = dict(conn.execute("SELECT kind, role, odds, p_win, ev, result, pnl FROM tickets").fetchone())
        self.assertEqual(ticket["kind"], "2串1")
        self.assertEqual(ticket["role"], "研究")
        self.assertEqual(ticket["odds"], 3.077)
        self.assertEqual(ticket["result"], "赢")
        self.assertEqual(ticket["pnl"], 2.077)

    def capture(self, hour, minute, title, ref="UTAH -1.5", fetch_injuries=no_injuries, day=31):
        month = 10 if day == 31 else 11
        return jobs.run_capture(now=datetime(2026, month, day, hour, minute, tzinfo=BJ), db_path=self.db,
                                fetch_jc=self.fake_jc(title), fetch_injuries=fetch_injuries,
                                fetch_espn=lambda d: sources.parse_espn(espn(line=ref)) if d == "2026-10-31" else [])

    def test_capture_tracks_jc_until_close_and_records_late_shadow_pick(self):
        self.run_picks("客队受让1.5分", hour=16)  # 16:40 出单：竞彩与国际线一致，主信号不出单
        early = self.capture(18, 0, "客队受让1.5分")
        self.assertEqual((early["tracked"], early["new_late_picks"]), (1, 0), "停售前 5 小时只跟踪，不出影子单")
        skipped = self.capture(18, 5, "客队受让1.5分")
        self.assertEqual(skipped["tracked"], 0, "距上次竞彩抓取不足 10 分钟不重复抓")
        late = self.capture(22, 30, "客队让1.5分", ref="UTAH -1.5")  # 竞彩主队 +1.5 vs 国际 -1.5 → 偏离 3 分
        self.assertEqual((late["tracked"], late["new_late_picks"]), (1, 1), "停售前 45 分钟内按主信号规则出影子单")
        again = self.capture(22, 45, "客队让4.5分")
        self.assertEqual(again["new_late_picks"], 0, "影子单同样只记第一次")
        closed = self.capture(23, 30, "客队让4.5分")
        self.assertEqual(closed["tracked"], 0, "停售后不再抓竞彩")
        with store.session(self.db) as conn:
            sources_seen = [r[0] for r in conn.execute("SELECT source FROM snapshots ORDER BY id")]
            late_pick = dict(conn.execute("SELECT side, line, edge, injury_tags FROM picks WHERE strategy='sharp_late'").fetchone())
            sharp = conn.execute("SELECT COUNT(*) FROM picks WHERE strategy='sharp'").fetchone()[0]
        self.assertEqual(sources_seen, ["picks", "track", "capture", "track", "track", "capture"],
                         "跟踪快照已含国际线；没抓竞彩的时刻只记国际线")
        self.assertEqual(late_pick, {"side": "主", "line": 1.5, "edge": 3.0, "injury_tags": None},
                         "伤停不可用时标签留空，而不是记成无伤停")
        self.assertEqual(sharp, 0, "影子单不混入主信号")

    def test_capture_skips_closed_games_while_another_game_is_still_on_sale(self):
        self.run_picks("客队受让1.5分", hour=16)
        with store.session(self.db) as conn:  # 同一销售日另有一场次日上午才停售
            store.upsert_game(conn, "2026-10-31", {"home": "Boston", "away": "Miami", "matchnum": "周六320",
                                                   "tip_at": "2026-11-01T11:30:00+08:00",
                                                   "buy_end": "2026-11-01T11:30:00+08:00"})
        summary = self.capture(23, 30, "客队让4.5分")
        self.assertEqual(summary["tracked"], 0, "爵士这场 23:00 已停售：页面上的盘口买不到，不记竞彩快照")
        with store.session(self.db) as conn:
            sources_seen = [r[0] for r in conn.execute("SELECT source FROM snapshots WHERE home='Utah' ORDER BY id")]
        self.assertEqual(sources_seen, ["picks", "capture"], "停售后只记国际线")

    def test_jc_failure_during_tracking_does_not_block_close_lines(self):
        self.run_picks("客队受让1.5分", hour=16)

        def broken(day, playid):
            raise RuntimeError("500.com down")
        summary = jobs.run_capture(now=datetime(2026, 10, 31, 20, 0, tzinfo=BJ), db_path=self.db, fetch_jc=broken,
                                   fetch_injuries=no_injuries,
                                   fetch_espn=lambda d: sources.parse_espn(espn(line="UTAH -2.5")) if d == "2026-10-31" else [])
        self.assertEqual((summary["jc_errors"], summary["tracked"], summary["close_lines"]), (1, 0, 1))

    def test_injury_names_saved_on_change_and_tagged_on_picks(self):
        recent = injury_payload(("UTAH", "Lauri Markkanen", "Out", "Back", "2026-10-31T02:00Z"),
                                ("POR", "Shaedon Sharpe", "Day-To-Day", "Ankle", "2026-10-29T02:00Z"))
        self.run_picks("客队让1.5分", hour=16, fetch_injuries=lambda: sources.parse_injuries(recent))
        with store.session(self.db) as conn:
            tags = dict(conn.execute("SELECT strategy, injury_tags FROM picks WHERE strategy='sharp'").fetchone())
            detail = conn.execute("SELECT injury_detail FROM picks WHERE strategy='sharp'").fetchone()[0]
            first = conn.execute("SELECT COUNT(*) FROM injury_reports").fetchone()[0]
            self.assertEqual(store.save_injuries(conn, sources.parse_injuries(recent)), 0, "名单不变不追加")
            changed = injury_payload(("UTAH", "Lauri Markkanen", "Out", "Back", "2026-10-31T09:00Z"))
            self.assertEqual(store.save_injuries(conn, sources.parse_injuries(changed)), 1, "只有开拓者名单变化")
            self.assertEqual(store.save_injuries(conn, []), 0, "空名单视为接口异常，不写入")
        self.assertEqual(first, len(store.ALL_TEAMS), "首次记录全部球队（无伤停的球队为空名单）")
        self.assertEqual(tags["injury_tags"], f"{injuries.TAG_RECENT},{injuries.TAG_UNCERTAIN}")
        self.assertIn("Lauri Markkanen", detail)

    def test_import_legacy_ledger_is_idempotent(self):
        path = os.path.join(self.folder.name, "ledger.csv")
        fields = ["date", "strategy", "market", "matchnum", "home", "away", "home_cn", "away_cn", "pick_side",
                  "pick_odds", "edge", "ref_line", "jc_line", "stake", "result", "pnl", "home_score", "away_score"]
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(dict(zip(fields, ["2026-10-26", "sharp", "让分", "周一303", "Charlotte", "Washington", "黄蜂", "奇才",
                                              "主", "1.7", "3.0", "-1.5", "1.5", "1.0", "赢", "0.7", "110", "103"])))
            writer.writerow(dict(zip(fields, ["2026-10-29", "model", "让分", "周四306", "Atlanta", "Cleveland", "老鹰", "骑士",
                                              "主", "1.7", "7.5", "1.5", "1.5", "1.0", "", "", "", ""])))
        first = jobs.import_ledger(path, db_path=self.db)
        self.assertEqual((first["added"], first["pending_days"]), (2, ["2026-10-29"]))
        self.assertEqual(jobs.import_ledger(path, db_path=self.db)["added"], 0)
        with store.session(self.db) as conn:
            strategies = sorted(r[0] for r in conn.execute("SELECT strategy FROM picks"))
            games = {r["sale_date"]: dict(r) for r in conn.execute("SELECT sale_date, state, home_score, away_score FROM games")}
        self.assertEqual(strategies, ["model_legacy", "sharp"])
        self.assertEqual((games["2026-10-26"]["state"], games["2026-10-26"]["home_score"], games["2026-10-26"]["away_score"]),
                         ("post", 110, 103), "已结算注单的比分同步到比赛，场次页显示已完赛")
        self.assertIsNone(games["2026-10-29"]["home_score"], "未结算注单不写比分")


class StatsTests(unittest.TestCase):
    def rows(self, results):
        return [{"id": i, "sale_date": f"2026-11-{i % 28 + 1:02d}", "strategy": "sharp", "result": r,
                 "pnl": 0.7 if r == "赢" else -1.0 if r == "输" else 0.0, "odds": 1.7, "clv": 1.0, "settled_at": f"{i:04d}"}
                for i, r in enumerate(results)]

    def test_summary_and_wilson_interval(self):
        s = stats.summarize(self.rows(["赢", "输", "走", "赢"]))
        self.assertEqual((s["wins"], s["losses"], s["pushes"]), (2, 1, 1))
        self.assertAlmostEqual(s["hit_rate"], 2 / 3)
        low, high = s["hit_rate_ci95"]
        self.assertTrue(low < 2 / 3 < high)
        self.assertAlmostEqual(s["breakeven_hit_rate"], 1 / 1.7)

    def test_stop_rules(self):
        self.assertEqual(stats.stop_status(self.rows(["赢"] * 10))["checks"][0]["status"], "pending")
        failing = stats.stop_status(self.rows(["赢"] * 25 + ["输"] * 25))
        self.assertEqual(failing["checks"][0]["status"], "stop")
        self.assertTrue(failing["drawdown_triggered"], "连续 25 注输 = 回撤 25 单位")
        self.assertTrue(failing["triggered"])

    def test_injury_tag_groups_keep_unrecorded_separate(self):
        rows = self.rows(["赢", "输", "赢"])
        rows[0]["injury_tags"] = injuries.TAG_RECENT
        rows[1]["injury_tags"] = ""
        rows[2]["injury_tags"] = None
        recent = stats.by_injury_tag(rows)[0]
        self.assertEqual(recent["tag"], injuries.TAG_RECENT)
        self.assertEqual((recent["with"]["wins"], recent["without"]["losses"], recent["unrecorded"]), (1, 1, 1))

    def test_jc_movement_counts_line_changes_and_direction(self):
        def snap(minute, jc_line, ref_line, sp=1.70):
            return {"captured_at": f"2026-10-31T16:{minute:02d}:00+08:00", "jc_line": jc_line, "ref_line": ref_line,
                    "jc_sp_home": sp, "jc_sp_away": 1.70, "jc_total": 220.5, "ref_total": 221.5,
                    "jc_sp_over": 1.70, "jc_sp_under": 1.70}
        moved = [snap(30, 1.5, -1.5), snap(0, 1.5, -1.5), snap(59, -0.5, -1.5)]  # 乱序输入也按时间排
        flat = [snap(0, 2.5, 2.5), snap(30, 2.5, 2.5, sp=1.75)]
        single = [snap(0, 3.5, 0.5)]
        result = stats.jc_movement([moved, flat, single])
        spread = result["让分"]
        self.assertEqual((spread["games"], spread["line_changed"], spread["odds_changed"]), (2, 1, 1), "只抓到一次的场次不计入")
        self.assertEqual((spread["deviation_narrowed"], spread["deviation_widened"]), (1, 0), "偏离 3 → 1 分：竞彩向国际线靠拢")
        self.assertEqual(spread["median_minutes_to_change"], 59)
        self.assertEqual(result["大小分"]["line_changed"], 0)


class InjuryRuleTests(unittest.TestCase):
    def test_context_tags_and_missing_data(self):
        now = datetime(2026, 10, 31, 16, 0, tzinfo=BJ)
        items = [{"team": "Utah", "player": "A", "status": "Out", "type": "Knee", "updated": "2026-10-20T00:00Z"}]
        self.assertEqual(injuries.context(items, "Utah", "Portland", now)["tags"], [], "长期缺阵且无近期更新：不打标签")
        items.append({"team": "Portland", "player": "B", "status": "Questionable", "type": None, "updated": "2026-10-31T05:00Z"})
        self.assertEqual(injuries.context(items, "Utah", "Portland", now)["tags"], [injuries.TAG_RECENT, injuries.TAG_UNCERTAIN])
        self.assertEqual(injuries.context(items, "Boston", "Miami", now)["tags"], [], "其他球队的伤停不影响本场")
        self.assertIsNone(injuries.context(None, "Utah", "Portland", now), "数据不可用 ≠ 无伤停")

    def test_diff_reports_status_changes_and_removals(self):
        before = [{"player": "A", "status": "Day-To-Day"}, {"player": "B", "status": "Out"}]
        after = [{"player": "A", "status": "Out"}, {"player": "C", "status": "Questionable"}]
        self.assertEqual([(c["player"], c["from"], c["to"]) for c in injuries.diff(before, after)],
                         [("A", "Day-To-Day", "Out"), ("B", "Out", None), ("C", None, "Questionable")])


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.routers import lottery as router
        self.router = router
        self.folder = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.folder.name, "lottery.db")
        app = FastAPI()
        app.include_router(router.router, prefix="/api")
        self.client = TestClient(app)
        self.patches = [patch("lottery.store.LOTTERY_DB", self.db)]
        for p in self.patches:
            p.start()
        jobs.run_picks(day="2026-10-31", now=datetime(2026, 10, 31, 21, 40, tzinfo=BJ), db_path=self.db,
                       fetch_jc=lambda d, p: sources.parse_jc_page(ROW.replace("客队让8.5分", "客队让1.5分")),
                       fetch_espn=lambda d: sources.parse_espn(espn(line="UTAH -1.5")),
                       fetch_injuries=lambda: sources.parse_injuries(injury_payload(
                           ("POR", "Shaedon Sharpe", "Day-To-Day", "Ankle", "2026-10-29T02:00Z"))))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.folder.cleanup()

    def at(self, hour, day=31):
        month = 10 if day == 31 else 11
        return patch.object(self.router, "now_beijing", return_value=datetime(2026, month, day, hour, 0, tzinfo=BJ))

    def test_picks_public_as_soon_as_recorded(self):
        with self.at(22):
            ledger = self.client.get("/api/lottery/ledger").json()
            summary = self.client.get("/api/lottery/summary").json()
            games = self.client.get("/api/lottery/games").json()
            sharp_only = self.client.get("/api/lottery/ledger?strategy=sharp").json()
            single_only = self.client.get("/api/lottery/ledger?single_open=true").json()
            parlay_only = self.client.get("/api/lottery/ledger?single_open=false").json()
        self.assertEqual(ledger["total"], 3, "开赛前即公开：主信号 1 注 + 全场预测 2 条")
        self.assertEqual(sharp_only["items"][0]["selection"], "爵士 +1.5")
        self.assertTrue(all(it["single_open"] is True for it in single_only["items"]))
        self.assertTrue(all(it["single_open"] is False for it in parlay_only["items"]))
        self.assertEqual(single_only["total"] + parlay_only["total"], ledger["total"])
        self.assertFalse(sharp_only["items"][0]["started"])
        self.assertEqual(summary["latest_day_picks"], {"sharp": 1, "sharp_late": 0, "all_games": 2})
        self.assertEqual([s["key"] for s in summary["strategies"]], ["sharp", "all_games"], "两个策略分开统计")
        self.assertEqual(sharp_only["items"][0]["injury_tags"], [injuries.TAG_UNCERTAIN])
        self.assertEqual([g["key"] for g in summary["injury_groups"]], ["sharp", "all_games"])
        self.assertTrue(games["games"][0]["analysis"], "开赛前展示偏离与期望值")
        self.assertEqual(len(games["games"][0]["picks"]), 3)
        self.assertEqual([(t["type"], t["role"]) for t in games["tickets"]], [("单关", "研究")])
        self.assertIn("不构成投注建议", summary["disclaimer"])

    def test_games_show_jc_timeline_and_injury_changes(self):
        later = injury_payload(("POR", "Shaedon Sharpe", "Out", "Ankle", "2026-10-31T14:00Z"))
        jobs.run_capture(now=datetime(2026, 10, 31, 22, 30, tzinfo=BJ), db_path=self.db,
                         fetch_jc=lambda d, p: sources.parse_jc_page(ROW.replace("客队让8.5分", "客队让2.5分")),
                         fetch_espn=lambda d: sources.parse_espn(espn(line="UTAH -2.5")) if d == "2026-10-31" else [],
                         fetch_injuries=lambda: sources.parse_injuries(later))
        with self.at(23):
            game = self.client.get("/api/lottery/games?sale_date=2026-10-31").json()["games"][0]
            health = self.client.get("/api/lottery/health").json()
            summary = self.client.get("/api/lottery/summary").json()
        self.assertEqual(game["buy_end"], "2026-10-31T23:00:00+08:00")
        self.assertEqual((game["jc_line"], game["jc_latest"]["jc_line"]), (1.5, 2.5), "出单时与最新竞彩线并列")
        self.assertEqual([(s["source"], s["jc_line"], s["ref_line"]) for s in game["timeline"]],
                         [("出单", 1.5, -1.5), ("竞彩跟踪", 2.5, -2.5)])
        self.assertEqual([r["player"] for r in game["injuries"]["away"]], ["Shaedon Sharpe"])
        self.assertEqual(game["injuries"]["home"], [], "主队出单时无伤停")
        self.assertEqual([(c["player"], c["from"], c["to"]) for c in game["injuries"]["changes"]],
                         [("Shaedon Sharpe", "每日观察", "缺阵")])
        self.assertEqual(health["jc_movement_30d"]["让分"]["line_changed"], 1)
        self.assertEqual(health["jobs"]["capture"]["detail"]["new_late_picks"], 1, "停售前 30 分钟偏离 5 分 → 影子单")
        self.assertIn("sharp_late", [s["key"] for s in summary["strategies"]])

    def test_games_ignore_jc_lines_captured_after_sales_close(self):
        with store.session(self.db) as conn:  # 历史数据：停售后误记的竞彩跟踪快照
            store.add_snapshot(conn, "2026-10-31", {"home": "Utah", "away": "Portland", "jc_line": 4.5,
                                                    "jc_sp_home": 1.70, "jc_sp_away": 1.70, "ref_line": -2.5,
                                                    "ref_total": 234.5}, "track",
                               captured_at="2026-10-31T23:30:00+08:00")
        with self.at(22, day=31):
            before_close = self.client.get("/api/lottery/games").json()["games"][0]
        with patch.object(self.router, "now_beijing", return_value=datetime(2026, 10, 31, 23, 45, tzinfo=BJ)):
            game = self.client.get("/api/lottery/games").json()["games"][0]
        self.assertFalse(before_close["sale_closed"])
        self.assertTrue(game["sale_closed"])
        self.assertIsNone(game["jc_latest"], "停售后的竞彩线不算“最新竞彩”")
        self.assertEqual([(s["source"], s["jc_line"], s["ref_line"]) for s in game["timeline"]],
                         [("出单", 1.5, -1.5), ("国际线", None, -2.5)], "停售后时间线只保留国际线")

    def test_games_day_navigation_scores_and_summary(self):
        jobs.run_capture(now=datetime(2026, 11, 1, 6, 0, tzinfo=BJ), db_path=self.db, fetch_injuries=no_injuries,
                         fetch_jc=lambda d, p: [],
                         fetch_espn=lambda d: sources.parse_espn(espn(state="post", scores=(100, 105))) if d == "2026-10-31" else [])
        with store.session(self.db) as conn:
            store.upsert_game(conn, "2026-10-29", {"home": "Boston", "away": "Miami", "matchnum": "周四301"})
        with self.at(7, day=1):
            day = self.client.get("/api/lottery/games").json()
            empty = self.client.get("/api/lottery/games?sale_date=2026-10-30").json()
        self.assertEqual((day["sale_date"], day["latest_sale_date"]), ("2026-10-31", "2026-10-31"))
        self.assertEqual((day["prev_sale_date"], day["next_sale_date"]), ("2026-10-29", None))
        game = day["games"][0]
        self.assertEqual((game["home"], game["home_score"], game["away_score"], game["score"]), ("爵士", 100, 105, "105-100"))
        summary = {s["key"]: s for s in day["day_summary"]}
        self.assertEqual(list(summary), ["sharp", "all_games"], "当天汇总按策略分开")
        self.assertEqual((summary["sharp"]["bets"], summary["sharp"]["losses"], summary["sharp"]["pnl_units"]), (1, 1, -1.0))
        self.assertEqual(summary["all_games"]["settled"], 2)
        self.assertEqual(day["thresholds"]["min_ev"], signals.MIN_EV)
        self.assertEqual((empty["games"], empty["prev_sale_date"], empty["next_sale_date"]), ([], "2026-10-29", "2026-10-31"),
                         "没有场次的日子也能前后翻页")

    def test_picks_visible_after_tip(self):
        with self.at(4, day=1):
            ledger = self.client.get("/api/lottery/ledger?strategy=sharp").json()
            games = self.client.get("/api/lottery/games?sale_date=2026-10-31").json()
            health = self.client.get("/api/lottery/health").json()
        self.assertEqual(ledger["total"], 1)
        self.assertEqual(ledger["items"][0]["selection"], "爵士 +1.5")
        self.assertTrue(ledger["items"][0]["started"])
        self.assertTrue(games["games"][0]["analysis"])
        self.assertEqual(health["jobs"]["picks"]["last_status"], "ok")
        self.assertEqual(health["jobs"]["picks"]["detail"]["new_full_card"], 2)
        self.assertNotIn(self.folder.name, str(health), "不暴露内部路径")

    def test_missing_database_is_unavailable_not_created(self):
        missing = os.path.join(self.folder.name, "missing.db")
        with patch("lottery.store.LOTTERY_DB", missing):
            self.assertFalse(self.client.get("/api/lottery/summary").json()["available"])
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(self.client.get("/api/lottery/ledger?strategy=bogus").status_code, 422)


if __name__ == "__main__":
    unittest.main()
