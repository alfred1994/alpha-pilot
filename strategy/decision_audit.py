"""只读的空仓 HOLD 机会审计；重复扫描、持仓和实际成交不混入分母。"""
import math
from collections import Counter, defaultdict


def _positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def select_flat_holds(decisions, trades):
    """同日同股全部有效动作均为 HOLD，且当日没有持仓、信号或成交。"""
    groups = defaultdict(list)
    excluded = Counter()
    for row in decisions:
        reason = str(row.get("reasoning") or "")
        if not _positive(row.get("confidence")) or any(
            marker in reason for marker in ("解析失败", "LLM无响应")
        ):
            excluded["invalid_decisions"] += 1
            continue
        groups[(row["date"], row["code"])].append(row)
    by_code = defaultdict(list)
    for trade in trades:
        if not trade.get("is_replay"):
            by_code[trade["code"]].append(trade)
    selected = []
    for (day, code), rows in sorted(groups.items()):
        if any(row.get("action") != "HOLD" for row in rows):
            excluded["day_with_buy_or_sell_signal"] += 1
            continue
        history = by_code[code]
        if any(str(t.get("created_at", ""))[:10] == day for t in history):
            excluded["day_with_trade"] += 1
            continue
        shares = sum(
            float(t.get("shares") or 0) * (1 if t.get("action") == "BUY" else -1)
            for t in history if str(t.get("created_at", ""))[:10] < day
        )
        # 保存的持仓段可以补充不完整的历史成交；负持仓说明历史不全。
        if shares != 0 or any("持仓卖出分析" in str(r.get("llm_prompt") or "") for r in rows):
            excluded["held_or_incomplete_position_history"] += 1
            continue
        last = max(rows, key=lambda r: (str(r.get("created_at") or ""), r.get("id", 0)))
        selected.append({
            "date": day, "code": code, "decision_id": last.get("id"),
            "reason": last.get("reasoning", ""), "scan_count": len(rows),
        })
    return selected, dict(excluded)


def audit_flat_holds(decisions, trades, bars, calendar, as_of, roundtrip_cost=0.0021):
    """次日开盘假设入场，T+3/T+5 收盘退出；峰值仅为事后波段线索。

    bars: {code: [{date, open, high, low, close}, ...]}。缺任何窗口日不算成熟。
    T+1 禁止当日回转，因此上涨空间从假设买入的下一交易日开始计算。
    不模拟涨停成交、停牌、盘口及真实仓位，不能解释为可实现的策略收益。
    """
    selected, excluded = select_flat_holds(decisions, trades)
    calendar = sorted({day for day in calendar if day <= as_of})
    indexed = {code: {r["date"]: r for r in rows if r["date"] <= as_of}
               for code, rows in bars.items()}
    samples = []
    for item in selected:
        sample = dict(item, horizons={})
        prices = indexed.get(item["code"], {})
        future = calendar[calendar.index(item["date"]) + 1:] if item["date"] in calendar else []
        for horizon in (3, 5):
            window = future[:horizon]
            stats = {"status": "pending", "net_return": None, "mfe": None, "mae": None}
            if len(window) == horizon:
                rows = [prices.get(day) for day in window]
                if all(r and all(_positive(r.get(k)) for k in ("open", "high", "low", "close"))
                       and r["low"] <= min(r["open"], r["close"])
                       and r["high"] >= max(r["open"], r["close"]) for r in rows):
                    entry = float(rows[0]["open"])
                    stats.update(
                        status="matured", entry_date=window[0], entry_price=entry,
                        exit_date=window[-1],
                        net_return=round(float(rows[-1]["close"]) / entry - 1 - roundtrip_cost, 6),
                        mfe=round(max(float(r["high"]) for r in rows[1:]) / entry - 1 - roundtrip_cost, 6),
                        mae=round(min(float(r["low"]) for r in rows) / entry - 1 - roundtrip_cost, 6),
                    )
                else:
                    stats["status"] = "missing_or_invalid_prices"
            sample["horizons"][str(horizon)] = stats
        samples.append(sample)
    summary = {}
    for horizon in ("3", "5"):
        outcomes = [r["horizons"][horizon] for r in samples]
        mature = [r for r in outcomes if r["status"] == "matured"]
        summary[horizon] = {
            "matured": len(mature),
            "pending": sum(r["status"] == "pending" for r in outcomes),
            "missing_prices": sum(r["status"] == "missing_or_invalid_prices" for r in outcomes),
            "positive_net": sum(r["net_return"] > 0 for r in mature),
            "net_at_least_3pct": sum(r["net_return"] >= 0.03 for r in mature),
            "net_at_least_5pct": sum(r["net_return"] >= 0.05 for r in mature),
            "peak_at_least_5pct": sum(r["mfe"] >= 0.05 for r in mature),
            "mean_net_return": round(sum(r["net_return"] for r in mature) / len(mature), 6) if mature else None,
        }
    return {"as_of": as_of, "n_flat_hold_stock_days": len(samples), "excluded": excluded,
            "roundtrip_cost": roundtrip_cost, "summary": summary, "samples": samples,
            "method": "next_session_open_to_session_3_or_5_close; stock_day_dedup; T+1_sellable_peak",
            "limitations": "日线假设入场，非实际成交；峰值不等于可捕获收益；同股票跨日样本相关"}


def load_hold_audit_inputs(conn, as_of, sessions=10):
    """只用原生只读连接，不触发建表、回填、行情下载或模型调用。"""
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM llm_decisions WHERE date<=? ORDER BY date DESC LIMIT ?",
        (as_of, sessions),
    )]
    if not days:
        return [], [], []
    decisions = [dict(r) for r in conn.execute(
        "SELECT * FROM llm_decisions WHERE date>=? AND date<=? ORDER BY created_at,id", (min(days), as_of))]
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE substr(created_at,1,10)<=? ORDER BY created_at,id", (as_of,))]
    return decisions, trades, sorted(days)


def build_hold_audit_from_db(conn, as_of, sessions=10):
    decisions, trades, days = load_hold_audit_inputs(conn, as_of, sessions)
    if not days:
        return {}
    selected, _ = select_flat_holds(decisions, trades)
    codes = {r["code"] for r in selected}
    bars = {code: [dict(r) for r in conn.execute(
        "SELECT date,open,high,low,close FROM k_daily WHERE code=? AND date>=? AND date<=? ORDER BY date",
        (code, min(days), as_of))] for code in codes}
    # 不能用个股缺失日线的行数充当交易日数；无参考日历时报告待核验。
    calendar = [r[0] for r in conn.execute(
        "SELECT date FROM k_daily WHERE code='000300' AND date>=? AND date<=? ORDER BY date",
        (min(days), as_of))]
    result = audit_flat_holds(decisions, trades, bars, calendar, as_of)
    result["decision_dates"] = days
    result["price_quality"] = "unverified_daily_bars" if calendar else "missing_reference_calendar"
    result["promotion_evidence"] = False
    return result
