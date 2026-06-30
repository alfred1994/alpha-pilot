import subprocess
import json
import sys
import os

def test_cmd_json(args):
    """通过子进程执行命令，并校验其 stdout 是否为可直接解析的纯净 JSON 字符串"""
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [sys.executable, "main.py"] + args
    
    print(f"Testing command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_dir, encoding="utf-8")
    
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    
    if result.returncode != 0:
        print(f"[FAIL] Command exited with non-zero code {result.returncode}")
        print(f"Stderr:\n{stderr}")
        return False
        
    try:
        parsed = json.loads(stdout)
        print("[OK] Success! Output is pure JSON:")
        print(json.dumps(parsed, ensure_ascii=False, indent=2)[:300] + "\n...")
        return True
    except json.JSONDecodeError as e:
        print("[FAIL] Output is NOT pure JSON:")
        print(f"Error: {e}")
        print(f"Raw stdout (Length: {len(stdout)}):\n{stdout}")
        return False

def run_tests():
    success = True
    if not test_cmd_json(["--agent-status"]):
        success = False
    
    if not test_cmd_json(["--crash-info"]):
        success = False
        
    if success:
        print("[SUCCESS] ALL DRIVER INTERFACE TESTS PASSED!")
        sys.exit(0)
    else:
        print("[ALERT] DRIVER INTERFACE TESTS FAILED!")
        sys.exit(1)

if __name__ == "__main__":
    run_tests()
