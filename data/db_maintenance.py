"""
数据库维护任务

生产 SQLite（data/quant.db）此前没有任何备份/巡检/清理机制，本模块补齐:
  1. 完整性检查: PRAGMA quick_check，发现页损坏立即报告
  2. 在线备份: sqlite3 backup API（WAL 安全），保留最近 N 份
  3. 过期清理: auto_events / trade_plan_executions 超过保留期的运维日志
     （trades / llm_decisions / k_daily 等业务与训练数据不清理）
  4. 可选 VACUUM: 回收已删除页的空间（默认关闭，建议每周一次）

设计为无交易副作用：只读校验 + 备份 + 日志类清理，BROKER_MODE 无关。
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from config import DATA_DIR

logger = logging.getLogger("data.db_maintenance")

DEFAULT_DB_PATH = os.path.join(DATA_DIR, "quant.db")
DEFAULT_BACKUP_DIR = os.path.join(DATA_DIR, "backups")
# 运维日志表保留天数（业务/训练数据不清理）
OPS_TABLE_RETENTION_DAYS = 180
# 本地备份保留份数
BACKUP_KEEP_COUNT = 7


def _table_names(conn: sqlite3.Connection) -> list:
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return [r[0] for r in c.fetchall()]


def _row_counts(conn: sqlite3.Connection) -> dict:
    counts = {}
    c = conn.cursor()
    for table in _table_names(conn):
        if table.startswith("sqlite_"):
            continue
        try:
            c.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = c.fetchone()[0]
        except sqlite3.Error:
            counts[table] = -1
    return counts


def _integrity_check(db_path: str) -> dict:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        c = conn.cursor()
        c.execute("PRAGMA quick_check")
        rows = [r[0] for r in c.fetchall()]
        ok = rows == ["ok"]
        return {"ok": ok, "detail": rows[:5]}
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "detail": [f"无法打开数据库: {exc}"]}
    finally:
        conn.close()


def _backup(db_path: str, backup_dir: str) -> dict:
    """使用 sqlite3 backup API 做在线备份（对 WAL 库安全，不需要停写）。"""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = os.path.join(backup_dir, f"quant_{stamp}.db")
    src = sqlite3.connect(db_path, timeout=30)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    size_mb = os.path.getsize(target) / 1024 / 1024
    logger.info("数据库备份完成: %s (%.1fMB)", target, size_mb)

    # 保留最近 BACKUP_KEEP_COUNT 份
    backups = sorted(
        f for f in os.listdir(backup_dir)
        if f.startswith("quant_") and f.endswith(".db")
    )
    removed = []
    for stale in backups[:-BACKUP_KEEP_COUNT]:
        try:
            os.unlink(os.path.join(backup_dir, stale))
            removed.append(stale)
        except OSError:
            pass
    return {"backup": target, "size_mb": round(size_mb, 1), "removed_old": removed}


def _prune_ops_tables(db_path: str, keep_days: int) -> dict:
    """清理运维日志类表的过期记录（业务数据不动）。"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
    date_cutoff = cutoff[:10]
    pruned = {}
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        c = conn.cursor()
        c.execute("DELETE FROM auto_events WHERE created_at < ?", (cutoff,))
        pruned["auto_events"] = c.rowcount
        c.execute("DELETE FROM trade_plan_executions WHERE plan_date < ?", (date_cutoff,))
        pruned["trade_plan_executions"] = c.rowcount
        conn.commit()
    finally:
        conn.close()
    return pruned


def _vacuum(db_path: str) -> dict:
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        before = os.path.getsize(db_path) / 1024 / 1024
        conn.execute("VACUUM")
        after = os.path.getsize(db_path) / 1024 / 1024
        return {"vacuum": True, "size_before_mb": round(before, 1),
                "size_after_mb": round(after, 1)}
    finally:
        conn.close()


def run_db_maintenance(
    db_path: str = None,
    backup: bool = True,
    prune: bool = True,
    vacuum: bool = False,
    keep_days: int = OPS_TABLE_RETENTION_DAYS,
    backup_dir: str = None,
) -> dict:
    """
    执行数据库维护并返回汇总（同时写入 data/db_maintenance_report.json）。

    Args:
        db_path: 数据库路径（默认 data/quant.db）
        backup: 是否做在线备份
        prune: 是否清理运维日志表
        vacuum: 是否 VACUUM 回收空间
        keep_days: 运维日志保留天数
        backup_dir: 备份目录（默认 data/backups）
    """
    db_path = db_path or DEFAULT_DB_PATH
    backup_dir = backup_dir or DEFAULT_BACKUP_DIR
    started_at = datetime.now().isoformat()
    summary = {
        "started_at": started_at,
        "db_path": db_path,
        "db_size_mb": round(os.path.getsize(db_path) / 1024 / 1024, 1)
        if os.path.exists(db_path) else 0,
    }

    integrity = _integrity_check(db_path)
    summary["integrity"] = integrity
    if not integrity["ok"]:
        # 完整性受损时绝不继续写入操作（备份只读不受影响，但清理/VACUUM跳过）
        logger.error("数据库完整性检查失败: %s", integrity["detail"])
        summary["status"] = "corrupt"
        summary["row_counts"] = {}
        _write_report(summary)
        return summary

    if backup:
        summary["backup"] = _backup(db_path, backup_dir)
    if prune:
        summary["pruned"] = _prune_ops_tables(db_path, keep_days)
    if vacuum:
        summary.update(_vacuum(db_path))
    counts_conn = sqlite3.connect(db_path, timeout=30)
    try:
        summary["row_counts"] = _row_counts(counts_conn)
    finally:
        counts_conn.close()
    summary["finished_at"] = datetime.now().isoformat()
    summary["status"] = "ok"

    _write_report(summary)
    logger.info(
        "数据库维护完成: integrity=%s backup=%s pruned=%s vacuum=%s",
        integrity["ok"], bool(summary.get("backup")),
        summary.get("pruned"), bool(vacuum),
    )
    return summary


def _write_report(summary: dict):
    report_file = os.path.join(DATA_DIR, "db_maintenance_report.json")
    try:
        tmp = report_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        os.replace(tmp, report_file)
    except OSError as exc:
        logger.warning("维护报告写入失败: %s", exc)
