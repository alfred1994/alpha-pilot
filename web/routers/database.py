from fastapi import APIRouter, Query
from typing import Optional
import os
import json
import re
from datetime import datetime, timedelta
from web.public_safety import is_production, public_error_message, sanitize_public_text
from web.api_errors import unavailable
from fastapi import HTTPException
from scheduler.market_calendar import _now_bj

router = APIRouter()

def _get_db():
    from data.database import Database
    return Database(readonly=True)


def _account_total_assets_with_realtime(account):
    """账户总资产优先按实时价估算，行情不可用时回退到账户保存价格。"""
    prices = {}
    if getattr(account, "positions", None):
        try:
            from data.realtime import get_realtime
            quotes = get_realtime(list(account.positions.keys()))
            from data.quote_validation import validate_quote
            for quote in quotes:
                code = str(getattr(quote, "code", "") or "")
                checked = validate_quote(quote, expected_code=code)
                if code in account.positions and checked.valid:
                    prices[code] = checked.price
        except Exception:
            prices = {}
    return account.total_assets(prices or None)


def _normalize_performance_daily_pnl(perf_data: list) -> list:
    """按相邻总资产校准日盈亏，避免旧复盘文件的持仓口径污染图表。"""
    normalized = [dict(item) for item in perf_data]
    for index in range(1, len(normalized)):
        try:
            previous_assets = float(normalized[index - 1].get("total_assets") or 0)
            current_assets = float(normalized[index].get("total_assets") or 0)
        except (TypeError, ValueError):
            continue
        if previous_assets > 0:
            normalized[index]["daily_pnl"] = current_assets - previous_assets
    return normalized


def _nullable_float(value):
    """保留未知数值为None，避免把缺失盈亏误报为0。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_signal_cache():
    """读取本地信号缓存，用于把决策和股票名称补齐到公开接口。"""
    project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cache_file = os.path.join(project_dir, "data", "signal_cache.json")
    if not os.path.exists(cache_file):
        return {}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _is_no_response_decision(reasoning: str, confidence: float, response: str = "") -> bool:
    """LLM未返回内容不是有效交易决策，公开列表中过滤掉这类噪声记录。"""
    reason_text = (reasoning or "").strip()
    response_text = (response or "").strip()
    try:
        confidence_value = float(confidence or 0)
    except (TypeError, ValueError):
        confidence_value = 0
    return reason_text == "LLM无响应" and not response_text and confidence_value <= 0


def _extract_name_from_prompt(prompt: str, code: str) -> str:
    """从LLM提示词的股票信息段提取股票名称。"""
    if not prompt:
        return ""
    match = re.search(r"名称[:：]\s*([^\s\n\r，,]+)", prompt)
    if not match:
        return ""
    name = match.group(1).strip()
    return name if name and name != code else ""


def _resolve_stock_name(cursor, code: str, scores_map: dict, llm_prompt: str = "") -> str:
    """按信号缓存、持仓、成交记录的顺序补齐股票名称。"""
    score_item = scores_map.get(code) or {}
    name = (score_item.get("name") or "").strip()
    if name and name != code:
        return name

    name = _extract_name_from_prompt(llm_prompt, code)
    if name:
        return name

    for sql in (
        "SELECT name FROM positions WHERE code=? AND COALESCE(name, '') <> '' LIMIT 1",
        "SELECT name FROM trades WHERE code=? AND COALESCE(name, '') <> '' ORDER BY created_at DESC, id DESC LIMIT 1",
    ):
        cursor.execute(sql, (code,))
        row = cursor.fetchone()
        if row and row[0] and row[0] != code:
            return row[0]
    return code


def _resolve_trade_reason(cursor, code: str, action: str, reason: str, date_text: str) -> str:
    """把历史成交里的泛化执行文案替换为同日LLM决策理由。"""
    raw_reason = (reason or "").strip()
    if raw_reason and raw_reason != "TradePlan执行":
        return sanitize_public_text(raw_reason)

    trade_date = (date_text or "").split(" ")[0].split("T")[0]
    if not code or not trade_date:
        return sanitize_public_text(raw_reason or "-")

    cursor.execute(
        """
        SELECT reasoning
        FROM llm_decisions
        WHERE code=? AND action=? AND date=? AND COALESCE(reasoning, '') <> ''
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (code, action, trade_date),
    )
    row = cursor.fetchone()
    if row and row[0] and not _is_no_response_decision(row[0], 0, ""):
        return sanitize_public_text(f"LLM决策: {row[0]}")
    return sanitize_public_text(raw_reason or "-")


