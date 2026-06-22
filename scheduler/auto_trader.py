"""
模拟盘自动盯盘交易员
====================================================================
按市场状态自动执行：
  - 盘前：数据预热、市场环境识别
  - 盘中：持仓止损巡检、定时扫描、生成TradePlan并执行模拟交易
  - 盘后：每日复盘、教训提取、prompt进化、自适应参数更新

该模块只通过 BrokerAdapter 交易，默认 BROKER_MODE=paper。
====================================================================
"""
import json
import os
import time
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Dict, Optional, Callable, Any
from scheduler.market_calendar import _now_bj

from config import (
    AUTO_LOOP_INTERVAL,
    AUTO_RESCUE_SCAN_INTERVAL,
    AUTO_SCAN_INTERVAL,
    AUTO_STOP_INTERVAL,
    AUTO_WATCH_INTERVAL,
    AUTO_REVIEW_AFTER,
    AUTO_NOTIFY_ENABLED,
    DATA_DIR,
)
from scheduler.market_calendar import get_market_status, is_trading_day

logger = logging.getLogger("scheduler.auto_trader")

AUTO_STATE_FILE = os.path.join(DATA_DIR, "auto_trader_state.json")
AUTO_LOCK_FILE = os.path.join(DATA_DIR, "auto_trader.lock")


@dataclass
class AutoTraderState:
    """自动盯盘状态"""
    date: str = ""
    last_prefetch_date: str = ""
    last_regime_date: str = ""
    last_scan_at: float = 0.0
    last_execute_at: float = 0.0
    last_stop_check_at: float = 0.0
    last_watch_at: float = 0.0
    last_rescue_scan_at: float = 0.0
    last_review_date: str = ""
    morning_baseline: dict = field(default_factory=dict)
    watchlist_count: int = 0
    missed_opportunity_count: int = 0
    rescue_scan_count: int = 0
    loop_count: int = 0
    last_status: str = ""
    last_error: str = ""
    updated_at: str = ""


class AutoLoopLockError(RuntimeError):
    """自动盯盘循环已在运行"""


class AutoLoopLock:
    """自动盯盘单实例锁，防止多个--auto常驻循环并行运行"""

    def __init__(self, lock_file: str = None, stale_after: int = None):
        self.lock_file = lock_file or AUTO_LOCK_FILE
        self.stale_after = stale_after or max(600, AUTO_LOOP_INTERVAL * 5)
        self.token = f"{os.getpid()}-{time.time():.6f}"
        self.acquired = False

    def _payload(self) -> dict:
        return {
            "pid": os.getpid(),
            "token": self.token,
            "updated_at": _now_bj().isoformat(),
        }

    def _read_payload(self) -> dict:
        try:
            with open(self.lock_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _is_stale(self) -> bool:
        try:
            age = time.time() - os.path.getmtime(self.lock_file)
            return age > self.stale_after
        except OSError:
            return True

    def _write_payload_fd(self, fd):
        data = json.dumps(self._payload(), ensure_ascii=False, indent=2).encode("utf-8")
        os.write(fd, data)

    def acquire(self):
        os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)
        while True:
            try:
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    self._write_payload_fd(fd)
                finally:
                    os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                payload = self._read_payload()
                if self._is_stale():
                    try:
                        os.remove(self.lock_file)
                        logger.warning(f"自动盯盘锁已过期，回收: {payload}")
                        continue
                    except OSError:
                        pass
                raise AutoLoopLockError(
                    f"自动盯盘循环可能已在运行 lock={self.lock_file} pid={payload.get('pid')}"
                )

    def heartbeat(self):
        if not self.acquired:
            return
        tmp_file = self.lock_file + ".heartbeat"
        try:
            # 先写入临时文件，避免写入过程中锁文件处于不完整状态
            data = json.dumps(self._payload(), ensure_ascii=False, indent=2)
            fd = os.open(tmp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, data.encode("utf-8"))
            finally:
                os.close(fd)
            # 原子替换锁文件（POSIX rename保证原子性）
            os.replace(tmp_file, self.lock_file)
            # 替换后验证token，检测是否被其他进程抢占
            payload = self._read_payload()
            if payload.get("token") != self.token:
                logger.warning("自动盯盘锁已被其他进程接管，停止刷新心跳")
                self.acquired = False
        except Exception as e:
            logger.warning(f"自动盯盘锁心跳失败: {e}")
        finally:
            try:
                os.remove(tmp_file)
            except OSError:
                pass

    def release(self):
        if not self.acquired:
            return
        try:
            payload = self._read_payload()
            if payload.get("token") == self.token and os.path.exists(self.lock_file):
                os.remove(self.lock_file)
        except Exception as e:
            logger.warning(f"自动盯盘锁释放失败: {e}")
        finally:
            self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False


