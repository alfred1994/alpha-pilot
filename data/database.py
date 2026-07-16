"""
SQLite统一存储层
功能: K线缓存、交易记录、持仓、快照、市场环境、LLM决策、教训库
数据库路径: data/quant.db

表结构:
    k_daily         - 日K线数据
    k_minute        - 分钟K线数据
    trades          - 交易记录
    positions       - 持仓
    daily_snapshots - 每日快照
    market_regimes  - 市场环境
    llm_decisions   - LLM决策记录
    lessons         - 交易教训库
"""
import sqlite3
import json
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

import config

logger = logging.getLogger("data.database")

# 数据库路径
DB_PATH = os.path.join(config.DATA_DIR, "quant.db")


class Database:
    """
    量化系统SQLite数据库

    支持 context manager:
        with Database() as db:
            db.get_kline_cached("600519", "2024-01-01", "2024-12-31", source_func)
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self._conn = None

    def __enter__(self):
        self._ensure_dir()
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_tables()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._conn:
            if exc_type:
                self._conn.rollback()
            else:
                self._conn.commit()
            self._conn.close()
            self._conn = None
        return False

    def _ensure_dir(self):
        """确保数据库目录存在"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    @property
    def conn(self):
        """获取当前连接（需在 with 块内使用）"""
        if self._conn is None:
            raise RuntimeError("Database 未初始化，请使用 with Database() as db: 方式调用")
        return self._conn

    def _init_tables(self):
        """初始化所有表结构"""
        c = self.conn.cursor()

        # ── 日K线 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS k_daily (
                code      TEXT NOT NULL,
                date      TEXT NOT NULL,
                open      REAL,
                high      REAL,
                low       REAL,
                close     REAL,
                volume    REAL,
                amount    REAL,
                turn      REAL,
                pctChg    REAL,
                source    TEXT DEFAULT 'longport',
                PRIMARY KEY (code, date)
            )
        """)

        # ── 分钟K线 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS k_minute (
                code     TEXT NOT NULL,
                datetime TEXT NOT NULL,
                open     REAL,
                high     REAL,
                low      REAL,
                close    REAL,
                volume   REAL,
                amount   REAL,
                period   TEXT DEFAULT '5m',
                PRIMARY KEY (code, datetime, period)
            )
        """)

        # ── 交易记录 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                code          TEXT NOT NULL,
                name          TEXT,
                action        TEXT NOT NULL,
                price         REAL,
                shares        INTEGER,
                amount        REAL,
                commission    REAL DEFAULT 0,
                reason        TEXT,
                signal_score  REAL,
                signal_detail TEXT,
                market_regime TEXT,
                dimensions    TEXT,
                pnl           REAL,
                pnl_pct       REAL,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── 持仓 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                code               TEXT PRIMARY KEY,
                name               TEXT,
                shares             INTEGER,
                buy_price          REAL,
                buy_date           TEXT,
                cost               REAL,
                highest_price      REAL,
                current_price      REAL,
                market_regime_at_buy TEXT
            )
        """)

        # ── 每日快照 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_snapshots (
                date              TEXT PRIMARY KEY,
                cash              REAL,
                market_value      REAL,
                total_assets      REAL,
                position_count    INTEGER,
                market_regime     TEXT,
                regime_confidence REAL,
                details           TEXT
            )
        """)

        # ── 模拟账户状态 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS account_state (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                initial_capital REAL,
                cash            REAL,
                total_assets    REAL,
                position_count  INTEGER,
                positions       TEXT,
                updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── 市场环境 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS market_regimes (
                date        TEXT PRIMARY KEY,
                regime      TEXT,
                confidence  REAL,
                indicators  TEXT,
                llm_reasoning TEXT
            )
        """)

        # ── LLM决策记录 ──
        # 【Phase1-Task2】新增trade_id字段，用于精确关联决策与交易记录
        c.execute("""
            CREATE TABLE IF NOT EXISTS llm_decisions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                code         TEXT,
                date         TEXT,
                action       TEXT,
                llm_prompt   TEXT,
                llm_response TEXT,
                reasoning    TEXT,
                confidence   REAL,
                outcome      TEXT,
                outcome_pct  REAL,
                trade_id     INTEGER,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 【Phase1-Task2】确保旧数据库也能添加trade_id字段
        try:
            c.execute("ALTER TABLE llm_decisions ADD COLUMN trade_id INTEGER")
        except sqlite3.OperationalError:
            pass  # 字段已存在，忽略

        # 确保 trades 表有 pnl 和 pnl_pct 字段
        try:
            c.execute("ALTER TABLE trades ADD COLUMN pnl REAL")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE trades ADD COLUMN pnl_pct REAL")
        except sqlite3.OperationalError:
            pass

        # ── 交易教训库 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                date           TEXT,
                category       TEXT,
                content        TEXT,
                importance     INTEGER,
                market_regime  TEXT,
                related_trades TEXT,
                created_at     TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── 分层交易记忆 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS memory_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                layer       TEXT NOT NULL,
                scope       TEXT NOT NULL,
                key         TEXT NOT NULL,
                category    TEXT,
                content     TEXT NOT NULL,
                evidence    TEXT,
                score       REAL DEFAULT 0,
                source      TEXT,
                expires_at  TEXT,
                active      INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── 自动盯盘运行事件 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS auto_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT,
                event_type  TEXT,
                status      TEXT,
                actions     TEXT,
                details     TEXT,
                error       TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # TradePlan 执行状态。计划被领取后不会被自动重复执行，避免重试或多进程
        # 同时运行时重复产生模拟成交。
        c.execute("""
            CREATE TABLE IF NOT EXISTS trade_plan_executions (
                plan_id       TEXT PRIMARY KEY,
                plan_date     TEXT NOT NULL,
                payload_hash  TEXT NOT NULL,
                status        TEXT NOT NULL,
                claimed_at    TEXT NOT NULL,
                completed_at  TEXT,
                error         TEXT
            )
        """)

        # ── 索引 ──
        c.execute("CREATE INDEX IF NOT EXISTS idx_k_daily_code ON k_daily(code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_k_daily_date ON k_daily(date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_code ON trades(code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_llm_decisions_code ON llm_decisions(code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_llm_decisions_date ON llm_decisions(date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_lessons_category ON lessons(category)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_layer ON memory_items(layer)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_scope_key ON memory_items(scope, key)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_active ON memory_items(active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_updated ON memory_items(updated_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_auto_events_date ON auto_events(date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_auto_events_type ON auto_events(event_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trade_plan_executions_date ON trade_plan_executions(plan_date)")

        # ── 复盘快照（统一存储） ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS review_snapshots (
                date        TEXT PRIMARY KEY,
                data        TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── 自适应状态（统一存储） ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS adaptive_state (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                state       TEXT,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.conn.commit()
        logger.debug(f"数据库初始化完成: {self.db_path}")

    # ════════════════════════════════════════════════════════════════
    # k_daily 日K线 CRUD
    # ════════════════════════════════════════════════════════════════

    def insert_k_daily(self, records: List[Dict], source: str = "longport"):
        """
        批量插入日K线

        Args:
            records: 每条需有 code/date/open/high/low/close/volume/amount，可选 turn/pctChg
            source:  数据来源
        """
        if not records:
            return
        c = self.conn.cursor()
        for r in records:
            c.execute("""
                INSERT OR REPLACE INTO k_daily
                (code, date, open, high, low, close, volume, amount, turn, pctChg, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r["code"], r["date"],
                r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                r.get("volume"), r.get("amount"),
                r.get("turn", 0), r.get("pctChg", 0),
                source,
            ))
        self.conn.commit()
        logger.debug(f"插入日K线: {len(records)}条 source={source}")

    def get_k_daily(self, code: str, start_date: str, end_date: str) -> List[Dict]:
        """
        查询日K线

        Args:
            code: 股票代码，如 "600519"
            start_date: "YYYY-MM-DD"
            end_date:   "YYYY-MM-DD"

        Returns:
            按日期排序的K线列表
        """
        c = self.conn.cursor()
        c.execute("""
            SELECT * FROM k_daily
            WHERE code = ? AND date >= ? AND date <= ?
            ORDER BY date
        """, (code, start_date, end_date))
        return [dict(row) for row in c.fetchall()]

    def delete_k_daily(self, code: str, start_date: str = None, end_date: str = None):
        """删除日K线数据"""
        c = self.conn.cursor()
        if start_date and end_date:
            c.execute("DELETE FROM k_daily WHERE code=? AND date>=? AND date<=?",
                      (code, start_date, end_date))
        else:
            c.execute("DELETE FROM k_daily WHERE code=?", (code,))
        self.conn.commit()

    # ════════════════════════════════════════════════════════════════
    # k_minute 分钟K线 CRUD
    # ════════════════════════════════════════════════════════════════

    def insert_k_minute(self, records: List[Dict], period: str = "5m"):
        """批量插入分钟K线"""
        if not records:
            return
        c = self.conn.cursor()
        for r in records:
            c.execute("""
                INSERT OR REPLACE INTO k_minute
                (code, datetime, open, high, low, close, volume, amount, period)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r["code"], r["datetime"],
                r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                r.get("volume"), r.get("amount"),
                period,
            ))
        self.conn.commit()
        logger.debug(f"插入分钟K线: {len(records)}条 period={period}")

    def get_k_minute(self, code: str, start_dt: str, end_dt: str,
                     period: str = "5m") -> List[Dict]:
        """查询分钟K线"""
        c = self.conn.cursor()
        c.execute("""
            SELECT * FROM k_minute
            WHERE code = ? AND datetime >= ? AND datetime <= ? AND period = ?
            ORDER BY datetime
        """, (code, start_dt, end_dt, period))
        return [dict(row) for row in c.fetchall()]

    def delete_k_minute(self, code: str, period: str = None):
        """删除分钟K线"""
        c = self.conn.cursor()
        if period:
            c.execute("DELETE FROM k_minute WHERE code=? AND period=?", (code, period))
        else:
            c.execute("DELETE FROM k_minute WHERE code=?", (code,))
        self.conn.commit()

    # ════════════════════════════════════════════════════════════════
    # trades 交易记录 CRUD
    # ════════════════════════════════════════════════════════════════

    def insert_trade(self, trade: Dict) -> int:
        """
        插入一条交易记录

        Returns:
            新记录的 id
        """
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO trades
            (code, name, action, price, shares, amount, commission,
             reason, signal_score, signal_detail, market_regime, dimensions, pnl, pnl_pct, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.get("code", ""),
            trade.get("name", ""),
            trade.get("action", ""),
            trade.get("price", 0),
            trade.get("shares", 0),
            trade.get("amount", 0),
            trade.get("commission", 0),
            trade.get("reason", ""),
            trade.get("signal_score"),
            trade.get("signal_detail"),
            trade.get("market_regime"),
            trade.get("dimensions"),
            trade.get("pnl"),
            trade.get("pnl_pct"),
            trade.get("created_at", datetime.now().isoformat()),
        ))
        self.conn.commit()
        return c.lastrowid

    def get_trades(self, code: str = None, start_date: str = None,
                   end_date: str = None, limit: int = 100) -> List[Dict]:
        """查询交易记录"""
        conditions, params = [], []
        if code:
            conditions.append("code = ?")
            params.append(code)
        if start_date:
            conditions.append("created_at >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("created_at <= ?")
            params.append(end_date + "T23:59:59")
        where = " AND ".join(conditions) if conditions else "1=1"

        c = self.conn.cursor()
        c.execute(f"""
            SELECT * FROM trades
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ?
        """, params + [limit])
        return [dict(row) for row in c.fetchall()]

    # trades表允许更新的列名白名单
    _TRADE_UPDATE_COLUMNS = {
        "code", "name", "action", "price", "shares", "amount", "commission",
        "reason", "signal_score", "signal_detail", "market_regime", "dimensions",
        "pnl", "pnl_pct",
    }

    def update_trade(self, trade_id: int, updates: Dict):
        """更新交易记录（白名单校验列名防SQL注入）"""
        safe_updates = {k: v for k, v in updates.items() if k in self._TRADE_UPDATE_COLUMNS}
        if not safe_updates:
            return
        sets = ", ".join(f"{k}=?" for k in safe_updates)
        values = list(safe_updates.values()) + [trade_id]
        c = self.conn.cursor()
        c.execute(f"UPDATE trades SET {sets} WHERE id=?", values)
        self.conn.commit()

    def backfill_missing_trade_pnl(self, code: str = None) -> Dict[str, int]:
        """按同股票前序买入价回填缺失的卖出盈亏，避免历史空值被展示成0。"""
        conditions = ["action = 'SELL'", "(pnl IS NULL OR pnl_pct IS NULL)"]
        params = []
        if code:
            conditions.append("code = ?")
            params.append(code)
        where = " AND ".join(conditions)

        c = self.conn.cursor()
        c.execute(f"""
            SELECT id, code, price, shares, amount, commission, created_at
            FROM trades
            WHERE {where}
            ORDER BY created_at ASC, id ASC
        """, params)
        sell_rows = c.fetchall()

        updated = 0
        skipped = 0
        for sell in sell_rows:
            sell_code = sell["code"]
            sell_time = sell["created_at"] or ""
            c.execute("""
                SELECT price
                FROM trades
                WHERE code = ? AND action = 'BUY' AND created_at < ?
                  AND price IS NOT NULL AND price > 0
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            """, (sell_code, sell_time))
            buy = c.fetchone()
            if not buy:
                skipped += 1
                continue

            buy_price = float(buy["price"] or 0)
            sell_price = float(sell["price"] or 0)
            shares = int(sell["shares"] or 0)
            if buy_price <= 0 or sell_price <= 0 or shares <= 0:
                skipped += 1
                continue

            amount = float(sell["amount"] or (sell_price * shares))
            commission = sell["commission"]
            if commission is None:
                commission = max(amount * config.COMMISSION_RATE, 5)
            stamp_tax = amount * config.STAMP_TAX_RATE
            pnl = (sell_price - buy_price) * shares - float(commission or 0) - stamp_tax
            pnl_pct = (sell_price - buy_price) / buy_price

            c.execute(
                "UPDATE trades SET pnl = ?, pnl_pct = ? WHERE id = ?",
                (pnl, pnl_pct, sell["id"]),
            )
            updated += 1

        if updated:
            self.conn.commit()
        return {"updated": updated, "skipped": skipped}

    def delete_trade(self, trade_id: int):
        """删除交易记录"""
        c = self.conn.cursor()
        c.execute("DELETE FROM trades WHERE id=?", (trade_id,))
        self.conn.commit()

    # ════════════════════════════════════════════════════════════════
    # positions 持仓 CRUD
    # ════════════════════════════════════════════════════════════════

    def upsert_position(self, pos: Dict):
        """插入或更新持仓"""
        c = self.conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO positions
            (code, name, shares, buy_price, buy_date, cost,
             highest_price, current_price, market_regime_at_buy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pos["code"],
            pos.get("name", ""),
            pos.get("shares", 0),
            pos.get("buy_price", 0),
            pos.get("buy_date", ""),
            pos.get("cost", 0),
            pos.get("highest_price", 0),
            pos.get("current_price", 0),
            pos.get("market_regime_at_buy", ""),
        ))
        self.conn.commit()

    def get_position(self, code: str) -> Optional[Dict]:
        """获取单只持仓"""
        c = self.conn.cursor()
        c.execute("SELECT * FROM positions WHERE code=?", (code,))
        row = c.fetchone()
        return dict(row) if row else None

    def get_all_positions(self) -> List[Dict]:
        """获取全部持仓"""
        c = self.conn.cursor()
        c.execute("SELECT * FROM positions ORDER BY code")
        return [dict(row) for row in c.fetchall()]

    def delete_position(self, code: str):
        """删除持仓"""
        c = self.conn.cursor()
        c.execute("DELETE FROM positions WHERE code=?", (code,))
        self.conn.commit()

    # ════════════════════════════════════════════════════════════════
    # daily_snapshots 每日快照 CRUD
    # ════════════════════════════════════════════════════════════════

    def insert_snapshot(self, snapshot: Dict):
        """插入每日快照"""
        c = self.conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO daily_snapshots
            (date, cash, market_value, total_assets, position_count,
             market_regime, regime_confidence, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot["date"],
            snapshot.get("cash", 0),
            snapshot.get("market_value", 0),
            snapshot.get("total_assets", 0),
            snapshot.get("position_count", 0),
            snapshot.get("market_regime", ""),
            snapshot.get("regime_confidence", 0),
            snapshot.get("details"),
        ))
        self.conn.commit()

    def get_snapshot(self, date: str) -> Optional[Dict]:
        """获取指定日期快照"""
        c = self.conn.cursor()
        c.execute("SELECT * FROM daily_snapshots WHERE date=?", (date,))
        row = c.fetchone()
        return dict(row) if row else None

    def get_snapshots(self, start_date: str = None, end_date: str = None,
                      limit: int = 30) -> List[Dict]:
        """查询快照列表"""
        conditions, params = [], []
        if start_date:
            conditions.append("date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("date <= ?")
            params.append(end_date)
        where = " AND ".join(conditions) if conditions else "1=1"

        c = self.conn.cursor()
        c.execute(f"""
            SELECT * FROM daily_snapshots
            WHERE {where}
            ORDER BY date DESC LIMIT ?
        """, params + [limit])
        return [dict(row) for row in c.fetchall()]

    # ════════════════════════════════════════════════════════════════
    # market_regimes 市场环境 CRUD
    # ════════════════════════════════════════════════════════════════

    def insert_market_regime(self, regime: Dict):
        """插入市场环境记录"""
        c = self.conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO market_regimes
            (date, regime, confidence, indicators, llm_reasoning)
            VALUES (?, ?, ?, ?, ?)
        """, (
            regime["date"],
            regime.get("regime", ""),
            regime.get("confidence", 0),
            regime.get("indicators"),
            regime.get("llm_reasoning"),
        ))
        self.conn.commit()

    def get_market_regime(self, date: str) -> Optional[Dict]:
        """获取指定日期市场环境"""
        c = self.conn.cursor()
        c.execute("SELECT * FROM market_regimes WHERE date=?", (date,))
        row = c.fetchone()
        return dict(row) if row else None

    def get_latest_regime(self) -> Optional[Dict]:
        """获取最新市场环境"""
        c = self.conn.cursor()
        c.execute("SELECT * FROM market_regimes ORDER BY date DESC LIMIT 1")
        row = c.fetchone()
        return dict(row) if row else None

    # ════════════════════════════════════════════════════════════════
    # llm_decisions LLM决策记录 CRUD
    # ════════════════════════════════════════════════════════════════

    def insert_llm_decision(self, decision: Dict) -> int:
        """【Phase1-Task2】插入LLM决策记录，支持trade_id关联，返回 id"""
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO llm_decisions
            (code, date, action, llm_prompt, llm_response, reasoning,
             confidence, outcome, outcome_pct, trade_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            decision.get("code", ""),
            decision.get("date", ""),
            decision.get("action", ""),
            decision.get("llm_prompt", ""),
            decision.get("llm_response", ""),
            decision.get("reasoning", ""),
            decision.get("confidence", 0),
            decision.get("outcome"),
            decision.get("outcome_pct"),
            decision.get("trade_id"),
            decision.get("created_at", datetime.now().isoformat()),
        ))
        self.conn.commit()
        return c.lastrowid

    def get_llm_decisions(self, code: str = None, start_date: str = None,
                          end_date: str = None, limit: int = 50) -> List[Dict]:
        """查询LLM决策记录"""
        conditions, params = [], []
        if code:
            conditions.append("code = ?")
            params.append(code)
        if start_date:
            conditions.append("date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("date <= ?")
            params.append(end_date)
        where = " AND ".join(conditions) if conditions else "1=1"

        c = self.conn.cursor()
        c.execute(f"""
            SELECT * FROM llm_decisions
            WHERE {where}
            ORDER BY created_at DESC LIMIT ?
        """, params + [limit])
        return [dict(row) for row in c.fetchall()]

    # llm_decisions表允许更新的列名白名单
    _LLM_DECISION_UPDATE_COLUMNS = {
        "code", "date", "action", "llm_prompt", "llm_response", "reasoning",
        "confidence", "outcome", "outcome_pct", "trade_id",
    }

    def update_llm_decision(self, decision_id: int, updates: Dict):
        """更新LLM决策（如补充 outcome，白名单校验列名防SQL注入）"""
        safe_updates = {k: v for k, v in updates.items() if k in self._LLM_DECISION_UPDATE_COLUMNS}
        if not safe_updates:
            return
        sets = ", ".join(f"{k}=?" for k in safe_updates)
        values = list(safe_updates.values()) + [decision_id]
        c = self.conn.cursor()
        c.execute(f"UPDATE llm_decisions SET {sets} WHERE id=?", values)
        self.conn.commit()

    # ════════════════════════════════════════════════════════════════
    # lessons 交易教训库 CRUD
    # ════════════════════════════════════════════════════════════════

    def insert_lesson(self, lesson: Dict) -> int:
        """插入教训，返回 id"""
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO lessons
            (date, category, content, importance, market_regime,
             related_trades, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            lesson.get("date", ""),
            lesson.get("category", ""),
            lesson.get("content", ""),
            lesson.get("importance", 3),
            lesson.get("market_regime", ""),
            lesson.get("related_trades"),
            lesson.get("created_at", datetime.now().isoformat()),
        ))
        self.conn.commit()
        return c.lastrowid

    def get_lessons(self, category: str = None, limit: int = 50) -> List[Dict]:
        """查询教训"""
        if category:
            c = self.conn.cursor()
            c.execute("""
                SELECT * FROM lessons
                WHERE category = ?
                ORDER BY importance DESC, created_at DESC LIMIT ?
            """, (category, limit))
        else:
            c = self.conn.cursor()
            c.execute("""
                SELECT * FROM lessons
                ORDER BY importance DESC, created_at DESC LIMIT ?
            """, (limit,))
        return [dict(row) for row in c.fetchall()]

    def delete_lesson(self, lesson_id: int):
        """删除教训"""
        c = self.conn.cursor()
        c.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
        self.conn.commit()

    # ════════════════════════════════════════════════════════════════
    # memory_items 分层交易记忆 CRUD
    # ════════════════════════════════════════════════════════════════

    def insert_memory_item(self, item: Dict) -> int:
        """插入分层记忆，返回 id"""
        c = self.conn.cursor()
        now = datetime.now().isoformat()
        evidence = item.get("evidence", {})
        if not isinstance(evidence, str):
            evidence = json.dumps(evidence, ensure_ascii=False)
        c.execute("""
            INSERT INTO memory_items
            (layer, scope, key, category, content, evidence, score,
             source, expires_at, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.get("layer", ""),
            item.get("scope", "global"),
            item.get("key", ""),
            item.get("category", ""),
            item.get("content", ""),
            evidence,
            item.get("score", 0),
            item.get("source", ""),
            item.get("expires_at"),
            item.get("active", 1),
            item.get("created_at", now),
            item.get("updated_at", now),
        ))
        self.conn.commit()
        return c.lastrowid

    def get_memory_items(self, layer: str = None, scope: str = None,
                         key: str = None, category: str = None,
                         active: bool = True, limit: int = 20) -> List[Dict]:
        """查询分层记忆"""
        conditions, params = [], []
        if layer:
            conditions.append("layer = ?")
            params.append(layer)
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        if key is not None:
            conditions.append("key = ?")
            params.append(key)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if active is not None:
            conditions.append("active = ?")
            params.append(1 if active else 0)

        where = " AND ".join(conditions) if conditions else "1=1"
        c = self.conn.cursor()
        c.execute(f"""
            SELECT * FROM memory_items
            WHERE {where}
            ORDER BY score DESC, updated_at DESC
            LIMIT ?
        """, params + [limit])
        return [dict(row) for row in c.fetchall()]

    _MEMORY_ITEM_UPDATE_COLUMNS = {
        "layer", "scope", "key", "category", "content", "evidence",
        "score", "source", "expires_at", "active", "updated_at",
    }

    def update_memory_item(self, item_id: int, updates: Dict):
        """更新分层记忆"""
        safe_updates = {
            k: v for k, v in updates.items()
            if k in self._MEMORY_ITEM_UPDATE_COLUMNS
        }
        if not safe_updates:
            return
        if "evidence" in safe_updates and not isinstance(safe_updates["evidence"], str):
            safe_updates["evidence"] = json.dumps(safe_updates["evidence"], ensure_ascii=False)
        safe_updates.setdefault("updated_at", datetime.now().isoformat())
        sets = ", ".join(f"{k}=?" for k in safe_updates)
        values = list(safe_updates.values()) + [item_id]
        c = self.conn.cursor()
        c.execute(f"UPDATE memory_items SET {sets} WHERE id=?", values)
        self.conn.commit()

    def get_memory_stats(self) -> Dict:
        """统计分层记忆数量"""
        c = self.conn.cursor()
        stats = {"total": 0, "layers": {}, "latest_update": None}
        c.execute("SELECT COUNT(*) AS cnt FROM memory_items WHERE active = 1")
        stats["total"] = c.fetchone()["cnt"]
        c.execute("""
            SELECT layer, COUNT(*) AS cnt
            FROM memory_items
            WHERE active = 1
            GROUP BY layer
        """)
        stats["layers"] = {row["layer"]: row["cnt"] for row in c.fetchall()}
        c.execute("SELECT MAX(updated_at) AS latest_update FROM memory_items WHERE active = 1")
        row = c.fetchone()
        stats["latest_update"] = row["latest_update"] if row else None
        return stats

    # ════════════════════════════════════════════════════════════════
    # get_kline_cached - 缓存优先的K线获取
    # ════════════════════════════════════════════════════════════════

    def get_kline_cached(self, code: str, start_date: str, end_date: str,
                         source_func) -> List[Dict]:
        """
        先查本地缓存，没有则调 source_func 获取并存入

        Args:
            code: 股票代码，如 "600519"
            start_date: "YYYY-MM-DD"
            end_date:   "YYYY-MM-DD"
            source_func: 可调用对象，签名 (code, start_date, end_date) -> List[Dict]
                         每条记录需包含 date/open/high/low/close/volume/amount

        Returns:
            K线数据列表
        """
        # 1. 查本地缓存
        cached = self.get_k_daily(code, start_date, end_date)
        if cached:
            logger.debug(f"缓存命中: {code} {len(cached)}条")
            return cached

        # 2. 调用数据源获取
        try:
            data = source_func(code, start_date, end_date)
        except Exception as e:
            logger.error(f"数据源获取失败: {code} - {e}")
            return []

        if not data:
            return []

        # 3. 确保每条记录带上 code
        for row in data:
            row.setdefault("code", code)

        # 4. 存入缓存
        self.insert_k_daily(data, source="longport")
        logger.debug(f"缓存写入: {code} {len(data)}条")
        return data

    # ════════════════════════════════════════════════════════════════
    # 数据迁移：从JSON文件迁入
    # ════════════════════════════════════════════════════════════════

    def migrate_from_json(self, account_file: str = None, review_dir: str = None) -> Dict[str, Any]:
        """
        从JSON文件迁移到SQLite

        迁移内容:
            1. data/paper_account.json → trades + positions
            2. data/reviews/*.json     → daily_snapshots

        Args:
            account_file: 模拟账户JSON路径，None时使用默认路径
            review_dir: 复盘JSON目录，None时使用默认目录

        Returns:
            迁移统计 {"trades": N, "snapshots": N}
        """
        result = {"trades": 0, "snapshots": 0}

        # 1. 迁移 paper_account.json
        account_file = account_file or os.path.join(config.DATA_DIR, "paper_account.json")
        if os.path.exists(account_file):
            try:
                with open(account_file, "r", encoding="utf-8") as f:
                    account = json.load(f)

                # 迁移交易记录：兼容不同版本的账户字段命名
                trade_records = (
                    account.get("trades")
                    or account.get("trade_history")
                    or account.get("orders")
                    or []
                )
                for trade in trade_records:
                    action = trade.get("action") or trade.get("side") or trade.get("type") or ""
                    created_at = (
                        trade.get("timestamp")
                        or trade.get("created_at")
                        or trade.get("date")
                        or datetime.now().isoformat()
                    )
                    action_upper = str(action).upper()
                    pnl_pct_val = 0.0
                    pnl_val = 0.0
                    if action_upper == "SELL":
                        profit_pct = trade.get("profit_pct") or trade.get("pnl_pct") or 0.0
                        pnl_pct_val = profit_pct / 100.0 if profit_pct != 0 else 0.0
                        pnl_val = trade.get("pnl") or (trade.get("amount", 0) * pnl_pct_val / (1 + pnl_pct_val) if pnl_pct_val != -1 else 0.0)

                    self.insert_trade({
                        "code": trade.get("code", ""),
                        "name": trade.get("name", ""),
                        "action": action_upper,
                        "price": trade.get("price", 0),
                        "shares": trade.get("shares", 0),
                        "amount": trade.get("amount", 0),
                        "commission": trade.get("commission", 0),
                        "reason": trade.get("reason", ""),
                        "pnl": pnl_val,
                        "pnl_pct": pnl_pct_val,
                        "created_at": created_at,
                    })
                    result["trades"] += 1

                # 迁移当前持仓
                for code, pos in account.get("positions", {}).items():
                    if isinstance(pos, dict):
                        self.upsert_position({
                            "code": code,
                            "name": pos.get("name", ""),
                            "shares": pos.get("shares", 0),
                            "buy_price": pos.get("buy_price", 0),
                            "buy_date": pos.get("buy_date", ""),
                            "cost": pos.get("cost", 0),
                            "highest_price": pos.get("highest_price", 0),
                            "current_price": pos.get("current_price", 0),
                        })

                logger.info(f"迁移账户: {result['trades']}条交易")
            except Exception as e:
                logger.error(f"迁移账户数据失败: {e}")

        # 2. 迁移 reviews/*.json → daily_snapshots
        review_dir = review_dir or os.path.join(config.DATA_DIR, "reviews")
        if os.path.exists(review_dir):
            for filename in sorted(os.listdir(review_dir)):
                if filename.startswith("review_") and filename.endswith(".json"):
                    filepath = os.path.join(review_dir, filename)
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            review = json.load(f)
                        self.insert_snapshot({
                            "date": review.get("date", ""),
                            "cash": review.get("cash", 0),
                            "market_value": review.get("market_value", 0),
                            "total_assets": review.get("total_assets", 0),
                            "position_count": review.get("position_count", 0),
                            "details": json.dumps(review, ensure_ascii=False),
                        })
                        result["snapshots"] += 1
                    except Exception as e:
                        logger.error(f"迁移复盘数据失败 {filename}: {e}")

            logger.info(f"迁移复盘: {result['snapshots']}条快照")

        return result

    # ════════════════════════════════════════════════════════════════
    # 统计信息
    # ════════════════════════════════════════════════════════════════

    def get_stats(self) -> Dict:
        """获取数据库统计信息"""
        c = self.conn.cursor()
        stats = {}

        for table in ["k_daily", "k_minute", "trades", "positions", "account_state",
                       "daily_snapshots", "market_regimes", "llm_decisions", "lessons",
                       "memory_items", "review_snapshots", "adaptive_state", "auto_events"]:
            c.execute(f"SELECT COUNT(*) as cnt FROM {table}")
            stats[table] = c.fetchone()["cnt"]

        # 数据库文件大小
        if os.path.exists(self.db_path):
            stats["db_size_mb"] = round(os.path.getsize(self.db_path) / 1024 / 1024, 2)

        return stats

    def format_stats(self) -> str:
        """格式化统计信息"""
        stats = self.get_stats()
        lines = [
            "=" * 50,
            "SQLite 数据库统计",
            "=" * 50,
            f"日K线缓存:     {stats['k_daily']:>8}条",
            f"分钟K线缓存:   {stats['k_minute']:>8}条",
            f"交易记录:       {stats['trades']:>8}条",
            f"持仓:           {stats['positions']:>8}条",
            f"账户状态:       {stats['account_state']:>8}条",
            f"每日快照:       {stats['daily_snapshots']:>8}条",
            f"市场环境:       {stats['market_regimes']:>8}条",
            f"LLM决策:        {stats['llm_decisions']:>8}条",
            f"教训库:         {stats['lessons']:>8}条",
            f"分层记忆:       {stats['memory_items']:>8}条",
            f"复盘快照:       {stats['review_snapshots']:>8}条",
            f"自适应状态:     {stats['adaptive_state']:>8}条",
            f"自动盯盘事件:   {stats['auto_events']:>8}条",
            f"数据库大小:     {stats.get('db_size_mb', 0):>8.2f} MB",
            "=" * 50,
        ]
        return "\n".join(lines)


    # ════════════════════════════════════════════════════════════════
    # 统一数据层：双写接口
    # ════════════════════════════════════════════════════════════════

    def save_account_state(self, state: Dict):
        """
        统一接口：保存模拟账户状态到 SQLite

        Args:
            state: 账户状态，包含 initial_capital/cash/positions 等
        """
        positions = state.get("positions", {}) or {}
        total_assets = state.get("total_assets")
        if total_assets is None:
            market_value = sum(
                (pos.get("current_price") or pos.get("buy_price") or 0) * pos.get("shares", 0)
                for pos in positions.values()
                if isinstance(pos, dict)
            )
            total_assets = state.get("cash", 0) + market_value

        c = self.conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO account_state
            (id, initial_capital, cash, total_assets, position_count, positions, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
        """, (
            state.get("initial_capital", 0),
            state.get("cash", 0),
            total_assets,
            len(positions),
            json.dumps(positions, ensure_ascii=False),
            state.get("updated_at", datetime.now().isoformat()),
        ))
        self.conn.commit()
        logger.debug("模拟账户状态写入SQLite")

    def _write_account_state(self, cursor, state: Dict):
        """在调用方控制的事务内写入账户快照。"""
        positions = state.get("positions", {}) or {}
        total_assets = state.get("total_assets")
        if total_assets is None:
            market_value = sum(
                (pos.get("current_price") or pos.get("buy_price") or 0) * pos.get("shares", 0)
                for pos in positions.values()
                if isinstance(pos, dict)
            )
            total_assets = state.get("cash", 0) + market_value
        cursor.execute("""
            INSERT OR REPLACE INTO account_state
            (id, initial_capital, cash, total_assets, position_count, positions, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
        """, (
            state.get("initial_capital", 0),
            state.get("cash", 0),
            total_assets,
            len(positions),
            json.dumps(positions, ensure_ascii=False),
            state.get("updated_at", datetime.now().isoformat()),
        ))

    @staticmethod
    def _replace_positions(cursor, positions: Dict[str, Dict]):
        """以账户快照重建持仓投影，保持账户和持仓表同一事务提交。"""
        cursor.execute("DELETE FROM positions")
        for code, pos in positions.items():
            cursor.execute("""
                INSERT INTO positions
                (code, name, shares, buy_price, buy_date, cost,
                 highest_price, current_price, market_regime_at_buy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                code,
                pos.get("name", ""),
                pos.get("shares", 0),
                pos.get("buy_price", 0),
                pos.get("buy_date", ""),
                pos.get("cost", 0),
                pos.get("highest_price", 0),
                pos.get("current_price") or pos.get("buy_price", 0),
                pos.get("market_regime_at_buy", ""),
            ))

    @staticmethod
    def _insert_trade(cursor, trade: Dict) -> int:
        """在调用方控制的事务内写入成交记录。"""
        cursor.execute("""
            INSERT INTO trades
            (code, name, action, price, shares, amount, commission,
             reason, signal_score, signal_detail, market_regime, dimensions, pnl, pnl_pct, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.get("code", ""),
            trade.get("name", ""),
            trade.get("action", ""),
            trade.get("price", 0),
            trade.get("shares", 0),
            trade.get("amount", 0),
            trade.get("commission", 0),
            trade.get("reason", ""),
            trade.get("signal_score"),
            trade.get("signal_detail"),
            trade.get("market_regime"),
            trade.get("dimensions"),
            trade.get("pnl"),
            trade.get("pnl_pct"),
            trade.get("timestamp", trade.get("created_at", datetime.now().isoformat())),
        ))
        return cursor.lastrowid

    def save_account_snapshot(self, state: Dict):
        """原子同步账户快照和持仓投影，不产生交易记录。"""
        cursor = self.conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            self._write_account_state(cursor, state)
            self._replace_positions(cursor, state.get("positions", {}) or {})
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def record_account_transaction(self, state: Dict, trade: Dict) -> int:
        """原子写入账户、持仓和成交，三者不会出现半成功状态。"""
        cursor = self.conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            self._write_account_state(cursor, state)
            self._replace_positions(cursor, state.get("positions", {}) or {})
            trade_id = self._insert_trade(cursor, trade)
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
            return trade_id

    def claim_trade_plan_execution(self, plan_id: str, plan_date: str, payload_hash: str) -> Dict[str, Any]:
        """领取一次交易计划执行权；已领取的同一计划绝不重复下单。"""
        cursor = self.conn.cursor()
        now = datetime.now().isoformat()
        try:
            cursor.execute("""
                INSERT INTO trade_plan_executions
                (plan_id, plan_date, payload_hash, status, claimed_at)
                VALUES (?, ?, ?, 'executing', ?)
            """, (plan_id, plan_date, payload_hash, now))
            self.conn.commit()
            return {"claimed": True, "status": "executing"}
        except sqlite3.IntegrityError:
            cursor.execute(
                "SELECT status, claimed_at, completed_at, error FROM trade_plan_executions WHERE plan_id=?",
                (plan_id,),
            )
            row = cursor.fetchone()
            return {"claimed": False, **(dict(row) if row else {"status": "unknown"})}

    def complete_trade_plan_execution(self, plan_id: str, error: str = ""):
        """标记计划执行结束；部分失败的计划也不允许自动重复执行。"""
        self.conn.execute("""
            UPDATE trade_plan_executions
            SET status='completed', completed_at=?, error=?
            WHERE plan_id=? AND status='executing'
        """, (datetime.now().isoformat(), error or None, plan_id))
        self.conn.commit()

    def get_account_state(self) -> Optional[Dict]:
        """
        统一接口：读取模拟账户状态

        Returns:
            账户状态 dict，不存在则返回 None
        """
        c = self.conn.cursor()
        c.execute("SELECT * FROM account_state WHERE id = 1")
        row = c.fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["positions"] = json.loads(item.get("positions") or "{}")
        except json.JSONDecodeError:
            item["positions"] = {}
        return item

    def save_trade_record(self, trade: Dict) -> int:
        """
        统一接口：保存交易记录到 SQLite

        Args:
            trade: 交易记录 dict，需包含 code/action/price/shares，
                   可选 name/amount/commission/reason/timestamp 等
        Returns:
            新记录 id
        """
        return self.insert_trade({
            "code": trade.get("code", ""),
            "name": trade.get("name", ""),
            "action": trade.get("action", ""),
            "price": trade.get("price", 0),
            "shares": trade.get("shares", 0),
            "amount": trade.get("amount", 0),
            "commission": trade.get("commission", 0),
            "reason": trade.get("reason", ""),
            "signal_score": trade.get("signal_score"),
            "signal_detail": trade.get("signal_detail"),
            "market_regime": trade.get("market_regime"),
            "dimensions": trade.get("dimensions"),
            "pnl": trade.get("pnl"),
            "pnl_pct": trade.get("pnl_pct"),
            "created_at": trade.get("timestamp", datetime.now().isoformat()),
        })

    def get_trade_history(self, code: str = None, limit: int = 50) -> List[Dict]:
        """
        统一接口：查询交易历史

        Args:
            code: 股票代码（可选，None=全部）
            limit: 返回条数
        Returns:
            交易记录列表
        """
        return self.get_trades(code=code, limit=limit)

    def save_daily_snapshot(self, snapshot: Dict):
        """
        统一接口：保存每日快照到 SQLite

        Args:
            snapshot: 快照数据，需包含 date，可选 cash/market_value/total_assets/
                      position_count/market_regime/regime_confidence/details
        """
        self.insert_snapshot(snapshot)
        logger.debug(f"每日快照写入SQLite: {snapshot.get('date')}")

    def save_review_snapshot(self, date: str, data: Dict):
        """
        统一接口：保存复盘快照到 SQLite

        Args:
            date: 复盘日期 "YYYY-MM-DD"
            data: 复盘数据 dict（完整 JSON）
        """
        c = self.conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO review_snapshots (date, data, created_at)
            VALUES (?, ?, ?)
        """, (date, json.dumps(data, ensure_ascii=False), datetime.now().isoformat()))
        self.conn.commit()
        logger.debug(f"复盘快照写入SQLite: {date}")

    def save_adaptive_state(self, state: Dict):
        """
        统一接口：保存自适应状态到 SQLite

        Args:
            state: 自适应状态 dict（完整 JSON）
        """
        c = self.conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO adaptive_state (id, state, updated_at)
            VALUES (1, ?, ?)
        """, (json.dumps(state, ensure_ascii=False), datetime.now().isoformat()))
        self.conn.commit()
        logger.debug("自适应状态写入SQLite")

    def get_adaptive_state(self) -> Optional[Dict]:
        """
        统一接口：读取自适应状态

        Returns:
            自适应状态 dict，不存在则返回 None
        """
        c = self.conn.cursor()
        c.execute("SELECT state FROM adaptive_state WHERE id = 1")
        row = c.fetchone()
        if row and row["state"]:
            try:
                return json.loads(row["state"])
            except (json.JSONDecodeError, KeyError):
                return None
        return None

    def insert_auto_event(self, event: Dict) -> int:
        """
        记录自动盯盘运行事件

        Args:
            event: 事件数据，可包含 date/event_type/status/actions/details/error
        Returns:
            新记录 id
        """
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO auto_events
            (date, event_type, status, actions, details, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            event.get("date", datetime.now().strftime("%Y-%m-%d")),
            event.get("event_type", "auto_cycle"),
            event.get("status", ""),
            json.dumps(event.get("actions", []), ensure_ascii=False),
            json.dumps(event.get("details", {}), ensure_ascii=False),
            event.get("error", ""),
            event.get("created_at", datetime.now().isoformat()),
        ))
        self.conn.commit()
        return c.lastrowid

    def get_auto_events(self, date: str = None, event_type: str = None,
                        limit: int = 100) -> List[Dict]:
        """
        查询自动盯盘运行事件

        Args:
            date: 日期过滤，None=不限
            event_type: 事件类型过滤，None=不限
            limit: 返回条数
        Returns:
            事件列表
        """
        conditions, params = [], []
        if date:
            conditions.append("date = ?")
            params.append(date)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        where = " AND ".join(conditions) if conditions else "1=1"

        c = self.conn.cursor()
        c.execute(f"""
            SELECT * FROM auto_events
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ?
        """, params + [limit])

        events = []
        for row in c.fetchall():
            item = dict(row)
            for key, default in (("actions", []), ("details", {})):
                try:
                    item[key] = json.loads(item[key]) if item.get(key) else default
                except json.JSONDecodeError:
                    item[key] = default
            events.append(item)
        return events


# 兼容旧代码：QuantDatabase 指向 Database
QuantDatabase = Database


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    with Database() as db:
        # 测试迁移
        print("执行数据迁移...")
        result = db.migrate_from_json()
        print(f"迁移结果: {result}")

        # 显示统计
        print(db.format_stats())
