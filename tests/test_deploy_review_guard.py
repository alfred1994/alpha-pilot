"""执行部署停靠函数，验证等待复盘边界、超时取消以及恢复定时器。"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def main():
    bash = shutil.which("bash")
    if not bash and os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        bash = str(candidate) if candidate.exists() else None
    if not bash:
        raise RuntimeError("部署契约测试需要bash")
    source = (Path(__file__).resolve().parents[1] / "scripts/deploy_hermes.sh").read_text(encoding="utf-8")
    guard = source[source.index("QUIESCED_UNITS=()"):source.index("git_auth() {")]
    guard = guard.replace("time.monotonic() + 660", "time.monotonic() + .4").replace("time.sleep(2)", "time.sleep(.01)")
    for acknowledge in [True, False]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir(); (root / "data").mkdir(); (root / "scheduler").mkdir()
            (root / "scheduler/auto_trader.py").write_text("AUTO_DEPLOY_HOLD_FILE")
            log = root / "systemctl.log"
            errors = []
            def fake_auto():
                try:
                    deadline = time.monotonic() + 5
                    hold = root / "data/auto_deploy_hold.json"
                    while time.monotonic() < deadline and not hold.exists():
                        time.sleep(.01)
                    if not hold.exists():
                        raise AssertionError("未生成停靠请求")
                    time.sleep(.08)
                    assert "stop alpha-pilot-auto.service" not in log.read_text(), "复盘未结束时不能停止服务"
                    nonce = json.loads(hold.read_text())["nonce"]
                    Path(str(hold) + ".ready").write_text(json.dumps({"nonce": nonce, "pid": os.getpid()}))
                except BaseException as exc:
                    errors.append(exc)
            worker = threading.Thread(target=fake_auto) if acknowledge else None
            if worker:worker.start()
            harness = """set -euo pipefail
log() { printf '%s\\n' "$*"; }
systemctl() {
  printf '%s\\n' "$*" >> "$TASK_SYSTEMCTL_LOG"
  if [ "$2" = show ]; then printf 'inactive\\n'; fi
  return 0
}
""" + "PROJECT_DIR=" + shlex.quote(str(root).replace("\\", "/")) + "\nPYTHON_CMD=" + shlex.quote(sys.executable.replace("\\", "/")) + "\n" + guard + "\nquiesce_auto_before_deploy\n"
            result = subprocess.run([bash, "-c", harness], env=dict(os.environ, TASK_SYSTEMCTL_LOG=str(log).replace("\\", "/")),
                                    capture_output=True, text=True, encoding="utf-8", timeout=12)
            if worker:worker.join(6)
            assert not errors, errors
            commands = log.read_text()
            assert (result.returncode == 0) == acknowledge, result.stdout + result.stderr
            assert ("stop alpha-pilot-auto.service" in commands) == acknowledge
            assert "start alpha-pilot-doctor.timer alpha-pilot-auto-restart.timer" in commands
            assert not (root / "data/auto_deploy_hold.json").exists()
            print("OK", "review completed then quiesced" if acknowledge else "timeout cancels deploy and restores timers")
    assert source.index("quiesce_auto_before_deploy\nprepare_git_repository") < source.index("reset_git_worktree_for_deploy\n")


if __name__ == "__main__":
    main()
