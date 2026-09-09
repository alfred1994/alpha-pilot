"""进程存活检查不能发送信号或误回收仍被占用的研究锁。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data import research_universe as research


class ResearchLockTest(unittest.TestCase):
    def test_current_process_and_live_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "research.lock")
            self.assertTrue(research._acquire_lock(path))
            os.utime(path, (1, 1))
            self.assertTrue(research._pid_is_alive(os.getpid()))
            self.assertFalse(research._acquire_lock(path))
            self.assertTrue(Path(path).exists())

    def test_child_lifecycle(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        try:
            self.assertTrue(research._pid_is_alive(child.pid))
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertFalse(research._pid_is_alive(child.pid))
        self.assertFalse(research._pid_is_alive(0))

    @unittest.skipUnless(os.name == "nt", "Windows process query")
    def test_windows_never_sends_signal(self):
        with patch.object(research.os, "kill", side_effect=AssertionError("must not signal")):
            self.assertTrue(research._pid_is_alive(os.getpid()))


if __name__ == "__main__":
    unittest.main()
