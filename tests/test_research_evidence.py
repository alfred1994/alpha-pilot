"""旁路证据层离线契约：时间因果、公平比较、冻结输入、只读接口与失败隔离。"""
import contextlib
import copy
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.audit import audit_panel
from research.compare import capture_scan, compare, new_experiment, normalize_scan
from research.store import BJ, archive, connect, import_report, read_experiments, register, save_report
from research import runtime


DATES = pd.bdate_range("2026-10-12", periods=9).strftime("%Y-%m-%d").tolist()


def instant(day, hour=16, minute=0):
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=BJ)


def payload():
    bars = []
    for i, day in enumerate(DATES):
        for code in ("600001", "600002", "600003", "510300", "510500"):
            price = 10 + i * .05
            bars.append({"date": day, "code": code, "open": price, "close": price,
                         "high": price + .1, "low": price - .1, "volume": 100000,
                         "raw_open": price, "raw_close": price, "tradable": True,
                         "limit_up": price * 1.1, "limit_down": price * .9,
                         "eligible": True, "source": "fixture", "corporate_action": False})
    return {"bars": bars, "calendar": DATES,
            "metadata": {"calendar_source": "fixture", "adjustment": "raw", "source": "fixture",
                         "universe": "point_in_time", "delisted_coverage_verified": True,
                         "corporate_actions_verified": True}}


