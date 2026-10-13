"""真实工作区净值记录、同批估值基准比较及只读 CSV 导出。

采集之外的读取接口不创建表、不请求行情、不补写历史。净值仅在全部持仓
可估值且账户在网络请求期间没有变化时入库；失败记录进入 coverage。
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import io
import json
import os
import sqlite3
import uuid

from services import workspace_db
from services.market_types import (
    MarketError, get_instrument, get_market_spec, utc_datetime, validate_quote,
)


DEFAULT_INTERVAL_SECONDS = 300
MAX_EXPORT_ROWS = 100000
MAX_PAGE_SIZE = 2000
MAX_GAPS = 100
BENCHMARKS = {"CN": "510300", "US": "SPY", "CRYPTO": "BTC-USD"}
CAPTURE_REASONS = frozenset(("scheduled", "trade", "manual"))

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS workspace_performance_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL REFERENCES workspace_accounts(id),
        slot INTEGER NOT NULL, reason TEXT NOT NULL, as_of TEXT NOT NULL,
        equity TEXT NOT NULL, cash TEXT NOT NULL, pending_cash TEXT NOT NULL,
        market_value TEXT NOT NULL, positions_json TEXT NOT NULL,
        benchmark_instrument_id TEXT NOT NULL, benchmark_price TEXT,
        benchmark_as_of TEXT, benchmark_source TEXT, benchmark_status TEXT NOT NULL,
        benchmark_error TEXT, valuation_status TEXT NOT NULL,
        ledger_sequence INTEGER NOT NULL, state_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(account_id,slot,reason)
    )""",
    """CREATE INDEX IF NOT EXISTS workspace_performance_account_time
        ON workspace_performance_snapshots(account_id,as_of,id)""",
    """CREATE TABLE IF NOT EXISTS workspace_performance_attempts (
        account_id TEXT NOT NULL REFERENCES workspace_accounts(id),
        slot INTEGER NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
        capture_token TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        error_code TEXT, snapshot_id INTEGER REFERENCES workspace_performance_snapshots(id),
        PRIMARY KEY(account_id,slot,reason)
    )""",
    """CREATE INDEX IF NOT EXISTS workspace_performance_attempt_time
        ON workspace_performance_attempts(account_id,started_at)""",
)


def _iso(value):
    return utc_datetime(value).isoformat(timespec="microseconds")


