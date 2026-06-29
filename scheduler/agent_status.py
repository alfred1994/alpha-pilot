import os
import sys
import json
from datetime import datetime

def build_agent_status_snapshot():
    """统一构建 Quant Pilot 系统当前的运行态、健康态、资产、自适应指标及未决崩溃状态"""
    from scheduler.health import run_health_check
    from scheduler.watchdog import run_auto_watchdog
    from scheduler.control import get_auto_control_state
    
    # 1. 运行健康检查
    health_ok = True
    health_failed = []
    try:
        health_items = run_health_check()
        health_ok = all(item.ok or not item.required for item in health_items)
        health_failed = [item.name for item in health_items if not item.ok and item.required]
    except Exception as e:
        health_ok = False
        health_failed = [str(e)]
        
    # 2. 运行 Watchdog
    watchdog_ok = True
    watchdog_criticals = []
    try:
        watchdog_items = run_auto_watchdog()
        watchdog_criticals = [item.name for item in watchdog_items if item.severity == "critical"]
        watchdog_ok = (len(watchdog_criticals) == 0)
    except Exception as e:
        watchdog_ok = False
        watchdog_criticals = [str(e)]
        
    # 3. 读取控制阀门状态
    control_paused = False
    control_reason = ""
    try:
        control = get_auto_control_state()
        control_paused = bool(control.get("paused"))
        control_reason = control.get("reason", "")
    except Exception:
        pass
        
    # 4. 获取账户与当前持仓
    positions = []
    total_assets = 1000000.0
    cash = 1000000.0
    try:
        from execution.paper_account import PaperAccount
        account = PaperAccount()
        total_assets = account.total_assets()
        cash = account.cash
        positions = list(account.positions.keys())
    except Exception:
        pass
        
    # 5. 读取最新自适应参数
    from strategy.adaptive import ADAPTIVE_FILE
    adaptive_params = {}
    if os.path.exists(ADAPTIVE_FILE):
        try:
            with open(ADAPTIVE_FILE, "r", encoding="utf-8") as f:
                adaptive_data = json.load(f)
                adaptive_params = {
                    "weights": adaptive_data.get("current_weights"),
                    "buy_threshold": adaptive_data.get("current_buy_threshold"),
                    "min_score": adaptive_data.get("current_min_score"),
                    "top_k_delta": adaptive_data.get("current_top_k_delta"),
                    "position_scale": adaptive_data.get("current_position_scale"),
                    "regime": adaptive_data.get("current_regime", "sideways")
                }
        except Exception:
            pass

    # 6. 检测未决 Crash 状态
    crash_open = False
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    crash_file = os.path.join(project_dir, "data", "latest_crash.json")
    if os.path.exists(crash_file):
        try:
            with open(crash_file, "r", encoding="utf-8") as f:
                crash_data = json.load(f)
                if crash_data.get("status") == "open":
                    crash_open = True
        except Exception:
            pass

    return {
        "timestamp": datetime.now().isoformat(),
        "health": {
            "ok": health_ok,
            "failed_required": health_failed
        },
        "watchdog": {
            "ok": watchdog_ok,
            "criticals": watchdog_criticals
        },
        "control": {
            "paused": control_paused,
            "reason": control_reason
        },
        "account": {
            "total_assets": total_assets,
            "cash": cash,
            "positions": positions
        },
        "adaptive": adaptive_params,
        "crash_open": crash_open
    }
