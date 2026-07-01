"""
交易记忆系统
====================================================================
从SQLite读取历史教训和决策记录，提供"记忆检索"能力:
  - 给定股票代码，返回该股票的历史教训
  - 给定市场环境，返回该环境下的经验总结
  - 生成模式总结（哪些操作模式盈利/亏损）
  - 支持教训写入和决策记录

数据来源:
  - SQLite lessons 表 (教训库)
  - SQLite llm_decisions 表 (LLM决策记录)
  - SQLite trades 表 (交易记录)
====================================================================
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger("strategy.memory")


@dataclass
class Lesson:
    """一条教训"""
    id: int
    date: str
    category: str        # buy/sell/risk/regime/general
    content: str
    importance: int      # 1-5, 5最重要
    market_regime: str
    related_trades: str  # JSON字符串

    def __repr__(self):
        return f"[{self.category}] {self.content[:50]}... (重要度={self.importance})"


@dataclass
class DecisionRecord:
    """一条LLM决策记录"""
    id: int
    code: str
    date: str
    action: str
    reasoning: str
    confidence: float
    outcome: Optional[str]     # win/lose/pending
    outcome_pct: Optional[float]


class TradeMemory:
    """
    交易记忆系统

    提供记忆检索能力，供LLM决策时参考历史经验。

    使用方法:
        memory = TradeMemory()
        context = memory.recall("600519", regime="bull")
        # context 是一段文本，直接放入LLM prompt
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path
        self._db = None

    def _get_db(self):
        """获取数据库连接"""
        if self._db is None:
            from data.database import Database
            self._db = Database(db_path=self.db_path)
            self._db.__enter__()
        return self._db

    def close(self):
        """关闭数据库连接"""
        if self._db:
            self._db.__exit__(None, None, None)
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ════════════════════════════════════════════════════════════════
    # 记忆检索 — 供LLM决策参考
    # ════════════════════════════════════════════════════════════════

    def recall(self, stock_code: str = None, regime: str = None) -> str:
        """
        检索相关记忆，返回一段文本供LLM参考

        Args:
            stock_code: 股票代码（可选，检索该股票相关教训）
            regime: 市场环境（可选，检索该环境下的教训）

        Returns:
            格式化的记忆文本，可直接放入LLM prompt
        """
        parts = []

        # 1. 该股票的历史教训
        if stock_code:
            stock_lessons = self.recall_lessons_by_stock(stock_code, limit=3)
            if stock_lessons:
                parts.append("【该股票历史教训】")
                for l in stock_lessons:
                    parts.append(f"- [{l.date}] {l.content}")

            # 该股票的历史决策及结果
            past_decisions = self.recall_decisions(stock_code, limit=3)
            if past_decisions:
                parts.append("【该股票历史决策】")
                for d in past_decisions:
                    outcome_str = f"结果:{d.outcome}({d.outcome_pct:+.1f}%)" if d.outcome else "待验证"
                    parts.append(f"- [{d.date}] {d.action} 置信度{d.confidence:.0%} {outcome_str} | {d.reasoning[:80]}")

        # 2. 该市场环境下的教训
        if regime:
            regime_lessons = self.recall_lessons_by_regime(regime, limit=3)
            if regime_lessons:
                regime_label = {"bull": "牛市", "bear": "熊市", "sideways": "震荡", "rebound": "反弹"}.get(regime, regime)
                parts.append(f"【{regime_label}环境下的经验】")
                for l in regime_lessons:
                    parts.append(f"- {l.content}")

        # 3. 最近的高重要度教训
        recent_lessons = self.recall_lessons(limit=3, min_importance=4)
        if recent_lessons and not parts:  # 只在没有其他记忆时才加
            parts.append("【近期重要教训】")
            for l in recent_lessons:
                parts.append(f"- [{l.date}] {l.content}")

        # 4. 模式总结
        pattern = self.get_pattern_summary()
        if pattern:
            parts.append(f"【交易模式总结】\n{pattern}")

        if not parts:
            return "暂无相关历史记忆。"

        return "\n".join(parts)

    def recall_lessons(self, category: str = None, limit: int = 10,
                       min_importance: int = 1) -> List[Lesson]:
        """
        检索教训列表

        Args:
            category: 类别过滤 (buy/sell/risk/regime/general)
            limit: 返回条数
            min_importance: 最低重要度

        Returns:
            Lesson列表
        """
        try:
            db = self._get_db()
            c = db.conn.cursor()

            if category:
                c.execute("""
                    SELECT * FROM lessons
                    WHERE category = ? AND importance >= ?
                    ORDER BY importance DESC, created_at DESC
                    LIMIT ?
                """, (category, min_importance, limit))
            else:
                c.execute("""
                    SELECT * FROM lessons
                    WHERE importance >= ?
                    ORDER BY importance DESC, created_at DESC
                    LIMIT ?
                """, (min_importance, limit))

            return [self._row_to_lesson(row) for row in c.fetchall()]
        except Exception as e:
            logger.warning(f"检索教训失败: {e}")
            return []

    def recall_lessons_by_stock(self, code: str, limit: int = 5) -> List[Lesson]:
        """检索与某只股票相关的教训"""
        try:
            db = self._get_db()
            c = db.conn.cursor()
            # 在related_trades字段中搜索该股票代码
            c.execute("""
                SELECT * FROM lessons
                WHERE related_trades LIKE ?
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
            """, (f"%{code}%", limit))
            return [self._row_to_lesson(row) for row in c.fetchall()]
        except Exception as e:
            logger.warning(f"检索股票教训失败: {e}")
            return []

    def recall_lessons_by_regime(self, regime: str, limit: int = 5) -> List[Lesson]:
        """检索某市场环境下的教训"""
        try:
            db = self._get_db()
            c = db.conn.cursor()
            c.execute("""
                SELECT * FROM lessons
                WHERE market_regime = ?
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
            """, (regime, limit))
            return [self._row_to_lesson(row) for row in c.fetchall()]
        except Exception as e:
            logger.warning(f"检索环境教训失败: {e}")
            return []

    def recall_decisions(self, code: str = None, limit: int = 10) -> List[DecisionRecord]:
        """检索历史LLM决策记录"""
        try:
            db = self._get_db()
            c = db.conn.cursor()

            if code:
                c.execute("""
                    SELECT * FROM llm_decisions
                    WHERE code = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (code, limit))
            else:
                c.execute("""
                    SELECT * FROM llm_decisions
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,))

            return [self._row_to_decision(row) for row in c.fetchall()]
        except Exception as e:
            logger.warning(f"检索决策记录失败: {e}")
            return []

    # ════════════════════════════════════════════════════════════════
    # 模式总结
    # ════════════════════════════════════════════════════════════════

    def get_pattern_summary(self) -> str:
        """
        生成交易模式总结

        分析历史交易，总结:
        - 哪些来源选股胜率高
        - 哪些信号维度预测准
        - 哪些市场环境下操作效果好
        """
        try:
            db = self._get_db()
            c = db.conn.cursor()

            # 分析最近30笔卖出交易
            c.execute("""
                SELECT code, name, action, price, reason, signal_score, market_regime, created_at
                FROM trades
                WHERE action = 'SELL'
                ORDER BY created_at DESC
                LIMIT 30
            """)
            sells = [dict(row) for row in c.fetchall()]

            if not sells:
                return ""

            # 分析LLM决策准确率
            c.execute("""
                SELECT action, outcome, outcome_pct, confidence
                FROM llm_decisions
                WHERE outcome IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 20
            """)
            decisions = [dict(row) for row in c.fetchall()]

            parts = []

            # LLM决策准确率
            if decisions:
                wins = sum(1 for d in decisions if d.get("outcome") == "win")
                total = len(decisions)
                if total > 0:
                    parts.append(f"LLM决策准确率: {wins}/{total} ({wins/total:.0%})")

            # 高置信度决策表现
            high_conf = [d for d in decisions if d.get("confidence", 0) >= 0.7]
            if high_conf:
                high_wins = sum(1 for d in high_conf if d.get("outcome") == "win")
                parts.append(f"高置信度(>=70%)决策: {high_wins}/{len(high_conf)} 盈利")

            return "\n".join(parts) if parts else ""

        except Exception as e:
            logger.warning(f"生成模式总结失败: {e}")
            return ""

    # ════════════════════════════════════════════════════════════════
    # 写入记忆
    # ════════════════════════════════════════════════════════════════

    def save_lesson(self, category: str, content: str,
                    importance: int = 3, market_regime: str = "",
                    related_trades: list = None) -> int:
        """
        保存一条教训

        Args:
            category: 类别 (buy/sell/risk/regime/general)
            content: 教训内容
            importance: 重要度 1-5
            market_regime: 当时的市场环境
            related_trades: 相关交易代码列表

        Returns:
            新记录的id
        """
        try:
            db = self._get_db()
            related_json = json.dumps(related_trades or [], ensure_ascii=False)
            lesson_id = db.insert_lesson({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "category": category,
                "content": content,
                "importance": importance,
                "market_regime": market_regime,
                "related_trades": related_json,
            })
            logger.info(f"保存教训: [{category}] {content[:50]}... id={lesson_id}")
            return lesson_id
        except Exception as e:
            logger.error(f"保存教训失败: {e}")
            return -1

    def save_decision(self, code: str, action: str, prompt: str,
                      response: str, reasoning: str,
                      confidence: float = 0.5, trade_id: int = None) -> int:
        """
        保存一条LLM决策记录

        Args:
            code: 股票代码
            action: BUY/SELL/HOLD
            prompt: LLM的输入prompt
            response: LLM的原始响应
            reasoning: 推理过程
            confidence: 置信度
            trade_id: 【Phase1-Task2】关联的交易记录ID，用于精确回填outcome

        Returns:
            新记录的id
        """
        try:
            # LLM没有返回内容时不写入决策表，避免仪表盘把调用失败误展示为交易判断。
            try:
                confidence_value = float(confidence or 0)
            except (TypeError, ValueError):
                confidence_value = 0
            if (not (response or "").strip()
                    and (reasoning or "").strip() == "LLM无响应"
                    and confidence_value <= 0):
                logger.info(f"跳过无响应LLM决策记录: {code} {action}")
                return -1

            db = self._get_db()
            decision_id = db.insert_llm_decision({
                "code": code,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "action": action,
                "llm_prompt": prompt[:2000],  # 截断避免过大
                "llm_response": response[:2000],
                "reasoning": reasoning,
                "confidence": confidence,
                "trade_id": trade_id,  # 【Phase1-Task2】精确关联交易记录
                "created_at": datetime.now().isoformat(),
            })
            logger.info(f"保存LLM决策: {code} {action} conf={confidence:.0%} id={decision_id} trade_id={trade_id}")
            return decision_id
        except Exception as e:
            logger.error(f"保存LLM决策失败: {e}")
            return -1

    def update_decision_outcome(self, decision_id: int,
                                outcome: str, outcome_pct: float):
        """
        更新决策结果（卖出后回填）

        Args:
            decision_id: 决策ID
            outcome: win/lose
            outcome_pct: 盈亏百分比
        """
        try:
            db = self._get_db()
            db.update_llm_decision(decision_id, {
                "outcome": outcome,
                "outcome_pct": outcome_pct,
            })
            logger.info(f"更新决策结果: id={decision_id} {outcome} {outcome_pct:+.1f}%")
        except Exception as e:
            logger.error(f"更新决策结果失败: {e}")

    def update_pending_decisions(self, prices: dict = None):
        """
        【Phase1-Task2】回填待验证决策结果 — 优先通过trade_id精确关联

        扫描所有 outcome 为 NULL 的 BUY 决策记录，检查该股票是否已卖出。
        优先使用trade_id精确匹配交易记录，避免多笔交易互相污染。
        只有已卖出的才回填 win/lose，避免持仓中的浮亏被误判为真亏。

        Args:
            prices: 未使用，保留接口兼容
        """
        try:
            db = self._get_db()
            c = db.conn.cursor()

            # 查询 outcome 为 NULL 的 BUY 决策
            c.execute("""
                SELECT id, code, action, trade_id, created_at FROM llm_decisions
                WHERE outcome IS NULL AND action = 'BUY'
                ORDER BY created_at DESC
            """)

            pending = c.fetchall()
            if not pending:
                return

            updated = 0
            for row in pending:
                decision_id = row["id"]
                code = row["code"]
                trade_id = row["trade_id"]

                # 【Phase1-Task2】优先通过trade_id精确匹配
                if trade_id:
                    # 通过trade_id直接获取对应的买入交易
                    c.execute("SELECT price FROM trades WHERE id = ?", (trade_id,))
                    buy_row = c.fetchone()
                    if not buy_row or not buy_row["price"]:
                        continue
                    entry_price = float(buy_row["price"])

                    # 查找该决策之后的卖出交易（同一只股票）
                    c.execute("""
                        SELECT price FROM trades
                        WHERE code = ? AND action = 'SELL'
                          AND created_at > (SELECT created_at FROM trades WHERE id = ?)
                        ORDER BY created_at ASC LIMIT 1
                    """, (code, trade_id))
                    sell_row = c.fetchone()
                else:
                    # 回退到旧逻辑：按code匹配最近的买卖（兼容历史数据）
                    c.execute("""
                        SELECT price FROM trades
                        WHERE code = ? AND action = 'BUY'
                        ORDER BY created_at DESC LIMIT 1
                    """, (code,))
                    buy_row = c.fetchone()
                    if not buy_row or not buy_row["price"]:
                        continue
                    entry_price = float(buy_row["price"])

                    c.execute("""
                        SELECT price FROM trades
                        WHERE code = ? AND action = 'SELL'
                        ORDER BY created_at DESC LIMIT 1
                    """, (code,))
                    sell_row = c.fetchone()

                if not sell_row:
                    continue  # 还在持仓，不回填

                sell_price = float(sell_row["price"])

                if not sell_price or not entry_price:
                    continue

                if entry_price <= 0:
                    continue

                pnl_pct = (sell_price - entry_price) / entry_price
                outcome = "win" if pnl_pct > 0 else "lose"

                db.update_llm_decision(decision_id, {
                    "outcome": outcome,
                    "outcome_pct": round(pnl_pct * 100, 2),
                })
                updated += 1

            if updated:
                logger.info(f"决策结果回填: 更新 {updated} 条 (已卖出)")

        except Exception as e:
            logger.warning(f"回填决策结果失败: {e}")

    # ════════════════════════════════════════════════════════════════
    # 辅助方法
    # ════════════════════════════════════════════════════════════════

    def _row_to_lesson(self, row) -> Lesson:
        """数据库行转Lesson对象"""
        row_dict = dict(row)
        return Lesson(
            id=row_dict.get("id", 0),
            date=row_dict.get("date", ""),
            category=row_dict.get("category", ""),
            content=row_dict.get("content", ""),
            importance=row_dict.get("importance", 3),
            market_regime=row_dict.get("market_regime", ""),
            related_trades=row_dict.get("related_trades", "[]"),
        )

    def _row_to_decision(self, row) -> DecisionRecord:
        """数据库行转DecisionRecord对象"""
        row_dict = dict(row)
        return DecisionRecord(
            id=row_dict.get("id", 0),
            code=row_dict.get("code", ""),
            date=row_dict.get("date", ""),
            action=row_dict.get("action", ""),
            reasoning=row_dict.get("reasoning", ""),
            confidence=row_dict.get("confidence", 0),
            outcome=row_dict.get("outcome"),
            outcome_pct=row_dict.get("outcome_pct"),
        )

    def format_memory_report(self, stock_code: str = None, regime: str = None) -> str:
        """格式化记忆报告"""
        parts = [
            "=" * 50,
            "交易记忆系统",
            "=" * 50,
        ]

        # 教训统计
        lessons = self.recall_lessons(limit=100)
        parts.append(f"教训总数: {len(lessons)}")
        categories = {}
        for l in lessons:
            categories[l.category] = categories.get(l.category, 0) + 1
        for cat, count in sorted(categories.items()):
            parts.append(f"  {cat}: {count}条")

        # 决策统计
        decisions = self.recall_decisions(limit=100)
        parts.append(f"\nLLM决策记录: {len(decisions)}条")
        if decisions:
            outcomes = {}
            for d in decisions:
                o = d.outcome or "pending"
                outcomes[o] = outcomes.get(o, 0) + 1
            for o, count in outcomes.items():
                parts.append(f"  {o}: {count}条")

        # 检索记忆
        if stock_code or regime:
            parts.append("-" * 50)
            parts.append("相关记忆:")
            context = self.recall(stock_code, regime)
            parts.append(context)

        parts.append("=" * 50)
        return "\n".join(parts)


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    memory = TradeMemory()
    print(memory.format_memory_report())

    # 测试记忆检索
    print("\n--- 检索测试 ---")
    print(memory.recall(stock_code="600519", regime="bull"))
