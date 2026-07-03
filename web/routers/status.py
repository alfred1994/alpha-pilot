from fastapi import APIRouter
from scheduler.agent_status import build_agent_status_snapshot
from web.public_safety import is_internal_status_exposed, is_production, public_error_message, sanitize_public_text, sanitize_status_snapshot

router = APIRouter()


def _persist_realtime_valuation(account, prices: dict):
    """把网页实时估值同步回SQLite，保持查库口径与网页口径一致。"""
    if not prices or not getattr(account, "positions", None):
        return

    try:
        from data.database import Database

        positions = {}
        for code, pos in account.positions.items():
            item = dict(pos)
            price = prices.get(code)
            if price and price > 0:
                item["current_price"] = price
                pos["current_price"] = price
            positions[code] = item

        market_value = sum(
            (pos.get("current_price") or pos.get("buy_price") or 0) * (pos.get("shares") or 0)
            for pos in positions.values()
        )
        with Database(db_path=getattr(account, "db_path", None)) as db:
            for pos in positions.values():
                db.upsert_position(pos)
            db.save_account_state({
                "initial_capital": getattr(account, "initial_capital", 0),
                "cash": getattr(account, "cash", 0),
                "total_assets": getattr(account, "cash", 0) + market_value,
                "positions": positions,
            })
    except Exception:
        pass


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

        total_assets = account.total_assets(prices or None)
        _persist_realtime_valuation(account, prices)
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
            
            # 获取最近一次 LLM 决策
            latest_decision = None
            try:
                from data.database import Database
                with Database() as db:
                    decs = db.get_llm_decisions(code=code, limit=1)
                    if decs:
                        latest_decision = {
                            "action": decs[0].get("action"),
                            "date": decs[0].get("date"),
                            "reasoning": sanitize_public_text(decs[0].get("reasoning")),
                            "confidence": decs[0].get("confidence")
                        }
            except Exception:
                pass

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
                "latest_decision": latest_decision
            })
            
        return {"success": True, "positions": pos_list}
    except Exception as e:
        return {"success": False, "error": public_error_message() if is_production() else str(e)}