def _trade_reason_map(cursor, rows):
    keys = {(str(row[2]), str(row[4]), str(row[1] or "")[:10]) for row in rows
            if not row[10] or str(row[10]).strip() == "TradePlan执行"}
    if not keys:
        return {}
    dates = sorted({key[2] for key in keys if key[2]})
    codes = sorted({key[0] for key in keys if key[0]})
    if not dates or not codes:
        return {}
    sql = ("SELECT code, action, date, reasoning FROM llm_decisions "
           f"WHERE code IN ({','.join('?' for _ in codes)}) "
           f"AND date IN ({','.join('?' for _ in dates)}) "
           "AND COALESCE(reasoning,'')<>'' ORDER BY created_at DESC,id DESC")
    mapping = {}
    for row in cursor.execute(sql, codes + dates):
        key = (row[0], row[1], row[2])
        if key in keys and key not in mapping and not _is_no_response_decision(row[3], 0, ""):
            mapping[key] = sanitize_public_text("LLM决策: " + row[3])
    return mapping


def _name_map(cursor, rows):
    codes = sorted({str(row["code"]) for row in rows})
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    names = {}
    for row in cursor.execute(f"SELECT code,name FROM positions WHERE code IN ({placeholders})", codes):
        if row[1] and row[1] != row[0]:
            names[row[0]] = row[1]
    for row in cursor.execute(
            f"SELECT code,name FROM trades WHERE code IN ({placeholders}) ORDER BY created_at DESC,id DESC", codes):
        if row[0] not in names and row[1] and row[1] != row[0]:
            names[row[0]] = row[1]
    return names

