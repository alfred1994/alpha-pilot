"""
AI交易员试运行报告
====================================================================
聚合最近N天模拟盘自动盯盘记录，检查系统是否形成：
  - 自动盯盘：盘前/盘中/盘后循环是否运行
  - 自动交易：是否产生LLM决策和模拟成交
  - 自我复盘：是否保存复盘快照和交易教训
  - 纠正进化：自适应参数是否被更新并影响下一轮交易

报告只读取SQLite和本地文件，不触发真实行情、LLM或交易接口。
====================================================================
"""
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from config import DATA_DIR
from data.database import Database
from scheduler.control import get_auto_control_state


REPORT_DIR = os.path.join(DATA_DIR, "reports")


def _parse_json(value, default):
    """解析SQLite里存储的JSON字段，失败时返回默认值"""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _date_only(value: str) -> str:
    """从ISO时间或日期字符串中提取日期"""
    if not value:
        return ""
    return value[:10]


def _calc_range(days: int, end_date: str = None) -> tuple:
    """计算报告日期范围"""
    days = max(1, int(days or 1))
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()
    start = end - timedelta(days=days - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _query_range(db: Database, table: str, date_column: str,
                 start_date: str, end_date: str, limit: int = 1000) -> List[Dict]:
    """按日期范围读取表数据"""
    c = db.conn.cursor()
    c.execute(f"""
        SELECT * FROM {table}
        WHERE {date_column} >= ? AND {date_column} <= ?
        ORDER BY {date_column} ASC, created_at ASC
        LIMIT ?
    """, (start_date, end_date, limit))
    return [dict(row) for row in c.fetchall()]


def _load_review_snapshots(db: Database, start_date: str, end_date: str) -> List[Dict]:
    """读取复盘快照并解析JSON内容"""
    snapshots = _query_range(db, "review_snapshots", "date", start_date, end_date)
    for item in snapshots:
        item["data"] = _parse_json(item.get("data"), {})
    return snapshots


def _normalise_auto_events(events: List[Dict]) -> List[Dict]:
    """解析自动盯盘事件里的动作和详情"""
    result = []
    for event in events:
        item = dict(event)
        item["actions"] = _parse_json(item.get("actions"), [])
        item["details"] = _parse_json(item.get("details"), {})
        result.append(item)
    return result


def _group_by_date(items: List[Dict], date_field: str) -> Dict[str, List[Dict]]:
    """按日期聚合记录"""
    grouped: Dict[str, List[Dict]] = {}
    for item in items:
        date = _date_only(item.get(date_field, ""))
        if not date:
            continue
        grouped.setdefault(date, []).append(item)
    return grouped


def _summarise_actions(events: List[Dict]) -> Dict:
    """统计自动循环动作覆盖"""
    all_actions = []
    order_audit = []
    paused_count = 0
    for event in events:
        actions = event.get("actions", [])
        all_actions.extend(actions)
        details = event.get("details") or {}
        order_audit.extend(details.get("order_audit") or [])
        if details.get("paused") or any(
            "自动盯盘已暂停" in action or "交易动作跳过" in action
            for action in actions
        ):
            paused_count += 1

    def count_contains(text: str) -> int:
        return sum(1 for action in all_actions if text in action)

    return {
        "actions": all_actions,
        "prefetch_count": count_contains("盘前数据预热"),
        "regime_count": count_contains("市场环境识别"),
        "scan_count": count_contains("盘中扫描"),
        "execute_count": count_contains("模拟执行"),
        "stop_check_count": count_contains("止损巡检"),
        "review_count": count_contains("盘后复盘进化"),
        "idle_count": count_contains("等待复盘窗口"),
        "paused_count": paused_count,
        "order_audit": order_audit,
        "order_audit_count": len(order_audit),
        "order_filled_count": sum(1 for item in order_audit if item.get("status") == "filled"),
        "order_blocked_count": sum(1 for item in order_audit if item.get("status") in ("blocked", "skipped")),
        "order_failed_count": sum(1 for item in order_audit if item.get("status") == "failed"),
        "holiday_count": sum(
            1 for event in events
            if event.get("status") == "休市"
            or any("休市" in action for action in event.get("actions", []))
        ),
        "error_count": sum(1 for event in events if event.get("error")),
    }


def _diagnose_daily_row(statuses: List[str], action_summary: Dict,
                        has_market_loop: bool, has_decision_or_trade: bool,
                        has_learning: bool, closed_loop: bool) -> str:
    """给每日闭环结果打诊断标签"""
    if action_summary["error_count"]:
        return "异常"
    if not has_market_loop:
        return "无循环"
    if action_summary["holiday_count"] and set(statuses or ["休市"]) <= {"休市"}:
        return "休市"
    if action_summary["paused_count"] and action_summary["scan_count"] == 0:
        return "暂停"
    if closed_loop:
        return "闭环"
    if "盘中" in statuses and action_summary["scan_count"] == 0:
        return "缺扫描"
    if action_summary["scan_count"] > 0 and action_summary["execute_count"] == 0:
        return "缺执行"
    if has_decision_or_trade and not has_learning:
        return "缺复盘"
    if action_summary["scan_count"] > 0 and not has_decision_or_trade:
        return "无可交易决策"
    if has_learning and not has_decision_or_trade:
        return "仅复盘"
    if action_summary["idle_count"]:
        return "等待复盘"
    return "观察中"


def _build_daily_rows(dates: List[str], events: List[Dict], trades: List[Dict],
                      decisions: List[Dict], lessons: List[Dict],
                      reviews: List[Dict]) -> List[Dict]:
    """构造每日闭环明细"""
    events_by_date = _group_by_date(events, "date")
    trades_by_date = _group_by_date(trades, "created_at")
    decisions_by_date = _group_by_date(decisions, "date")
    lessons_by_date = _group_by_date(lessons, "date")
    reviews_by_date = _group_by_date(reviews, "date")

    rows = []
    for date in dates:
        day_events = events_by_date.get(date, [])
        action_summary = _summarise_actions(day_events)
        day_trades = trades_by_date.get(date, [])
        day_decisions = decisions_by_date.get(date, [])
        day_lessons = lessons_by_date.get(date, [])
        day_reviews = reviews_by_date.get(date, [])
        has_market_loop = bool(day_events)
        has_decision_or_trade = bool(day_decisions or day_trades)
        has_learning = bool(day_reviews or day_lessons or action_summary["review_count"])
        closed_loop = has_market_loop and has_decision_or_trade and has_learning
        statuses = sorted({e.get("status", "") for e in day_events if e.get("status")})
        diagnosis = _diagnose_daily_row(
            statuses,
            action_summary,
            has_market_loop,
            has_decision_or_trade,
            has_learning,
            closed_loop,
        )

        rows.append({
            "date": date,
            "cycle_count": len(day_events),
            "statuses": statuses,
            "scan_count": action_summary["scan_count"],
            "execute_count": action_summary["execute_count"],
            "stop_check_count": action_summary["stop_check_count"],
            "review_count": len(day_reviews) or action_summary["review_count"],
            "paused_count": action_summary["paused_count"],
            "order_audit_count": action_summary["order_audit_count"],
            "order_filled_count": action_summary["order_filled_count"],
            "order_blocked_count": action_summary["order_blocked_count"],
            "order_failed_count": action_summary["order_failed_count"],
            "holiday_count": action_summary["holiday_count"],
            "trade_count": len(day_trades),
            "buy_count": sum(1 for t in day_trades if t.get("action") == "BUY"),
            "sell_count": sum(1 for t in day_trades if t.get("action") == "SELL"),
            "decision_count": len(day_decisions),
            "lesson_count": len(day_lessons),
            "error_count": action_summary["error_count"],
            "diagnosis": diagnosis,
            "closed_loop": closed_loop,
        })
    return rows


def _summarise_adaptive_state(state: Optional[Dict]) -> Dict:
    """提取自适应状态里的关键参数"""
    if not state:
        return {
            "available": False,
            "current_buy_threshold": None,
            "current_min_score": None,
            "current_top_k_delta": None,
            "current_position_scale": None,
            "adjustment_count": 0,
            "latest_adjustments": [],
        }

    history = state.get("adjustments", [])
    latest = history[-1] if history else {}
    return {
        "available": True,
        "last_update": state.get("last_update"),
        "current_buy_threshold": state.get("current_buy_threshold"),
        "current_min_score": state.get("current_min_score"),
        "current_top_k_delta": state.get("current_top_k_delta", 0),
        "current_position_scale": state.get("current_position_scale", 1.0),
        "adjustment_count": len(history),
        "latest_adjustments": latest.get("adjustments", []),
    }


def _load_latest_regime(db: Database, end_date: str) -> Dict:
    """读取报告截止日前最近一次市场环境"""
    try:
        row = db.conn.execute("""
            SELECT * FROM market_regimes
            WHERE date <= ?
            ORDER BY date DESC
            LIMIT 1
        """, (end_date,)).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


def _load_active_prompt_hints(db: Database, limit: int = 3) -> List[Dict]:
    """读取当前生效的prompt进化建议"""
    try:
        rows = db.conn.execute("""
            SELECT id, hint, source, created_at
            FROM active_prompt_hints
            WHERE active = 1
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _summarise_next_trade_params(adaptive_state: Optional[Dict],
                                 latest_regime: Dict) -> Dict:
    """计算自适应状态对下一轮交易参数的实际影响"""
    from strategy.regime_config import get_trade_params

    current_regime = latest_regime.get("regime") or "sideways"
    regimes = ["bull", "sideways", "rebound", "bear"]
    by_regime = {}
    for regime in regimes:
        by_regime[regime] = get_trade_params(regime, adaptive_state=adaptive_state or {})

    return {
        "current_regime": current_regime,
        "current": by_regime.get(current_regime, by_regime["sideways"]),
        "by_regime": by_regime,
    }


def _summarise_metrics(daily_rows: List[Dict], events: List[Dict],
                       trades: List[Dict], decisions: List[Dict],
                       lessons: List[Dict], reviews: List[Dict]) -> Dict:
    """生成报告指标"""
    buy_count = sum(1 for trade in trades if trade.get("action") == "BUY")
    sell_count = sum(1 for trade in trades if trade.get("action") == "SELL")
    llm_buy_sell = sum(1 for d in decisions if d.get("action") in ("BUY", "SELL"))
    outcome_decisions = [d for d in decisions if d.get("outcome")]
    wins = sum(1 for d in outcome_decisions if d.get("outcome") == "win")

    return {
        "days": len(daily_rows),
        "auto_cycle_count": len(events),
        "active_days": sum(1 for row in daily_rows if row["cycle_count"] > 0),
        "closed_loop_days": sum(1 for row in daily_rows if row["closed_loop"]),
        "paused_days": sum(1 for row in daily_rows if row["diagnosis"] == "暂停"),
        "holiday_days": sum(1 for row in daily_rows if row["diagnosis"] == "休市"),
        "missing_scan_days": sum(1 for row in daily_rows if row["diagnosis"] == "缺扫描"),
        "missing_execute_days": sum(1 for row in daily_rows if row["diagnosis"] == "缺执行"),
        "missing_review_days": sum(1 for row in daily_rows if row["diagnosis"] == "缺复盘"),
        "no_trade_signal_days": sum(1 for row in daily_rows if row["diagnosis"] == "无可交易决策"),
        "no_loop_days": sum(1 for row in daily_rows if row["diagnosis"] == "无循环"),
        "review_only_days": sum(1 for row in daily_rows if row["diagnosis"] == "仅复盘"),
        "error_events": sum(1 for event in events if event.get("error")),
        "scan_count": sum(row["scan_count"] for row in daily_rows),
        "execute_count": sum(row["execute_count"] for row in daily_rows),
        "order_audit_count": sum(row["order_audit_count"] for row in daily_rows),
        "order_filled_count": sum(row["order_filled_count"] for row in daily_rows),
        "order_blocked_count": sum(row["order_blocked_count"] for row in daily_rows),
        "order_failed_count": sum(row["order_failed_count"] for row in daily_rows),
        "review_days": len(reviews),
        "trade_count": len(trades),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "llm_decision_count": len(decisions),
        "llm_actionable_count": llm_buy_sell,
        "llm_outcome_count": len(outcome_decisions),
        "llm_win_rate": wins / len(outcome_decisions) if outcome_decisions else None,
        "lesson_count": len(lessons),
    }


def format_ai_trader_report(report: Dict) -> str:
    """格式化AI交易员试运行报告"""
    metrics = report["metrics"]
    adaptive = report["adaptive"]
    account = report.get("account") or {}
    control = report.get("control") or {}
    latest_regime = report.get("latest_regime") or {}
    next_params = report.get("next_trade_params") or {}
    prompt_hints = report.get("prompt_hints") or []
    control_status = "暂停" if control.get("paused") else "运行"
    current_params = next_params.get("current") or {}
    current_regime = next_params.get("current_regime") or latest_regime.get("regime") or "sideways"

    lines = [
        "# AI交易员模拟盘试运行报告",
        "",
        f"周期: {report['start_date']} 至 {report['end_date']} ({metrics['days']}天)",
        f"自动交易控制: {control_status} ({control.get('reason', '')})",
        "",
        "## 闭环概览",
        "",
        f"- 自动循环: {metrics['auto_cycle_count']}轮，覆盖{metrics['active_days']}天",
        f"- 扫描/执行: 扫描{metrics['scan_count']}次，模拟执行{metrics['execute_count']}次",
        f"- 执行审计: 计划{metrics['order_audit_count']}笔，成交{metrics['order_filled_count']}笔，阻断/跳过{metrics['order_blocked_count']}笔，失败{metrics['order_failed_count']}笔",
        f"- 决策/交易: LLM决策{metrics['llm_decision_count']}条，可交易动作{metrics['llm_actionable_count']}条，模拟成交{metrics['trade_count']}笔",
        f"- 复盘/学习: 复盘{metrics['review_days']}天，沉淀教训{metrics['lesson_count']}条",
        f"- 完整闭环日: {metrics['closed_loop_days']}/{metrics['days']}天",
        f"- 状态归因: 无循环{metrics['no_loop_days']}天，仅复盘{metrics['review_only_days']}天，暂停{metrics['paused_days']}天，休市{metrics['holiday_days']}天，缺扫描{metrics['missing_scan_days']}天，缺执行{metrics['missing_execute_days']}天，缺复盘{metrics['missing_review_days']}天，无可交易决策{metrics['no_trade_signal_days']}天",
        f"- 异常事件: {metrics['error_events']}条",
    ]

    if metrics["llm_win_rate"] is not None:
        lines.append(f"- 已验证LLM胜率: {metrics['llm_win_rate']:.0%} ({metrics['llm_outcome_count']}条)")

    lines.extend([
        "",
        "## 当前账户",
        "",
        f"- 总资产: {float(account.get('total_assets') or 0):,.2f}",
        f"- 现金: {float(account.get('cash') or 0):,.2f}",
        f"- 持仓数: {int(account.get('position_count') or 0)}",
        "",
        "## 自适应参数",
        "",
    ])
    if adaptive["available"]:
        lines.extend([
            f"- 买入阈值: {adaptive['current_buy_threshold']}",
            f"- 最低选股分: {adaptive['current_min_score']}",
            f"- TopK偏移: {adaptive['current_top_k_delta']}",
            f"- 仓位缩放: {adaptive['current_position_scale']}",
            f"- 调整轮次: {adaptive['adjustment_count']}",
        ])
        latest = adaptive.get("latest_adjustments") or []
        if latest:
            lines.append("- 最近调整:")
            for adj in latest[:5]:
                lines.append(f"  - {adj.get('reason', adj.get('type', '参数调整'))}")
    else:
        lines.append("- 暂无自适应状态，通常表示复盘样本还不足。")

    lines.extend([
        "",
        "## 纠偏应用",
        "",
        f"- 最新市场环境: {current_regime} ({latest_regime.get('date', '无记录')})",
        f"- 下一轮参数: Top{current_params.get('top_k', '-')}，最低分{current_params.get('min_score', '-')}，单票仓位上限{float(current_params.get('max_weight') or 0):.1%}",
        f"- Prompt进化建议: {len(prompt_hints)}条生效",
    ])
    for hint in prompt_hints[:3]:
        lines.append(f"  - {hint.get('hint', '')}")

    lines.extend([
        "",
        "## 每日明细",
        "",
        "| 日期 | 状态 | 诊断 | 循环 | 扫描 | 执行 | 审计 | 阻断 | 失败 | 交易 | 决策 | 复盘 | 教训 | 闭环 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for row in report["daily"]:
        statuses = ",".join(row["statuses"]) if row["statuses"] else "-"
        closed_loop = "是" if row["closed_loop"] else "否"
        lines.append(
            f"| {row['date']} | {statuses} | {row['diagnosis']} | {row['cycle_count']} | "
            f"{row['scan_count']} | {row['execute_count']} | {row['order_audit_count']} | "
            f"{row['order_blocked_count']} | {row['order_failed_count']} | {row['trade_count']} | "
            f"{row['decision_count']} | {row['review_count']} | {row['lesson_count']} | {closed_loop} |"
        )

    lines.extend([
        "",
        "## 下一步观察",
        "",
    ])
    if metrics["closed_loop_days"] == 0:
        lines.append("- 先跑满至少1个包含盘中扫描、模拟成交、盘后复盘的交易日。")
    if metrics["no_loop_days"]:
        lines.append("- 存在无自动循环日期，优先确认计划任务或 --auto 常驻进程是否覆盖交易日。")
    if control.get("paused"):
        lines.append("- 当前自动交易处于暂停状态，盘中不会扫描或执行模拟交易。")
    if metrics["missing_scan_days"]:
        lines.append("- 存在盘中缺扫描日期，优先检查 --auto 常驻循环和 Watchdog/Doctor 日志。")
    if metrics["missing_execute_days"]:
        lines.append("- 存在扫描后未执行日期，优先检查 TradePlan、风控和模拟账户执行链路。")
    if metrics["order_blocked_count"]:
        lines.append("- 存在计划订单被阻断/跳过，优先查看 auto_events.details.order_audit 的原因分布。")
    if metrics["order_failed_count"]:
        lines.append("- 存在计划订单执行失败，优先检查实时行情、价格限制和模拟账户现金/持仓。")
    if metrics["missing_review_days"]:
        lines.append("- 存在交易/决策后缺复盘日期，优先检查盘后复盘窗口和 run_review 日志。")
    if metrics["no_trade_signal_days"]:
        lines.append("- 有扫描但无可交易决策，属于策略观望，需要结合候选分数和LLM理由判断是否过于保守。")
    if metrics["lesson_count"] == 0:
        lines.append("- 复盘暂未沉淀教训，需要检查LLM复盘或降级教训提取是否运行。")
    if not adaptive["available"]:
        lines.append("- 自适应状态尚未形成，需要至少3笔已复盘卖出样本。")
    if metrics["error_events"]:
        lines.append("- 存在自动盯盘异常事件，优先查看 auto_events.error。")
    if metrics["closed_loop_days"] > 0 and metrics["lesson_count"] > 0 and adaptive["available"]:
        lines.append("- 闭环已出现，下一阶段应连续观察5个交易日的参数变化和交易质量。")

    return "\n".join(lines)


def generate_ai_trader_report(days: int = 5, end_date: str = None,
                              db_path: str = None, output_dir: str = None,
                              save: bool = True, control_file: str = None) -> Dict:
    """
    生成AI交易员最近N天试运行报告

    Args:
        days: 回看天数
        end_date: 结束日期，默认今天
        db_path: SQLite路径，默认data/quant.db
        output_dir: 报告输出目录，默认data/reports
        save: 是否落盘Markdown和JSON
        control_file: 控制状态文件路径，None时使用默认data/auto_control.json

    Returns:
        包含metrics/daily/adaptive/markdown/path的报告字典
    """
    start_date, end_date = _calc_range(days, end_date)
    dates = [
        (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range((datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days + 1)
    ]

    with Database(db_path=db_path) as db:
        events = _normalise_auto_events(
            _query_range(db, "auto_events", "date", start_date, end_date)
        )
        trades = db.get_trades(start_date=start_date, end_date=end_date, limit=1000)
        decisions = db.get_llm_decisions(start_date=start_date, end_date=end_date, limit=1000)
        lessons = _query_range(db, "lessons", "date", start_date, end_date)
        reviews = _load_review_snapshots(db, start_date, end_date)
        adaptive_state = db.get_adaptive_state()
        account = db.get_account_state() or {}
        latest_regime = _load_latest_regime(db, end_date)
        prompt_hints = _load_active_prompt_hints(db)

    daily_rows = _build_daily_rows(dates, events, trades, decisions, lessons, reviews)
    report = {
        "start_date": start_date,
        "end_date": end_date,
        "metrics": _summarise_metrics(daily_rows, events, trades, decisions, lessons, reviews),
        "daily": daily_rows,
        "adaptive": _summarise_adaptive_state(adaptive_state),
        "latest_regime": latest_regime,
        "next_trade_params": _summarise_next_trade_params(adaptive_state, latest_regime),
        "prompt_hints": prompt_hints,
        "account": account,
        "control": get_auto_control_state(control_file=control_file),
        "paths": {},
    }
    markdown = format_ai_trader_report(report)
    report["markdown"] = markdown

    if save:
        output_dir = output_dir or REPORT_DIR
        os.makedirs(output_dir, exist_ok=True)
        stem = f"ai_trader_report_{end_date}_{len(dates)}d"
        md_path = os.path.join(output_dir, f"{stem}.md")
        json_path = os.path.join(output_dir, f"{stem}.json")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in report.items() if k != "markdown"}, f, ensure_ascii=False, indent=2)
        report["paths"] = {"markdown": md_path, "json": json_path}

    return report


if __name__ == "__main__":
    result = generate_ai_trader_report()
    print(result["markdown"])
