"""Isolated paper portfolios. Financial changes commit with their audit ledger."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import re
import uuid
from zoneinfo import ZoneInfo

from services import workspace_db
from services.market_types import (
    MarketError, estimate_fill, get_instrument, get_market_spec,
    settlement_available_at, validate_quantity,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_accounts (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES workspace_users(id),
    market TEXT NOT NULL CHECK(market IN ('CN','US','CRYPTO')),
    name TEXT NOT NULL, currency TEXT NOT NULL, initial_cash TEXT NOT NULL,
    cash TEXT NOT NULL, realized_pnl TEXT NOT NULL DEFAULT '0', created_at TEXT NOT NULL,
    UNIQUE(user_id,market)
);
CREATE TABLE IF NOT EXISTS workspace_lots (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES workspace_accounts(id),
    instrument_id TEXT NOT NULL, quantity TEXT NOT NULL, unit_cost TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workspace_lots_account ON workspace_lots(account_id,instrument_id);
CREATE TABLE IF NOT EXISTS workspace_orders (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES workspace_accounts(id),
    request_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')), quantity TEXT NOT NULL,
    price TEXT NOT NULL, fee TEXT NOT NULL, realized_pnl TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'filled', source TEXT NOT NULL, quote_as_of TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(account_id,request_id)
);
CREATE TABLE IF NOT EXISTS workspace_settlements (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES workspace_accounts(id),
    amount TEXT NOT NULL, available_at TEXT NOT NULL, settled_at TEXT
);
CREATE TABLE IF NOT EXISTS workspace_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES workspace_accounts(id), order_id TEXT,
    kind TEXT NOT NULL, cash_delta TEXT NOT NULL, pending_delta TEXT NOT NULL,
    instrument_id TEXT, quantity_delta TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_strategies (
    account_id TEXT PRIMARY KEY REFERENCES workspace_accounts(id),
    enabled INTEGER NOT NULL DEFAULT 0, instrument_id TEXT NOT NULL,
    max_allocation TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
    high_watermark TEXT NOT NULL DEFAULT '100000', max_drawdown TEXT NOT NULL DEFAULT '0.10',
    version INTEGER NOT NULL DEFAULT 1, last_run_at TEXT, last_reason TEXT,
    last_bar_at TEXT, lease_token TEXT, lease_until TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_strategy_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL REFERENCES workspace_accounts(id),
    status TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_strategy_versions (
    account_id TEXT NOT NULL REFERENCES workspace_accounts(id), version INTEGER NOT NULL,
    instrument_id TEXT NOT NULL, max_allocation TEXT NOT NULL, max_drawdown TEXT NOT NULL,
    enabled INTEGER NOT NULL, created_at TEXT NOT NULL, origin TEXT NOT NULL,
    PRIMARY KEY(account_id,version)
);
CREATE TABLE IF NOT EXISTS workspace_worker_state (
    id INTEGER PRIMARY KEY CHECK(id=1), heartbeat_at TEXT NOT NULL
);
"""


def utc_now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def number(value):
    return format(Decimal(value), "f")


def require_paper():
    if os.environ.get("BROKER_MODE", "paper").lower() != "paper":
        raise MarketError("paper_required", "用户工作台仅支持模拟盘，请联系管理员检查运行模式。")


def initialize():
    workspace_db.init_db()
    conn = workspace_db.connect()
    try:
        with conn:
            conn.executescript(SCHEMA)
    finally:
        conn.close()
    from services import workspace_performance
    workspace_performance.initialize()


@contextmanager
def connection():
    initialize()
    conn = workspace_db.connect()
    try:
        yield conn
    finally:
        conn.close()


def instrument_from_id(instrument_id):
    if not isinstance(instrument_id, str) or ":" not in instrument_id:
        raise MarketError("invalid_instrument", "请选择支持的交易标的。")
    market, symbol = instrument_id.split(":", 1)
    return get_instrument(market, symbol)


