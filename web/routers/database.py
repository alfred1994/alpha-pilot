from fastapi import APIRouter, Query
from typing import Optional
import os
import json
import re
from datetime import datetime, timedelta
from web.public_safety import is_production, public_error_message, sanitize_public_text

router = APIRouter()

def _get_db():
    from data.database import Database
    return Database()


def _account_total_assets_with_realtime(account):
    """账户总资产优先按实时价估算，行情不可用时回退到账户保存价格。"""
    prices = {}
    if getattr(account, "positions", None):
        try:
            from data.realtime import get_realtime
            quotes = get_realtime(list(account.positions.keys()))
            prices = {
                quote.code: quote.price
                for quote in quotes
                if getattr(quote, "price", 0) > 0
            }
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

@router.get("/trades")
def get_trades(limit: int = Query(50, ge=1, le=200), page: int = Query(1, ge=1)):
    """获取历史交易成交明细"""
    try:
        with _get_db() as db:
            offset = (page - 1) * limit
            cursor = db.conn.cursor()
            try:
                db.backfill_missing_trade_pnl()
            except Exception:
                pass
            cursor.execute(
                "SELECT id, created_at, code, name, action, price, shares, commission, pnl, pnl_pct, reason "
                "FROM trades ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            rows = cursor.fetchall()
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
                    "reason": _resolve_trade_reason(cursor, r[2], r[4], r[10], date_str)
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
        return {"success": False, "error": public_error_message() if is_production() else str(e)}

@router.get("/decisions")
def get_decisions(limit: int = Query(10, ge=1, le=50)):
    """获取最新 LLM 决策记录，包含5维得分及链式推理思维栈"""
    try:
        with _get_db() as db:
            cursor = db.conn.cursor()
            fetch_limit = min(limit * 5, 200)
            cursor.execute(
                "SELECT id, code, date, action, reasoning, confidence, outcome, outcome_pct, llm_response, llm_prompt "
                "FROM llm_decisions ORDER BY date DESC, id DESC LIMIT ?",
                (fetch_limit,)
            )
            rows = cursor.fetchall()
            decisions_list = []
            
            cache_data = _load_signal_cache()
            raw_scores = cache_data.get("raw_scores", [])
            scores_map = {item.get("code"): item for item in raw_scores if "code" in item}

            for r in rows:
                code = r[1]
                if _is_no_response_decision(r[4], r[5], r[8]):
                    continue
                dimensions = {}
                score_item = scores_map.get(code)
                if score_item:
                    dimensions = score_item.get("dimensions", {})
                
                decisions_list.append({
                    "id": r[0],
                    "code": code,
                    "name": _resolve_stock_name(cursor, code, scores_map, r[9]),
                    "date": r[2],
                    "action": r[3],
                    "reasoning": sanitize_public_text(r[4], max_len=520),
                    "confidence": r[5],
                    "outcome": r[6],
                    "outcome_pct": r[7],
                    "dimensions": dimensions
                })
                if len(decisions_list) >= limit:
                    break
            return {"success": True, "decisions": decisions_list}
    except Exception as e:
        return {"success": False, "error": public_error_message() if is_production() else str(e)}

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
        return {"success": False, "error": public_error_message() if is_production() else str(e)}

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
                        "total_assets": data.get("total_assets", 1000000.0),
                        "daily_pnl": data.get("daily_pnl", 0.0),
                        "cumulative_pnl_pct": data.get("cumulative_pnl_pct", 0.0),
                        "benchmark_pnl_pct": data.get("benchmark_pnl_pct", 0.0)
                    })
                except Exception:
                    pass
        
        today = datetime.now().strftime("%Y-%m-%d")
        if perf_data and perf_data[-1].get("date") != today:
            try:
                from execution.paper_account import PaperAccount
                account = PaperAccount()
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
                    "benchmark_pnl_pct": perf_data[-1].get("benchmark_pnl_pct", 0.0)
                })
                perf_data = perf_data[-days:]
            except Exception:
                pass

        if not perf_data:
            from execution.paper_account import PaperAccount
            account = PaperAccount()
            total_assets = _account_total_assets_with_realtime(account)
            perf_data.append({
                "date": today,
                "total_assets": total_assets,
                "daily_pnl": 0.0,
                "cumulative_pnl_pct": 0.0,
                "benchmark_pnl_pct": 0.0
            })
            
        return {"success": True, "performance": _normalize_performance_daily_pnl(perf_data)}
    except Exception as e:
        return {"success": False, "error": public_error_message() if is_production() else str(e)}
