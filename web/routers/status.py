from fastapi import APIRouter
from scheduler.agent_status import build_agent_status_snapshot
from web.public_safety import is_internal_status_exposed, is_production, public_error_message, sanitize_public_text, sanitize_status_snapshot

router = APIRouter()


@router.get("/status")
def get_system_status():
    """获取系统运行健康状态、Watchdog及仓位账户信息"""
    snapshot = build_agent_status_snapshot()
    if is_production() and not is_internal_status_exposed():
        return sanitize_status_snapshot(snapshot)
    return snapshot


@router.get("/public/status")
def get_public_system_status():
    """获取公开仪表盘使用的脱敏状态快照"""
    return sanitize_status_snapshot(build_agent_status_snapshot())


@router.get("/positions")
def get_detailed_positions():
    """获取当前持仓的详细交易信息（包括浮盈亏、持仓占比、止损及决策）"""
    try:
        from execution.paper_account import PaperAccount
        account = PaperAccount()

        prices = {}
        quote_names = {}
        if account.positions:
            try:
                from data.realtime import get_realtime
                quotes = get_realtime(list(account.positions.keys()))
                for quote in quotes:
                    if getattr(quote, "price", 0) > 0:
                        prices[quote.code] = quote.price
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
                with Database(db_path=account.db_path) as db:
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
            
        return {"success": True, "positions": pos_list}
    except Exception as e:
        return {"success": False, "error": public_error_message() if is_production() else str(e)}

