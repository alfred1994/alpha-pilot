"""
影子策略评估与晋级门禁
====================================================================
样本成熟 → 自动对比 baseline → 产生晋级候选（status=candidate）。

安全约定（对应原始规划"不应让日终 LLM 仅凭一天的散文复盘直接改变
主策略"）：
- 晋级只写入 shadow_promotions 候选行，绝不自动修改正式策略参数；
- 应用候选需要人工确认或显式命令，由人决定是否生成新的 StrategyDirective；
- 旧 A/B 框架长期 running 的实验按最大年龄标记 expired，不再永久挂起。
====================================================================
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List

from strategy.shadow_traders import (
    SHADOW_VARIANT_IDS,
    compute_variant_metrics,
    ensure_tables,
)

def ensure_eval_tables(db) -> None:
    """确保影子决策与晋级表存在，供只读接口在空库上安全调用。"""
    ensure_tables(db)
    db.conn.execute(SHADOW_PROMOTIONS_DDL)
    db.conn.commit()

logger = logging.getLogger("strategy.shadow_eval")

SHADOW_PROMOTIONS_DDL = """
CREATE TABLE IF NOT EXISTS shadow_promotions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id       TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'candidate',
    metrics          TEXT,
    baseline_metrics TEXT,
    note             TEXT DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT
)
"""

DEFAULT_MIN_DAYS = 20
DEFAULT_MIN_BUYS = 10
DEFAULT_PROMOTION_MARGIN = 0.005


def _ensure_promotion_table(db) -> None:
    db.conn.execute(SHADOW_PROMOTIONS_DDL)
    db.conn.commit()


def evaluate_variants(db, min_days: int = DEFAULT_MIN_DAYS,
                      min_buys: int = DEFAULT_MIN_BUYS) -> List[dict]:
    """
    全变体排行榜：baseline 置首，其余按相对 baseline 的净收益差降序。

    mature 判定：交易日数与已回填买入样本数同时达标。
    """
    # 空库上直接调用也安全：先确保影子表存在
    ensure_tables(db)
    baseline = compute_variant_metrics(db, "baseline")
    rows = []
    for variant_id in SHADOW_VARIANT_IDS:
        metrics = compute_variant_metrics(db, variant_id)
        mature = (
            metrics["trading_days"] >= min_days
            and metrics["buys"] >= min_buys
        )
        delta = None
        if (
            metrics["avg_net_5d"] is not None
            and baseline["avg_net_5d"] is not None
        ):
            delta = round(metrics["avg_net_5d"] - baseline["avg_net_5d"], 6)
        rows.append({
            "variant_id": variant_id,
            "metrics": metrics,
            "mature": mature,
            "avg_net_5d_delta": delta,
            "win_rate_delta": (
                round(metrics["win_rate"] - baseline["win_rate"], 4)
                if metrics["win_rate"] is not None and baseline["win_rate"] is not None
                else None
            ),
        })
    rows.sort(key=lambda r: (
        r["variant_id"] != "baseline",
        -(r["avg_net_5d_delta"] if r["avg_net_5d_delta"] is not None else -1.0),
    ))
    return rows


def promote_candidates(db, min_days: int = DEFAULT_MIN_DAYS,
                       min_buys: int = DEFAULT_MIN_BUYS,
                       margin: float = DEFAULT_PROMOTION_MARGIN) -> Dict[str, list]:
    """
    评估全部变体并把达标者写入晋级候选（幂等）。

    晋级条件（全部满足）：
    - 样本成熟（min_days 个交易日且 min_buys 笔已回填买入）；
    - T+5 平均净收益超出 baseline 至少 margin；
    - 胜率不低于 baseline。

    已被人工拒绝（rejected）或已批准（approved）的变体不再重复提名。

    Returns:
        {"candidates": [当前处于 candidate 状态的变体]}
    """
    ensure_tables(db)
    _ensure_promotion_table(db)
    now = datetime.now().isoformat()
    leaderboard = evaluate_variants(db, min_days=min_days, min_buys=min_buys)
    baseline = next(r for r in leaderboard if r["variant_id"] == "baseline")

    existing = {
        row[0]: row[1]
        for row in db.conn.execute(
            "SELECT variant_id, status FROM shadow_promotions"
        ).fetchall()
    }

    for row in leaderboard:
        variant_id = row["variant_id"]
        if variant_id == "baseline" or not row["mature"]:
            continue
        delta = row["avg_net_5d_delta"]
        metrics = row["metrics"]
        if delta is None or delta < margin:
            continue
        if (
            metrics["win_rate"] is None
            or baseline["metrics"]["win_rate"] is None
            or metrics["win_rate"] < baseline["metrics"]["win_rate"]
        ):
            continue
        status = existing.get(variant_id)
        if status == "rejected" or status == "approved":
            continue
        if status == "candidate":
            db.conn.execute(
                "UPDATE shadow_promotions SET metrics=?, baseline_metrics=?, updated_at=?"
                " WHERE variant_id=?",
                (json.dumps(metrics, ensure_ascii=False),
                 json.dumps(baseline["metrics"], ensure_ascii=False), now, variant_id),
            )
        else:
            db.conn.execute(
                "INSERT INTO shadow_promotions"
                " (variant_id, status, metrics, baseline_metrics, note, created_at)"
                " VALUES (?, 'candidate', ?, ?, '', ?)",
                (variant_id,
                 json.dumps(metrics, ensure_ascii=False),
                 json.dumps(baseline["metrics"], ensure_ascii=False), now),
            )
            logger.info("[影子] 晋级候选: %s (净收益差 %.2f%%)", variant_id, delta * 100)
        existing.setdefault(variant_id, "candidate")
    db.conn.commit()

    candidates = [
        variant_id for variant_id, status in existing.items() if status == "candidate"
    ]
    return {"candidates": sorted(candidates)}


def expire_stale_ab_tests(db, max_age_days: int = 14) -> int:
    """把超过最大年龄仍 running 的旧 A/B 实验标记 expired，返回清理数。"""
    table = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ab_tests'"
    ).fetchone()
    if not table:
        return 0
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    cursor = db.conn.execute(
        "UPDATE ab_tests SET status='expired', finished_at=?,"
        " result=? WHERE status='running' AND created_at <= ?",
        (datetime.now().isoformat(),
         json.dumps({"expired": True, "reason": "stale_running"}, ensure_ascii=False),
         cutoff),
    )
    db.conn.commit()
    if cursor.rowcount:
        logger.info("[A/B] 清理过期running实验 %d 个", cursor.rowcount)
    return cursor.rowcount