def _owned(conn, user_id, account_id):
    row = conn.execute("SELECT a.* FROM workspace_accounts a JOIN workspace_users u ON u.id=a.user_id WHERE a.id=? AND a.user_id=? AND u.is_active=1", (account_id, user_id)).fetchone()
    if row is None:
        raise MarketError("not_found", "账户不存在。")
    return row


def _check_trading_user(conn, user_id):
    from services.identity import assert_email_verified
    user = conn.execute("SELECT email_verified FROM workspace_users WHERE id=? AND is_active=1", (user_id,)).fetchone()
    if user is None:
        raise MarketError("not_found", "用户不可用。")
    assert_email_verified(dict(user))


def _ledger(conn, account_id, kind, now, cash=0, pending=0, instrument_id=None, quantity=0, order_id=None):
    conn.execute(
        "INSERT INTO workspace_ledger(account_id,order_id,kind,cash_delta,pending_delta,instrument_id,quantity_delta,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (account_id, order_id, kind, number(cash), number(pending), instrument_id, number(quantity), iso(now)),
    )


def _settle(conn, account_id, now):
    rows = conn.execute("SELECT * FROM workspace_settlements WHERE account_id=? AND settled_at IS NULL AND available_at<=?", (account_id, iso(now))).fetchall()
    if not rows:
        return
    amount = sum((Decimal(r["amount"]) for r in rows), Decimal(0))
    current = conn.execute("SELECT cash FROM workspace_accounts WHERE id=?", (account_id,)).fetchone()
    conn.execute("UPDATE workspace_accounts SET cash=? WHERE id=?", (number(Decimal(current["cash"]) + amount), account_id))
    for row in rows:
        conn.execute("UPDATE workspace_settlements SET settled_at=? WHERE id=?", (iso(now), row["id"]))
    _ledger(conn, account_id, "settlement", now, cash=amount, pending=-amount)


def create_account(user_id, market, name=None, limits=None):
    require_paper()
    spec = get_market_spec(market)
    market = spec.market.value
    name = str(name or {"CN": "A 股模拟账户", "US": "美股模拟账户", "CRYPTO": "数字资产模拟账户"}[market]).strip()
    if not name or len(name) > 60:
        raise MarketError("invalid_name", "账户名称需为 1–60 个字符。")
    now = utc_now()
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM workspace_users WHERE id=? AND is_active=1", (user_id,)).fetchone():
            raise MarketError("not_found", "用户不可用。")
        _check_trading_user(conn, user_id)
        if conn.execute("SELECT 1 FROM workspace_accounts WHERE user_id=? AND market=?", (user_id, market)).fetchone():
            raise MarketError("account_exists", "该市场已有模拟账户，请直接切换账户。")
        count = conn.execute("SELECT COUNT(*) FROM workspace_accounts WHERE user_id=?", (user_id,)).fetchone()[0]
        if count >= (limits or {}).get("accounts", 3):
            raise MarketError("account_limit", "已达到当前套餐的模拟账户额度。")
        account_id = uuid.uuid4().hex
        conn.execute("INSERT INTO workspace_accounts(id,user_id,market,name,currency,initial_cash,cash,created_at) VALUES(?,?,?,?,?,?,?,?)", (account_id, user_id, market, name, spec.currency, "100000", "100000", iso(now)))
        _ledger(conn, account_id, "initial_cash", now, cash=100000)
        from services.workspace_performance import seed_snapshot
        seed_snapshot(conn, _owned(conn, user_id, account_id), now)
    return get_account(user_id, account_id)["account"]


def _summary(row, lots, settlements, now):
    # Matured proceeds are spendable even before the next mutating transaction materializes them.
    matured = sum((Decimal(r["amount"]) for r in settlements if r["available_at"] <= iso(now)), Decimal(0))
    pending = sum((Decimal(r["amount"]) for r in settlements if r["available_at"] > iso(now)), Decimal(0))
    cash = Decimal(row["cash"]) + matured
    result = {k: row[k] for k in ("id", "market", "name", "currency", "initial_cash", "realized_pnl", "created_at")}
    result.update(cash=number(cash), pending_cash=number(pending), positions_count=len({r["instrument_id"] for r in lots}), equity=number(cash + pending) if not lots else None, valuation_status="current" if not lots else "unavailable", mode="paper")
    return result


