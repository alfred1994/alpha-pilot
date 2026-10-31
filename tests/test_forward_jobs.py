"""独立研究调度必须幂等，保持冻结代码与真实日期的门禁。"""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_forward_jobs import run
from scripts.install_forward_research import units


class ForwardJobTests(unittest.TestCase):
    def test_unchanged_does_not_collect_and_missed_day_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            (root/"calendar.json").write_text("{}", encoding="utf-8")
            config = dict(health_file=str(root/"health.json"), calendar=str(root/"calendar.json"),
                          factor_state="factor", ml_state="ml", snapshot_root=str(root/"snapshots"))
            latest = {"observed_days": 0, "data_date": "2026-10-14", "execution": {"status": "waiting_execution_data"}}
            status = {"implementation_matches": True, "manifest": {"evaluation_days": 60, "implementation": {}}, "latest": latest}
            stack.enter_context(patch("research.ml_forward.implementation", return_value={}))
            stack.enter_context(patch("research.ml_forward.read_status", return_value=status))
            stack.enter_context(patch("strategy.factor_forward.read_status", return_value=status))
            stack.enter_context(patch("research.ml_forward.calendar_days", return_value=["2026-10-14", "2026-10-15", "2026-10-16"]))
            closed = stack.enter_context(patch("research.ml_forward.latest_closed", return_value="2026-10-14"))
            collect = stack.enter_context(patch("scripts.collect_factor_forward.collect", side_effect=AssertionError("不得采集")))
            result = run(config)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["jobs"]["factor"]["execution_status"], "waiting_execution_data")
            closed.return_value = "2026-10-16"
            result = run(config)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["jobs"]["ml"]["reason"], "missed_observation_day")
            self.assertIsNotNone(result["last_success"])
            collect.assert_not_called()

    def test_units_force_paper_and_use_beijing_time(self):
        output = units("/app/venv/bin/python", "/app/run.py", "/app/config.json", "/app/frozen")
        service = output["alpha-pilot-forward-research.service"]
        timer = output["alpha-pilot-forward-research.timer"]
        self.assertIn("BROKER_MODE=paper", service)
        self.assertIn("WorkingDirectory=/app/frozen\n", service)
        self.assertIn("MemoryMax=3G", service)
        self.assertIn("08:30:00 Asia/Shanghai", timer)
        self.assertIn("16,17,20:20:00 Asia/Shanghai", timer)
        self.assertNotIn(".env", service)

    def test_evidence_calendar_is_independent_without_inventing_execution_fields(self):
        from data.database import Database
        from research.store import connect, BJ
        from research.compare import new_experiment
        from scripts.refresh_research_input import prepare
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);evidence = root/"evidence.db";market = root/"quant.db"
            new_experiment(path=evidence, start="2026-10-13", now=datetime(2026, 10, 13, 8, tzinfo=BJ))
            with connect(evidence) as db:
                db.execute("INSERT INTO scans VALUES(?,?,?,?)", ("scan", "2026-10-13", "2026-10-13T10:00:00+08:00",
                           json.dumps({"candidates": [{"code": "600001"}]})))
            with Database(db_path=str(market)) as db:
                db.insert_k_daily([dict(code="600001", date="2026-10-14", open=10, high=11, low=9, close=10, volume=100)])
            calendar = root/"calendar.json"
            calendar.write_text(json.dumps({"error_code": "0", "data": [[f"2026-10-{day}", "1"] for day in range(12, 17)]}), encoding="utf-8")
            output = root/"panel.json"
            result = prepare(evidence_db=evidence, market_db=market, calendar_file=calendar, output=output,
                             now=datetime(2026, 10, 14, 16, tzinfo=BJ))
            panel = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(panel["calendar"], ["2026-10-13", "2026-10-14"])
            self.assertEqual(result["rows"], 1)
            self.assertEqual(result["codes_requested"], 3)
            self.assertFalse(panel["metadata"]["corporate_actions_verified"])
            self.assertNotIn("raw_open", panel["bars"][0])


if __name__ == "__main__":
    unittest.main()
