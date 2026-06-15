"""
正式模拟盘日内闭环缺口诊断
====================================================================
只读正式SQLite和AI交易员报告，判断目标交易日距离完整闭环还差什么：
  - 盘前预热/市场识别
  - 盘中扫描/模拟执行/成交或可解释的无交易
  - 盘后复盘/教训沉淀/自适应参数

该模块不触发行情、LLM或交易接口，用于观察跑盘后的证据核对和运维提示。
====================================================================
"""
from datetime import datetime
from scheduler.market_calendar import _now_bj
from typing import Callable, Dict, List

from review.ai_trader_report import generate_ai_trader_report
from scheduler.market_calendar import get_market_status, is_trading_day, next_trading_day


def _today(now: datetime = None) -> str:
    """返回YYYY-MM-DD格式日期"""
    return (now or _now_bj()).strftime("%Y-%m-%d")


def _find_daily_row(report: Dict, date: str) -> Dict:
    """从报告中提取指定日期明细"""
    for row in report.get("daily") or []:
        if row.get("date") == date:
            return row
    return {}


def _ok_item(name: str, detail: str) -> Dict:
    """构造完成项"""
    return {"name": name, "ok": True, "severity": "ok", "detail": detail, "suggestion": ""}


def _gap_item(name: str, severity: str, detail: str, suggestion: str) -> Dict:
    """构造缺口项"""
    return {"name": name, "ok": False, "severity": severity, "detail": detail, "suggestion": suggestion}


def _next_trading_day_label(date: str) -> str:
    """计算下一交易日的展示日期"""
    try:
        raw = next_trading_day(date)
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    except Exception:
        return "下一交易日"