def _today() -> str:
    return _now_bj().strftime("%Y-%m-%d")


def _load_state(state_file: str = None) -> AutoTraderState:
    state_file = state_file or AUTO_STATE_FILE
    if not os.path.exists(state_file):
        return AutoTraderState(date=_today())
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        values = {}
        defaults = AutoTraderState(date=_today())
        for k in AutoTraderState.__dataclass_fields__:
            value = data.get(k, getattr(defaults, k))
            if value is None:
                value = getattr(defaults, k)
            values[k] = value
        return AutoTraderState(**values)
    except Exception as e:
        logger.warning(f"自动盯盘状态读取失败，重新初始化: {e}")
        return AutoTraderState(date=_today())


def _save_state(state: AutoTraderState, state_file: str = None):
    state_file = state_file or AUTO_STATE_FILE
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    state.updated_at = _now_bj().isoformat()
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, ensure_ascii=False, indent=2)


def _parse_hhmm(value: str) -> tuple:
    try:
        hh, mm = value.split(":", 1)
        return int(hh), int(mm)
    except Exception:
        return 15, 5


def _after_review_time(now: datetime = None) -> bool:
    hh, mm = _parse_hhmm(AUTO_REVIEW_AFTER)
    now = now or _now_bj()
    return (now.hour, now.minute) >= (hh, mm)


def _format_cycle_report(status: str, actions: list, state: AutoTraderState,
                         today: str = None, now: datetime = None) -> str:
    today = today or _today()
    now = now or _now_bj()
    lines = [
        "=" * 60,
        f"自动盯盘循环 | {today} {now.strftime('%H:%M:%S')}",
        "=" * 60,
        f"市场状态: {status}",
        f"循环次数: {state.loop_count}",
    ]
    if actions:
        lines.append("动作:")
        for action in actions:
            lines.append(f"  - {action}")
    else:
        lines.append("动作: 本轮无需操作")
    if state.last_error:
        lines.append(f"最近错误: {state.last_error}")
    lines.append("=" * 60)
    return "\n".join(lines)


def check_stops_once() -> dict:
    """
    执行一次持仓止损巡检

    Returns:
        {"checked": N, "sold": M, "trades": [...]}
    """
    from execution.broker import get_broker_adapter
    from data.realtime import get_realtime

    broker = get_broker_adapter()
    positions = broker.get_positions()
    if not positions:
        return {"checked": 0, "sold": 0, "trades": []}

    prices = {}
    for code in positions:
        try:
            quotes = get_realtime([code])
            if quotes and quotes[0].price > 0:
                prices[code] = quotes[0].price
        except Exception as e:
            logger.debug(f"止损巡检行情失败 {code}: {e}")

    if not prices:
        return {"checked": len(positions), "sold": 0, "trades": [], "error": "无法获取持仓行情"}

    trades = broker.check_stop_conditions(prices)
    return {"checked": len(positions), "sold": len(trades), "trades": trades}


def _service(services: Optional[Dict[str, Callable]], name: str, default: Callable) -> Callable:
    """获取可替换服务函数，便于模拟盘链路做可控验证"""
    if services and name in services:
        return services[name]
    return default


def _record_auto_event(db_path: str, event: dict):
    """写入自动盯盘事件，失败不影响主循环。"""
    try:
        from data.database import Database
        with Database(db_path=db_path) as db:
            db.insert_auto_event(event)
    except Exception as e:
        logger.warning(f"自动盯盘事件写入失败(非致命): {e}")


