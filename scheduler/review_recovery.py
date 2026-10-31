"""只重试已核对为策略指令失败的复盘步骤，不重放成交、教训或账户快照。"""
from datetime import datetime, timedelta
import logging

from data.database import Database
from scheduler.market_calendar import _now_bj
from strategy.directive import _next_trading_date, generate_and_save_strategy_directive

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
RETRY_MINUTES = 15


def recover_pending_directives(*, db_path=None, now=None, llm_call=None):
    now = now or _now_bj()
    # 复盘调用不能挤占盘中止损与执行；过了生效日开盘不补造当日策略。
    if (9, 25) <= (now.hour, now.minute) < (15, 15):
        return []
    today = now.strftime("%Y-%m-%d")
    with Database(db_path=db_path) as db:
        rows = db.conn.execute("SELECT review_date,error FROM review_executions WHERE status='needs_review' ORDER BY review_date").fetchall()
    results = []
    for row in rows:
        date, error = row["review_date"], row["error"] or ""
        # 其他失败或并列错误仍需核账，不能将整个复盘标成成功。
        if not error.startswith("AI明日策略指令未完成:") or "; " in error:
            continue
        effective = _next_trading_date(date)
        if effective < today or (effective == today and (now.hour, now.minute) >= (9, 25)):
            continue
        with Database(db_path=db_path) as db:
            db.conn.execute("BEGIN IMMEDIATE")
            snapshot = db.get_review_snapshot(date)
            attempt = db.conn.execute("SELECT * FROM review_directive_recovery WHERE review_date=?", (date,)).fetchone()
            if not snapshot or not snapshot.get("llm_review"):
                continue
            if attempt and (attempt["attempts"] >= MAX_ATTEMPTS or
                            now - datetime.fromisoformat(attempt["attempted_at"]) < timedelta(minutes=RETRY_MINUTES)):
                continue
            db.conn.execute("INSERT INTO review_directive_recovery VALUES(?,1,?,'running',NULL) "
                            "ON CONFLICT(review_date) DO UPDATE SET attempts=attempts+1,attempted_at=excluded.attempted_at,status='running',error=NULL",
                            (date, now.isoformat()))
            active = db.get_effective_strategy_directive(date)
            existing = db.get_effective_strategy_directive(effective)
            db.conn.commit()
        try:
            from scheduler.trader_brief import build_daily_facts
            from review.daily_review import _compute_benchmark_pnl_pct
            from strategy.regime_config import get_trade_params
            snapshot["daily_facts"] = build_daily_facts(date=date, db_path=db_path, now=now, readonly=True)
            # 恢复时不让旧的零基准/错误能力说明继续污染策略判断。
            snapshot["benchmark_pnl_pct"] = _compute_benchmark_pnl_pct(date)
            snapshot["performance"] = {}
            regime = (active or {}).get("regime") or "sideways"
            directive = existing if existing and existing.get("review_date") == date else None
            if directive is None:
                directive = generate_and_save_strategy_directive(
                    review_date=date, review_data=snapshot,
                    llm_review="前次文字复盘留作审计。本次只依据上方修正后的结构化事实生成指令，基准未知时不得比较超额。",
                    regime=regime, current_params=(active or {}).get("params") or get_trade_params(regime),
                    current_directive=active, db_path=db_path, effective_date=effective, llm_call=llm_call,
                )
            if not directive:
                raise ValueError("strategy_directive_invalid")
            with Database(db_path=db_path) as db:
                snapshot["strategy_directive"] = directive
                snapshot["directive_recovery"] = {"recovered_at": now.isoformat(), "original_error": error,
                                                   "scope": "directive_only", "facts_recomputed": True}
                db.save_review_snapshot(date, snapshot)
                db.conn.execute("UPDATE review_executions SET status='completed',completed_at=?,error=NULL "
                                "WHERE review_date=? AND status='needs_review' AND error=?", (now.isoformat(), date, error))
                db.conn.execute("UPDATE review_directive_recovery SET status='completed',error=NULL WHERE review_date=?", (date,))
                db.conn.commit()
            results.append({"review_date": date, "status": "completed", "version": directive["version"]})
        except Exception as exc:
            # 仅记录错误类型，响应的内容哈希由指令调用记录，避免泄露供应方凭据。
            reason = type(exc).__name__
            with Database(db_path=db_path) as db:
                db.conn.execute("UPDATE review_directive_recovery SET status='failed',error=? WHERE review_date=?", (reason, date))
                db.conn.commit()
            logger.warning("策略指令恢复失败: date=%s error=%s", date, reason)
            results.append({"review_date": date, "status": "failed", "reason": reason})
    return results