def list_accounts(user_id):
    now = utc_now()
    with connection() as conn:
        rows = conn.execute("SELECT * FROM workspace_accounts WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()
        return [_summary(row, conn.execute("SELECT * FROM workspace_lots WHERE account_id=?", (row["id"],)).fetchall(), conn.execute("SELECT * FROM workspace_settlements WHERE account_id=? AND settled_at IS NULL", (row["id"],)).fetchall(), now) for row in rows]


def _order_dict(row):
    result = dict(row)
    result.pop("account_id", None)
    result["symbol"] = row["instrument_id"].split(":", 1)[1]
    return result


def get_account(user_id, account_id, quotes=None, now=None):
    fixed_now = now
    now = now or utc_now()
    with connection() as conn:
        conn.execute("BEGIN")
        row = _owned(conn, user_id, account_id)
        lots = conn.execute("SELECT * FROM workspace_lots WHERE account_id=? ORDER BY acquired_at,id", (account_id,)).fetchall()
        settlements = conn.execute("SELECT * FROM workspace_settlements WHERE account_id=? AND settled_at IS NULL", (account_id,)).fetchall()
        orders = conn.execute("SELECT * FROM workspace_orders WHERE account_id=? ORDER BY created_at DESC,id DESC LIMIT 100", (account_id,)).fetchall()
        revision = conn.execute("SELECT COALESCE(MAX(id),0) FROM workspace_ledger WHERE account_id=?", (account_id,)).fetchone()[0]
        strategy = conn.execute("SELECT * FROM workspace_strategies WHERE account_id=?", (account_id,)).fetchone()
        events = conn.execute("SELECT status,reason,created_at FROM workspace_strategy_events WHERE account_id=? ORDER BY id DESC LIMIT 20", (account_id,)).fetchall()
        worker = conn.execute("SELECT heartbeat_at FROM workspace_worker_state WHERE id=1").fetchone()
        conn.rollback()
    account = _summary(row, lots, settlements, now)
    account["ledger_sequence"] = revision
    grouped = {}
    for lot in lots:
        entry = grouped.setdefault(lot["instrument_id"], {"quantity": Decimal(0), "cost": Decimal(0), "available": Decimal(0)})
        qty = Decimal(lot["quantity"])
        entry["quantity"] += qty
        entry["cost"] += qty * Decimal(lot["unit_cost"])
        acquired_date = datetime.fromisoformat(lot["acquired_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date()
        if row["market"] != "CN" or acquired_date < now.astimezone(ZoneInfo("Asia/Shanghai")).date():
            entry["available"] += qty
    positions = []
    total = Decimal(account["cash"]) + Decimal(account["pending_cash"])
    complete = True
    for iid, entry in grouped.items():
        instrument = instrument_from_id(iid)
        item = {"instrument_id": iid, "symbol": instrument.symbol, "name": instrument.name, "quantity": number(entry["quantity"]), "available_quantity": number(entry["available"]), "average_cost": number(entry["cost"] / entry["quantity"]), "last_price": None, "market_value": None, "unrealized_pnl": None, "quote_status": "unavailable"}
        try:
            if quotes is None:
                raise MarketError("quote_unavailable", "尚未估值。")
            quote = quotes.get_quote(instrument.market, instrument.symbol, now=fixed_now)
            value = quote.price * entry["quantity"]
            item.update(last_price=number(quote.price), market_value=number(value), unrealized_pnl=number(value - entry["cost"]), quote_status=quote.status, quote_as_of=iso(quote.as_of))
            total += value
        except MarketError:
            complete = False
        positions.append(item)
    account.update(equity=number(total) if complete else None, valuation_status="current" if complete else "unavailable")
    strategy_data = None
    if strategy:
        strategy_data = {k: strategy[k] for k in ("version", "instrument_id", "max_allocation", "status", "max_drawdown", "last_run_at", "last_reason", "last_bar_at")}
        strategy_data["enabled"] = bool(strategy["enabled"])
        strategy_data["name"] = "趋势跟随（MA20/50）"
    heartbeat = worker["heartbeat_at"] if worker else None
    worker_running = bool(heartbeat and 0 <= (now - datetime.fromisoformat(heartbeat)).total_seconds() < 180)
    return {"account": account, "positions": positions, "orders": [_order_dict(r) for r in orders], "strategy": strategy_data, "strategy_events": [dict(r) for r in events], "worker": {"running": worker_running, "heartbeat_at": heartbeat}}


def place_order(user_id, account_id, instrument_id, side, quantity, request_id, quotes, now=None, source="manual", strategy_guard=None):
    require_paper()
    fixed_now = now
    now = now or utc_now()
    instrument = instrument_from_id(instrument_id)
    # Persist canonical identifiers so aliases cannot duplicate positions or idempotency keys.
    instrument_id = f"{instrument.market.value}:{instrument.symbol}"
    quantity = validate_quantity(instrument, quantity)
    if side not in ("buy", "sell") or not re.fullmatch(r"[A-Za-z0-9:_-]{8,120}", request_id or ""):
        raise MarketError("invalid_order", "委托方向或请求标识无效。")

    def existing(conn):
        found = conn.execute("SELECT * FROM workspace_orders WHERE account_id=? AND request_id=?", (account_id, request_id)).fetchone()
        if found and (found["instrument_id"] != instrument_id or found["side"] != side or Decimal(found["quantity"]) != quantity):
            raise MarketError("idempotency_conflict", "该请求标识已用于其他委托。")
        return found

    with connection() as conn:
        row = _owned(conn, user_id, account_id)
        if row["market"] != instrument.market.value:
            raise MarketError("market_mismatch", "标的与当前账户市场不一致。")
        prior = existing(conn)
    if prior:
        return {"order": _order_dict(prior), "replayed": True}
    quote = quotes.get_quote(instrument.market, instrument.symbol, now=fixed_now)
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _owned(conn, user_id, account_id)
        _check_trading_user(conn, user_id)
        prior = existing(conn)
        if prior:
            return {"order": _order_dict(prior), "replayed": True}
        # Network and database lock waits can cross market close or quote expiry.
        now = fixed_now or utc_now()
        fill = estimate_fill(instrument, side, quantity, quote, now=now)
        if strategy_guard:
            strategy = conn.execute("SELECT * FROM workspace_strategies WHERE account_id=?", (account_id,)).fetchone()
            if not strategy or not strategy["enabled"] or strategy["version"] != strategy_guard["version"] or strategy["lease_token"] != strategy_guard["lease_token"] or strategy["lease_until"] < iso(now):
                raise MarketError("strategy_changed", "策略状态已变化，本次委托已取消。")
        _settle(conn, account_id, now)
        row = _owned(conn, user_id, account_id)
        cash = Decimal(row["cash"])
        fee = fill.fee
        notional = fill.notional
        realized = Decimal(0)
        pending_delta = Decimal(0)
        order_id = uuid.uuid4().hex
        if side == "buy":
            cost = notional + fee
            if cost > cash:
                raise MarketError("insufficient_funds", "可用模拟资金不足；美股待结算资金需下一交易日到账。")
            if strategy_guard:
                # Enforce the allocation again inside the write lock, after any concurrent order.
                held = conn.execute("SELECT quantity FROM workspace_lots WHERE account_id=? AND instrument_id=?", (account_id, instrument_id)).fetchall()
                held_qty = sum((Decimal(r["quantity"]) for r in held), Decimal(0))
                if (held_qty + quantity) * fill.price > Decimal(strategy_guard["max_position_value"]):
                    raise MarketError("allocation_limit", "本次委托超过策略仓位上限。")
            cash_delta = -cost
            conn.execute("INSERT INTO workspace_lots(id,account_id,instrument_id,quantity,unit_cost,acquired_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, account_id, instrument_id, number(quantity), number(cost / quantity), iso(now)))
        else:
            lots = conn.execute("SELECT * FROM workspace_lots WHERE account_id=? AND instrument_id=? ORDER BY acquired_at,id", (account_id, instrument_id)).fetchall()
            if sum((Decimal(r["quantity"]) for r in lots), Decimal(0)) < quantity:
                raise MarketError("insufficient_position", "可卖持仓不足，不支持做空。")
            remaining = quantity
            cost_basis = Decimal(0)
            local_day = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
            for lot in lots:
                acquired = datetime.fromisoformat(lot["acquired_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date()
                if row["market"] == "CN" and acquired >= local_day:
                    continue
                taken = min(remaining, Decimal(lot["quantity"]))
                cost_basis += taken * Decimal(lot["unit_cost"])
                rest = Decimal(lot["quantity"]) - taken
                if rest:
                    conn.execute("UPDATE workspace_lots SET quantity=? WHERE id=?", (number(rest), lot["id"]))
                else:
                    conn.execute("DELETE FROM workspace_lots WHERE id=?", (lot["id"],))
                remaining -= taken
                if not remaining:
                    break
            if remaining:
                raise MarketError("t_plus_one", "A 股 ETF 当日买入部分需下一交易日才可卖出。")
            proceeds = notional - fee
            if proceeds <= 0:
                raise MarketError("minimum_notional", "成交金额不足以支付模拟手续费。")
            available_at = settlement_available_at(instrument, now)
            cash_delta = proceeds if available_at <= now else Decimal(0)
            pending_delta = proceeds - cash_delta
            if pending_delta:
                conn.execute("INSERT INTO workspace_settlements(id,account_id,amount,available_at) VALUES(?,?,?,?)", (uuid.uuid4().hex, account_id, number(pending_delta), iso(available_at)))
            realized = proceeds - cost_basis
        conn.execute("UPDATE workspace_accounts SET cash=?,realized_pnl=? WHERE id=?", (number(cash + cash_delta), number(Decimal(row["realized_pnl"]) + realized), account_id))
        conn.execute("INSERT INTO workspace_orders(id,account_id,request_id,instrument_id,side,quantity,price,fee,realized_pnl,source,quote_as_of,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (order_id, account_id, request_id, instrument_id, side, number(quantity), number(fill.price), number(fee), number(realized), source, iso(quote.as_of), iso(now)))
        _ledger(conn, account_id, side, now, cash=cash_delta, pending=pending_delta, instrument_id=instrument_id, quantity=quantity if side == "buy" else -quantity, order_id=order_id)
        result = conn.execute("SELECT * FROM workspace_orders WHERE id=?", (order_id,)).fetchone()
    return {"order": _order_dict(result), "replayed": False}


def configure_strategy(user_id, account_id, enabled, instrument_id, max_allocation, quotes):
    require_paper()
    instrument = instrument_from_id(instrument_id)
    try:
        allocation = Decimal(str(max_allocation))
        if not allocation.is_finite() or not Decimal("0.01") <= allocation <= Decimal("0.20"):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise MarketError("invalid_allocation", "策略仓位上限需为 1%–20%。") from None
    instrument_id = f"{instrument.market.value}:{instrument.symbol}"
    snapshot = get_account(user_id, account_id, quotes=quotes)
    if instrument.market.value != snapshot["account"]["market"]:
        raise MarketError("market_mismatch", "策略标的与账户市场不一致。")
    if enabled and instrument.market.value == "CN":
        raise MarketError("strategy_unavailable", "新工作台暂未开放 A 股自动策略；可使用模拟委托或查看原有 AI 看板。")
    if enabled and snapshot["account"]["equity"] is None:
        raise MarketError("valuation_unavailable", "持仓行情暂不可用，无法建立策略风险基准。")
    now = utc_now()
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _owned(conn, user_id, account_id)
        if enabled:
            now = utc_now()
            _check_trading_user(conn, user_id)
            revision = conn.execute("SELECT COALESCE(MAX(id),0) FROM workspace_ledger WHERE account_id=?", (account_id,)).fetchone()[0]
            if revision != snapshot["account"]["ledger_sequence"]:
                raise MarketError("account_changed", "账户在配置期间发生交易，请刷新后重试。")
            ttl = get_market_spec(snapshot["account"]["market"]).max_quote_age_seconds
            for position in snapshot["positions"]:
                stamp = datetime.fromisoformat(position["quote_as_of"])
                if not -1 <= (now - stamp).total_seconds() <= ttl:
                    raise MarketError("stale_quote", "配置期间持仓行情已过期，请刷新后重试。")
        current = conn.execute("SELECT * FROM workspace_strategies WHERE account_id=?", (account_id,)).fetchone()
        if current:
            _record_strategy_version(conn, current, now, "observed_existing")
            if instrument_id != current["instrument_id"] and conn.execute("SELECT 1 FROM workspace_lots WHERE account_id=? AND instrument_id=?", (account_id, current["instrument_id"])).fetchone():
                raise MarketError("strategy_position_open", "原策略标的仍有持仓，请先暂停策略并处理持仓，再切换标的。")
        # A pause/resume must not erase a drawdown. The running portfolio owns its risk baseline.
        high = current["high_watermark"] if current else snapshot["account"]["equity"] or snapshot["account"]["initial_cash"]
        if enabled and current and current["status"] == "risk_paused":
            raise MarketError("risk_paused", "策略已触及回撤限制，自动策略已暂停；请先检查持仓与风险。")
        unchanged = current and bool(current["enabled"]) == enabled and current["instrument_id"] == instrument_id and Decimal(current["max_allocation"]) == allocation
        if unchanged:
            # Saving the same form must not reset the last-bar checkpoint or create a new order key.
            return {k: (bool(current[k]) if k == "enabled" else current[k]) for k in ("version", "enabled", "instrument_id", "max_allocation", "max_drawdown", "status", "last_run_at", "last_reason", "last_bar_at")}
        status = "waiting" if enabled else "paused"
        reason = "等待独立策略任务执行。" if enabled else "用户已暂停策略。"
        conn.execute("INSERT INTO workspace_strategies(account_id,enabled,instrument_id,max_allocation,status,high_watermark,last_reason,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled,instrument_id=excluded.instrument_id,max_allocation=excluded.max_allocation,status=CASE WHEN workspace_strategies.status='risk_paused' THEN 'risk_paused' ELSE excluded.status END,version=workspace_strategies.version+1,last_reason=excluded.last_reason,lease_token=NULL,lease_until=NULL,last_bar_at=NULL,updated_at=excluded.updated_at", (account_id, int(enabled), instrument_id, number(allocation), status, high, reason, iso(now)))
        conn.execute("INSERT INTO workspace_strategy_events(account_id,status,reason,created_at) VALUES(?,?,?,?)", (account_id, status, reason, iso(now)))
        _record_strategy_version(conn, conn.execute("SELECT * FROM workspace_strategies WHERE account_id=?", (account_id,)).fetchone(), now, "user_change")
    return get_account(user_id, account_id)["strategy"]


def _record_strategy_version(conn, row, now, origin):
    # Existing strategies have no historical version archive. Mark the moment we
    # first observed them rather than inventing a past publication timestamp.
    conn.execute("INSERT OR IGNORE INTO workspace_strategy_versions(account_id,version,instrument_id,max_allocation,max_drawdown,enabled,created_at,origin) VALUES(?,?,?,?,?,?,?,?)", (row["account_id"], row["version"], row["instrument_id"], row["max_allocation"], row["max_drawdown"], row["enabled"], iso(now), origin))


def strategy_history(user_id, account_id):
    with connection() as conn:
        _owned(conn, user_id, account_id)
        rows = conn.execute("SELECT version,instrument_id,max_allocation,max_drawdown,enabled,created_at,origin FROM workspace_strategy_versions WHERE account_id=? ORDER BY version DESC LIMIT 50", (account_id,)).fetchall()
        events = conn.execute("SELECT status,reason,created_at FROM workspace_strategy_events WHERE account_id=? ORDER BY id DESC LIMIT 50", (account_id,)).fetchall()
    return {"versions": [{**dict(r), "enabled": bool(r["enabled"])} for r in rows], "events": [dict(r) for r in events]}