def _build_items(row: Dict, status: str, is_trade_day: bool,
                 target_date: str, adaptive_available: bool) -> List[Dict]:
    """根据日报行构造闭环缺口项"""
    items: List[Dict] = []
    statuses = set(row.get("statuses") or [])
    premarket_seen = bool(statuses.intersection({"盘前", "集合竞价"}))
    scan_count = int(row.get("scan_count") or 0)
    execute_count = int(row.get("execute_count") or 0)
    trade_count = int(row.get("trade_count") or 0)
    decision_count = int(row.get("decision_count") or 0)
    review_count = int(row.get("review_count") or 0)
    lesson_count = int(row.get("lesson_count") or 0)
    closed_loop = bool(row.get("closed_loop"))

    if not is_trade_day:
        return [_ok_item("交易日", f"{target_date}非交易日，无需闭环")]

    if premarket_seen:
        items.append(_ok_item("盘前准备", "已出现盘前/集合竞价自动循环"))
    elif status in ("盘前", "集合竞价"):
        items.append(_gap_item(
            "盘前准备",
            "warn",
            "当前仍在盘前窗口，尚未完成预热/市场识别",
            "运行 python main.py --paper-observe --observe-cycles 1。",
        ))
    else:
        items.append(_gap_item(
            "盘前准备",
            "warn",
            "今日未见盘前预热/市场识别记录",
            "明日08:45前启动 --auto 或在盘前运行 --paper-observe。",
        ))

    if scan_count > 0:
        items.append(_ok_item("盘中扫描", f"已扫描{scan_count}次"))
    elif status == "盘中":
        items.append(_gap_item(
            "盘中扫描",
            "critical",
            "当前盘中但尚无扫描记录",
            "运行 python main.py --paper-observe --observe-cycles 5 --force-scan。",
        ))
    elif status in ("盘前", "集合竞价", "午休"):
        items.append(_gap_item(
            "盘中扫描",
            "warn",
            f"当前{status}，等待盘中扫描窗口",
            "盘中运行 python main.py --paper-observe --observe-cycles 5 --force-scan。",
        ))
    else:
        items.append(_gap_item(
            "盘中扫描",
            "warn",
            "今日未见盘中扫描记录",
            "下一交易日盘中运行 --paper-observe --observe-cycles 5 --force-scan。",
        ))

    if execute_count > 0:
        items.append(_ok_item("模拟执行", f"已执行{execute_count}次"))
    elif scan_count > 0:
        items.append(_gap_item(
            "模拟执行",
            "critical",
            "已有扫描但未见模拟执行",
            "检查TradePlan缓存、风控和 execute_trades 链路；运行 python main.py --doctor --force-scan。",
        ))
    else:
        items.append(_gap_item(
            "模拟执行",
            "warn",
            "等待盘中扫描后执行",
            "先完成盘中扫描，再检查执行审计。",
        ))

    if trade_count > 0 or decision_count > 0:
        items.append(_ok_item("交易决策", f"决策{decision_count}条，成交{trade_count}笔"))
    elif scan_count > 0:
        items.append(_gap_item(
            "交易决策",
            "warn",
            "已有扫描但无LLM决策/模拟成交",
            "查看信号候选和LLM决策日志，判断是正常观望还是决策链路过保守。",
        ))
    else:
        items.append(_gap_item(
            "交易决策",
            "warn",
            "尚无盘中扫描结果，无法验证交易决策",
            "先跑盘中观察。",
        ))

    if review_count > 0:
        items.append(_ok_item("盘后复盘", f"已复盘{review_count}次"))
    elif status == "盘后":
        items.append(_gap_item(
            "盘后复盘",
            "critical" if (trade_count > 0 or decision_count > 0) else "warn",
            "当前盘后但尚无复盘快照",
            "运行 python main.py --review，或继续 --paper-observe 等待自动复盘窗口。",
        ))
    else:
        items.append(_gap_item(
            "盘后复盘",
            "warn",
            "尚未到盘后复盘窗口",
            "15:05后运行 python main.py --paper-observe --observe-cycles 1。",
        ))

    has_decision_or_trade = trade_count > 0 or decision_count > 0

    if lesson_count > 0:
        items.append(_ok_item("教训沉淀", f"已沉淀{lesson_count}条教训"))
    elif review_count > 0:
        items.append(_gap_item(
            "教训沉淀",
            "critical" if has_decision_or_trade else "warn",
            "已有复盘但未沉淀教训",
            (
                "检查 review.llm_review 的教训提取和降级逻辑。"
                if has_decision_or_trade
                else "当前缺少交易/决策样本，继续积累盘中观察。"
            ),
        ))
    else:
        items.append(_gap_item(
            "教训沉淀",
            "warn",
            "等待盘后复盘后提取教训",
            "完成盘后复盘。",
        ))

    if adaptive_available:
        items.append(_ok_item("参数纠偏", "自适应状态可用"))
    else:
        items.append(_gap_item(
            "参数纠偏",
            "warn",
            "自适应状态尚不可用，通常需要更多已复盘卖出样本",
            "继续积累正式模拟成交和复盘样本。",
        ))

    if closed_loop:
        items.append(_ok_item("完整闭环", "今日已形成自动盯盘-交易-复盘-学习闭环"))
    return items


def _next_action(items: List[Dict], status: str, target_date: str) -> Dict:
    """根据缺口选择下一条最重要动作"""
    priority = [
        "盘中扫描",
        "模拟执行",
        "交易决策",
        "盘后复盘",
        "教训沉淀",
        "盘前准备",
        "参数纠偏",
    ]
    for severity in ("critical", "warn"):
        by_name = {item.get("name"): item for item in items if item.get("severity") == severity}
        for name in priority:
            item = by_name.get(name)
            if item and item.get("suggestion"):
                return {
                    "severity": severity,
                    "title": f"补齐{item['name']}",
                    "command": _command_for_gap(item["name"], status),
                    "detail": item["suggestion"],
                }
    return {
        "severity": "ok",
        "title": "继续观察",
        "command": "python main.py --ops-status --report-days 5",
        "detail": f"{target_date}闭环缺口已处理，继续跟踪后续交易日。",
    }


