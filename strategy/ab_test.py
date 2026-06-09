"""
AB测试框架
====================================================================
同时维护多套策略参数，在模拟盘中跟踪各自表现。
通过对比胜率、盈亏比、期望收益等指标，选出最优参数组合。

工作流程：
1. 自适应引擎调整参数 → 生成新版本
2. AB测试框架维护"对照组"(当前参数)和"实验组"(新参数)
3. 两组在相同股票池上运行，比较N天后的表现
4. 如果实验组显著优于对照组 → 自动切换到新参数
5. 否则 → 回滚到对照组
====================================================================
"""
import json
import logging
import math
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("strategy.ab_test")


class ABTestManager:
    """AB测试管理器"""

    def __init__(self, db=None):
        self.db = db
        self._ensure_table()

    def _ensure_table(self):
        """确保AB测试相关表存在"""
        if not self.db:
            try:
                from data.database import Database
                self.db = Database()
                self.db.__enter__()
                self._owns_db = True
            except Exception:
                self._owns_db = False
                return
        else:
            self._owns_db = False

        self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS ab_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'running',
                control_params TEXT NOT NULL,
                treatment_params TEXT NOT NULL,
                control_regime TEXT,
                treatment_regime TEXT,
                min_trades INTEGER DEFAULT 5,
                min_days INTEGER DEFAULT 3,
                result TEXT,
                winner TEXT,
                finished_at TEXT
            )
        """)

        self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS ab_test_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id TEXT NOT NULL,
                group_name TEXT NOT NULL,
                code TEXT,
                action TEXT,
                price REAL,
                pnl_pct REAL,
                timestamp TEXT,
                FOREIGN KEY (test_id) REFERENCES ab_tests(test_id)
            )
        """)
        self.db.conn.commit()

    def __del__(self):
        """清理：如果是我们自己创建的数据库连接，关闭它"""
        if getattr(self, '_owns_db', False) and self.db:
            try:
                self.db.__exit__(None, None, None)
            except Exception:
                pass

    def create_test(self, control_params: dict, treatment_params: dict,
                    control_regime: str = "", treatment_regime: str = "",
                    min_trades: int = 5, min_days: int = 3) -> str:
        """
        创建AB测试

        Args:
            control_params: 对照组参数（当前生产参数）
            treatment_params: 实验组参数（新参数）
            control_regime: 对照组市场环境标签
            treatment_regime: 实验组市场环境标签
            min_trades: 最少交易数才能评估
            min_days: 最少天数才能评估

        Returns:
            test_id
        """
        test_id = f"ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        if not self.db:
            return test_id

        try:
            self.db.conn.execute(
                "INSERT INTO ab_tests (test_id, created_at, control_params, treatment_params, "
                "control_regime, treatment_regime, min_trades, min_days) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (test_id, datetime.now().isoformat(),
                 json.dumps(control_params, ensure_ascii=False),
                 json.dumps(treatment_params, ensure_ascii=False),
                 control_regime, treatment_regime, min_trades, min_days)
            )
            self.db.conn.commit()

            logger.info(f"AB测试创建: {test_id}")
        except Exception as e:
            logger.error(f"AB测试创建失败: {e}")

        return test_id

    def record_trade(self, test_id: str, group: str, code: str,
                     action: str, price: float, pnl_pct: float = 0):
        """
        记录一笔交易到AB测试

        Args:
            test_id: 测试ID
            group: 'control' 或 'treatment'
            code: 股票代码
            action: 'BUY' 或 'SELL'
            price: 成交价格
            pnl_pct: 盈亏百分比（仅SELL时有意义）
        """
        if not self.db:
            return
        try:
            self.db.conn.execute(
                "INSERT INTO ab_test_trades (test_id, group_name, code, action, price, pnl_pct, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (test_id, group, code, action, price, pnl_pct,
                 datetime.now().isoformat())
            )
            self.db.conn.commit()
        except Exception as e:
            logger.debug(f"AB测试记录失败: {e}")

    def evaluate_test(self, test_id: str) -> dict:
        """
        评估AB测试结果

        Returns:
            {
                "test_id": "...",
                "status": "running/concluded",
                "control": {"trades": N, "win_rate": X, "avg_pnl": Y, "sharpe": Z},
                "treatment": {"trades": N, "win_rate": X, "avg_pnl": Y, "sharpe": Z},
                "winner": "control/treatment/tie",
                "confidence": "high/medium/low"
            }
        """
        if not self.db:
            return {"status": "no_db"}

        # 获取测试配置
        cursor = self.db.conn.execute(
            "SELECT status, control_params, treatment_params, min_trades, min_days, created_at "
            "FROM ab_tests WHERE test_id = ?",
            (test_id,)
        )
        test_row = cursor.fetchone()
        if not test_row:
            return {"status": "not_found"}

        status, control_params, treatment_params, min_trades, min_days, created_at = test_row

        if status != "running":
            # 已结论，返回存储的结果
            cursor2 = self.db.conn.execute(
                "SELECT result, winner FROM ab_tests WHERE test_id = ?",
                (test_id,)
            )
            row2 = cursor2.fetchone()
            result = {"status": status, "test_id": test_id}
            if row2:
                if row2[0]:
                    try:
                        result = json.loads(row2[0])
                    except Exception:
                        pass
                result["winner"] = row2[1]
            return result

        # 统计两组表现
        result = {"test_id": test_id, "status": "running"}

        for group in ["control", "treatment"]:
            cursor = self.db.conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END), "
                "AVG(pnl_pct), "
                "SUM(pnl_pct) "
                "FROM ab_test_trades "
                "WHERE test_id = ? AND group_name = ? AND action = 'SELL'",
                (test_id, group)
            )
            row = cursor.fetchone()
            if row and row[0] > 0:
                total = row[0]
                wins = row[1] or 0
                avg_pnl = row[2] or 0
                total_pnl = row[3] or 0

                # 计算简化版Sharpe（收益率均值/标准差）
                pnl_cursor = self.db.conn.execute(
                    "SELECT pnl_pct FROM ab_test_trades "
                    "WHERE test_id = ? AND group_name = ? AND action = 'SELL'",
                    (test_id, group)
                )
                pnl_values = [r[0] for r in pnl_cursor.fetchall()
                              if r[0] is not None]

                if len(pnl_values) > 1:
                    mean = sum(pnl_values) / len(pnl_values)
                    variance = sum((x - mean) ** 2 for x in pnl_values) / (len(pnl_values) - 1)
                    std = math.sqrt(variance) if variance > 0 else 0.001
                    sharpe = mean / std if std > 0 else 0
                else:
                    sharpe = 0

                result[group] = {
                    "trades": total,
                    "wins": wins,
                    "win_rate": round(wins / total, 4),
                    "avg_pnl": round(avg_pnl, 4),
                    "total_pnl": round(total_pnl, 4),
                    "sharpe": round(sharpe, 2),
                }
            else:
                result[group] = {
                    "trades": 0, "wins": 0, "win_rate": 0,
                    "avg_pnl": 0, "total_pnl": 0, "sharpe": 0
                }

        # 判断是否达到评估条件
        c = result.get("control", {})
        t = result.get("treatment", {})

        try:
            created_dt = datetime.fromisoformat(created_at)
        except Exception:
            created_dt = datetime.now()

        days_elapsed = (datetime.now() - created_dt).days

        c_trades = c.get("trades", 0)
        t_trades = t.get("trades", 0)

        if c_trades >= min_trades and t_trades >= min_trades and days_elapsed >= min_days:
            # 综合评分: Sharpe(40%) + 胜率(30%) + 平均盈亏(30%)
            c_score = (c.get("sharpe", 0) * 0.4
                       + c.get("win_rate", 0) * 0.3
                       + c.get("avg_pnl", 0) * 10 * 0.3)
            t_score = (t.get("sharpe", 0) * 0.4
                       + t.get("win_rate", 0) * 0.3
                       + t.get("avg_pnl", 0) * 10 * 0.3)

            diff = abs(t_score - c_score)

            if diff < 0.05:
                winner = "tie"
                confidence = "low"
            elif t_score > c_score:
                winner = "treatment"
                confidence = "high" if diff > 0.2 else "medium"
            else:
                winner = "control"
                confidence = "high" if diff > 0.2 else "medium"

            result["winner"] = winner
            result["confidence"] = confidence
            result["status"] = "concluded"
            result["c_score"] = round(c_score, 4)
            result["t_score"] = round(t_score, 4)
            result["diff"] = round(diff, 4)

            # 更新数据库
            try:
                self.db.conn.execute(
                    "UPDATE ab_tests SET status = 'concluded', winner = ?, "
                    "result = ?, finished_at = ? WHERE test_id = ?",
                    (winner, json.dumps(result, ensure_ascii=False),
                     datetime.now().isoformat(), test_id)
                )
                self.db.conn.commit()
            except Exception as e:
                logger.error(f"AB测试更新失败: {e}")

            logger.info(
                f"AB测试结论: {test_id} → winner={winner} "
                f"(confidence={confidence}, diff={diff:.4f})"
            )

        return result

    def evaluate_all_running(self) -> List[dict]:
        """评估所有运行中的测试，返回已结论的测试列表"""
        if not self.db:
            return []

        concluded = []
        for test in self.get_running_tests():
            result = self.evaluate_test(test["test_id"])
            if result.get("status") == "concluded":
                concluded.append(result)
        return concluded

    def get_running_tests(self) -> List[dict]:
        """获取所有运行中的测试"""
        if not self.db:
            return []
        cursor = self.db.conn.execute(
            "SELECT test_id, created_at, control_params, treatment_params "
            "FROM ab_tests WHERE status = 'running'"
        )
        results = []
        for row in cursor.fetchall():
            results.append({
                "test_id": row[0],
                "created_at": row[1],
                "control_params": json.loads(row[2]),
                "treatment_params": json.loads(row[3]),
            })
        return results

    def get_concluded_tests(self, limit: int = 10) -> List[dict]:
        """获取最近已结论的测试"""
        if not self.db:
            return []
        cursor = self.db.conn.execute(
            "SELECT test_id, created_at, winner, result, finished_at "
            "FROM ab_tests WHERE status = 'concluded' "
            "ORDER BY finished_at DESC LIMIT ?",
            (limit,)
        )
        results = []
        for row in cursor.fetchall():
            entry = {
                "test_id": row[0],
                "created_at": row[1],
                "winner": row[2],
                "finished_at": row[4],
            }
            if row[3]:
                try:
                    entry["result"] = json.loads(row[3])
                except Exception:
                    entry["result"] = {}
            results.append(entry)
        return results

    def get_best_params(self) -> Optional[dict]:
        """
        获取历史最优参数
        从所有已结论的测试中，选出 treatment 胜出且表现最好的参数
        """
        if not self.db:
            return None
        cursor = self.db.conn.execute(
            "SELECT treatment_params, result FROM ab_tests "
            "WHERE status = 'concluded' AND winner = 'treatment' "
            "ORDER BY finished_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None

    def get_test_params(self, test_id: str, group: str = "treatment") -> Optional[dict]:
        """获取指定测试的对照组或实验组参数"""
        if not self.db:
            return None
        col = "treatment_params" if group == "treatment" else "control_params"
        cursor = self.db.conn.execute(
            f"SELECT {col} FROM ab_tests WHERE test_id = ?",
            (test_id,)
        )
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None

    def format_status(self) -> str:
        """格式化AB测试状态概览"""
        if not self.db:
            return "AB测试: 无数据库连接"

        running = self.get_running_tests()
        cursor = self.db.conn.execute(
            "SELECT COUNT(*) FROM ab_tests WHERE status = 'concluded'"
        )
        concluded = cursor.fetchone()[0] or 0

        # 统计胜出率
        cursor_w = self.db.conn.execute(
            "SELECT winner, COUNT(*) FROM ab_tests "
            "WHERE status = 'concluded' GROUP BY winner"
        )
        winner_counts = dict(cursor_w.fetchall())

        lines = [
            f"📊 AB测试状态: {len(running)} 运行中, {concluded} 已完成",
        ]

        if winner_counts:
            parts = []
            if winner_counts.get("treatment"):
                parts.append(f"实验组胜出 {winner_counts['treatment']} 次")
            if winner_counts.get("control"):
                parts.append(f"对照组胜出 {winner_counts['control']} 次")
            if winner_counts.get("tie"):
                parts.append(f"平局 {winner_counts['tie']} 次")
            lines.append("  " + ", ".join(parts))

        for test in running[:3]:
            lines.append(
                f"  🔄 {test['test_id']} (自 {test['created_at'][:10]})"
            )

        return "\n".join(lines)

    def format_detail(self, test_id: str) -> str:
        """格式化单个测试的详细报告"""
        result = self.evaluate_test(test_id)
        if result.get("status") == "not_found":
            return f"测试 {test_id} 不存在"

        lines = [
            f"AB测试报告: {test_id}",
            f"状态: {result.get('status', 'unknown')}",
        ]

        for group_name, label in [("control", "对照组"), ("treatment", "实验组")]:
            g = result.get(group_name, {})
            if g.get("trades", 0) > 0:
                lines.append(
                    f"  {label}: {g['trades']}笔交易, "
                    f"胜率{g['win_rate']:.1%}, "
                    f"均盈{g['avg_pnl']:.2f}%, "
                    f"Sharpe {g['sharpe']:.2f}"
                )
            else:
                lines.append(f"  {label}: 暂无交易数据")

        if result.get("winner"):
            winner_label = {
                "treatment": "🏆 实验组胜出",
                "control": "🏆 对照组胜出",
                "tie": "🤝 平局",
            }.get(result["winner"], result["winner"])
            lines.append(f"结论: {winner_label} (置信度: {result.get('confidence', 'N/A')})")

        return "\n".join(lines)
