"""体彩纸面账本存储：``data/lottery.db``（``LOTTERY_DB`` 可覆盖），与交易主库分离。

- ``snapshots``：每次抓取的竞彩与国际盘口快照，只追加；
- ``games``：每场比赛的赛程、状态、比分与开赛前最后一次国际线（收盘近似）；
- ``picks``：纸面注单，同一 (销售日, 主, 客, 策略, 玩法) 只记第一次决定，之后不改写；
  结算只补结果字段；
- ``runs``：定时任务运行记录，供健康页使用。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime

from config import DATA_DIR

LOTTERY_DB = os.environ.get("LOTTERY_DB", os.path.join(DATA_DIR, "lottery.db"))

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL, sale_date TEXT NOT NULL,
        matchnum TEXT, home TEXT NOT NULL, away TEXT NOT NULL, source TEXT NOT NULL,
        jc_line REAL, jc_sp_home REAL, jc_sp_away REAL, jc_total REAL, jc_sp_over REAL, jc_sp_under REAL,
        single_spread INTEGER, single_total INTEGER, buy_end TEXT,
        ref_provider TEXT, ref_line REAL, ref_total REAL, ref_state TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_game ON snapshots(sale_date, home, away, captured_at)",
    """CREATE TABLE IF NOT EXISTS games (
        sale_date TEXT NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL,
        matchnum TEXT, home_cn TEXT, away_cn TEXT, tip_at TEXT, buy_end TEXT, state TEXT,
        home_score INTEGER, away_score INTEGER,
        close_ref_line REAL, close_ref_total REAL, close_captured_at TEXT, updated_at TEXT,
        PRIMARY KEY (sale_date, home, away))""",
    """CREATE TABLE IF NOT EXISTS picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, sale_date TEXT NOT NULL,
        matchnum TEXT, home TEXT NOT NULL, away TEXT NOT NULL, home_cn TEXT, away_cn TEXT,
        strategy TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL, line REAL NOT NULL,
        odds REAL NOT NULL, ref_line REAL, edge REAL, p_win REAL, ev REAL, single_open INTEGER,
        stake REAL NOT NULL DEFAULT 1.0, tip_at TEXT,
        result TEXT, pnl REAL, home_score INTEGER, away_score INTEGER,
        close_ref REAL, clv REAL, settled_at TEXT, source TEXT NOT NULL DEFAULT 'alphapilot',
        UNIQUE (sale_date, home, away, strategy, market))""",
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, status TEXT, detail TEXT)""",
)


def connect(db_path: str = None) -> sqlite3.Connection:
    path = os.path.abspath(db_path or LOTTERY_DB)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    for statement in SCHEMA:
        conn.execute(statement)
    return conn


@contextmanager
def session(db_path: str = None):
    with closing(connect(db_path)) as conn:
        yield conn
        conn.commit()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def add_snapshot(conn, sale_date: str, game: dict, source: str, captured_at: str = None):
    conn.execute(
        """INSERT INTO snapshots (captured_at, sale_date, matchnum, home, away, source, jc_line, jc_sp_home,
           jc_sp_away, jc_total, jc_sp_over, jc_sp_under, single_spread, single_total, buy_end,
           ref_provider, ref_line, ref_total, ref_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (captured_at or _now(), sale_date, game.get("matchnum"), game["home"], game["away"], source,
         game.get("jc_line"), game.get("jc_sp_home"), game.get("jc_sp_away"), game.get("jc_total"),
         game.get("jc_sp_over"), game.get("jc_sp_under"),
         None if game.get("single_spread") is None else int(bool(game.get("single_spread"))),
         None if game.get("single_total") is None else int(bool(game.get("single_total"))),
         game.get("buy_end"), game.get("ref_provider"), game.get("ref_line"), game.get("ref_total"),
         game.get("state")))


def upsert_game(conn, sale_date: str, game: dict):
    """新增或补全比赛信息；只覆盖本次提供的非空字段。"""
    fields = ["matchnum", "home_cn", "away_cn", "tip_at", "buy_end", "state", "home_score", "away_score",
              "close_ref_line", "close_ref_total", "close_captured_at"]
    conn.execute("INSERT OR IGNORE INTO games (sale_date, home, away, updated_at) VALUES (?,?,?,?)",
                 (sale_date, game["home"], game["away"], _now()))
    values = {key: game[key] for key in fields if game.get(key) is not None}
    if values:
        assignments = ", ".join(f"{key}=?" for key in values)
        conn.execute(f"UPDATE games SET {assignments}, updated_at=? WHERE sale_date=? AND home=? AND away=?",
                     (*values.values(), _now(), sale_date, game["home"], game["away"]))


def add_pick(conn, pick: dict) -> bool:
    """记录纸面注单；同一场同一策略同一玩法已有记录时不改写，返回是否新增。"""
    columns = ["created_at", "sale_date", "matchnum", "home", "away", "home_cn", "away_cn", "strategy", "market",
               "side", "line", "odds", "ref_line", "edge", "p_win", "ev", "single_open", "stake", "tip_at",
               "result", "pnl", "home_score", "away_score", "close_ref", "clv", "settled_at", "source"]
    row = {"created_at": _now(), "stake": 1.0, "source": "alphapilot", **pick}
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO picks ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
        [row.get(column) for column in columns])
    return cursor.rowcount == 1


def count_picks(conn, sale_date: str, strategy: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM picks WHERE sale_date=? AND strategy=?",
                        (sale_date, strategy)).fetchone()[0]


def start_run(conn, job: str) -> int:
    cursor = conn.execute("INSERT INTO runs (job, started_at, status) VALUES (?,?,?)", (job, _now(), "running"))
    conn.commit()
    return cursor.lastrowid


def finish_run(conn, run_id: int, status: str, detail: str = ""):
    conn.execute("UPDATE runs SET finished_at=?, status=?, detail=? WHERE id=?",
                 (_now(), status, (detail or "")[:500], run_id))
    conn.commit()