@router.get("/trades")
def get_trades(limit: int = Query(50, ge=1, le=200), page: int = Query(1, ge=1)):
    """获取历史交易成交明细"""
    try:
        with _get_db() as db:
            offset = (page - 1) * limit
            cursor = db.conn.cursor()
            cursor.execute(
                "SELECT id, created_at, code, name, action, price, shares, commission, pnl, pnl_pct, reason "
                "FROM trades ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            rows = cursor.fetchall()
            reasons = _trade_reason_map(cursor, rows)
            trades_list = []
            for r in rows:
                date_str = r[1] or ""
                if "T" in date_str:
                    date_str = date_str.replace("T", " ")
                if len(date_str) > 19:
                    date_str = date_str[:19]

                trades_list.append({
                    "id": r[0],
                    "date": date_str,
                    "code": r[2],
                    "name": r[3],
                    "action": r[4],
                    "price": r[5],
                    "shares": r[6],
                    "fee": r[7] or 0.0,
                    "pnl": _nullable_float(r[8]),
                    "pnl_pct": _nullable_float(r[9]),
                    "reason": reasons.get((str(r[2]), str(r[4]), str(r[1] or "")[:10]), sanitize_public_text(r[10] or "-"))
                })
            
            cursor.execute("SELECT COUNT(*) FROM trades")
            total = cursor.fetchone()[0]
            
            return {
                "success": True,
                "total": total,
                "page": page,
                "limit": limit,
                "trades": trades_list
            }
    except Exception as e:
        return unavailable()

@router.get("/decisions")
def get_decisions(limit: int = Query(10, ge=1, le=50), kind: Optional[str] = None,
                  page: int = 1, start_date: Optional[str] = None,
                  end_date: Optional[str] = None):
    """分页查询时点判断。历史记录缺少评分快照时不拼接最新缓存。"""
    try:
        if page < 1 or page > 1000:
            raise HTTPException(422, "页码超出范围")
        for value in (start_date, end_date):
            if value:
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError as exc:
                    raise HTTPException(422, "日期格式须为YYYY-MM-DD") from exc
        if kind not in (None, "all", "signal", "observation"):
            raise HTTPException(422, "未知的判断类型")
        effective_end = end_date or _now_bj().strftime("%Y-%m-%d")
        effective_start = start_date or (datetime.strptime(effective_end, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
        start_date, end_date = effective_start, effective_end
        if start_date and end_date and (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days > 366:
            raise HTTPException(422, "日期范围不得超过367天")
        if start_date and end_date and start_date > end_date:
            raise HTTPException(422, "开始日期不得晚于结束日期")
        conditions = ["NOT (COALESCE(reasoning, '')='LLM无响应' AND "
                      "TRIM(COALESCE(llm_response, ''))='' AND COALESCE(confidence, 0)<=0)"]
        params = []
        if kind == "signal":
            conditions.append("action IN ('BUY', 'SELL')")
        elif kind == "observation":
            conditions.append("action='HOLD'")
        for value, op in ((start_date, ">="), (end_date, "<=")):
            if value:
                conditions.append(f"date {op} ?")
                params.append(value)
        where = " AND ".join(conditions)
        with _get_db() as db:
            cursor = db.conn.cursor()
            total = cursor.execute(f"SELECT COUNT(*) FROM llm_decisions WHERE {where}", params).fetchone()[0]
            rows = cursor.execute(
                f"SELECT id, code, date, created_at, scan_id, action, reasoning, confidence, outcome, outcome_pct, dimensions, llm_prompt FROM llm_decisions WHERE {where} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
                params + [limit, (page - 1) * limit],
            ).fetchall()
            names = _name_map(cursor, rows)
            decisions = []
            for row in rows:
                r = dict(row)
                try:
                    evidence = json.loads(r.get("dimensions") or "{}")
                except (ValueError, TypeError):
                    evidence = {}
                dimensions = {}
                if isinstance(evidence, dict):
                    for key in ("technical", "capital", "sentiment", "emotion", "fundamental", "ml"):
                        item = evidence.get(key)
                        if isinstance(item, dict):
                            dimensions[key] = {field: item[field] for field in ("score", "confidence")
                                               if isinstance(item.get(field), (int, float))}
                decisions.append({
                    "id": r["id"], "code": r["code"],
                    "name": sanitize_public_text(_extract_name_from_prompt(r["llm_prompt"], r["code"]) or names.get(r["code"], r["code"]), 40),
                    "date": r["date"], "created_at": r["created_at"],
                    "scan_id": sanitize_public_text(r.get("scan_id"), 80),
                    "action": r["action"],
                    "decision_type": "signal" if r["action"] in ("BUY", "SELL") else "observation",
                    "reasoning": sanitize_public_text(r["reasoning"], 520),
                    "confidence": r["confidence"], "outcome": r["outcome"],
                    "outcome_pct": r["outcome_pct"], "dimensions": dimensions,
                    "evidence_available": bool(dimensions),
                })
            return {"success": True, "decisions": decisions, "total": total,
                    "page": page, "limit": limit, "has_more": page * limit < total}
    except HTTPException:
        raise
    except Exception as e:
        return unavailable()

@router.get("/lessons")
def get_lessons(limit: int = Query(20, ge=1, le=100)):
    """获取交易教训库"""
    try:
        with _get_db() as db:
            cursor = db.conn.cursor()
            cursor.execute(
                "SELECT id, date, category, content, importance, market_regime, related_trades "
                "FROM lessons ORDER BY date DESC, id DESC LIMIT ?",
                (limit,)
            )
            rows = cursor.fetchall()
            lessons_list = []
            for r in rows:
                lessons_list.append({
                    "id": r[0],
                    "date": r[1],
                    "category": r[2],
                    "content": sanitize_public_text(r[3], max_len=520),
                    "importance": r[4],
                    "market_regime": r[5],
                    "related_trades": r[6]
                })
            return {"success": True, "lessons": lessons_list}
    except Exception as e:
        return unavailable()

@router.get("/performance")
def get_performance(days: int = Query(10, ge=2, le=90)):
    """获取近N天收益及基准(对比沪深300)走势，用于业绩画图"""
    try:
        project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        reviews_dir = os.path.join(project_dir, "data", "reviews")
        
        perf_data = []
        if os.path.exists(reviews_dir):
            files = sorted([f for f in os.listdir(reviews_dir) if f.startswith("review_") and f.endswith(".json")])
            for f_name in files[-days:]:
                try:
                    f_path = os.path.join(reviews_dir, f_name)
                    with open(f_path, "r", encoding="utf-8") as file:
                        data = json.load(file)
                    
                    date_str = f_name.replace("review_", "").replace(".json", "")
                    perf_data.append({
                        "date": date_str,
                        "total_assets": _nullable_float(data.get("total_assets")),
                        "daily_pnl": _nullable_float(data.get("daily_pnl")),
                        "cumulative_pnl_pct": _nullable_float(data.get("cumulative_pnl_pct")),
                        "benchmark_pnl_pct": _nullable_float(data.get("benchmark_pnl_pct"))
                    })
                except Exception:
                    pass
        
        today = _now_bj().strftime("%Y-%m-%d")
        if perf_data and perf_data[-1].get("date") != today:
            try:
                from execution.paper_account import PaperAccount
                account = PaperAccount(read_only=True)
                total_assets = _account_total_assets_with_realtime(account)
                initial_capital = account.initial_capital or 0
                previous_assets = float(perf_data[-1].get("total_assets") or total_assets)
                cumulative_pnl_pct = (
                    (total_assets - initial_capital) / initial_capital
                    if initial_capital > 0 else 0.0
                )
                perf_data.append({
                    "date": today,
                    "total_assets": total_assets,
                    "daily_pnl": total_assets - previous_assets,
                    "cumulative_pnl_pct": cumulative_pnl_pct,
                    "benchmark_pnl_pct": perf_data[-1].get("benchmark_pnl_pct")
                })
                perf_data = perf_data[-days:]
            except Exception:
                pass

        if not perf_data:
            from execution.paper_account import PaperAccount
            account = PaperAccount(read_only=True)
            total_assets = _account_total_assets_with_realtime(account)
            perf_data.append({
                "date": today,
                "total_assets": total_assets,
                "daily_pnl": None,
                "cumulative_pnl_pct": None,
                "benchmark_pnl_pct": None
            })
            
        return {"success": True, "performance": _normalize_performance_daily_pnl(perf_data)}
    except Exception as e:
        return unavailable()
