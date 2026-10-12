"""独立 SQLite 研究档案：只追加报告，GET 使用 mode=ro，不迁移正式库。"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess

from research.audit import encode, fingerprint

ROOT = Path(__file__).resolve().parents[1]
BJ = timezone(timedelta(hours=8))
STATES = ("registered", "running", "waiting_data", "completed", "archived")
VERDICTS = ("supported", "refuted", "inconclusive")


def now_iso():
    return datetime.now(BJ).isoformat()


def default_path():
    fallback = Path(os.environ.get("ALPHAPILOT_RISK_STATE_DIR", ROOT / "data")) / "research_evidence.db"
    return Path(os.environ.get("RESEARCH_EVIDENCE_DB", fallback))


def code_version():
    digest = hashlib.sha256()
    paths = list((ROOT / "research").glob("*.py")) + [ROOT / "portfolio/factor_backtest.py"]
    paths += [ROOT / p for p in ("strategy/factor_research.py", "strategy/ml_baseline.py", "strategy/etf_research.py", "scheduler/pipeline.py")]
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, timeout=3, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = "unknown"
    return {"git_head": sha, "source_sha256": digest.hexdigest()}


@contextmanager
def connect(path=None, readonly=False):
    path = Path(path or default_path()).resolve()
    if readonly:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        db.execute("PRAGMA query_only=ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=1)
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if existing - {"experiments", "reports", "scans", "frozen_days", "inputs", "jobs"}:
            db.close()
            raise ValueError("research_db_must_be_isolated")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS experiments (
          id TEXT PRIMARY KEY, hypothesis TEXT NOT NULL, kind TEXT NOT NULL,
          manifest TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL,
          verdict TEXT NOT NULL, parent_id TEXT);
        CREATE TABLE IF NOT EXISTS reports (
          experiment_id TEXT NOT NULL, digest TEXT NOT NULL, as_of TEXT NOT NULL,
          created_at TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(experiment_id,digest));
        CREATE TABLE IF NOT EXISTS scans (
          scan_id TEXT PRIMARY KEY, day TEXT NOT NULL, observed_at TEXT NOT NULL,
          payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS frozen_days (
          experiment_id TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(experiment_id,day));
        CREATE TABLE IF NOT EXISTS inputs (
          experiment_id TEXT NOT NULL, day TEXT NOT NULL, code TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(experiment_id,day,code));
        CREATE TABLE IF NOT EXISTS jobs (
          name TEXT PRIMARY KEY, attempted_at TEXT, last_success TEXT, status TEXT, reason TEXT);
        """)
    db.row_factory = sqlite3.Row
    try:
        yield db
        if not readonly:
            db.commit()
    finally:
        db.close()


def register(manifest, path=None):
    """相同假设/输入/配置/代码生成相同 id，变更创建新版本。"""
    manifest = dict(manifest)
    manifest.setdefault("code_version", code_version())
    manifest.setdefault("interval_role", "unknown")
    manifest.setdefault("promotion_allowed", False)
    manifest["promotion_allowed"] = False
    identity = fingerprint(manifest)[:32]
    with connect(path) as db:
        if manifest.get("parent_id") and not db.execute("SELECT 1 FROM experiments WHERE id=?", (manifest["parent_id"],)).fetchone():
            raise ValueError("unknown parent experiment")
        db.execute("INSERT OR IGNORE INTO experiments VALUES(?,?,?,?,?,?,?,?)", (
            identity, manifest.get("hypothesis", "unknown"), manifest.get("kind", "legacy"),
            encode(manifest), now_iso(), "registered", "inconclusive", manifest.get("parent_id")))
    return identity


def save_report(identity, report, path=None, status="completed", verdict="inconclusive"):
    if status not in STATES or verdict not in VERDICTS:
        raise ValueError("invalid research state")
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM experiments WHERE id=?", (identity,)).fetchone():
            raise ValueError("unknown experiment")
        payload = dict(report, experiment_id=identity, promotion_allowed=False)
        db.execute("INSERT OR IGNORE INTO reports VALUES(?,?,?,?,?)", (
            identity, fingerprint(payload), payload.get("as_of", "unknown"), now_iso(), encode(payload)))
        db.execute("UPDATE experiments SET status=?,verdict=? WHERE id=? AND status!='archived'", (status, verdict, identity))
    return payload


def read_experiments(path=None, identity=None, limit=100, offset=0):
    import json
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ValueError("invalid pagination")
    if not Path(path or default_path()).exists():
        return []
    with connect(path, readonly=True) as db:
        query = "SELECT * FROM experiments"
        args = []
        if identity:
            query += " WHERE id=?"
            args.append(identity)
        query += " ORDER BY created_at DESC,id LIMIT ? OFFSET ?"
        rows = db.execute(query, (*args, limit, offset)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["manifest"] = json.loads(item["manifest"])
            reports = db.execute("SELECT payload FROM reports WHERE experiment_id=? ORDER BY rowid DESC LIMIT 1", (row["id"],)).fetchone()
            item["latest"] = json.loads(reports[0]) if reports else None
            item["report_count"] = db.execute("SELECT count(*) FROM reports WHERE experiment_id=?", (row["id"],)).fetchone()[0]
            item["hypothesis_attempts"] = db.execute("SELECT count(*) FROM experiments WHERE hypothesis=?", (row["hypothesis"],)).fetchone()[0]
            result.append(item)
        return result


def import_report(report, path=None, hypothesis="legacy-import", parent_id=None):
    """适配现有 JSON；未保存的实验元数据不猜测、不改原报告。"""
    manifest = {"kind": "legacy", "hypothesis": hypothesis, "parent_id": parent_id,
                "input_sha256": report.get("input_sha256", "unknown"),
                "report_sha256": fingerprint(report), "code_version": report.get("code_version", "unknown"),
                "parameters": report.get("config", report.get("model_params", "unknown")),
                "interval_role": "review", "intervals": report.get("folds", "unknown"),
                "benchmarks": [k for k in ("equal_weight_benchmark", "buy_hold_net", "cash_return") if k in report]}
    identity = register(manifest, path)
    return save_report(identity, {"as_of": report.get("as_of", "unknown"), "legacy": report,
                                 "limitations": ["历史导入，仅复查；未记录的元数据保持未知。"]}, path)


def archive(identity, path=None):
    with connect(path) as db:
        cursor = db.execute("UPDATE experiments SET status='archived' WHERE id=?", (identity,))
        if not cursor.rowcount:
            raise ValueError("unknown experiment")
    return {"experiment_id": identity, "status": "archived", "promotion_allowed": False}
