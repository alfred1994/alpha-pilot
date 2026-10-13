"""Bounded MA20/50 paper strategy worker: python -m services.workspace_runner.

Uses completed daily bars, current executable quotes and durable per-account leases.
It neither imports nor drives the legacy single-account AI trading loop.
"""
import argparse
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
import logging
import time
import uuid

from services import workspace_accounts as store
from services.market_quotes import QuoteService
from services.identity import IdentityError, require_verified_email
from services.market_types import MarketError, get_market_spec, market_session

log = logging.getLogger(__name__)


def _finish(account_id, token, version, now, status, reason, bar=None, high=None, risk_paused=False):
    with store.connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM workspace_strategies WHERE account_id=? AND lease_token=? AND version=?", (account_id, token, version)).fetchone()
        if not row:
            return
        conn.execute("UPDATE workspace_strategies SET status=?,last_reason=?,last_run_at=?,last_bar_at=?,high_watermark=?,enabled=?,lease_token=NULL,lease_until=NULL WHERE account_id=?", (status, reason, store.iso(now), store.iso(bar) if bar else row["last_bar_at"], store.number(high) if high is not None else row["high_watermark"], 0 if risk_paused else row["enabled"], account_id))
        # Keep the explanation history compact; repeated idle ticks still update the heartbeat.
        if status != row["status"] or reason != row["last_reason"] or bar and store.iso(bar) != row["last_bar_at"]:
            conn.execute("INSERT INTO workspace_strategy_events(account_id,status,reason,created_at) VALUES(?,?,?,?)", (account_id, status, reason, store.iso(now)))


def _execute(job, token, quotes, clock, fixed_time=False):
    now = clock()
    account_id, user_id = job["account_id"], job["user_id"]
    instrument = store.instrument_from_id(job["instrument_id"])
    session = market_session(instrument.market, now)
    if not session.is_open:
        _finish(account_id, token, job["version"], now, "waiting_market", session.reason)
        return "waiting_market"
    snapshot = store.get_account(user_id, account_id, quotes=quotes, now=now if fixed_time else None)
    if snapshot["account"]["equity"] is None:
        raise MarketError("valuation_unavailable", "无法完整估值，已暂停本轮策略。")
    equity = Decimal(snapshot["account"]["equity"])
    high = max(Decimal(job["high_watermark"]), equity)
    if high > 0 and (high - equity) / high >= Decimal(job["max_drawdown"]):
        _finish(account_id, token, job["version"], now, "risk_paused", "账户回撤达到 10%，策略已暂停；请检查持仓。", high=high, risk_paused=True)
        return "risk_paused"
    bars = quotes.get_bars(instrument.market, instrument.symbol, limit=80, now=now if fixed_time else None)
    if len(bars) < 50:
        raise MarketError("insufficient_history", "至少需要 50 根已收盘日线，暂不产生模拟委托。")
    latest = bars[-1].as_of
    if job["last_bar_at"] and store.iso(latest) <= job["last_bar_at"]:
        _finish(account_id, token, job["version"], now, "observing", "最近已收盘日线已处理，等待下一根日线。", high=high)
        return "observing"
    # Recover a crash after the money transaction committed but before the strategy
    # checkpoint. Recomputing quantity here would conflict with the original fill.
    bar_key = f"strategy:{job['version']}:{int(latest.timestamp())}"
    with store.connection() as conn:
        completed = conn.execute("SELECT 1 FROM workspace_orders WHERE account_id=? AND source='ma20_50' AND request_id IN (?,?)", (account_id, f"{bar_key}:buy", f"{bar_key}:sell")).fetchone()
    if completed:
        _finish(account_id, token, job["version"], clock(), "executed", "已恢复该日线的模拟成交记录，无需重复委托。", bar=latest, high=high)
        return "executed"
    fast = sum((b.close for b in bars[-20:]), Decimal(0)) / 20
    slow = sum((b.close for b in bars[-50:]), Decimal(0)) / 50
    held = next((p for p in snapshot["positions"] if p["instrument_id"] == instrument.instrument_id), None)
    current_qty = Decimal(held["quantity"]) if held else Decimal(0)
    side, quantity = None, Decimal(0)
    max_value = equity * Decimal(job["max_allocation"])
    if fast > slow:
        quote = quotes.get_quote(instrument.market, instrument.symbol, now=now if fixed_time else None)
        spec = get_market_spec(instrument.market)
        # Reserve spread, slippage and commission before choosing a whole quantity increment.
        price = (quote.ask or quote.price) * (1 + spec.slippage_rate) + instrument.price_tick
        budget = min(Decimal(snapshot["account"]["cash"]), max(Decimal(0), max_value - current_qty * price))
        spendable = max(Decimal(0), budget - spec.minimum_commission)
        quantity = (spendable / (price * (1 + spec.commission_rate)) / instrument.quantity_step).to_integral_value(rounding=ROUND_FLOOR) * instrument.quantity_step
        side = "buy"
        reason = "MA20 高于 MA50，按仓位上限建立模拟持仓。"
        if quantity * price < max(instrument.min_notional, quote.minimum_notional or Decimal(0)):
            quantity = Decimal(0)
    else:
        quantity = Decimal(held["available_quantity"]) if held else Decimal(0)
        side = "sell"
        reason = "MA20 不高于 MA50，退出该标的模拟持仓。"
    if quantity > 0:
        # Stable across worker crashes: a retry returns the already committed order.
        request_id = f"strategy:{job['version']}:{int(latest.timestamp())}:{side}"
        store.place_order(user_id, account_id, instrument.instrument_id, side, store.number(quantity), request_id, quotes, now=clock() if fixed_time else None, source="ma20_50", strategy_guard={"version": job["version"], "lease_token": token, "max_position_value": store.number(max_value)})
        status = "executed"
    else:
        reason = "无需调整持仓，或可用资金不足最小委托单位。"
        status = "observing"
    _finish(account_id, token, job["version"], clock(), status, reason, bar=latest, high=high)
    return status


