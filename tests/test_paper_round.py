"""新资金轮次必须保留可恢复归档，且不能误删持仓或混用旧净值。"""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.database import Database
from scripts.start_paper_round import start_round


class PaperRoundTest(unittest.TestCase):
    def seed(self, folder):
        path = Path(folder) / "quant.db"
        with Database(db_path=str(path)) as db:
            db.conn.execute("INSERT INTO account_state(id,initial_capital,cash,total_assets,position_count,positions,version) "
                            "VALUES(1,1000000,1120000,1120000,0,'{}',2)")
            db.conn.execute("INSERT INTO trades(code,action,price,shares) VALUES('000001','SELL',12,100)")
            db.conn.execute("INSERT INTO k_daily(code,date,close) VALUES('000001','2026-10-09',12)")
            db.conn.commit()
        (Path(folder) / "paper_account.json").write_text('{"initial_capital":1000000,"cash":1120000}', encoding="utf-8")
        (Path(folder) / "auto_control.json").write_text('{"paused":true}', encoding="utf-8")
        return path

    def test_archive_reset_and_retry_does_not_reset_again(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BROKER_MODE": "paper"}):
            path = self.seed(folder)
            result = start_round(path, 100000, "round-test", workers_stopped=True)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT initial_capital,cash,total_assets,version FROM account_state").fetchone(),
                                 (100000, 100000, 100000, 3))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM k_daily").fetchone()[0], 1)
                db.execute("UPDATE account_state SET cash=99000")
            with closing(sqlite3.connect(Path(result["archive"]) / "consistent.db")) as old:
                self.assertEqual(old.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
                self.assertEqual(old.execute("SELECT cash FROM account_state").fetchone()[0], 1120000)
            self.assertTrue(json.loads((Path(folder) / "auto_control.json").read_text())["paused"])
            self.assertEqual(json.loads((Path(folder) / "circuit_breaker.json").read_text())["peak_value"], 100000)
            self.assertEqual(start_round(path, 100000, "round-test", workers_stopped=True)["status"], "already_completed")
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT cash FROM account_state").fetchone()[0], 99000)

    def test_refuses_positions_live_mode_and_missing_maintenance(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BROKER_MODE": "paper"}):
            path = self.seed(folder)
            with self.assertRaises(ValueError):
                start_round(path, 100000, "round-test")
            with patch.dict(os.environ, {"BROKER_MODE": "live"}), self.assertRaises(ValueError):
                start_round(path, 100000, "round-test", workers_stopped=True)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE account_state SET positions='{\"000001\":{\"shares\":100}}',position_count=1")
            with self.assertRaises(ValueError):
                start_round(path, 100000, "round-test", workers_stopped=True)
            self.assertFalse((Path(folder) / "round_archives").exists())

    def test_file_failure_rolls_back_database_and_restores_snapshot(self):
        from scripts import start_paper_round as module
        write_json = module._write_json
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"BROKER_MODE": "paper"}):
            path = self.seed(folder)
            def failing_write(target, value):
                if target.name == "circuit_breaker.json":
                    raise OSError("test disk failure")
                return write_json(target, value)
            with patch.object(module, "_write_json", side_effect=failing_write), self.assertRaises(OSError):
                start_round(path, 100000, "round-test", workers_stopped=True)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT cash FROM account_state").fetchone()[0], 1120000)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
            self.assertEqual(json.loads((Path(folder) / "paper_account.json").read_text())["cash"], 1120000)


if __name__ == "__main__":
    unittest.main()
