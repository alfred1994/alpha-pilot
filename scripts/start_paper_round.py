#!/usr/bin/env python3
"""停服维护时归档旧模拟盘并开启新资金基准；不出售持仓、不触碰真实账户。"""
import argparse
from contextlib import closing
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
from zoneinfo import ZoneInfo


ROUND_TABLES = (
    "trades", "positions", "daily_snapshots", "llm_decisions", "auto_events",
    "review_snapshots", "review_executions", "trade_plan_executions", "candidate_outcomes",
    "shadow_decisions", "shadow_promotions", "strategy_directives", "strategy_versions",
    "ab_test_trades", "ab_tests", "prompt_evolution", "active_prompt_hints", "memory_items", "adaptive_state",
)
RUNTIME_PATHS = (
    "paper_account.json", "circuit_breaker.json", "system_risk.json", "auto_trader_state.json",
    "adaptive_state.json", "signal_cache.json", "signal_stability.json", "intraday_watchlist.json",
    "current_paper_round.json", "reviews", "trades", "ab_tests", "laya",
)


def _write_json(path, value):
    temporary = path.with_name(path.name + ".round-tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def start_round(db_path, capital, round_id, *, workers_stopped=False):
    if not workers_stopped:
        raise ValueError("必须先停止所有账户/数据库写入服务和定时器")
    if os.environ.get("BROKER_MODE", "paper") != "paper":
        raise ValueError("只允许 BROKER_MODE=paper")
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("资金必须是有限正数")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", round_id):
        raise ValueError("round_id 格式无效")
    db_path = Path(db_path).resolve(strict=True)
    data_dir = db_path.parent
    marker = data_dir / "current_paper_round.json"
    if marker.exists() and json.loads(marker.read_text(encoding="utf-8")).get("round_id") == round_id:
        return {"status": "already_completed", "round_id": round_id}
    archive = data_dir / "round_archives" / round_id
    if archive.exists():
        raise ValueError("归档目录已存在，拒绝覆盖；请先核对上次维护结果")
    state_path = data_dir / "auto_trader_state.json"
    if state_path.exists() and json.loads(state_path.read_text(encoding="utf-8")).get("active_stage"):
        raise ValueError("自动交易仍有执行阶段，拒绝切换资金基准")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    committed = False
    sources = []
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("数据库完整性检查失败")
        state = conn.execute("SELECT * FROM account_state WHERE id=1").fetchone()
        if state is None:
            raise ValueError("找不到已有模拟账户")
        if (json.loads(state["positions"] or "{}") or state["position_count"]
                or conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]):
            raise ValueError("账户仍有持仓，拒绝自动清仓或丢弃持仓")
        # 所有待移动路径必须位于当前data目录，拒绝跟随外部符号链接。
        sources = [data_dir / name for name in RUNTIME_PATHS if (data_dir / name).exists()]
        for source in sources:
            if source.is_symlink() or not source.resolve().is_relative_to(data_dir):
                raise ValueError("运行态路径不在预期目录内")
        archive.mkdir(parents=True, mode=0o700)
        (archive / "runtime").mkdir(mode=0o700)
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(db_path) + suffix)
            if source.exists():
                shutil.copy2(source, archive / source.name)
        with closing(sqlite3.connect(archive / "consistent.db")) as backup:
            conn.backup(backup)
        stamp = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        day = stamp[:10]
        manifest = {"round_id": round_id, "status": "prepared", "created_at": stamp,
                    "old_initial_capital": state["initial_capital"], "old_cash": state["cash"],
                    "new_initial_capital": capital, "runtime_paths": [p.name for p in sources]}
        _write_json(archive / "manifest.json", manifest)
        # 写锁在归档前后核对版本；调用者必须已停止写入进程。
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT cash,version FROM account_state WHERE id=1").fetchone()
        if current["cash"] != state["cash"] or current["version"] != state["version"]:
            raise ValueError("账户在归档期间发生变化，终止维护")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ROUND_TABLES:
            if table in tables:
                conn.execute(f'DELETE FROM "{table}"')
        conn.execute("UPDATE account_state SET initial_capital=?,cash=?,total_assets=?,position_count=0,"
                     "positions='{}',updated_at=?,version=version+1 WHERE id=1", (capital, capital, capital, stamp))
        conn.execute("INSERT INTO daily_snapshots(date,cash,market_value,total_assets,position_count,details) "
                     "VALUES(?,?,0,?,0,?)", (day, capital, capital, json.dumps({"round_id": round_id})))
        for source in sources:
            source.replace(archive / "runtime" / source.name)
        _write_json(data_dir / "paper_account.json", {
            "initial_capital": capital, "cash": capital, "positions": {}, "trades": [],
            "created_at": stamp, "updated_at": stamp,
        })
        _write_json(data_dir / "circuit_breaker.json", {
            "peak_value": capital, "current_value": capital, "current_drawdown": 0,
            "max_drawdown": 0, "max_drawdown_date": "", "is_circuit_breaker": False,
            "circuit_breaker_until": "",
        })
        _write_json(data_dir / "system_risk.json", {
            "daily_records": [{"date": day, "total_assets": capital}], "forbid_new_buy": False,
            "forbid_new_buy_reason": "", "consecutive_loss_days": 0, "reduce_position": False,
            "reduce_position_ratio": 1.0, "system_halted": False, "halt_reason": "", "halt_time": "",
        })
        _write_json(state_path, {"date": day, "active_stage": "", "last_review_date": day,
                                 "last_review_attempt_date": day, "last_review_notice": ""})
        conn.commit()
        committed = True
        manifest["status"] = "completed"
        _write_json(archive / "manifest.json", manifest)
        _write_json(marker, {"round_id": round_id, "initial_capital": capital, "started_at": stamp})
        return {"status": "completed", "round_id": round_id, "initial_capital": capital,
                "cash": capital, "position_count": 0, "archive": str(archive)}
    except Exception:
        conn.rollback()
        if committed:
            # 提交后的标记失败不能恢复旧JSON而制造双事实源；停服核验后补标记。
            raise
        # 数据库事务回滚；恢复已移动的运行态，保留归档供核查。
        runtime = archive / "runtime"
        if runtime.is_dir():
            original_names = {p.name for p in sources}
            for name in ("paper_account.json", "circuit_breaker.json", "system_risk.json", "auto_trader_state.json"):
                target = data_dir / name
                if name not in original_names and target.exists() and target.resolve().is_relative_to(data_dir):
                    target.unlink()
            for source in runtime.iterdir():
                target = data_dir / source.name
                if source.is_file():
                    shutil.copy2(source, target)
                elif source.is_dir() and not target.exists():
                    shutil.copytree(source, target)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--round-id", required=True)
    parser.add_argument("--workers-stopped", action="store_true")
    args = parser.parse_args()
    print(json.dumps(start_round(args.db, args.capital, args.round_id,
                                 workers_stopped=args.workers_stopped), ensure_ascii=False))
