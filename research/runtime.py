"""扫描轻量记录 + 盘后独立子进程，默认关闭，无外部网络调用。"""
from datetime import datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

from research.store import BJ, ROOT, connect, default_path, now_iso, read_experiments

logger = logging.getLogger(__name__)


def enabled():
    return os.environ.get("RESEARCH_EVIDENCE_ENABLED", "0") == "1"


def job_status(name, status, reason="", path=None, now=None):
    stamp = (now or datetime.now(BJ)).isoformat()
    with connect(path) as db:
        db.execute("INSERT INTO jobs(name,attempted_at,last_success,status,reason) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(name) DO UPDATE SET attempted_at=excluded.attempted_at,"
                   "last_success=COALESCE(excluded.last_success,jobs.last_success),status=excluded.status,reason=excluded.reason",
                   (name, stamp, stamp if status == "ok" else None, status, reason))


def record_scan(plan):
    if not enabled():
        return
    try:
        from research.compare import capture_scan
        capture_scan(plan)
        job_status("capture", "ok")
    except Exception as exc:
        logger.warning("研究扫描记录失败 (%s)，正式交易继续", type(exc).__name__)
        try:
            job_status("capture", "failed", type(exc).__name__)
        except Exception:
            logger.warning("研究健康状态写入失败")


def launch_due(now=None):
    """与复盘成败独立；每日成功只跑一次，失败/中断30分钟后可安全重放。"""
    if not enabled():
        return []
    now = now or datetime.now(BJ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BJ)  # 调度器传入的 _now_bj 本身是北京时间
    now = now.astimezone(BJ)
    if now.time() < time(15, 15):
        return []
    launched = []
    try:
        for item in read_experiments():
            if item["kind"] != "comparison" or item["status"] == "archived" or item["manifest"]["interval_role"] != "forward":
                continue
            name = "compare:" + item["id"]
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT * FROM jobs WHERE name=?", (name,)).fetchone()
                if old and ((old["status"] == "ok" and (old["last_success"] or "")[:10] == now.date().isoformat()) or
                            now - datetime.fromisoformat(old["attempted_at"]) < timedelta(minutes=30)):
                    continue
                db.execute("INSERT INTO jobs VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET attempted_at=excluded.attempted_at,status=excluded.status,reason=excluded.reason",
                           (name, now.isoformat(), None, "running", ""))
            try:
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                subprocess.Popen([sys.executable, "-m", "research.runtime", item["id"]], cwd=ROOT,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=dict(os.environ), **options)
                launched.append(item["id"])
            except Exception as exc:
                job_status(name, "failed", type(exc).__name__)
    except Exception as exc:
        logger.warning("研究盘后调度失败 (%s)，正式循环继续", type(exc).__name__)
    return launched


def run_worker(identity, path=None):
    try:
        from research.compare import compare
        items = read_experiments(path, identity)
        if not items:
            raise ValueError("unknown experiment")
        source = items[0]["manifest"].get("input_path")
        if not source or not Path(source).is_file():
            job_status("compare:" + identity, "waiting_data", "local_input_missing", path)
            return
        with open(source, encoding="utf-8") as file:
            payload = json.load(file)
        result = compare(identity, payload, path=path)
        status = "waiting_data" if result["status"] == "waiting_data" else "ok"
        job_status("compare:" + identity, status, ",".join(result.get("reasons", [])), path)
    except Exception as exc:
        reason = type(exc).__name__
        for fragment, label in (("frozen implementation", "implementation_changed"),
                                ("historical input", "historical_input_changed"),
                                ("calendar revision", "calendar_changed"),
                                ("historical equity", "equity_revision_blocked"),
                                ("requires a closed", "session_not_closed")):
            if fragment in str(exc):
                reason = label
                break
        job_status("compare:" + identity, "failed", reason, path)


def health(path=None, now=None):
    now = now or datetime.now(BJ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BJ)
    result = {"enabled": enabled(), "status": "disabled" if not enabled() else "not_started", "jobs": []}
    if not Path(path or default_path()).exists():
        return result
    try:
        with connect(path, readonly=True) as db:
            for row in db.execute("SELECT * FROM jobs ORDER BY name"):
                item = dict(row)
                if item["status"] == "running" and now - datetime.fromisoformat(item["attempted_at"]) > timedelta(minutes=30):
                    item.update(status="stale", reason="worker_overdue")
                elif item["status"] == "ok" and now - datetime.fromisoformat(item["last_success"]) > timedelta(days=4):
                    item.update(status="stale", reason="no_recent_success")
                result["jobs"].append(item)
        if result["enabled"] and result["jobs"]:
            result["status"] = "degraded" if any(j["status"] in ("failed", "waiting_data", "stale") for j in result["jobs"]) else "ok"
    except Exception:
        result["status"] = "unavailable"
    return result


if __name__ == "__main__":
    run_worker(sys.argv[1])