def _command_for_gap(name: str, status: str) -> str:
    """为缺口生成推荐命令"""
    if name in ("盘中扫描", "模拟执行", "交易决策"):
        return "python main.py --paper-observe --observe-cycles 5 --force-scan"
    if name in ("盘后复盘", "教训沉淀"):
        return "python main.py --review"
    if name == "盘前准备":
        return "python main.py --paper-observe --observe-cycles 1"
    if name == "参数纠偏":
        return "python main.py --ai-report --report-days 5"
    return "python main.py --paper-observe --observe-cycles 5"


def run_closure_check(
    *,
    date: str = None,
    now: datetime = None,
    db_path: str = None,
    control_file: str = None,
    report: Dict = None,
    status_override: str = None,
    trading_day_override: bool = None,
    report_func: Callable[..., Dict] = None,
) -> Dict:
    """
    执行正式模拟盘日内闭环缺口诊断

    Args中的 report/status/trading_day/report_func 用于测试注入。
    """
    now = now or _now_bj()
    target_date = date or _today(now)
    status = status_override or get_market_status()
    is_trade_day = (
        trading_day_override
        if trading_day_override is not None
        else is_trading_day(target_date)
    )
    if report is None:
        report_func = report_func or generate_ai_trader_report
        report = report_func(
            days=1,
            end_date=target_date,
            db_path=db_path,
            save=False,
            control_file=control_file,
        )

    row = _find_daily_row(report, target_date)
    adaptive_available = bool((report.get("adaptive") or {}).get("available"))
    items = _build_items(row, status, is_trade_day, target_date, adaptive_available)
    critical = [item["name"] for item in items if item.get("severity") == "critical"]
    warn = [item["name"] for item in items if item.get("severity") == "warn"]
    closed_loop = bool(row.get("closed_loop")) if row else False
    next_action = _next_action(items, status, target_date)
    next_trade_day = _next_trading_day_label(target_date) if not closed_loop else ""

    return {
        "date": target_date,
        "generated_at": now.isoformat(),
        "market_status": status,
        "is_trading_day": is_trade_day,
        "daily": row,
        "items": items,
        "next_action": next_action,
        "next_trading_day": next_trade_day,
        "overall": {
            "closed_loop": closed_loop,
            "level": "critical" if critical else ("warn" if warn else "ok"),
            "critical": critical,
            "warn": warn,
        },
    }


def format_closure_check(result: Dict) -> str:
    """格式化闭环缺口诊断"""
    overall = result.get("overall") or {}
    next_action = result.get("next_action") or {}
    level = overall.get("level", "unknown").upper()
    lines = [
        "=" * 60,
        "正式模拟盘日内闭环缺口",
        "=" * 60,
        f"日期: {result.get('date', '')} 市场状态: {result.get('market_status', '')}",
        f"总体: {level} closed_loop={overall.get('closed_loop')}",
    ]
    for item in result.get("items", []):
        label = item.get("severity", "ok").upper() if item.get("severity") != "ok" else "OK"
        lines.append(f"[{label}] {item.get('name', '')} - {item.get('detail', '')}")
        if not item.get("ok") and item.get("suggestion"):
            lines.append(f"  建议: {item['suggestion']}")
    lines.extend(["-" * 60, "下一步:"])
    lines.append(f"  [{next_action.get('severity', 'info').upper()}] {next_action.get('title', '')}")
    if next_action.get("detail"):
        lines.append(f"    说明: {next_action['detail']}")
    if next_action.get("command"):
        lines.append(f"    命令: {next_action['command']}")
    if result.get("next_trading_day") and not overall.get("closed_loop"):
        lines.append(f"  下一交易日: {result['next_trading_day']}")
    lines.append("=" * 60)
    return "\n".join(lines)