def _time(value):
    try:
        return utc_datetime(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        raise MarketError("performance_integrity", "绩效记录时间格式异常") from None


def _number(value):
    return format(value if isinstance(value, Decimal) else Decimal(value), "f")


def _decimal(value, *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise MarketError("performance_integrity", "账户金额或数量记录异常") from None
    if not result.is_finite() or (positive and result <= 0):
        raise MarketError("performance_integrity", "账户金额或数量记录异常")
    return result


def _percentage(value, baseline):
    if baseline <= 0:
        return None
    return _number(((value / baseline - 1) * 100).quantize(Decimal("0.000001")))


def interval_seconds():
    try:
        value = int(os.environ.get("ALPHAPILOT_PERFORMANCE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        raise MarketError("performance_configuration", "净值采集间隔配置无效") from None
    if not 60 <= value <= 3600:
        raise MarketError("performance_configuration", "净值采集间隔须为 60 至 3600 秒")
    return value


def _slot(now):
    return int(utc_datetime(now).timestamp()) // interval_seconds()


def _ensure_schema(conn):
    # execute 不会像 executescript 一样隐式提交调用者的账户创建事务。
    for statement in SCHEMA:
        conn.execute(statement)


def initialize():
    """由启动/worker 调用；不回调 accounts.initialize，避免初始化循环。"""
    workspace_db.init_db()
    with _connection() as conn, conn:
        _ensure_schema(conn)


@contextmanager
def _connection(*, readonly=False):
    if readonly:
        uri = workspace_db.database_path().resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
    else:
        conn = workspace_db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _owned(conn, user_id, account_id):
    row = conn.execute(
        "SELECT a.* FROM workspace_accounts a JOIN workspace_users u ON u.id=a.user_id "
        "WHERE a.id=? AND a.user_id=? AND u.is_active=1", (account_id, user_id),
    ).fetchone()
    if row is None:
        raise MarketError("not_found", "账户不存在。")
    return dict(row)


def _instrument(instrument_id):
    if not isinstance(instrument_id, str) or ":" not in instrument_id:
        raise MarketError("performance_integrity", "持仓标的记录异常")
    return get_instrument(*instrument_id.split(":", 1))


def _state(conn, user_id, account_id, now):
    """在调用者事务内流式读取持仓和流水指纹；内存仅随支持标的数增长。"""
    account = _owned(conn, user_id, account_id)
    digest = hashlib.sha256()

    def stamp(value):
        digest.update(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")

    stamp({key: account[key] for key in ("id", "user_id", "market", "currency", "initial_cash", "cash", "realized_pnl", "created_at")})
    if account["currency"] != get_market_spec(account["market"]).currency:
        raise MarketError("performance_integrity", "账户币种与市场不一致")
    positions = {}
    for row in conn.execute("SELECT * FROM workspace_lots WHERE account_id=? ORDER BY id", (account_id,)):
        stamp(dict(row))
        instrument = _instrument(row["instrument_id"])
        if instrument.market.value != account["market"]:
            raise MarketError("performance_integrity", "持仓与账户市场不一致")
        quantity = _decimal(row["quantity"], positive=True)
        cost = _decimal(row["unit_cost"], positive=True) * quantity
        entry = positions.setdefault(instrument.instrument_id, {"quantity": Decimal(0), "cost_basis": Decimal(0)})
        entry["quantity"] += quantity
        entry["cost_basis"] += cost
    cash, pending = _decimal(account["cash"]), Decimal(0)
    for row in conn.execute("SELECT * FROM workspace_settlements WHERE account_id=? AND settled_at IS NULL ORDER BY id", (account_id,)):
        stamp(dict(row))
        amount = _decimal(row["amount"], positive=True)
        if _time(row["available_at"]) <= now:
            cash += amount
        else:
            pending += amount
    ledger = conn.execute("SELECT COUNT(*) AS n,COALESCE(MAX(id),0) AS seq FROM workspace_ledger WHERE account_id=?", (account_id,)).fetchone()
    orders = conn.execute("SELECT COUNT(*) AS n FROM workspace_orders WHERE account_id=?", (account_id,)).fetchone()
    stamp({"ledger_count": ledger["n"], "ledger_sequence": ledger["seq"], "orders": orders["n"]})
    if cash < 0 or pending < 0:
        raise MarketError("performance_integrity", "账户现金不能为负")
    return {"account": account, "positions": positions, "cash": cash, "pending": pending,
            "fingerprint": digest.hexdigest(), "ledger_sequence": ledger["seq"], "orders": orders["n"]}


def _insert_snapshot(conn, state, now, slot, reason, positions, benchmark=None, benchmark_error=None):
    account = state["account"]
    market_value = sum((_decimal(item["market_value"]) for item in positions), Decimal(0))
    equity = state["cash"] + state["pending"] + market_value
    benchmark_instrument = get_instrument(account["market"], BENCHMARKS[account["market"]])
    values = (
        account["id"], slot, reason, _iso(now), _number(equity), _number(state["cash"]),
        _number(state["pending"]), _number(market_value),
        json.dumps(positions, ensure_ascii=False, separators=(",", ":")),
        benchmark_instrument.instrument_id, _number(benchmark.price) if benchmark else None,
        _iso(benchmark.as_of) if benchmark else None, benchmark.source if benchmark else None,
        "paired" if benchmark else "unavailable", benchmark_error,
        "initial" if reason == "initial" else "current", state["ledger_sequence"], state["fingerprint"], _iso(now),
    )
    cursor = conn.execute(
        "INSERT INTO workspace_performance_snapshots(account_id,slot,reason,as_of,equity,cash,pending_cash,market_value,positions_json,benchmark_instrument_id,benchmark_price,benchmark_as_of,benchmark_source,benchmark_status,benchmark_error,valuation_status,ledger_sequence,state_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
    )
    return cursor.lastrowid


def seed_snapshot(conn, accountrow, now):
    """仅供创建账户事务使用；账户已有交易或时间不等于创建时间时拒绝补造。"""
    current = utc_datetime(now)
    if not conn.in_transaction:
        raise MarketError("transaction_required", "初始净值必须与账户创建在同一事务中写入")
    _ensure_schema(conn)
    state = _state(conn, accountrow["user_id"], accountrow["id"], current)
    account = state["account"]
    existing = conn.execute("SELECT id,as_of FROM workspace_performance_snapshots WHERE account_id=? AND reason='initial'", (account["id"],)).fetchone()
    if existing:
        return {"status": "duplicate", "snapshot_id": existing["id"], "as_of": existing["as_of"]}
    if (_time(account["created_at"]) != current or state["orders"] or state["positions"]
            or state["pending"] or state["cash"] != _decimal(account["initial_cash"], positive=True)
            or conn.execute("SELECT 1 FROM workspace_performance_snapshots WHERE account_id=?", (account["id"],)).fetchone()):
        raise MarketError("invalid_initial_snapshot", "不能为已有账户补造创建时的净值记录")
    snapshot_id = _insert_snapshot(conn, state, current, _slot(current), "initial", [], benchmark_error="not_sampled")
    return {"status": "captured", "snapshot_id": snapshot_id, "as_of": _iso(current)}


def list_due_accounts(now=None, limit=100):
    """只读选择 active 用户中到期的账户，最久未尝试者优先。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise MarketError("invalid_limit", "采集批次必须为 1 至 100")
    current = utc_datetime(now)
    cutoff = _iso(current - timedelta(seconds=interval_seconds()))
    with _connection(readonly=True) as conn:
        rows = conn.execute(
            "SELECT a.id,a.user_id FROM workspace_accounts a JOIN workspace_users u ON u.id=a.user_id "
            "LEFT JOIN (SELECT account_id,MAX(started_at) last_attempt FROM workspace_performance_attempts GROUP BY account_id) p ON p.account_id=a.id "
            "WHERE u.is_active=1 AND a.created_at<=? AND (p.last_attempt IS NULL OR p.last_attempt<=?) "
            "ORDER BY COALESCE(p.last_attempt,''),a.created_at,a.id LIMIT ?", (_iso(current), cutoff, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _finish_attempt(conn, account_id, slot, reason, token, now, status, code=None, snapshot_id=None):
    conn.execute(
        "UPDATE workspace_performance_attempts SET status=?,finished_at=?,error_code=?,snapshot_id=? "
        "WHERE account_id=? AND slot=? AND reason=? AND capture_token=? AND status='running'",
        (status, _iso(now), code, snapshot_id, account_id, slot, reason, token),
    )


def capture_account(user_id, account_id, quotes, now=None, reason="scheduled"):
    """每账户每时间槽至多领取一次；网络期间不持有数据库写锁。"""
    if os.environ.get("BROKER_MODE", "paper").lower() != "paper":
        raise MarketError("paper_required", "净值采集仅支持模拟账户")
    if not isinstance(reason, str) or reason not in CAPTURE_REASONS:
        raise MarketError("invalid_reason", "净值采集原因无效")
    started = utc_datetime(now)
    slot, token = _slot(started), uuid.uuid4().hex
    with localcontext() as context:
        context.prec = 48
        with _connection() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            state = _state(conn, user_id, account_id, started)
            if started < _time(state["account"]["created_at"]):
                raise MarketError("invalid_timestamp", "采集时间不能早于账户创建时间")
            duplicate = conn.execute("SELECT status,error_code,snapshot_id FROM workspace_performance_attempts WHERE account_id=? AND slot=? AND reason=?", (account_id, slot, reason)).fetchone()
            if duplicate:
                return {"status": "duplicate", "snapshot_id": duplicate["snapshot_id"], "code": duplicate["error_code"], "as_of": _iso(started)}
            latest = conn.execute("SELECT started_at FROM workspace_performance_attempts WHERE account_id=? ORDER BY started_at DESC LIMIT 1", (account_id,)).fetchone()
            if reason == "scheduled" and latest and (started - _time(latest["started_at"])).total_seconds() < interval_seconds():
                return {"status": "duplicate", "code": "capture_throttled", "as_of": _iso(started)}
            conn.execute("INSERT INTO workspace_performance_attempts(account_id,slot,reason,status,capture_token,started_at) VALUES(?,?,?,'running',?,?)", (account_id, slot, reason, token, _iso(started)))
        marks, benchmark, benchmark_error, failure = {}, None, None, None
        benchmark_instrument = get_instrument(state["account"]["market"], BENCHMARKS[state["account"]["market"]])
        try:
            for instrument_id in sorted(state["positions"]):
                instrument = _instrument(instrument_id)
                marks[instrument_id] = quotes.get_quote(instrument.market, instrument.symbol, now=started if now is not None else None)
            if benchmark_instrument.instrument_id in marks:
                benchmark = marks[benchmark_instrument.instrument_id]
            else:
                try:
                    benchmark = quotes.get_quote(benchmark_instrument.market, benchmark_instrument.symbol, now=started if now is not None else None)
                except MarketError as exc:
                    benchmark_error = exc.code
                except Exception:
                    benchmark_error = "benchmark_unavailable"
            completed = utc_datetime(now)
            for instrument_id, quote in marks.items():
                validate_quote(quote, instrument=_instrument(instrument_id), now=completed)
            if benchmark is not None:
                try:
                    validate_quote(benchmark, instrument=benchmark_instrument, now=completed)
                except MarketError as exc:
                    benchmark, benchmark_error = None, exc.code
        except MarketError as exc:
            completed, failure = utc_datetime(now), exc.code
        except Exception:
            # 不输出 provider 异常内容；短槽失败同时阻止不可控重试风暴。
            completed, failure = utc_datetime(now), "valuation_unavailable"
        with _connection() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            # 写锁可能等待数秒；以拿到锁后的真实时间复核，不能沿用网络结束时刻。
            # 显式 now 仅供可重复的离线采集/测试，来源报价时间始终保持原值。
            completed = utc_datetime(now)
            final = _state(conn, user_id, account_id, completed)
            last = conn.execute("SELECT as_of FROM workspace_performance_snapshots WHERE account_id=? ORDER BY as_of DESC,id DESC LIMIT 1", (account_id,)).fetchone()
            if final["fingerprint"] != state["fingerprint"]:
                failure = "account_changed"
            elif completed < started or (last and completed < _time(last["as_of"])):
                failure = "out_of_order_capture"
            if failure is None:
                try:
                    for instrument_id, quote in marks.items():
                        validate_quote(quote, instrument=_instrument(instrument_id), now=completed)
                except MarketError as exc:
                    failure = exc.code
                if benchmark is not None:
                    try:
                        validate_quote(benchmark, instrument=benchmark_instrument, now=completed)
                    except MarketError as exc:
                        benchmark, benchmark_error = None, exc.code
            if failure:
                _finish_attempt(conn, account_id, slot, reason, token, completed, "skipped", failure)
                return {"status": "skipped", "code": failure, "as_of": _iso(completed)}
            positions = []
            for instrument_id, position in sorted(final["positions"].items()):
                quote = marks[instrument_id]
                positions.append({"instrument_id": instrument_id,
                                  "quantity": _number(position["quantity"]),
                                  "cost_basis": _number(position["cost_basis"]),
                                  "price": _number(quote.price),
                                  "market_value": _number(position["quantity"] * quote.price),
                                  "quote_as_of": _iso(quote.as_of), "source": quote.source})
            snapshot_id = _insert_snapshot(conn, final, completed, slot, reason, positions, benchmark, benchmark_error)
            _finish_attempt(conn, account_id, slot, reason, token, completed, "captured", snapshot_id=snapshot_id)
            return {"status": "captured", "snapshot_id": snapshot_id, "as_of": _iso(completed)}


def _point(row, initial_cash, peak, baseline):
    equity = _decimal(row["equity"])
    peak = max(peak, equity)
    paired = row["benchmark_status"] == "paired" and row["benchmark_price"] is not None
    if baseline is None and paired:
        baseline = {"as_of": row["as_of"], "price": _decimal(row["benchmark_price"], positive=True),
                    "equity": equity, "quote_as_of": row["benchmark_as_of"]}
    point = {key: row[key] for key in ("id", "as_of", "equity", "cash", "pending_cash", "market_value", "reason", "valuation_status", "benchmark_as_of", "benchmark_source")}
    point.update(return_pct=_percentage(equity, initial_cash), drawdown_pct=_percentage(equity, peak),
                 benchmark_return_pct=_percentage(_decimal(row["benchmark_price"]), baseline["price"]) if paired else None,
                 comparison_return_pct=_percentage(equity, baseline["equity"]) if paired else None)
    return point, peak, baseline


def _page_arguments(limit, offset):
    if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE
            or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10000000):
        raise MarketError("invalid_pagination", "分页数量须为 1 至 2000，偏移须为非负整数")


def get_performance(user_id, account_id, *, limit=1000, offset=0, now=None):
    """页面从最新记录向过去分页，页内升序；全局汇总流式扫描且不填补缺测。"""
    _page_arguments(limit, offset)
    current = utc_datetime(now)
    with _connection(readonly=True) as conn, conn, localcontext() as context:
        context.prec = 48
        conn.execute("BEGIN")
        account = _owned(conn, user_id, account_id)
        total = conn.execute("SELECT COUNT(*) FROM workspace_performance_snapshots WHERE account_id=?", (account_id,)).fetchone()[0]
        initial = _decimal(account["initial_cash"], positive=True)
        interval = interval_seconds()
        begin, end = max(0, total - offset - limit), max(0, total - offset)
        points, gaps = [], []
        peak, max_drawdown, baseline = Decimal(0), Decimal(0), None
        paired_samples, missing_benchmark, gap_count, previous, first, last = 0, 0, 0, None, None, None
        initial_snapshot, last_pair_at, source = False, None, None
        for index, row in enumerate(conn.execute("SELECT * FROM workspace_performance_snapshots WHERE account_id=? ORDER BY as_of,id", (account_id,))):
            point, peak, baseline = _point(row, initial, peak, baseline)
            stamp = _time(row["as_of"])
            point["gap_before"] = previous is not None and (stamp - previous).total_seconds() > interval * 1.5
            if point["gap_before"]:
                gap_count += 1
                if len(gaps) < MAX_GAPS:
                    seconds = (stamp - previous).total_seconds()
                    gaps.append({"from": _iso(previous), "to": row["as_of"], "seconds": seconds,
                                 "missing_intervals": max(1, int(seconds // interval) - 1)})
            previous = stamp
            if first is None:
                first = point
            last = point
            initial_snapshot |= row["reason"] == "initial"
            if point["benchmark_return_pct"] is not None:
                paired_samples += 1
                last_pair_at, source = row["as_of"], row["benchmark_source"]
            else:
                missing_benchmark += 1
            if point["drawdown_pct"] is not None:
                max_drawdown = min(max_drawdown, Decimal(point["drawdown_pct"]))
            if begin <= index < end:
                points.append(point)
        counts = {row["status"]: row["n"] for row in conn.execute("SELECT status,COUNT(*) AS n FROM workspace_performance_attempts WHERE account_id=? GROUP BY status", (account_id,))}
        latest_attempt = conn.execute("SELECT status,started_at AS as_of,error_code FROM workspace_performance_attempts WHERE account_id=? ORDER BY started_at DESC LIMIT 1", (account_id,)).fetchone()
        valuation_gaps = [dict(row) for row in conn.execute(
            "SELECT started_at AS as_of,error_code AS code,status FROM workspace_performance_attempts "
            "WHERE account_id=? AND status IN ('skipped','running') ORDER BY started_at DESC LIMIT ?",
            (account_id, MAX_GAPS),
        )]
        benchmark_instrument = get_instrument(account["market"], BENCHMARKS[account["market"]])
        comparison = last["comparison_return_pct"] if last else None
        benchmark_return = last["benchmark_return_pct"] if last else None
        benchmark_status = "awaiting_samples" if total == 0 else "unavailable" if not paired_samples else "partial" if missing_benchmark else "paired"
        return {
            "points": points,
            "summary": {"total_return_pct": last["return_pct"] if last else None,
                        "max_drawdown_pct": _number(max_drawdown) if last else None,
                        "first_at": first["as_of"] if first else None, "last_at": last["as_of"] if last else None,
                        "samples": total, "currency": account["currency"], "initial_cash": _number(initial),
                        "last_equity": last["equity"] if last else None,
                        "total_return_basis": "initial_cash", "drawdown_basis": "observed_snapshots",
                        "comparison_return_pct": comparison, "benchmark_return_pct": benchmark_return,
                        "excess_return_pct": _number(Decimal(comparison) - Decimal(benchmark_return)) if comparison is not None and benchmark_return is not None else None},
            "benchmark": {"instrument_id": benchmark_instrument.instrument_id, "name": benchmark_instrument.name,
                          "currency": account["currency"], "status": benchmark_status, "method": "price_return",
                          "alignment": "same_capture_fresh_quotes", "baseline_as_of": baseline["as_of"] if baseline else None,
                          "baseline_price": _number(baseline["price"]) if baseline else None,
                          "baseline_equity": _number(baseline["equity"]) if baseline else None,
                          "baseline_quote_as_of": baseline["quote_as_of"] if baseline else None,
                          "last_pair_at": last_pair_at, "paired_samples": paired_samples, "source": source},
            "coverage": {"expected_interval_seconds": interval, "initial_snapshot": initial_snapshot,
                         "missing_benchmark_samples": missing_benchmark, "skipped_attempts": counts.get("skipped", 0),
                         "pending_attempts": counts.get("running", 0), "gap_count": gap_count, "gaps": gaps,
                         "gaps_truncated": gap_count > MAX_GAPS,
                         "valuation_gaps": list(reversed(valuation_gaps)),
                         "valuation_gaps_truncated": counts.get("skipped", 0) + counts.get("running", 0) > MAX_GAPS,
                         "last_sample_age_seconds": max(0.0, (current - _time(last["as_of"])).total_seconds()) if last else None,
                         "latest_attempt": dict(latest_attempt) if latest_attempt else None},
            "pagination": {"limit": limit, "offset": offset, "returned": len(points), "total": total,
                           "has_more": begin > 0, "next_offset": offset + len(points) if begin > 0 else None,
                           "order": "latest_page_ascending"},
        }


def _csv_text(value):
    text = "" if value is None else str(value)
    stripped = text.lstrip(" \t\r\n\ufeff")
    if text.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _export_size(conn, table, account_id, max_rows):
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_EXPORT_ROWS:
        raise MarketError("invalid_limit", "导出上限须为 1 至 100000 行")
    count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (account_id,)).fetchone()[0]
    if count > max_rows:
        raise MarketError("export_limit", f"记录超过 {max_rows} 行导出上限，请缩小范围后导出")


def orders_csv(user_id, account_id, *, max_rows=MAX_EXPORT_ROWS):
    """返回带 UTF-8 BOM 的 CSV bytes；金额保留十进制文本，不经 float。"""
    with _connection(readonly=True) as conn, conn:
        conn.execute("BEGIN")
        account = _owned(conn, user_id, account_id)
        _export_size(conn, "workspace_orders", account_id, max_rows)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        columns = ("order_id", "account_id", "account_name", "market", "currency", "instrument_id", "side", "quantity", "price", "fee", "realized_pnl", "status", "source", "quote_as_of", "created_at")
        writer.writerow(columns)
        for row in conn.execute("SELECT * FROM workspace_orders WHERE account_id=? ORDER BY created_at,id", (account_id,)):
            values = dict(row)
            values.update(order_id=row["id"], account_name=account["name"], market=account["market"], currency=account["currency"])
            writer.writerow([_number(_decimal(values[key])) if key in ("quantity", "price", "fee", "realized_pnl") else _csv_text(values[key]) for key in columns])
        return output.getvalue().encode("utf-8-sig")


def snapshots_csv(user_id, account_id, *, max_rows=MAX_EXPORT_ROWS):
    """导出实际快照和配对收益；缺基准保留空单元格，不人工填平曲线。"""
    with _connection(readonly=True) as conn, conn, localcontext() as context:
        context.prec = 48
        conn.execute("BEGIN")
        account = _owned(conn, user_id, account_id)
        _export_size(conn, "workspace_performance_snapshots", account_id, max_rows)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        columns = ("snapshot_id", "account_id", "account_name", "market", "currency", "as_of", "reason", "equity", "cash", "pending_cash", "market_value", "return_pct", "drawdown_pct", "benchmark_instrument_id", "benchmark_price", "benchmark_as_of", "benchmark_source", "benchmark_status", "benchmark_return_pct", "comparison_return_pct", "valuation_status", "positions_json")
        decimals = frozenset(("equity", "cash", "pending_cash", "market_value", "return_pct", "drawdown_pct", "benchmark_price", "benchmark_return_pct", "comparison_return_pct"))
        writer.writerow(columns)
        peak, baseline = Decimal(0), None
        for row in conn.execute("SELECT * FROM workspace_performance_snapshots WHERE account_id=? ORDER BY as_of,id", (account_id,)):
            point, peak, baseline = _point(row, _decimal(account["initial_cash"], positive=True), peak, baseline)
            values = dict(row)
            values.update(point, snapshot_id=row["id"], account_name=account["name"], market=account["market"], currency=account["currency"])
            writer.writerow(["" if values[key] is None else _number(_decimal(values[key])) if key in decimals else _csv_text(values[key]) for key in columns])
        return output.getvalue().encode("utf-8-sig")
