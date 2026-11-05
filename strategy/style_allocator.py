"""风格名额/持有期的自动调整（CANDIDATE_SOURCE=style_pools 的学习闭环）。

每次盘后风格池生成后调用 ``run``：距上次检查不足 ``MIN_INTERVAL_DAYS`` 个交易日则跳过；
否则用 ``style_shadow.evaluate`` 的影子证据（全部入池股票的扣费前瞻收益，样本远大于
模拟盘成交）做一次检查，**每次最多改一项**：

1. 名额：当前持有期口径下，超额收益最好与最差的风格都有 ≥ ``MIN_SIGNAL_DAYS`` 个成熟
   信号日、且差距 ≥ ``MIN_EXCESS_EDGE`` 时，从最差风格挪 1 个名额给最好风格（每个风格至少 1 个）；
2. 持有期：名额不动时，若相邻一档（5/10/20 日）按名额加权的日均净收益高出
   ``MIN_DAILY_EDGE``，持有期移动一档。

状态与历史写入 ``data/style_allocation.json``；``rollback`` 撤销最近一次调整，删除文件即
回到默认（名额均分、持有期 STYLE_HOLD_DAYS）。STYLE_AUTO_TUNE=0 时只读不调。
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

from config import DATA_DIR

logger = logging.getLogger("strategy.style_allocator")

ALLOCATION_PATH = os.path.join(DATA_DIR, "style_allocation.json")
HOLD_STEPS = (5, 10, 20)
MIN_INTERVAL_DAYS = 5        # 两次检查之间至少间隔的交易日数（约每周一次）
MIN_SIGNAL_DAYS = 10         # 参与比较的口径至少需要的成熟信号日
MIN_EXCESS_EDGE = 0.003      # 挪名额所需的超额收益差（0.3 个百分点）
MIN_DAILY_EDGE = 0.0003      # 换持有期所需的日均净收益差（每日 0.03 个百分点）
EVAL_WINDOW_DAYS = 120       # 评估窗口（自然日），只看近期证据
MAX_HISTORY = 100


def default_allocation(styles=None, total_slots: int = None, hold_days: int = None) -> dict:
    from config import MAX_POSITIONS, STYLE_HOLD_DAYS, STYLE_LIVE_STYLES
    styles = list(styles or STYLE_LIVE_STYLES)
    total = max(len(styles), int(total_slots or MAX_POSITIONS))
    slots = {s: total // len(styles) + (1 if i < total % len(styles) else 0) for i, s in enumerate(styles)}
    return {"slots": slots, "hold_days": int(hold_days or STYLE_HOLD_DAYS),
            "last_checked": None, "last_adjusted": None, "history": []}


def _normalize(state: dict, styles) -> dict:
    """只保留当前实盘风格；新增风格给 1 个名额，名额至少为 1。"""
    base = default_allocation(styles)
    slots = state.get("slots") if isinstance(state.get("slots"), dict) else {}
    base["slots"] = {s: max(1, int(slots.get(s, 1 if slots else base["slots"][s]))) for s in base["slots"]}
    try:
        base["hold_days"] = max(1, int(state.get("hold_days") or base["hold_days"]))
    except (TypeError, ValueError):
        pass
    for key in ("last_checked", "last_adjusted"):
        base[key] = state.get(key)
    base["history"] = list(state.get("history") or [])[-MAX_HISTORY:]
    return base


def load_allocation(path: str = None, styles=None) -> dict:
    """读取当前分配；文件缺失或损坏时返回默认值（不抛异常，盘中可安全调用）。"""
    path = path or ALLOCATION_PATH
    from config import STYLE_LIVE_STYLES
    styles = list(styles or STYLE_LIVE_STYLES)
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("allocation is not an object")
    except FileNotFoundError:
        return default_allocation(styles)
    except (OSError, ValueError) as exc:
        logger.warning("风格分配文件不可读，使用默认分配: %s", exc)
        return default_allocation(styles)
    return _normalize(state, styles)


def save_allocation(state: dict, path: str = None) -> None:
    path = path or ALLOCATION_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _metric(report: dict, style: str, horizon: int) -> Optional[dict]:
    m = ((report.get("styles") or {}).get(style) or {}).get(f"{horizon}d")
    if not m or int(m.get("signal_days") or 0) < MIN_SIGNAL_DAYS:
        return None
    return m


def propose(state: dict, report: dict) -> Optional[dict]:
    """根据影子评估给出至多一项调整；证据不足返回 None。"""
    if report.get("status") != "ok":
        return None
    slots, hold = state["slots"], int(state["hold_days"])
    excess = {s: m["mean_excess"] for s in slots if (m := _metric(report, s, hold))}
    if len(excess) >= 2:
        best = max(excess, key=lambda s: (excess[s], s))
        worst = min(excess, key=lambda s: (excess[s], s))
        if excess[best] - excess[worst] >= MIN_EXCESS_EDGE and slots[worst] > 1:
            return {"type": "slot", "from": worst, "to": best, "horizon": hold,
                    "evidence": {s: round(v, 5) for s, v in excess.items()}}

    def daily_net(h):
        pairs = [(slots[s], m["mean_net"]) for s in slots if (m := _metric(report, s, h))]
        weight = sum(w for w, _ in pairs)
        return sum(w * v for w, v in pairs) / weight / h if weight else None

    if hold not in HOLD_STEPS:
        return None
    current = daily_net(hold)
    if current is None:
        return None
    i = HOLD_STEPS.index(hold)
    options = {h: v for h in HOLD_STEPS[max(0, i - 1):i + 2] if h != hold and (v := daily_net(h)) is not None}
    if not options:
        return None
    target = max(options, key=lambda h: options[h])
    if options[target] - current >= MIN_DAILY_EDGE:
        return {"type": "hold", "from": hold, "to": target,
                "evidence": {f"{h}d": round(v, 6) for h, v in {hold: current, **options}.items()}}
    return None


def apply_change(state: dict, change: dict, today: str) -> dict:
    new = json.loads(json.dumps(state))
    before = {"slots": dict(state["slots"]), "hold_days": state["hold_days"]}
    if change["type"] == "slot":
        new["slots"][change["from"]] -= 1
        new["slots"][change["to"]] += 1
    else:
        new["hold_days"] = int(change["to"])
    new["last_adjusted"] = today
    new["history"] = (list(state.get("history") or []) + [{"date": today, "change": change, "before": before}])[-MAX_HISTORY:]
    return new


def rollback(path: str = None) -> Optional[dict]:
    """撤销最近一次调整，返回恢复后的状态；没有可撤销的调整返回 None。"""
    state = load_allocation(path)
    history = [h for h in state["history"] if h.get("before")]
    if not history:
        return None
    last = history[-1]
    state["slots"] = _normalize({"slots": last["before"]["slots"]}, list(state["slots"]))["slots"]
    state["hold_days"] = int(last["before"]["hold_days"])
    state["history"] = state["history"][:state["history"].index(last)] + [
        {"date": datetime.now().strftime("%Y-%m-%d"), "rollback_of": last["date"]}]
    save_allocation(state, path)
    return state


def describe(state: dict) -> str:
    from strategy.style_pools import STYLE_LABELS
    parts = [f"{STYLE_LABELS.get(s, s)}{n}" for s, n in state["slots"].items()]
    return f"名额 {' / '.join(parts)}，持有期 {state['hold_days']} 个交易日"


def format_report(change: Optional[dict], state: dict) -> str:
    from strategy.style_pools import STYLE_LABELS
    lines = ["AlphaPilot 风格自动调仓（模拟盘）"]
    if change is None:
        lines.append("本周证据不足或差距不显著，维持不变。")
    elif change["type"] == "slot":
        lines.append(f"挪 1 个名额：{STYLE_LABELS.get(change['from'], change['from'])} → "
                     f"{STYLE_LABELS.get(change['to'], change['to'])}（{change['horizon']} 日超额）")
        lines.append("超额: " + "，".join(f"{STYLE_LABELS.get(s, s)} {v * 100:+.2f}%"
                                         for s, v in change["evidence"].items()))
    else:
        lines.append(f"持有期 {change['from']} → {change['to']} 个交易日")
        lines.append("日均净收益: " + "，".join(f"{k} {v * 100:+.3f}%" for k, v in change["evidence"].items()))
    lines.append("当前: " + describe(state))
    lines.append("回滚: python main.py --style-allocation-rollback")
    return "\n".join(lines)


def _trading_days_between(start: str, end: str) -> Optional[int]:
    try:
        from scheduler.market_calendar import get_trading_days
        return max(0, len(get_trading_days(start, end)) - 1)
    except Exception as exc:  # noqa: BLE001 - 日历不可用时本次不调整
        logger.warning("交易日历不可用，跳过风格调仓: %s", exc)
        return None


def run(today: str = None, *, path: str = None, evaluate_fn: Callable = None,
        days_between: Callable = None, notify_fn: Callable = None) -> Dict:
    """盘后检查并（必要时）调整一次；返回摘要。任何失败都不影响风格池本身。"""
    from config import STYLE_AUTO_TUNE
    today = today or datetime.now().strftime("%Y-%m-%d")
    state = load_allocation(path)
    if not STYLE_AUTO_TUNE:
        return {"status": "disabled", "allocation": describe(state)}
    last = state.get("last_checked")
    if last:
        gap = (days_between or _trading_days_between)(last, today)
        if gap is None or gap < MIN_INTERVAL_DAYS:
            return {"status": "skipped", "days_since_check": gap, "allocation": describe(state)}

    if evaluate_fn is None:
        from strategy.style_shadow import evaluate as evaluate_fn
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=EVAL_WINDOW_DAYS)).strftime("%Y-%m-%d")
    report = evaluate_fn(horizons=HOLD_STEPS, since=since)
    change = propose(state, report)
    if change:
        state = apply_change(state, change, today)
    state["last_checked"] = today
    save_allocation(state, path)
    text = format_report(change, state)
    logger.info(text.replace("\n", " | "))
    notified = False
    try:
        if notify_fn is None:
            from config import AUTO_NOTIFY_ENABLED
            if AUTO_NOTIFY_ENABLED:
                from scheduler.notifier import send_message
                notify_fn = send_message
        if notify_fn is not None:
            notified = bool(notify_fn(text))
    except Exception as exc:  # noqa: BLE001 - 通知失败不回滚调整
        logger.warning("风格调仓通知失败: %s", exc)
    return {"status": "changed" if change else "unchanged", "change": change,
            "allocation": describe(state), "notified": notified}
