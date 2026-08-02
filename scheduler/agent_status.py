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
    total_assets = 0.0
    cash = 0.0
    initial_capital = 0.0
    total_pnl = 0.0
    total_pnl_pct = 0.0
    account_available = False
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
        account_available = True
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

    # 6. 读取当前生效的日终 AI 策略指令。它是次日扫描实际优先使用的
    # 策略版本，不能再只把旧自适应参数当作唯一策略状态展示。
    strategy_directive = None
    pending_strategy_directive = None
    try:
        from strategy.directive import get_effective_trade_policy
        from data.database import Database
        today = datetime.now().strftime("%Y-%m-%d")
        strategy_directive = get_effective_trade_policy(
            today,
            (adaptive_params or {}).get("regime", "sideways"),
        )
        with Database() as db:
            pending_strategy_directive = db.get_next_strategy_directive(today)
    except Exception:
        pass

    # 7. 检测未决 Crash 状态
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

    # 8. 读取自动盯盘状态文件中的进度与循环计数
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

    # 9. 读取最近系统运行日志事件
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

    # 10. 构建驾驶舱风险报警列表
    risk_warnings = []
    if not health_ok:
        risk_warnings.append(f"环境自检异常: {', '.join(health_failed)}")
    if not watchdog_ok:
        risk_warnings.append(f"看门狗监测报警: {', '.join(watchdog_criticals)}")
    if crash_open:
        risk_warnings.append("系统未决崩溃(Crash)报警！")
    if control_paused:
        risk_warnings.append(f"交易挂起暂停: {control_reason}")
    if not account_available:
        risk_warnings.append("模拟账户状态不可用")

    # 11. 统一每日交易员简报与能力健康。服务存活不等于交易能力完整，
    # 信号维度、LLM、执行和复盘必须分别给出状态。
    daily_trader = {}
    capabilities = []
    try:
        from scheduler.trader_brief import build_daily_facts
        daily_trader = build_daily_facts()
        if control_paused:
            daily_trader.update({
                "state": "paused",
                "headline": "AI 交易员当前处于暂停状态",
                "explanation": control_reason or "交易控制阀门已暂停。",
                "next_action": "等待暂停原因解除后继续自动循环。",
            })
        elif not watchdog_ok or crash_open:
            daily_trader.update({
                "state": "attention",
                "headline": "AI 交易员需要运行维护",
                "explanation": "守护检查发现影响自动循环的问题。",
                "next_action": "Doctor 将优先诊断并恢复交易闭环。",
            })

        funnel = daily_trader.get("funnel") or {}
        degradations = daily_trader.get("degradations") or []
        capabilities = [
            {
                "key": "scheduler",
                "label": "自动循环",
                "status": "healthy" if watchdog_ok and not crash_open else "degraded",
                "summary": "循环与守护正常" if watchdog_ok and not crash_open else "自动循环需要维护",
            },
            {
                "key": "market_data",
                "label": "行情数据",
                "status": "healthy" if health_ok else "degraded",
                "summary": "基础数据源可用" if health_ok else "基础数据源存在异常",
            },
            {
                "key": "signals",
                "label": "信号计算",
                "status": "degraded" if degradations else "healthy",
                "summary": "；".join(item.get("summary", "") for item in degradations) if degradations else "当前信号维度未发现降级",
            },
            {
                "key": "llm",
                "label": "LLM 判断",
                "status": (
                    "degraded" if daily_trader.get("llm_no_response_count", 0)
                    else "healthy" if funnel.get("llm_evaluated", 0)
                    else "idle"
                ),
                "summary": (
                    f"{daily_trader.get('llm_no_response_count', 0)} 次判断未获得有效响应"
                    if daily_trader.get("llm_no_response_count", 0)
                    else f"今日完成 {funnel.get('llm_evaluated', 0)} 次有效判断"
                    if funnel.get("llm_evaluated", 0)
                    else "当前阶段尚未调用 LLM"
                ),
            },
            {
                "key": "execution",
                "label": "计划执行",
                "status": "degraded" if funnel.get("failed", 0) else "healthy" if funnel.get("planned_orders", 0) else "idle",
                "summary": (
                    f"今日 {funnel.get('failed', 0)} 笔执行失败"
                    if funnel.get("failed", 0)
                    else f"计划 {funnel.get('planned_orders', 0)} 笔，成交 {funnel.get('filled', 0)} 笔"
                    if funnel.get("planned_orders", 0)
                    else "今日尚无待执行计划"
                ),
            },
            {
                "key": "review",
                "label": "日终复盘",
                "status": "healthy" if daily_trader.get("reviewed") else "pending",
                "summary": "今日复盘已保存" if daily_trader.get("reviewed") else "等待收盘后的复盘窗口",
            },
        ]
        for item in degradations:
            risk_warnings.append(item.get("summary", "信号能力降级"))
        if funnel.get("failed", 0):
            risk_warnings.append(f"今日有{funnel.get('failed')}笔计划执行失败")
    except Exception as e:
        daily_trader = {
            "state": "attention",
            "headline": "每日交易员简报暂不可用",
            "explanation": str(e),
            "next_action": "自动循环不受影响，等待下一次状态刷新。",
        }

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
            "available": account_available,
            "initial_capital": initial_capital,
            "total_assets": total_assets,
            "cash": cash,
            "positions": positions,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct
        },
        "adaptive": adaptive_params,
        "strategy_directive": strategy_directive,
        "pending_strategy_directive": pending_strategy_directive,
        "crash_open": crash_open,
        "pipeline_progress": pipeline_progress,
        "recent_logs": recent_logs[:20],
        "risk_warnings": risk_warnings,
        "daily_trader": daily_trader,
        "capabilities": capabilities,
        "loop_count": loop_count,
        "last_loop_time": last_loop_time
    }

