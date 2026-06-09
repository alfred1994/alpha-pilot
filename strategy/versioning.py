"""
策略版本管理
====================================================================
每次自适应调整前，保存当前参数版本和对应的绩效指标。
支持版本回滚和版本间绩效对比。

数据存储在 SQLite 的 strategy_versions 表中。
====================================================================
"""
import json
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("strategy.versioning")


class StrategyVersionManager:
    """策略版本管理器"""

    def __init__(self, db=None):
        self.db = db
        self._owns_db = False
        self._ensure_table()

    def _ensure_table(self):
        """确保 strategy_versions 表存在"""
        if not self.db:
            try:
                from data.database import Database
                self.db = Database().__enter__()
                self._owns_db = True
            except Exception:
                return

        self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                regime TEXT,
                params TEXT NOT NULL,
                description TEXT,
                performance TEXT,
                is_active INTEGER DEFAULT 0
            )
        """)
        self.db.conn.commit()

    def save_version(self, params: dict, regime: str = "", description: str = "", performance: dict = None) -> str:
        """
        保存当前策略参数版本

        Args:
            params: 策略参数字典（权重、阈值等）
            regime: 当前市场环境
            description: 版本描述（如"自适应调整 v3"）
            performance: 绩效指标 {"win_rate": 0.6, "pnl_pct": 0.05, "trade_count": 10}

        Returns:
            version_id: 版本号
        """
        version_id = datetime.now().strftime("v%Y%m%d_%H%M%S")

        # 将其他版本标记为非活跃
        if self.db:
            self.db.conn.execute("UPDATE strategy_versions SET is_active = 0")

        # 保存新版本
        if self.db:
            self.db.conn.execute(
                "INSERT INTO strategy_versions (version, created_at, regime, params, description, performance, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                (version_id, datetime.now().isoformat(), regime, json.dumps(params, ensure_ascii=False), description, json.dumps(performance or {}, ensure_ascii=False))
            )
            self.db.conn.commit()

        logger.info(f"策略版本保存: {version_id} ({description})")

        # P2-10: 清理旧版本（保留最近50个）
        try:
            if self.db:
                self.db.conn.execute("""
                    DELETE FROM strategy_versions
                    WHERE id NOT IN (
                        SELECT id FROM strategy_versions
                        ORDER BY id DESC LIMIT 50
                    )
                """)
                self.db.conn.commit()
        except Exception as e:
            logger.debug(f"版本清理失败(可忽略): {e}")

        return version_id

    def get_current_version(self) -> Optional[dict]:
        """获取当前活跃版本"""
        if not self.db:
            return None
        cursor = self.db.conn.execute(
            "SELECT version, created_at, regime, params, description, performance FROM strategy_versions WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "version": row[0],
            "created_at": row[1],
            "regime": row[2],
            "params": json.loads(row[3]),
            "description": row[4],
            "performance": json.loads(row[5]) if row[5] else {},
        }

    def get_version_history(self, limit: int = 10) -> List[dict]:
        """获取版本历史"""
        if not self.db:
            return []
        cursor = self.db.conn.execute(
            "SELECT version, created_at, regime, params, description, performance FROM strategy_versions ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        results = []
        for row in cursor.fetchall():
            results.append({
                "version": row[0],
                "created_at": row[1],
                "regime": row[2],
                "params": json.loads(row[3]),
                "description": row[4],
                "performance": json.loads(row[5]) if row[5] else {},
            })
        return results

    def rollback_to(self, version_id: str) -> Optional[dict]:
        """回滚到指定版本"""
        if not self.db:
            return None
        cursor = self.db.conn.execute(
            "SELECT params FROM strategy_versions WHERE version = ?",
            (version_id,)
        )
        row = cursor.fetchone()
        if not row:
            logger.warning(f"版本 {version_id} 不存在")
            return None

        params = json.loads(row[0])

        # 标记该版本为活跃
        self.db.conn.execute("UPDATE strategy_versions SET is_active = 0")
        self.db.conn.execute("UPDATE strategy_versions SET is_active = 1 WHERE version = ?", (version_id,))
        self.db.conn.commit()

        logger.info(f"回滚到版本 {version_id}")
        return params

    def compare_versions(self, v1: str, v2: str) -> dict:
        """对比两个版本的绩效"""
        if not self.db:
            return {}

        result = {}
        for vid in [v1, v2]:
            cursor = self.db.conn.execute(
                "SELECT version, regime, params, performance FROM strategy_versions WHERE version = ?",
                (vid,)
            )
            row = cursor.fetchone()
            if row:
                result[vid] = {
                    "regime": row[1],
                    "params": json.loads(row[2]),
                    "performance": json.loads(row[3]) if row[3] else {},
                }
        return result

    def format_history(self, limit: int = 5) -> str:
        """格式化版本历史（人类可读）"""
        versions = self.get_version_history(limit)
        if not versions:
            return "无版本历史"

        lines = ["策略版本历史:"]
        for v in versions:
            perf = v.get("performance", {})
            win_rate = perf.get("win_rate", 0)
            pnl = perf.get("pnl_pct", 0)
            # 检查是否为当前活跃版本
            active = ""
            if self.db:
                cursor = self.db.conn.execute(
                    "SELECT is_active FROM strategy_versions WHERE version = ?",
                    (v["version"],)
                )
                row = cursor.fetchone()
                if row and row[0]:
                    active = " ← 当前"
            lines.append(
                f"  {v['version']} | {v['regime'] or '-'} | 胜率:{win_rate:.0%} 盈亏:{pnl:+.1%} | {v['description']}{active}"
            )
        return "\n".join(lines)
