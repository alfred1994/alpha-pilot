"""CI discovery must include new scripts and fail on errors or real provider calls."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.py"
spec = importlib.util.spec_from_file_location("regression_runner", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RegressionRunnerTests(unittest.TestCase):
    def test_new_regression_is_discovered_and_live_integrations_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("test_new_contract.py", "test_low_position.py", "test_phase1.py", "helper.py"):
                (root / name).touch()
            self.assertEqual([path.name for path in runner.discover_tests(root)], ["test_new_contract.py"])

    def run_fixture(self, source):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture.py"
            fixture.write_text(source, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(RUNNER), "--run-one", str(fixture)],
                capture_output=True, text=True, timeout=10,
            )

    def test_script_exit_status_is_preserved(self):
        self.assertEqual(self.run_fixture("raise SystemExit(7)\n").returncode, 7)
        self.assertEqual(self.run_fixture("raise SystemExit(0)\n").returncode, 0)
        self.assertNotEqual(self.run_fixture("raise AssertionError('broken contract')\n").returncode, 0)

    def test_swallowed_network_error_still_fails_without_contacting_provider(self):
        result = self.run_fixture(
            "import socket\n"
            "try:\n"
            "    socket.getaddrinfo('provider.invalid', 443)\n"
            "except PermissionError:\n"
            "    pass\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("external network access attempted", result.stderr)


if __name__ == "__main__":
    unittest.main()
