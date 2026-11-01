"""体彩 NBA 纸面验证：解析、点值换算、出单只记一次、结算与收盘价差、公开边界。"""
import csv
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lottery import jobs, signals, sources, stats, store

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

    def run_picks(self, title, ref="UTAH -1.5", hour=21):
        now = datetime(2026, 10, 31, hour, 40, tzinfo=BJ)
        return jobs.run_picks(day="2026-10-31", now=now, db_path=self.db, fetch_jc=self.fake_jc(title),
                              fetch_espn=lambda day: sources.parse_espn(espn(line=ref)))

    def test_pick_recorded_once_and_never_rewritten(self):
        first = self.run_picks("客队受让1.5分")  # 竞彩主队 -1.5，与国际线相同
        self.assertEqual(first["new_picks"], 0, "竞彩与国际线一致时不出单")
        summary = self.run_picks("客队让1.5分")  # 竞彩主队 +1.5，国际主队 -1.5 → 偏离 3 分
        self.assertEqual(summary["new_picks"], 1)
        again = self.run_picks("客队让4.5分")
        self.assertEqual(again["new_picks"], 0, "同场同玩法只记第一次决定")
        with store.session(self.db) as conn:
            rows = conn.execute("SELECT side, line, edge FROM picks").fetchall()
            snaps = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        self.assertEqual([tuple(r) for r in rows], [("主", 1.5, 3.0)])
        self.assertEqual(snaps, 3, "每次抓取都追加快照")

    def test_no_pick_after_sales_close(self):
        self.assertEqual(self.run_picks("客队让1.5分", hour=23)["new_picks"], 0)

    def test_capture_records_close_line_and_settles_with_clv(self):
        self.run_picks("客队让1.5分")
        jobs.run_capture(now=datetime(2026, 11, 1, 2, 30, tzinfo=BJ), db_path=self.db,
                         fetch_espn=lambda day: sources.parse_espn(espn(line="UTAH -2.5")) if day == "2026-10-31" else [])
        jobs.run_capture(now=datetime(2026, 11, 1, 6, 0, tzinfo=BJ), db_path=self.db,
                         fetch_espn=lambda day: sources.parse_espn(espn(state="post", scores=(100, 105))) if day == "2026-10-31" else [])
        with store.session(self.db) as conn:
            pick = dict(conn.execute("SELECT result, pnl, close_ref, clv FROM picks").fetchone())
        self.assertEqual(pick, {"result": "输", "pnl": -1.0, "close_ref": -2.5, "clv": 4.0})

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
                       fetch_espn=lambda d: sources.parse_espn(espn(line="UTAH -1.5")))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.folder.cleanup()

    def at(self, hour, day=31):
        month = 10 if day == 31 else 11
        return patch.object(self.router, "now_beijing", return_value=datetime(2026, month, day, hour, 0, tzinfo=BJ))

    def test_pregame_picks_are_hidden(self):
        with self.at(22):
            ledger = self.client.get("/api/lottery/ledger").json()
            summary = self.client.get("/api/lottery/summary").json()
            games = self.client.get("/api/lottery/games").json()
        self.assertEqual(ledger["total"], 0)
        self.assertEqual(summary["hidden_pregame_picks"], 1)
        self.assertIsNone(games["games"][0]["analysis"])
        self.assertEqual(games["games"][0]["picks"], [])
        self.assertIn("不构成投注建议", summary["disclaimer"])

    def test_picks_visible_after_tip(self):
        with self.at(4, day=1):
            ledger = self.client.get("/api/lottery/ledger").json()
            games = self.client.get("/api/lottery/games?sale_date=2026-10-31").json()
            health = self.client.get("/api/lottery/health").json()
        self.assertEqual(ledger["total"], 1)
        self.assertEqual(ledger["items"][0]["selection"], "爵士 +1.5")
        self.assertTrue(games["games"][0]["analysis"])
        self.assertEqual(health["jobs"]["picks"]["last_status"], "ok")
        self.assertNotIn(self.folder.name, str(health), "不暴露内部路径")

    def test_missing_database_is_unavailable_not_created(self):
        missing = os.path.join(self.folder.name, "missing.db")
        with patch("lottery.store.LOTTERY_DB", missing):
            self.assertFalse(self.client.get("/api/lottery/summary").json()["available"])
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(self.client.get("/api/lottery/ledger?strategy=bogus").status_code, 422)


if __name__ == "__main__":
    unittest.main()
