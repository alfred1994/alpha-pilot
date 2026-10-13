"""Server-derived resource usage. No prices, checkout or client-granted upgrades."""
from datetime import datetime, timezone

from services import workspace_accounts
from services.identity import get_entitlements
from services.market_types import MarketError


def get_usage(user_id, now=None):
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    with workspace_accounts.connection() as conn:
        conn.execute("BEGIN")
        user = conn.execute("SELECT plan FROM workspace_users WHERE id=? AND is_active=1", (user_id,)).fetchone()
        if not user:
            raise MarketError("not_found", "用户不可用。")
        accounts = conn.execute("SELECT COUNT(*) FROM workspace_accounts WHERE user_id=?", (user_id,)).fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM workspace_strategies s JOIN workspace_accounts a ON a.id=s.account_id WHERE a.user_id=? AND s.enabled=1", (user_id,)).fetchone()[0]
        orders = conn.execute("SELECT COUNT(*) FROM workspace_orders o JOIN workspace_accounts a ON a.id=o.account_id WHERE a.user_id=? AND o.created_at>=? AND o.created_at<?", (user_id, workspace_accounts.iso(start), workspace_accounts.iso(end))).fetchone()[0]
        conn.rollback()
    limits = get_entitlements(user["plan"])
    return {
        "plan": {"id": "free", "name": "免费版", "status": "active"},
        "billing": {"enabled": False, "provider": None, "checkout_available": False},
        "limits": limits,
        "usage": {"accounts": accounts, "active_strategies": active, "filled_orders_this_month": orders},
        "period": {"start": workspace_accounts.iso(start), "end": workspace_accounts.iso(end), "timezone": "UTC"},
        "metering_note": "用量来自账户和模拟成交记录，当前不收费。",
    }
