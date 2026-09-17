"""Persisted account snapshot comparison, without live valuation or cash-flow guesses."""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from scheduler.market_calendar import _now_bj
from web.public_safety import public_error_message
from web.routers.research import _number, _object, _open_db, _table

router = APIRouter()


def _positive(value):
    value = _number(value)
    return value if value is not None and value > 0 else None


@router.get("/research/returns")
def account_returns(start_date: Optional[date] = None, end_date: Optional[date] = None):
    end = end_date or _now_bj().date()
    start = start_date or end - timedelta(days=29)
    if start > end or (end - start).days > 366:
        raise HTTPException(422, "日期范围须按先后顺序且不超过367天")
    if end > _now_bj().date():
        raise HTTPException(422, "结束日期不可晚于今天")
    try:
        with _open_db() as conn:
            if not _table(conn, "review_snapshots"):
                return {"success": True, "available": False}
            stored = conn.execute(
                "SELECT date, data FROM review_snapshots WHERE date >= ? AND date <= ? ORDER BY date",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            prices = {}
            if _table(conn, "k_daily"):
                prices = {row["date"]: _positive(row["close"]) for row in conn.execute(
                    "SELECT date, close FROM k_daily WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
                    ("000300.SH", start.isoformat(), end.isoformat()),
                )}
        rows = []
        for record in stored:
            try:
                date.fromisoformat(record["date"])
            except (ValueError, TypeError):
                continue
            raw = _object(record["data"])
            # Zero equity is valid (complete loss), unlike a missing or negative snapshot.
            assets = _number(raw.get("total_assets"))
            rows.append({"date": record["date"], "total_assets": assets if assets is not None and assets >= 0 else None,
                         "initial_capital": _positive(raw.get("initial_capital"))})
        valid = [row for row in rows if row["total_assets"] is not None]
        if not valid:
            return {"success": True, "available": False, "invalid_snapshots": len(rows)}
        # Effective range is the first/last valid snapshot inside the requested range.
        first, last = valid[0], valid[-1]
        rows = [row for row in rows if first["date"] <= row["date"] <= last["date"]]
        base = first["total_assets"]
        base_price = prices.get(first["date"])
        end_price = prices.get(last["date"])
        enough = len(valid) >= 2 and base > 0
        capital_values = {row["initial_capital"] for row in valid if row["initial_capital"] is not None}
        reset_suspected = len(capital_values) > 1
        comparable = enough and not reset_suspected
        benchmark_return = end_price / base_price - 1 if enough and base_price and end_price else None
        asset_return = last["total_assets"] / base - 1 if comparable else None
        peak, drawdown = base, 0.0
        previous = None
        for row in rows:
            assets = row["total_assets"]
            prior_assets = previous["total_assets"] if previous else None
            row.update({
                "change_since_previous": assets - prior_assets if assets is not None and prior_assets is not None and not reset_suspected else None,
                "change_rate_since_previous": assets / prior_assets - 1 if assets is not None and prior_assets is not None and prior_assets > 0 and not reset_suspected else None,
                "previous_date": previous["date"] if previous else None,
                "asset_return": assets / base - 1 if assets is not None and comparable else None,
                "benchmark_return": prices[row["date"]] / base_price - 1 if enough and base_price and prices.get(row["date"]) else None,
            })
            if assets is not None and comparable:
                peak = max(peak, assets)
                drawdown = min(drawdown, assets / peak - 1)
            previous = row
            del row["initial_capital"]
        return {"success": True, "available": True,
                "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                "effective_start": first["date"], "effective_end": last["date"],
                "source": "review_snapshots", "benchmark_source": "k_daily:000300.SH",
                "snapshots": len(valid), "invalid_snapshots": len(rows) - len(valid),
                "benchmark_points": sum(prices.get(row["date"]) is not None for row in rows),
                "cash_flow_adjusted": False, "reset_suspected": reset_suspected,
                "summary": {"start_assets": base, "end_assets": last["total_assets"],
                            "asset_change": last["total_assets"] - base if enough and not reset_suspected else None,
                            "asset_return": asset_return, "benchmark_return": benchmark_return,
                            "relative_asset_change": asset_return - benchmark_return if asset_return is not None and benchmark_return is not None else None,
                            "max_asset_drawdown": drawdown if comparable and len(valid) == len(rows) else None},
                "points": rows,
                "note": "账户数值为未调整出入金的资产变化，不是已核实投资收益；资金流未知，不展示净入金、年化或夏普。区间从首个有效快照收盘到最后快照收盘，首日无日盈亏。逐行变化是相邻已存快照之差，缺日不补造；基准按相同日期日线归一化，缺失不延续、不填零。仅日终历史，不含实时账户估值。"}
    except Exception:
        return {"success": False, "error": public_error_message()}
