"""AI 交易员每日事实与驾驶简报。

本模块只聚合已经发生的扫描、判断、计划和执行事实，不替 AI 做策略判断。
它为日终复盘、Agent 状态和公开驾驶舱提供同一份权威口径。
"""
import json
import os
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from scheduler.market_calendar import _now_bj, get_market_status, is_trading_day


_DIMENSION_LABELS = {
    "technical": "技术信号",
    "capital": "资金信号",
    "sentiment": "舆情信号",
    "emotion": "市场情绪",
    "fundamental": "基本面",
    "ml": "机器学习信号",
}


def _valid_llm_decision(item: Dict) -> bool:
    reasoning = str(item.get("reasoning") or "").strip()
    response = str(item.get("llm_response") or "").strip()
    try:
        confidence = float(item.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return not (reasoning == "LLM无响应" and not response and confidence <= 0)


def _dedupe_order_audit(items: Iterable[Dict]) -> List[Dict]:
    """同一轮复盘可能多次读取事件，按订单结果事实去重。"""
    result = []
    seen = set()
    for item in items:
        key = (
            item.get("code"),
            item.get("action"),
            item.get("status"),
            item.get("reason"),
            item.get("price"),
            item.get("shares"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _load_signal_degradations() -> List[Dict]:
    """从最新信号缓存提取能力降级，不向状态层传播原始异常堆栈。"""
    cache_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "signal_cache.json",
    )
    try:
        with open(cache_file, "r", encoding="utf-8") as file:
            cache = json.load(file)
    except Exception:
        return []

    found = {}
    market_snapshot = cache.get("market_snapshot") or {}
    market_source = market_snapshot.get("source")
    if market_source == "stale_cache":
        age = market_snapshot.get("age_seconds")
        age_text = f"{age:.0f}秒" if isinstance(age, (int, float)) else "未知时长"
        found["market_data"] = {
            "key": "market_data",
            "label": "市场数据",
            "summary": f"市场快照刷新失败，使用过期缓存（{age_text}）",
        }
    elif market_source == "unavailable":
        found["market_data"] = {
            "key": "market_data",
            "label": "市场数据",
            "summary": "市场快照不可用，市场级信号使用中性默认",
        }
    scores = (cache.get("raw_scores") or [])[:20]
    degraded_counts = {}
    for score in scores:
        coverage = score.get("signal_coverage") or {}
        for name in set(coverage.get("degraded_dimensions", []) or []):
            degraded_counts[name] = degraded_counts.get(name, 0) + 1

    # 单个标的可能因上市时间短或历史数据缺失无法计算 ML，不应把整轮能力
    # 误报为不可用；只有所有候选都降级时才提升为全局能力告警。
    total_scores = len(scores)
    for name, count in degraded_counts.items():
        if total_scores and count == total_scores:
            found[name] = {
                "key": name,
                "label": _DIMENSION_LABELS.get(name, name),
                "summary": f"{_DIMENSION_LABELS.get(name, name)}不可用，已从本轮综合评分剔除并归一化剩余权重",
            }

    # 兼容没有 signal_coverage 的历史缓存，仍按维度详情识别降级。
    if not degraded_counts:
        for score in scores:
            for name, dimension in (score.get("dimensions") or {}).items():
                detail = str((dimension or {}).get("detail") or "")
                lowered = detail.lower()
                if any(marker in lowered for marker in ("异常", "超时", "不可用", "error", "cannot", "failed")):
                    found.setdefault(name, {
                        "key": name,
                        "label": _DIMENSION_LABELS.get(name, name),
                        "summary": f"{_DIMENSION_LABELS.get(name, name)}当前处于降级状态",
                    })
    return list(found.values())


def strategy_diff(current: Optional[Dict], pending: Optional[Dict]) -> List[Dict]:
    """生成当前与待生效策略的用户可读差异。"""
    if not current or not pending:
        return []
    current_params = current.get("params") or {}
    pending_params = pending.get("params") or {}
    labels = {
        "top_k": "LLM 评估池",
        "min_score": "候选最低分",
        "max_weight": "单票仓位上限",
    }
    changes = []
    for key, label in labels.items():
        before = current_params.get(key)
        after = pending_params.get(key)
        if before != after:
            changes.append({"key": key, "label": label, "before": before, "after": after})
    if current.get("intent") != pending.get("intent"):
        changes.insert(0, {
            "key": "intent",
            "label": "策略意图",
            "before": current.get("intent"),
            "after": pending.get("intent"),
        })
    return changes


def build_daily_facts(date: str = None, db_path: str = None,
                      now: datetime = None, market_status: str = None,
                      trading_day: bool = None) -> Dict:
    """聚合指定日期的完整决策漏斗，供 AI 复盘和产品展示共同使用。"""
    now = now or _now_bj()
    date = date or now.strftime("%Y-%m-%d")
    status = market_status or get_market_status()
    is_open_day = is_trading_day(date) if trading_day is None else trading_day

    from data.database import Database

    events = []
    decisions = []
    trades = []
    reviewed = False
    current_directive = None
    pending_directive = None
    counterfactual = {}
    with Database(db_path=db_path) as db:
        # 日终事实必须覆盖当天全部事件；固定取最近500条会让早盘扫描
        # 被后续Doctor/心跳事件挤出窗口，进而污染AI复盘和次日策略。
        events = db.get_auto_events(date=date, limit=None)
        decisions = [
            item for item in db.get_llm_decisions(start_date=date, end_date=date, limit=None)
            if _valid_llm_decision(item)
        ]
        trades = [
            dict(row) for row in db.conn.execute(
                "SELECT code, name, action, created_at FROM trades WHERE substr(created_at, 1, 10)=?",
                (date,),
            ).fetchall()
        ]
        reviewed = db.get_review_snapshot(date) is not None
        current_directive = db.get_effective_strategy_directive(date)
        pending_directive = db.get_next_strategy_directive(date)
        rows = db.conn.execute("""
            SELECT outcome_label, COUNT(*) AS count
            FROM candidate_outcomes
            WHERE outcome_label <> ''
              AND observation_date >= date(?, '-30 days')
            GROUP BY outcome_label
        """, (date,)).fetchall()
        counterfactual = {str(row["outcome_label"]): int(row["count"]) for row in rows}

    scan_journeys = []
    order_audit = []
    event_errors = []
    for event in events:
        details = event.get("details") or {}
        journey = details.get("scan_journey")
        if isinstance(journey, dict) and journey:
            scan_journeys.append(journey)
        order_audit.extend(details.get("order_audit") or [])
        # Doctor事件是巡检审计，不代表自动主循环仍有执行错误；Watchdog
        # 已按同一规则排除它们，简报也应避免把历史诊断累计成维护告警。
        if event.get("error") and event.get("event_type") not in (
            "auto_doctor",
            "closure_repair",
            "ops_status",
        ):
            event_errors.append(str(event.get("error")))

    order_audit = _dedupe_order_audit(order_audit)
    decision_counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    no_response_count = 0
    for item in decisions:
        action = str(item.get("action") or "HOLD").upper()
        decision_counts[action if action in decision_counts else "HOLD"] += 1
        if float(item.get("confidence") or 0) <= 0:
            no_response_count += 1

    audit_counts = {"filled": 0, "blocked": 0, "failed": 0, "skipped": 0}
    for item in order_audit:
        state = str(item.get("status") or "")
        if state in audit_counts:
            audit_counts[state] += 1

    candidate_observations = sum(int(item.get("candidate_count") or 0) for item in scan_journeys)
    scored_observations = sum(int(item.get("scored_count") or 0) for item in scan_journeys)
    planned_orders = sum(int(item.get("planned_orders") or 0) for item in scan_journeys)
    if not audit_counts["filled"] and trades:
        audit_counts["filled"] = len(trades)

    degradations = _load_signal_degradations()
    latest_scan = scan_journeys[0] if scan_journeys else {}
    funnel = {
        "scan_cycles": len(scan_journeys),
        "candidates": candidate_observations,
        "scored": scored_observations,
        "llm_evaluated": len(decisions),
        "observations": decision_counts["HOLD"],
        "buy_signals": decision_counts["BUY"],
        "sell_signals": decision_counts["SELL"],
        "planned_orders": planned_orders,
        "filled": audit_counts["filled"],
        "blocked": audit_counts["blocked"],
        "failed": audit_counts["failed"],
        "skipped": audit_counts["skipped"],
    }

    if not is_open_day:
        state = "closed"
        headline = "今日休市，AI 交易员保持待命"
        explanation = "休市日不执行扫描和交易，最近策略与复盘仍可查看。"
        next_action = "下一交易日盘前自动预热，并按待生效策略开始观察。"
    elif audit_counts["failed"] or event_errors:
        state = "attention"
        headline = "今日执行链路存在需要关注的问题"
        explanation = f"发现 {audit_counts['failed'] + len(event_errors)} 项执行或循环异常。"
        next_action = "Watchdog 与 Doctor 将继续巡检，下一轮优先验证失败环节。"
    elif audit_counts["filled"]:
        state = "executed"
        headline = f"今日已完成 {audit_counts['filled']} 笔模拟成交"
        explanation = "交易信号已经经过计划、风控和模拟执行。"
        next_action = "继续监控持仓和止损条件，收盘后评估策略结果。"
    elif decision_counts["BUY"] + decision_counts["SELL"]:
        state = "signal_pending"
        headline = "AI 已产生交易信号，但尚未形成成交"
        explanation = "请在决策旅程中查看计划、风控阻断或执行失败原因。"
        next_action = "自动循环会按计划状态继续处理，不会重复执行已领取计划。"
    elif decisions:
        state = "observing"
        headline = "AI 已完成判断，当前主动观望"
        explanation = f"今日完成 {len(decisions)} 次 LLM 判断，结果均未形成可执行交易信号。"
        next_action = "继续按当前策略扫描；收盘后由 AI 复盘是否需要调整明日策略。"
    elif scan_journeys:
        state = "scanned"
        headline = "扫描已运行，但尚未完成有效 LLM 判断"
        explanation = "候选筛选、时间预算或模型能力可能限制了判断链路。"
        next_action = "自动循环将继续扫描，并由能力健康状态记录降级原因。"
    elif status in ("盘前", "集合竞价"):
        state = "preparing"
        headline = "AI 交易员正在准备今日任务"
        explanation = "盘前数据、市场环境和候选池正在更新。"
        next_action = "开盘后按当前生效策略自动扫描和判断。"
    elif reviewed:
        state = "reviewed"
        headline = "今日交易任务已完成复盘"
        explanation = "AI 已总结当日事实并生成或确认下一交易日策略。"
        next_action = "等待下一交易日盘前预热。"
    else:
        state = "waiting"
        headline = "AI 交易员正在等待今日交易窗口"
        explanation = "当前尚未形成新的扫描、判断或执行事实。"
        next_action = "到达对应交易阶段后自动运行，无需人工触发。"

    return {
        "date": date,
        "market_status": status,
        "is_trading_day": bool(is_open_day),
        "state": state,
        "headline": headline,
        "explanation": explanation,
        "next_action": next_action,
        "funnel": funnel,
        "order_audit": order_audit,
        "event_error_count": len(event_errors),
        "llm_no_response_count": no_response_count,
        "counterfactual": counterfactual,
        "degradations": degradations,
        "reviewed": reviewed,
        "latest_scan": latest_scan,
        "strategy": {
            "current": current_directive,
            "pending": pending_directive,
            "diff": strategy_diff(current_directive, pending_directive),
        },
    }
