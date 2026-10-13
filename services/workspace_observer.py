"""Capture tenant portfolio evidence independently of the strategy execution loop."""
import argparse
from datetime import datetime, timezone
import logging
import time

from services import workspace_accounts, workspace_db, workspace_performance
from services.market_quotes import QuoteService


log = logging.getLogger(__name__)


def _heartbeat(now, outcome=None):
    conn = workspace_db.connect()
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS workspace_observer_state(id INTEGER PRIMARY KEY CHECK(id=1),heartbeat_at TEXT NOT NULL,last_cycle_at TEXT,captured INTEGER NOT NULL DEFAULT 0,skipped INTEGER NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO workspace_observer_state(id,heartbeat_at) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at", (workspace_accounts.iso(now),))
            if outcome is not None:
                conn.execute("UPDATE workspace_observer_state SET last_cycle_at=?,captured=?,skipped=? WHERE id=1", (workspace_accounts.iso(now), outcome["captured"], outcome["skipped"]))
    finally:
        conn.close()


def status(now=None):
    now = now or datetime.now(timezone.utc)
    conn = workspace_db.connect()
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_observer_state'").fetchone():
            return {"running": False, "heartbeat_at": None}
        row = conn.execute("SELECT heartbeat_at,last_cycle_at,captured,skipped FROM workspace_observer_state WHERE id=1").fetchone()
    finally:
        conn.close()
    if not row:
        return {"running": False, "heartbeat_at": None}
    age = (now - datetime.fromisoformat(row["heartbeat_at"])).total_seconds()
    return {**dict(row), "running": 0 <= age < 180}


def run_once(quotes=None, now=None, batch_size=100):
    workspace_accounts.require_paper()
    workspace_accounts.initialize()
    clock = (lambda: now) if now is not None else lambda: datetime.now(timezone.utc)
    fixed_time = now is not None
    quotes = quotes or QuoteService()
    _heartbeat(clock())
    outcome = {"captured": 0, "skipped": 0, "duplicate": 0}
    for account in workspace_performance.list_due_accounts(now=clock(), limit=batch_size):
        _heartbeat(clock())
        try:
            result = workspace_performance.capture_account(account["user_id"], account["id"], quotes, now=clock() if fixed_time else None)
            kind = result["status"]
            outcome[kind if kind in outcome else "skipped"] += 1
        except Exception:
            # One unavailable account must not starve the other tenants' observations.
            outcome["skipped"] += 1
            log.error("Portfolio capture failed; next account will continue")
    _heartbeat(clock(), outcome)
    return outcome


def main():
    parser = argparse.ArgumentParser(description="独立采集用户模拟账户净值，不执行交易")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if not 30 <= args.interval <= 120 or not 1 <= args.batch_size <= 100:
        parser.error("interval must be 30..120 seconds and batch-size 1..100")
    logging.basicConfig(level=logging.INFO)
    try:
        while True:
            outcome = run_once(batch_size=args.batch_size)
            log.info("Portfolio observation completed: %s", outcome)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
