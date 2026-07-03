import os
import sys
import json
from datetime import datetime

def build_agent_status_snapshot():
    """统一构建 AlphaPilot 系统当前的运行态、健康态、资产、自适应指标及未决崩溃状态"""
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
    initial_capital = 1000000.0
    total_pnl = 0.0
    total_pnl_pct = 0.0
    try:
        from execution.paper_account import PaperAccount
        account = PaperAccount()
        price_map = {}
        if account.positions:
            try:
                from data.realtime import get_realtime
                quotes = get_realtime(list(account.positions.keys()))
                price_map = {
                    quote.code: quote.price
                    for quote in quotes
                    if getattr(quote, "price", 0) > 0
                }
            except Exception:
                price_map = {}
        total_assets = account.total_assets(price_map or None)
        cash = account.cash
        positions = list(account.positions.keys())
        initial_capital = account.initial_capital
        total_pnl = total_assets - initial_capital
        total_pnl_pct = total_pnl / initial_capital if initial_capital > 0 else 0.0
    except Exception:
        pass
        
    # 5. 读取最新自适应参数
    from strategy.adaptive import ADAPTIVE_FILE
    adaptive_params = {}
    if os.path.exists(ADAPTIVE_FILE):
        try:
            with open(ADAPTIVE_FILE, "r", encoding="utf-8") as f:
                adaptive_data = json.load(f)
                adjustments = adaptive_data.get("adjustments", [])
                latest_adj = adjustments[-1] if adjustments else None
                adaptive_params = {
                    "weights": adaptive_data.get("current_weights"),
                    "buy_threshold": adaptive_data.get("current_buy_threshold"),
                    "min_score": adaptive_data.get("current_min_score"),
                    "top_k_delta": adaptive_data.get("current_top_k_delta"),
                    "position_scale": adaptive_data.get("current_position_scale"),
                    "regime": adaptive_data.get("current_regime", "sideways"),
                    "last_update": adaptive_data.get("last_update") or adaptive_data.get("timestamp"),
                    "latest_adjustment": latest_adj
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

    # 7. 读取自动盯盘状态文件中的进度与循环计数
    from scheduler.auto_trader import AUTO_STATE_FILE
    pipeline_progress = {
        "prefetch": False,
        "scan": False,
        "execute": False,
        "review": False
    }
    loop_count = 0
    last_loop_time = "-"
    if os.path.exists(AUTO_STATE_FILE):
        try:
            with open(AUTO_STATE_FILE, "r", encoding="utf-8") as f:
                state_data = json.load(f)
            today = datetime.now().strftime("%Y-%m-%d")
            
            pipeline_progress["prefetch"] = (state_data.get("last_prefetch_date") == today)
            
            last_scan_at = state_data.get("last_scan_at", 0.0)
            if last_scan_at > 0:
                scan_date = datetime.fromtimestamp(last_scan_at).strftime("%Y-%m-%d")
                pipeline_progress["scan"] = (scan_date == today)
                
            last_execute_at = state_data.get("last_execute_at", 0.0)
            if last_execute_at > 0:
                exec_date = datetime.fromtimestamp(last_execute_at).strftime("%Y-%m-%d")
                pipeline_progress["execute"] = (exec_date == today)
                
            pipeline_progress["review"] = (state_data.get("last_review_date") == today)
            
            loop_count = state_data.get("loop_count", 0)
            if state_data.get("updated_at"):
                last_loop_time = state_data.get("updated_at")
        except Exception:
            pass

    # 8. 读取最近系统运行日志事件
    recent_logs = []
    try:
        from data.database import Database
        with Database() as db:
            events = db.get_auto_events(limit=15)
            for event in events:
                actions = event.get("actions")
                if actions:
                    try:
                        actions_list = json.loads(actions) if isinstance(actions, str) else actions
                    except Exception:
                        actions_list = [actions]
                    for action in actions_list:
                        created_at_time = ""
                        if event.get("created_at"):
                            try:
                                created_at_time = event.get("created_at").split("T")[-1][:8]
                            except Exception:
                                created_at_time = event.get("created_at")[:19]
                        recent_logs.append({
                            "time": created_at_time,
                            "type": event.get("event_type"),
                            "status": event.get("status"),
                            "action": action,
                            "error": event.get("error")
                        })
    except Exception:
        pass

    # 9. 构建驾驶舱风险报警列表
    risk_warnings = []
    if not health_ok:
        risk_warnings.append(f"环境自检异常: {', '.join(health_failed)}")
    if not watchdog_ok:
        risk_warnings.append(f"看门狗监测报警: {', '.join(watchdog_criticals)}")
    if crash_open:
        risk_warnings.append("系统未决崩溃(Crash)报警！")
    if control_paused:
        risk_warnings.append(f"交易挂起暂停: {control_reason}")

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
            "initial_capital": initial_capital,
            "total_assets": total_assets,
            "cash": cash,
            "positions": positions,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct
        },
        "adaptive": adaptive_params,
        "crash_open": crash_open,
        "pipeline_progress": pipeline_progress,
        "recent_logs": recent_logs[:20],
        "risk_warnings": risk_warnings,
        "loop_count": loop_count,
        "last_loop_time": last_loop_time
    }

