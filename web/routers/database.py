from fastapi import APIRouter, Query
from typing import Optional
import os
import json
from datetime import datetime, timedelta

router = APIRouter()

def _get_db():
    from data.database import Database
    return Database()

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
                    "pnl": r[8] or 0.0,
                    "pnl_pct": r[9] or 0.0,
                    "reason": r[10]
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
        return {"success": False, "error": str(e)}

@router.get("/decisions")
def get_decisions(limit: int = Query(10, ge=1, le=50)):
    """获取最新 LLM 决策记录，包含5维得分及链式推理思维栈"""
    try:
        with _get_db() as db:
            cursor = db.conn.cursor()
            cursor.execute(
                "SELECT id, code, date, action, reasoning, confidence, outcome, outcome_pct "
                "FROM llm_decisions ORDER BY date DESC, id DESC LIMIT ?",
                (limit,)
            )
            rows = cursor.fetchall()
            decisions_list = []
            
            project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            cache_file = os.path.join(project_dir, "data", "signal_cache.json")
            cache_data = {}
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        cache_data = json.load(f)
                except Exception:
                    pass
            
            raw_scores = cache_data.get("raw_scores", [])
            scores_map = {item.get("code"): item for item in raw_scores if "code" in item}

            for r in rows:
                code = r[1]
                dimensions = {}
                score_item = scores_map.get(code)
                if score_item:
                    dimensions = score_item.get("dimensions", {})
                
                decisions_list.append({
                    "id": r[0],
                    "code": code,
                    "date": r[2],
                    "action": r[3],
                    "reasoning": r[4],
                    "confidence": r[5],
                    "outcome": r[6],
                    "outcome_pct": r[7],
                    "dimensions": dimensions
                })
            return {"success": True, "decisions": decisions_list}
    except Exception as e:
        return {"success": False, "error": str(e)}

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
                    "content": r[3],
                    "importance": r[4],
                    "market_regime": r[5],
                    "related_trades": r[6]
                })
            return {"success": True, "lessons": lessons_list}
    except Exception as e:
        return {"success": False, "error": str(e)}

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
        
        if not perf_data:
            from execution.paper_account import PaperAccount
            account = PaperAccount()
            today = datetime.now().strftime("%Y-%m-%d")
            perf_data.append({
                "date": today,
                "total_assets": account.total_assets(),
                "daily_pnl": 0.0,
                "cumulative_pnl_pct": 0.0,
                "benchmark_pnl_pct": 0.0
            })
            
        return {"success": True, "performance": perf_data}
    except Exception as e:
        return {"success": False, "error": str(e)}
