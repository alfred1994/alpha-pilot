"""Discover and run offline regression scripts in separate Python processes."""
import argparse
import ipaddress
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
MANUAL_TESTS = {
    "test_low_position.py": "live market stock selection",
    "test_phase1.py": "optional live Longport integration",
}


def discover_tests(directory):
    return sorted(path for path in directory.glob("test_*.py") if path.name not in MANUAL_TESTS)


def run_one(path):
    """Allow local IPC, but fail if a regression tries to access a real provider."""
    network_attempts = []

    def guard(event, args):
        if event not in ("socket.connect", "socket.getaddrinfo"):
            return
        address = args[1] if event == "socket.connect" else args[0]
        if event == "socket.connect":
            if not isinstance(address, tuple):  # Unix-domain IPC
                return
            address = address[0]
        if address in (None, "localhost"):
            return
        try:
            if ipaddress.ip_address(address).is_loopback:
                return
        except ValueError:
            pass
        if not network_attempts:
            network_attempts.extend(traceback.extract_stack()[:-1])
        raise PermissionError("Offline regression attempted an external network request")

    sys.addaudithook(guard)
    sys.path.insert(0, str(ROOT))
    sys.argv = [str(path)]
    code = 0
    try:
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    if network_attempts:
        print("FAIL: external network access attempted; mock the provider in this test.", file=sys.stderr)
        for frame in network_attempts:
            if str(ROOT) in frame.filename:
                print(f"  {frame.filename}:{frame.lineno} in {frame.name}", file=sys.stderr)
        return 1
    return code


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list tests without running them")
    parser.add_argument("--timeout", type=int, default=120, help="timeout per script in seconds")
    parser.add_argument("--run-one", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    os.environ.update(BROKER_MODE="paper", ALPHAPILOT_ENV="testing",
                      HITHINK_ENABLED="0", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    if args.run_one:
        return run_one(args.run_one)
    tests = discover_tests(ROOT / "tests")
    if not tests:
        parser.error("no regression scripts found")
    for name, reason in MANUAL_TESTS.items():
        print(f"MANUAL {name}: {reason}")
    if args.list:
        for path in tests:
            print(path.relative_to(ROOT).as_posix())
        print(f"{len(tests)} offline regression scripts")
        return 0

    env = dict(os.environ)
    failed = []
    for path in tests:
        started = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--run-one", str(path)],
                cwd=ROOT, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=args.timeout,
            )
            success = result.returncode == 0
            print(f"{'PASS' if success else 'FAIL'} {path.name} ({time.monotonic() - started:.1f}s)", flush=True)
            if not success:
                failed.append(path.name)
                print(result.stdout + result.stderr, flush=True)
        except subprocess.TimeoutExpired:
            failed.append(path.name)
            print(f"TIMEOUT {path.name} ({args.timeout}s)", flush=True)
    print(f"Regression scripts: {len(tests) - len(failed)} passed, {len(failed)} failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