def plan(day, identity=None, action="HOLD", score=80):
    return SimpleNamespace(date=day, scan_id=identity or day, strategy_version="directive-test", elapsed=1.2,
                           research_context={"top_k": 3, "min_score": 58., "max_weight": .2},
                           research_complete=True,
                           raw_scores=[{"code": "600001", "composite": 90., "llm_action": "BUY", "llm_confidence": .8},
                                       {"code": "600002", "composite": score, "llm_action": action, "llm_confidence": .8},
                                       {"code": "600003", "composite": 70., "llm_status": "timeout"}])


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "evidence.db"
        self.env = patch.dict(os.environ, RESEARCH_EVIDENCE_DB=str(self.db), RESEARCH_EVIDENCE_ENABLED="1")
        self.env.start()
        self.addCleanup(self.env.stop)
        self.data = payload()

    def experiment(self):
        return new_experiment(self.db, now=instant(DATES[0], 9))

    def record(self, day, **kwargs):
        return capture_scan(plan(day, **kwargs), self.db, instant(day, 14, 45))

    def report(self, identity, day=None, data=None):
        day = day or DATES[-1]
        return compare(identity, data or self.data, day, self.db, instant(day))

    def test_audit_strict_missing_entire_session_and_invalid_bars(self):
        result = audit_panel(self.data["bars"], DATES, self.data["metadata"])
        self.assertEqual(result["status"], "strict_ready")
        self.assertFalse(result["execution_verified"])
        bars = [r for r in self.data["bars"] if r["date"] != DATES[2]]
        result = audit_panel(bars, DATES, self.data["metadata"])
        self.assertIn(DATES[2], result["missing_dates"])
        self.assertEqual(result["status"], "approximate_only")
        bars += [bars[0]]
        self.assertIn("duplicate_bars", audit_panel(bars)["errors"])

    def test_audit_unknown_history_actions_and_metadata(self):
        data = copy.deepcopy(self.data)
        data["metadata"].pop("universe")
        data["bars"][0].pop("raw_open")
        result = audit_panel(data["bars"], DATES, data["metadata"])
        self.assertIn("survivorship_bias_unknown", result["gaps"])
        self.assertIn("incomplete_raw_open", result["gaps"])
        data["bars"][0]["corporate_action"] = True
        self.assertEqual(audit_panel(data["bars"], DATES, data["metadata"])["status"], "unavailable")
        data["bars"][0]["tradable"] = "false"
        self.assertIn("invalid_tradable", audit_panel(data["bars"], DATES, data["metadata"])["errors"])

    def test_last_complete_scan_cutoff_and_idempotence(self):
        identity = self.experiment()
        self.record(DATES[0], identity="early")
        capture_scan(plan(DATES[0], "late", "BUY"), self.db, instant(DATES[0], 14, 49))
        bad = plan(DATES[0], "incomplete")
        bad.research_complete = False
        capture_scan(bad, self.db, instant(DATES[0], 14, 50))
        capture_scan(plan(DATES[0], "after"), self.db, instant(DATES[0], 14, 51))
        first = self.report(identity, DATES[1])
        self.assertEqual(first["selections"][0]["scan"]["scan_id"], "late")
        self.assertEqual(first["missing_scan_dates"], [DATES[1]])
        self.assertEqual(self.report(identity, DATES[1]), first)
        self.assertEqual(read_experiments(self.db, identity)[0]["report_count"], 1)
        self.assertFalse(capture_scan(plan(DATES[0], "late"), self.db, instant(DATES[0], 14, 50)))

    def test_filter_keeps_slot_weights_and_accounts_have_same_clock(self):
        identity = self.experiment()
        for day in DATES:
            self.record(day)
        report = self.report(identity)
        a, b, c, d = [report["accounts"][name] for name in "ABCD"]
        buys_a = {r["code"]: r for r in a["trades"] if r["date"] == DATES[1]}
        buys_b = {r["code"]: r for r in b["trades"] if r["date"] == DATES[1]}
        self.assertEqual(set(buys_b), {"600001"})
        self.assertEqual(buys_a["600001"]["shares"], buys_b["600001"]["shares"])
        self.assertGreater(b["curve"][0]["cash"], a["curve"][0]["cash"])
        self.assertEqual([r["date"] for r in a["curve"]], [r["date"] for r in c["curve"]])
        self.assertEqual(c["config"]["stamp_tax"], 0)
        self.assertEqual(d["config"]["price_tick"], .001)
        self.assertEqual(report["llm_status_counts"]["timeout"], len(DATES))
        self.assertGreater(report["paired"]["mature_count"], 0)
        self.assertTrue(all(r["code"] != "600003" for r in report["paired"]["samples"]))
        self.assertIsNone(report["llm_cost"])
        self.assertFalse(report["promotion_allowed"])

    def test_future_perturbations_do_not_change_past_and_old_bars_cannot_change(self):
        identity = self.experiment()
        self.record(DATES[0])
        first = self.report(identity, DATES[2])
        future = copy.deepcopy(self.data)
        for row in future["bars"]:
            if row["date"] > DATES[2]:
                row["close"] *= 100
        self.assertEqual(first, self.report(identity, DATES[2], future))
        later = self.report(identity, DATES[4])
        self.assertEqual(first["accounts"]["A"]["curve"], later["accounts"]["A"]["curve"][:2])
        changed = copy.deepcopy(self.data)
        changed["bars"][0]["raw_open"] += .001
        with self.assertRaisesRegex(ValueError, "historical input changed"):
            self.report(identity, data=changed)

    def test_no_fake_cash_when_data_missing_and_late_scans_never_backfill(self):
        identity = self.experiment()
        self.record(DATES[0])
        missing = copy.deepcopy(self.data)
        for row in missing["bars"]:
            row.pop("raw_open")
        result = self.report(identity, DATES[1], missing)
        self.assertEqual(result["status"], "waiting_data")
        self.assertEqual(result["metrics"], {})
        self.assertNotIn("accounts", result)
        self.record(DATES[1])  # 模拟迟到补录，已冻结缺失不允许变成有效样本
        result = self.report(identity, DATES[2])
        self.assertIn(DATES[1], result["missing_scan_dates"])

    def test_forward_rejects_imported_and_pre_registration_scans(self):
        self.record(DATES[0])
        identity = new_experiment(self.db, now=instant(DATES[0], 14, 46))
        result = self.report(identity, DATES[1])
        self.assertEqual(result["observed_days"], 0)
        data = dict(self.data, scans=[{"fake": True}])
        with self.assertRaisesRegex(ValueError, "reject imported"):
            self.report(identity, data=data)

    def test_missing_day_blocks_valuation_and_calendar_revisions(self):
        identity = self.experiment()
        self.record(DATES[0])
        self.report(identity, DATES[2])
        data = copy.deepcopy(self.data)
        data["bars"] = [r for r in data["bars"] if r["date"] != DATES[3]]
        self.assertEqual(self.report(identity, DATES[4], data)["status"], "waiting_data")
        data = copy.deepcopy(self.data)
        data["calendar"].remove(DATES[1])
        with self.assertRaisesRegex(ValueError, "calendar revision"):
            self.report(identity, data=data)

    def test_market_constraints_and_no_reinvestment_to_replace_filtered_stocks(self):
        identity = self.experiment()
        self.record(DATES[0])
        data = copy.deepcopy(self.data)
        for row in data["bars"]:
            if row["code"] == "600001" and row["date"] == DATES[1]:
                row["limit_up"] = row["raw_open"]
        report = self.report(identity, DATES[2], data)
        self.assertGreater(report["accounts"]["B"]["blocked_counts"]["limit_up"], 0)
        self.assertTrue(all(t["date"] >= DATES[2] for t in report["accounts"]["B"]["trades"]))

    def test_registry_dedup_versions_and_legacy_immutable(self):
        spec = {"kind": "factor", "hypothesis": "momentum", "input_sha256": "abc", "parameters": {"k": 2}}
        first = register(spec, self.db)
        self.assertEqual(first, register(spec, self.db))
        second = register(dict(spec, parameters={"k": 3}, parent_id=first), self.db)
        self.assertNotEqual(first, second)
        save_report(first, {"as_of": DATES[0], "reasons": ["cost_exceeds_alpha"]}, self.db)
        save_report(first, {"as_of": DATES[1], "reasons": ["data_insufficient"]}, self.db, "waiting_data")
        self.assertEqual(read_experiments(self.db, first)[0]["report_count"], 2)
        self.assertEqual(read_experiments(self.db, second)[0]["hypothesis_attempts"], 2)
        old = {"status": "research_only", "strategy_net": {"total_return": -.1}}
        saved = copy.deepcopy(old)
        report = import_report(old, self.db)
        self.assertEqual(old, saved)
        self.assertEqual(read_experiments(self.db, report["experiment_id"])[0]["manifest"]["code_version"], "unknown")

    def test_llm_failure_classes(self):
        for reason, expected in (("结构化解析失败", "parse_error"), ("请求超时", "timeout"), ("LLM无响应", "unavailable")):
            scan = {"scan_id": "x", "day": DATES[0], "observed_at": instant(DATES[0], 14).isoformat(),
                    "params": plan(DATES[0]).research_context,
                    "candidates": [{"code": "600001", "composite": 80, "llm_action": "HOLD", "llm_confidence": 0, "llm_reason": reason}]}
            self.assertEqual(normalize_scan(scan)["candidates"][0]["llm_status"], expected)

    def test_cli_report_and_http_are_readonly_even_empty(self):
        from web.routers.research import router
        app = FastAPI()
        app.include_router(router, prefix="/api")
        client = TestClient(app)
        self.assertEqual(client.get("/api/research/experiments").json()["experiments"], [])
        self.assertFalse(self.db.exists())
        import main
        with patch.object(sys, "argv", ["main.py", "--research-report", "--research-db", str(self.db)]), contextlib.redirect_stdout(io.StringIO()) as output:
            main.main()
        self.assertEqual(json.loads(output.getvalue())["experiments"], [])
        self.assertFalse(self.db.exists())
        identity = register({"kind": "audit", "hypothesis": "secret token=SECRET /private/x", "input_path": "C:/private"}, self.db)
        save_report(identity, {"as_of": DATES[0], "prompt": "SECRET", "audit": {"metadata": "SECRET"}}, self.db)
        before = self.db.read_bytes()
        for url in ("/api/research/experiments", "/api/research/experiments/" + identity, "/api/research/health"):
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("SECRET", response.text)
            self.assertNotIn("private", response.text)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(client.post("/api/research/experiments").status_code, 405)
        self.assertEqual(client.get("/api/research/experiments?limit=10000").status_code, 422)

    def test_capture_failure_isolated_and_worker_health_visible(self):
        with patch("research.compare.capture_scan", side_effect=RuntimeError("SECRET")):
            runtime.record_scan(plan(DATES[0]))
        self.assertEqual(runtime.health(self.db)["status"], "degraded")
        self.assertNotIn("SECRET", json.dumps(runtime.health(self.db)))
        identity = new_experiment(self.db, now=instant(DATES[0], 9), input_path="missing.json")
        runtime.run_worker(identity, self.db)
        self.assertTrue(any(j["status"] == "waiting_data" for j in runtime.health(self.db)["jobs"]))

    def test_scheduler_daily_lease_and_stale_restart(self):
        identity = new_experiment(self.db, now=instant(DATES[0], 9), input_path="missing.json")
        with patch("research.runtime.subprocess.Popen") as spawn:
            self.assertEqual(runtime.launch_due(instant(DATES[0])), [identity])
            self.assertEqual(runtime.launch_due(instant(DATES[0], 16, 1)), [])
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(runtime.health(self.db, instant(DATES[0], 17))["status"], "degraded")
            self.assertEqual(runtime.launch_due(instant(DATES[0], 17)), [identity])
        runtime.job_status("compare:" + identity, "ok", path=self.db, now=instant(DATES[0], 17))
        with patch("research.runtime.subprocess.Popen") as spawn:
            self.assertEqual(runtime.launch_due(instant(DATES[0], 18)), [])
            spawn.assert_not_called()

    def test_cannot_write_to_formal_database(self):
        formal = Path(self.temp.name) / "formal.db"
        with contextlib.closing(sqlite3.connect(formal)) as db:
            db.execute("CREATE TABLE trades (id INTEGER)")
            db.execute("INSERT INTO trades VALUES(1)")
            db.commit()
        before = formal.read_bytes()
        with self.assertRaisesRegex(ValueError, "must_be_isolated"):
            register({"kind": "audit"}, formal)
        self.assertEqual(formal.read_bytes(), before)

    def test_code_change_requires_new_experiment_and_archive_stops_updates(self):
        identity = self.experiment()
        self.record(DATES[0])
        with patch("research.compare.code_version", return_value={"changed": True}):
            with self.assertRaisesRegex(ValueError, "implementation changed"):
                self.report(identity)
        archive(identity, self.db)
        with self.assertRaisesRegex(ValueError, "archived"):
            self.report(identity)

    def test_failed_report_save_retry_does_not_double_fill(self):
        identity = self.experiment()
        self.record(DATES[0])
        with patch("research.compare.save_report", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.report(identity, DATES[1])
        report = self.report(identity, DATES[1])
        self.assertEqual(len(report["accounts"]["A"]["trades"]), 3)
        self.assertEqual(self.report(identity, DATES[1]), report)

    def test_retrospective_dedup_and_changed_import_rejected(self):
        identity = new_experiment(self.db, now=instant(DATES[1]), retrospective=True,
                                  start=DATES[0], input_sha256="same")
        second = new_experiment(self.db, now=instant(DATES[2]), retrospective=True,
                                start=DATES[0], input_sha256="same")
        self.assertEqual(identity, second)
        scan = {"scan_id": "old", "day": DATES[0], "observed_at": instant(DATES[0], 14).isoformat(),
                "complete": True, "params": plan(DATES[0]).research_context,
                "candidates": plan(DATES[0]).raw_scores}
        data = dict(self.data, scans=[scan])
        report = self.report(identity, DATES[2], data)
        self.assertEqual(report["interval_role"], "review")
        self.assertIn("historical_review_not_new_holdout", report["reasons"])
        changed = copy.deepcopy(data)
        changed["scans"][0]["candidates"][0]["llm_action"] = "HOLD"
        with self.assertRaisesRegex(ValueError, "imported scan changed"):
            self.report(identity, DATES[2], changed)

    def test_execution_parameters_create_distinct_trials(self):
        from research.cli import register_existing
        first = {"input_sha256": "same", "config": {}, "strategy": {"config": {"capital": 100000}}}
        second = copy.deepcopy(first)
        second["strategy"]["config"]["capital"] = 200000
        self.assertNotEqual(register_existing(first, "factor"), register_existing(second, "factor"))

    def test_cli_audit_compare_import_and_archive(self):
        import main
        input_path = Path(self.temp.name) / "panel.json"
        input_path.write_text(json.dumps(self.data), encoding="utf-8")

        def invoke(options):
            with patch.object(sys, "argv", ["main.py", "--research-db", str(self.db)] + options), contextlib.redirect_stdout(io.StringIO()) as output:
                main.main()
            return json.loads(output.getvalue())

        audit = invoke(["--research-audit", "--research-input", str(input_path)])
        self.assertEqual(audit["audit"]["status"], "strict_ready")
        old_path = Path(self.temp.name) / "old.json"
        old_path.write_text(json.dumps({"input_sha256": "old", "strategy_net": {"total_return": -.2}}), encoding="utf-8")
        before = old_path.read_bytes()
        legacy = invoke(["--research-audit", "--research-import", str(old_path)])
        self.assertEqual(old_path.read_bytes(), before)
        archived = invoke(["--research-audit", "--research-id", legacy["experiment_id"], "--research-archive"])
        self.assertEqual(archived["status"], "archived")
        with patch("research.compare.clock", side_effect=lambda now=None: now or instant(DATES[-1])):
            result = invoke(["--research-compare", "--research-input", str(input_path), "--research-retrospective",
                             "--research-start", DATES[0], "--research-as-of", DATES[-1]])
        self.assertEqual(result["interval_role"], "review")
        self.assertEqual(result["observed_days"], 0)

    def test_pipeline_recording_failure_cannot_change_scan_result(self):
        from scheduler.pipeline import TradePlan, run_scan
        p = TradePlan(date=DATES[0], scan_id="test", errors=["existing"])
        with patch("scheduler.pipeline.fast_scan", return_value=p), \
                patch("research.runtime.record_scan", side_effect=RuntimeError("archive failed")), \
                patch("strategy.counterfactual.record_trade_plan_candidates", return_value=0):
            result = run_scan()
        self.assertEqual(result.errors, ["existing"])
        self.assertEqual(result.trade_plan, p.to_dict())


if __name__ == "__main__":
    unittest.main()