def _run_rescue_scan_default(watch_result: dict):
    """
    默认救援扫描：复用快链路生成计划，但只执行二次确认观察池里的BUY。
    """
    from scheduler.pipeline import run_scan, execute_trade_plan
    from scheduler.intraday_watch import filter_trade_plan_for_rescue

    eligible_codes = watch_result.get("eligible_codes", [])
    top_watch = (watch_result.get("details") or {}).get("top_watch", [])
    rescue_items = [
        item for item in top_watch
        if item.get("code") in set(eligible_codes)
    ]
    scan_result = run_scan(
        budget_seconds=180,
        candidate_codes=eligible_codes,
        candidate_items=rescue_items,
    )
    plan_data = getattr(scan_result, "trade_plan", {}) or {}
    filtered_plan = filter_trade_plan_for_rescue(plan_data, eligible_codes)
    exec_result = execute_trade_plan(filtered_plan) if filtered_plan else None
    return {
        "scan_result": scan_result,
        "exec_result": exec_result,
        "eligible_codes": eligible_codes,
        "filtered_orders": len((filtered_plan or {}).get("orders", [])),
    }


def run_auto_cycle(
    state: Optional[AutoTraderState] = None,
    force_scan: bool = False,
    *,
    scan_interval: int = None,
    status_override: str = None,
    trading_day_override: bool = None,
    today_override: str = None,
    now_override: datetime = None,
    now_ts_override: float = None,
    after_review_time_override: bool = None,
    services: Optional[Dict[str, Callable[..., Any]]] = None,
    persist_state: bool = True,
    record_event: bool = True,
    db_path: str = None,
    state_file: str = None,
    control_file: str = None,
    notify: bool = None,
) -> dict:
    """
    执行一轮自动盯盘逻辑

    Args:
        state: 外部传入状态，None则自动读取
        force_scan: 忽略扫描间隔，强制盘中扫描
        scan_interval: 盘中扫描间隔秒数，None时使用配置默认值
        status_override: 覆盖市场状态，仅用于测试/演练
        trading_day_override: 覆盖交易日判断，仅用于测试/演练
        today_override: 覆盖当前日期，仅用于测试/演练
        now_override: 覆盖当前时间，仅用于测试/演练
        now_ts_override: 覆盖时间戳，仅用于测试/演练
        after_review_time_override: 覆盖盘后复盘窗口判断，仅用于测试/演练
        services: 覆盖外部服务函数，便于测试隔离真实API
        persist_state: 是否写入状态文件
        record_event: 是否写入auto_events
        db_path: auto_events写入的SQLite路径，None时使用默认数据库
        state_file: 状态文件路径，None时使用默认data/auto_trader_state.json
        control_file: 控制状态文件路径，None时使用默认data/auto_control.json
        notify: 是否发送关键动作通知，None时读取配置

    Returns:
        本轮摘要
    """
    from scheduler.pipeline import prefetch, run_scan, execute_trades, run_review
    from scheduler.control import get_auto_control_state
    from strategy.market_regime import detect_regime

    state = state or _load_state(state_file)
    now_dt = now_override or _now_bj()
    today = today_override or now_dt.strftime("%Y-%m-%d")
    if state.date != today:
        state = AutoTraderState(date=today)

    status = status_override or get_market_status()
    is_today_trading = trading_day_override if trading_day_override is not None else is_trading_day(today)
    state.last_status = status
    state.loop_count += 1
    actions = []
    now_ts = now_ts_override if now_ts_override is not None else time.time()
    exec_result = None
    control_state = get_auto_control_state(control_file=control_file)
    paused = bool(control_state.get("paused"))

    prefetch_func = _service(services, "prefetch", prefetch)
    detect_regime_func = _service(services, "detect_regime", detect_regime)
    check_stops_func = _service(services, "check_stops_once", check_stops_once)
    run_scan_func = _service(services, "run_scan", run_scan)
    execute_trades_func = _service(services, "execute_trades", execute_trades)
    run_review_func = _service(services, "run_review", run_review)
    from scheduler.intraday_watch import run_watch_cycle
    run_watch_func = _service(services, "run_watch_cycle", run_watch_cycle)
    run_rescue_scan_func = _service(services, "run_rescue_scan", _run_rescue_scan_default)
    notify_func = _service(services, "send_auto_cycle_report", None)
    notify_enabled = AUTO_NOTIFY_ENABLED if notify is None else notify
    after_review_time = (
        after_review_time_override
        if after_review_time_override is not None
        else _after_review_time(now_dt)
    )
    extra_events = []
    rescue_ran = False

    try:
        if not is_today_trading:
            actions.append("休市: 跳过交易动作")

        elif status in ("盘前", "集合竞价"):
            if state.last_prefetch_date != today:
                prefetch_func()
                state.last_prefetch_date = today
                actions.append("盘前数据预热完成")
            if state.last_regime_date != today:
                regime = detect_regime_func()
                state.last_regime_date = today
                actions.append(f"市场环境识别: {regime.regime} conf={regime.confidence:.0%}")

        elif status == "盘中" and paused:
            actions.append(f"自动盯盘已暂停: {control_state.get('reason', '')}")
            actions.append("盘中交易动作跳过: 止损巡检/扫描/模拟执行")

        elif status == "盘中":
            if now_ts - state.last_stop_check_at >= AUTO_STOP_INTERVAL:
                stop_result = check_stops_func()
                state.last_stop_check_at = now_ts
                actions.append(
                    f"止损巡检: 持仓{stop_result.get('checked', 0)}只 "
                    f"卖出{stop_result.get('sold', 0)}笔"
                )

            watch_result = None
            if now_ts - state.last_watch_at >= AUTO_WATCH_INTERVAL:
                watch_result = run_watch_func(
                    state=state,
                    now=now_dt,
                    now_ts=now_ts,
                    services=services,
                )
                state.last_watch_at = now_ts
                watchlist_items = (watch_result.get("watchlist") or {}).get("items") or {}
                state.watchlist_count = len(watchlist_items)
                if watch_result.get("missed_opportunity"):
                    state.missed_opportunity_count += 1
                actions.extend(watch_result.get("actions", []))
                extra_events.append({
                    "date": today,
                    "event_type": "watch_cycle",
                    "status": status,
                    "actions": watch_result.get("actions", []),
                    "details": watch_result.get("details", {}),
                    "error": "",
                })
                extra_events.append({
                    "date": today,
                    "event_type": "watchlist_update",
                    "status": status,
                    "actions": [f"观察池刷新: {state.watchlist_count}只"],
                    "details": {
                        "watchlist_count": state.watchlist_count,
                        "top_watch": (watch_result.get("details") or {}).get("top_watch", []),
                    },
                    "error": "",
                })
                if watch_result.get("missed_opportunity"):
                    extra_events.append({
                        "date": today,
                        "event_type": "missed_opportunity",
                        "status": status,
                        "actions": ["疑似踏空: 已进入观察/救援确认流程"],
                        "details": watch_result.get("details", {}),
                        "error": "",
                    })

            if (
                watch_result
                and watch_result.get("rescue_requested")
                and now_ts - state.last_rescue_scan_at >= AUTO_RESCUE_SCAN_INTERVAL
            ):
                rescue = run_rescue_scan_func(watch_result)
                rescue_ran = True
                state.last_rescue_scan_at = now_ts
                state.last_scan_at = now_ts
                state.rescue_scan_count += 1
                scan_result = rescue.get("scan_result") if isinstance(rescue, dict) else None
                exec_result = rescue.get("exec_result") if isinstance(rescue, dict) else None
                if exec_result is not None:
                    state.last_execute_at = now_ts
                executed_count = len(getattr(exec_result, "executed_orders", []) or []) if exec_result else 0
                actions.append(
                    f"救援扫描: 观察确认{len(watch_result.get('eligible_codes', []))}只 "
                    f"成交{executed_count}笔"
                )
                extra_events.append({
                    "date": today,
                    "event_type": "rescue_scan",
                    "status": status,
                    "actions": [actions[-1]],
                    "details": {
                        "eligible_codes": watch_result.get("eligible_codes", []),
                        "filtered_orders": rescue.get("filtered_orders") if isinstance(rescue, dict) else None,
                        "candidates": len(getattr(scan_result, "candidates", []) or []) if scan_result else 0,
                        "executed_orders": executed_count,
                        "errors": getattr(exec_result, "errors", []) if exec_result else [],
                    },
                    "error": "",
                })

            effective_scan_interval = scan_interval if scan_interval is not None else AUTO_SCAN_INTERVAL
            should_scan = force_scan or (now_ts - state.last_scan_at >= effective_scan_interval)
            if should_scan and not rescue_ran:
                scan_result = run_scan_func()
                state.last_scan_at = now_ts
                actions.append(f"盘中扫描: 候选{len(scan_result.candidates)}只 决策{len(scan_result.decisions)}条")

                exec_result = execute_trades_func()
                state.last_execute_at = now_ts if now_ts_override is not None else time.time()
                actions.append(
                    f"模拟执行: 成交{len(exec_result.executed_orders)}笔 "
                    f"风控{len(exec_result.risk_triggered)}项 错误{len(exec_result.errors)}项"
                )
                if getattr(exec_result, "order_audit", None):
                    audit = exec_result.order_audit
                    blocked = sum(1 for item in audit if item.get("status") in ("blocked", "skipped"))
                    failed = sum(1 for item in audit if item.get("status") == "failed")
                    actions.append(
                        f"执行审计: 计划{len(audit)}笔 阻断/跳过{blocked}笔 失败{failed}笔"
                    )

        elif status in ("午休",):
            actions.append("午休: 保持监控状态")

        else:
            if after_review_time and state.last_review_date != today:
                review_result = run_review_func()
                state.last_review_date = today
                actions.append(
                    f"盘后复盘进化: 步骤{len(review_result.steps)}项 "
                    f"错误{len(review_result.errors)}项"
                )
            else:
                actions.append("盘后: 等待复盘窗口或今日已复盘")

        state.last_error = ""
    except Exception as e:
        state.last_error = str(e)
        logger.error(f"自动盯盘循环异常: {e}", exc_info=True)
        actions.append(f"异常: {e}")
    finally:
        if persist_state:
            _save_state(state, state_file)

    report = _format_cycle_report(status, actions, state, today=today, now=now_dt)
    if record_event:
        for event in extra_events:
            _record_auto_event(db_path, event)
        _record_auto_event(db_path, {
            "date": today,
            "event_type": "auto_cycle",
            "status": status,
            "actions": actions,
            "details": {
                "loop_count": state.loop_count,
                "force_scan": force_scan,
                "paused": paused,
                "pause_reason": control_state.get("reason", ""),
                "last_prefetch_date": state.last_prefetch_date,
                "last_regime_date": state.last_regime_date,
                "last_review_date": state.last_review_date,
                "last_watch_at": state.last_watch_at,
                "last_rescue_scan_at": state.last_rescue_scan_at,
                "watchlist_count": state.watchlist_count,
                "missed_opportunity_count": state.missed_opportunity_count,
                "rescue_scan_count": state.rescue_scan_count,
                "order_audit": getattr(exec_result, "order_audit", []),
            },
            "error": state.last_error,
        })

    notified = False
    if notify_enabled:
        try:
            if notify_func is None:
                from scheduler.notifier import send_auto_cycle_report
                notify_func = send_auto_cycle_report
            notified = bool(notify_func(
                date=today,
                status=status,
                actions=actions,
                loop_count=state.loop_count,
                error=state.last_error,
            ))
        except Exception as e:
            logger.warning(f"自动盯盘通知失败(非致命): {e}")

    logger.info("\n" + report)
    return {
        "date": today,
        "status": status,
        "actions": actions,
        "state": asdict(state),
        "report": report,
        "notified": notified,
    }


def run_auto_loop(loop_interval: int = None, scan_interval: int = None,
                  once: bool = False, force_scan: bool = False,
                  lock_file: str = None):
    """
    启动自动盯盘循环

    Args:
        loop_interval: 主循环间隔秒数
        scan_interval: 盘中扫描间隔秒数
        once: True=只跑一轮，便于测试
        force_scan: True=盘中强制扫描
        lock_file: 单实例锁文件路径，None时使用默认data/auto_trader.lock
    """
    interval = loop_interval or AUTO_LOOP_INTERVAL
    state = _load_state()

    loop_lock = None
    if not once:
        loop_lock = AutoLoopLock(lock_file=lock_file).acquire()
        logger.info(f"自动盯盘锁已获取: {loop_lock.lock_file}")

    try:
        while True:
            if loop_lock:
                loop_lock.heartbeat()
            result = run_auto_cycle(state=state, force_scan=force_scan, scan_interval=scan_interval)
            print(result["report"])
            state = AutoTraderState(**result["state"])
            if once:
                return result
            if loop_lock:
                loop_lock.heartbeat()
            time.sleep(max(5, interval))
    finally:
        if loop_lock:
            loop_lock.release()