def run_once(quotes=None, now=None, batch_size=100):
    store.require_paper()
    fixed_time = now is not None
    clock = (lambda: now) if fixed_time else store.utc_now
    now = now or store.utc_now()
    quotes = quotes or QuoteService()
    with store.connection() as conn, conn:
        conn.execute("INSERT INTO workspace_worker_state(id,heartbeat_at) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at", (store.iso(now),))
        jobs = conn.execute("SELECT s.*,a.user_id FROM workspace_strategies s JOIN workspace_accounts a ON a.id=s.account_id JOIN workspace_users u ON u.id=a.user_id WHERE s.enabled=1 AND u.is_active=1 AND (?=0 OR u.email_verified=1) AND (s.lease_until IS NULL OR s.lease_until<?) ORDER BY COALESCE(s.last_run_at,'') LIMIT ?", (int(require_verified_email()), store.iso(now), min(max(int(batch_size), 1), 100))).fetchall()
    results = []
    for job in jobs:
        current = clock()
        token = uuid.uuid4().hex
        with store.connection() as conn, conn:
            conn.execute("UPDATE workspace_worker_state SET heartbeat_at=? WHERE id=1", (store.iso(current),))
            claim = conn.execute("UPDATE workspace_strategies SET lease_token=?,lease_until=? WHERE account_id=? AND enabled=1 AND version=? AND (lease_until IS NULL OR lease_until<?)", (token, store.iso(current + timedelta(seconds=120)), job["account_id"], job["version"], store.iso(current)))
            if claim.rowcount != 1:
                continue
        try:
            status = _execute(job, token, quotes, clock, fixed_time)
        except MarketError as exc:
            status = "blocked"
            _finish(job["account_id"], token, job["version"], clock(), status, exc.message)
        except IdentityError as exc:
            status = "blocked"
            _finish(job["account_id"], token, job["version"], clock(), status, exc.message)
        except Exception:
            status = "error"
            # No raw provider response, configuration or account values in public status.
            _finish(job["account_id"], token, job["version"], clock(), status, "策略执行异常，本轮未继续；请联系管理员检查。")
            log.error("Workspace strategy iteration failed; no further orders attempted")
        results.append({"status": status})
    return results


def main():
    parser = argparse.ArgumentParser(description="独立用户工作台模拟策略任务")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("interval must be at least 30 seconds")
    logging.basicConfig(level=logging.INFO)
    try:
        while True:
            results = run_once()
            log.info("Workspace paper iteration completed: %s accounts", len(results))
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
