from fastapi import APIRouter
from datetime import datetime, timezone
from web.snapshot_cache import cache
from web.api_errors import unavailable
from scheduler.agent_status import build_agent_status_snapshot
from web.public_safety import is_production, sanitize_public_text, sanitize_status_snapshot

router = APIRouter()


def _status_snapshot():
    from data.database import DB_PATH
    snapshot = cache.get(("system-status", DB_PATH), lambda: build_agent_status_snapshot(readonly=True))
    snapshot["fetched_at"] = datetime.now(timezone.utc).isoformat()
    snapshot["data_as_of"] = (snapshot.get("account") or {}).get("data_as_of")
    return snapshot


@router.get("/status")
def get_system_status():
    try:
        snapshot = _status_snapshot()
        return sanitize_status_snapshot(snapshot) if is_production() else snapshot
    except Exception:
        return unavailable()


@router.get("/public/status")
def get_public_system_status():
    try:
        return sanitize_status_snapshot(_status_snapshot())
    except Exception:
        return unavailable()


def _build_detailed_positions():
    """获取当前持仓的详细交易信息（包括浮盈亏、持仓占比、止损及决策）"""
    try:
        from execution.paper_account import PaperAccount
        account = PaperAccount(read_only=True)

        prices = {}
        quote_names = {}
        price_times = {}
        if account.positions:
            try:
                from data.realtime import get_realtime
                quotes = get_realtime(list(account.positions.keys()))
                for quote in quotes:
                    from data.quote_validation import validate_quote
                    validation = validate_quote(quote, expected_code=str(getattr(quote, "code", "")))
                    if validation.valid:
                        prices[quote.code] = validation.price
                        price_times[quote.code] = str(getattr(quote, "timestamp", "")) or None
                    if getattr(quote, "name", ""):
                        quote_names[quote.code] = quote.name
            except Exception:
                prices = {}

        # 看板的实时行情只参与本次响应计算，不得在 GET 请求中修改账户或数据库。
        total_assets = account.total_assets(prices or None)
        latest_decisions = {}
        if account.positions:
            try:
                from data.database import Database
                with Database(db_path=account.db_path, readonly=True) as db:
                    cursor = db.conn.cursor()
                    placeholders = ",".join("?" for _ in account.positions)
                    cursor.execute(
                        f"SELECT code, action, date, reasoning, confidence FROM llm_decisions "
                        f"WHERE code IN ({placeholders}) ORDER BY date DESC, id DESC",
                        list(account.positions),
                    )
                    for decision in cursor.fetchall():
                        code = decision[0]
                        if code not in latest_decisions:
                            latest_decisions[code] = {
                                "action": decision[1],
                                "date": decision[2],
                                "reasoning": sanitize_public_text(decision[3]),
                                "confidence": decision[4],
                            }
            except Exception:
                latest_decisions = {}
        pos_list = []
        for code, pos in account.positions.items():
            buy_price = pos.get("buy_price", 0.0)
            current_price = prices.get(code) or pos.get("current_price") or pos.get("buy_price", buy_price)
            shares = pos.get("shares", 0)
            cost = pos.get("cost") or (buy_price * shares)
            market_val = current_price * shares
            pnl = market_val - cost
            pnl_pct = (current_price - buy_price) / buy_price if buy_price > 0 else 0.0
            weight = market_val / total_assets if total_assets > 0 else 0.0
            
            # 计算 ATR 止损线
            import config
            from risk.stop_loss import StopLossManager
            slm = StopLossManager(atr_multiplier=config.ATR_MULTIPLIER, use_atr=config.USE_ATR_STOP)
            atr = pos.get("atr_at_buy", 0)
            levels = slm.get_stop_levels(
                buy_price=buy_price,
                highest_price=pos.get("highest_price", buy_price),
                atr=atr if atr > 0 else None
            )
            
            pos_list.append({
                "code": code,
                "name": quote_names.get(code) or pos.get("name") or code,
                "shares": shares,
                "buy_price": buy_price,
                "current_price": current_price,
                "price_fresh": code in prices,
                "price_source": "realtime" if code in prices else "stored",
                "price_as_of": price_times.get(code),
                "highest_price": pos.get("highest_price", buy_price),
                "market_value": market_val,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "weight": weight,
                "stop_loss_price": levels.get("stop_loss"),
                "take_profit_price": levels.get("take_profit"),
                "trailing_stop_price": levels.get("trailing_stop"),
                "latest_decision": latest_decisions.get(code)
            })
            
        return {"success": True, "positions": pos_list, "data_as_of": getattr(account, "updated_at", None)}
    except Exception as e:
        raise



def _build_shadow_leaderboard():
    """影子策略排行榜（只读）：各变体相对 baseline 的净收益对比与晋级候选。

    载荷只含变体名与数值指标，不含内部路径或凭证，公共仪表盘可安全展示。
    """
    try:
        from data.database import Database
        from strategy.shadow_eval import evaluate_variants
        from web.read_store import table
        with Database(readonly=True) as db:
            if not table(db.conn, "shadow_decisions"):
                return {"success": True, "available": False, "leaderboard": [], "promotion_candidates": []}
            leaderboard = evaluate_variants(db, ensure_schema=False)
            candidates = [row[0] for row in db.conn.execute(
                "SELECT variant_id FROM shadow_promotions WHERE status='candidate'"
            ).fetchall()] if table(db.conn, "shadow_promotions") else []
        return {"success": True, "available": True, "leaderboard": leaderboard,
                "promotion_candidates": sorted(candidates)}
    except Exception as e:
        raise


def _cached_result(key, builder):
    try:
        from data.database import DB_PATH
        result = cache.get((key, DB_PATH), builder)
        result["fetched_at"] = datetime.now(timezone.utc).isoformat()
        return result
    except Exception:
        return unavailable()


@router.get("/positions")
def get_detailed_positions():
    return _cached_result("positions", _build_detailed_positions)


@router.get("/shadow/leaderboard")
def get_shadow_leaderboard():
    return _cached_result("shadow", _build_shadow_leaderboard)
