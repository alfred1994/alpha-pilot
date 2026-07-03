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
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger("strategy.memory")

MEMORY_LAYERS = ("short", "medium", "long")
MEMORY_LAYER_LABELS = {
    "short": "短期记忆",
    "medium": "中期记忆",
    "long": "长期记忆",
}


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
        layered = self.recall_layered(stock_code=stock_code, regime=regime)
        if layered != "暂无分层记忆。":
            return layered

        return self._recall_legacy(stock_code=stock_code, regime=regime)

    def recall_layered(self, stock_code: str = None, regime: str = None,
                       sector: str = None, max_chars: int = 1500) -> str:
        """
        检索短期/中期/长期分层记忆。

        分层记忆只负责给LLM提供可验证经验，不直接改变下单或风控结果。
        """
        try:
            db = self._get_db()
            rows_by_layer = {layer: [] for layer in MEMORY_LAYERS}
            queries: List[Tuple[str, str]] = [("global", "")]
            if regime:
                queries.append(("regime", regime))
            if sector:
                queries.append(("sector", sector))
            if stock_code:
                queries.append(("stock", stock_code))

            seen = set()
            for layer in MEMORY_LAYERS:
                for scope, key in queries:
                    rows = db.get_memory_items(
                        layer=layer, scope=scope, key=key,
                        active=True, limit=5,
                    )
                    for row in rows:
                        row_id = row.get("id")
                        if row_id in seen:
                            continue
                        seen.add(row_id)
                        rows_by_layer[layer].append(row)

            parts = []
            for layer in MEMORY_LAYERS:
                rows = sorted(
                    rows_by_layer[layer],
                    key=lambda r: (float(r.get("score") or 0), r.get("updated_at") or ""),
                    reverse=True,
                )[:5]
                if not rows:
                    continue
                parts.append(f"【{MEMORY_LAYER_LABELS[layer]}】")
                for row in rows:
                    category = row.get("category") or "general"
                    score = float(row.get("score") or 0)
                    parts.append(f"- [{category} {score:.0f}] {row.get('content', '')}")

            if not parts:
                return "暂无分层记忆。"

            text = "\n".join(parts)
            if max_chars and len(text) > max_chars:
                text = text[:max_chars - 20].rstrip() + "\n...（记忆已截断）"
            return text
        except Exception as e:
            logger.warning(f"检索分层记忆失败: {e}")
            return "暂无分层记忆。"

    def _recall_legacy(self, stock_code: str = None, regime: str = None) -> str:
        """兼容旧版 lessons/llm_decisions 记忆检索。"""
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

    def save_memory_item(self, layer: str, scope: str, key: str, category: str,
                         content: str, evidence: dict = None, score: float = 50,
                         source: str = "memory", expires_at: str = None) -> int:
        """
        保存一条分层记忆。

        相同 layer/scope/key/category/content 的活跃记忆会更新而不是重复插入。
        """
        if layer not in MEMORY_LAYERS:
            raise ValueError(f"非法记忆层级: {layer}")
        content = (content or "").strip()
        if not content:
            return -1

        try:
            db = self._get_db()
            c = db.conn.cursor()
            key_value = key or ""
            category_value = category or "general"
            c.execute("""
                SELECT id, score, evidence FROM memory_items
                WHERE layer = ? AND scope = ? AND key = ? AND category = ?
                  AND content = ? AND active = 1
                ORDER BY updated_at DESC LIMIT 1
            """, (layer, scope, key_value, category_value, content))
            existing = c.fetchone()
            item = {
                "layer": layer,
                "scope": scope,
                "key": key_value,
                "category": category_value,
                "content": content,
                "evidence": evidence or {},
                "score": max(0, min(100, float(score or 0))),
                "source": source,
                "expires_at": expires_at,
                "active": 1,
            }
            if existing:
                merged_score = max(float(existing["score"] or 0), item["score"])
                db.update_memory_item(existing["id"], {
                    "evidence": item["evidence"],
                    "score": merged_score,
                    "source": source,
                    "expires_at": expires_at,
                    "active": 1,
                })
                return existing["id"]
            return db.insert_memory_item(item)
        except Exception as e:
            logger.warning(f"保存分层记忆失败: {e}")
            return -1

    def consolidate_layers(self, date: str = None, lookback_days: int = 10) -> Dict:
        """
        将复盘、订单审计、LLM决策和prompt进化建议沉淀为分层记忆。

        Returns:
            {"short": 2, "medium": 1, "long": 1, "expired": 3}
        """
        target_date = date or datetime.now().strftime("%Y-%m-%d")
        since = (
            datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")
        result = {"short": 0, "medium": 0, "long": 0, "expired": 0}

        try:
            result["expired"] = self._expire_short_memory()
            result["short"] += self._consolidate_short_memory(target_date)
            medium_count, promoted_count = self._consolidate_decision_patterns(since, target_date)
            result["medium"] += medium_count
            result["long"] += promoted_count
            result["long"] += self._consolidate_important_lessons(since)
            result["long"] += self._consolidate_prompt_hints()
            logger.info(
                "分层记忆进化完成: short=%s medium=%s long=%s expired=%s",
                result["short"], result["medium"], result["long"], result["expired"],
            )
        except Exception as e:
            logger.warning(f"分层记忆进化失败: {e}")
        return result

    def _expire_short_memory(self) -> int:
        """失效过期短期记忆。"""
        db = self._get_db()
        now = datetime.now().isoformat()
        c = db.conn.cursor()
        c.execute("""
            UPDATE memory_items
            SET active = 0, updated_at = ?
            WHERE layer = 'short' AND active = 1
              AND expires_at IS NOT NULL AND expires_at < ?
        """, (now, now))
        db.conn.commit()
        return c.rowcount if c.rowcount and c.rowcount > 0 else 0

    def _consolidate_short_memory(self, target_date: str) -> int:
        """沉淀当天事件和教训为短期记忆。"""
        db = self._get_db()
        count = 0
        expires_at = (
            datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=2)
        ).isoformat()

        for lesson in db.get_lessons(limit=50):
            if lesson.get("date") != target_date:
                continue
            codes = self._loads_json(lesson.get("related_trades"), [])
            key = codes[0] if codes else lesson.get("market_regime", "") or ""
            scope = "stock" if codes else ("regime" if lesson.get("market_regime") else "global")
            content = f"今日复盘教训: {lesson.get('content', '')}"
            item_id = self.save_memory_item(
                layer="short",
                scope=scope,
                key=key,
                category=lesson.get("category") or "general",
                content=content,
                evidence={"lesson_ids": [lesson.get("id")], "codes": codes},
                score=60 + min(30, int(lesson.get("importance") or 3) * 6),
                source="lesson",
                expires_at=expires_at,
            )
            if item_id > 0:
                count += 1

        for event in db.get_auto_events(date=target_date, limit=20):
            actions = event.get("actions") or []
            error = event.get("error") or ""
            if not actions and not error:
                continue
            summary = "；".join(str(x) for x in actions[:3])
            if error:
                summary = (summary + "；" if summary else "") + f"错误: {error[:80]}"
            item_id = self.save_memory_item(
                layer="short",
                scope="global",
                key="",
                category="execution",
                content=f"今日自动盯盘事件: {summary[:180]}",
                evidence={"auto_event_ids": [event.get("id")], "status": event.get("status")},
                score=55,
                source="auto_event",
                expires_at=expires_at,
            )
            if item_id > 0:
                count += 1
        return count

    def _consolidate_decision_patterns(self, since: str, target_date: str) -> Tuple[int, int]:
        """从已验证LLM决策聚合中期模式，并将稳定模式提升为长期记忆。"""
        db = self._get_db()
        decisions = db.get_llm_decisions(start_date=since, end_date=target_date, limit=500)
        groups: Dict[Tuple[str, str], List[dict]] = {}
        for decision in decisions:
            outcome = str(decision.get("outcome") or "").lower()
            if outcome not in ("win", "wins", "lose", "loss", "losses"):
                continue
            key = (decision.get("code") or "", decision.get("action") or "HOLD")
            groups.setdefault(key, []).append(decision)

        medium_count = 0
        long_count = 0
        for (code, action), rows in groups.items():
            if not code or len(rows) < 2:
                continue
            wins = [r for r in rows if str(r.get("outcome")).lower() in ("win", "wins")]
            loses = [r for r in rows if r not in wins]
            total = len(rows)
            win_rate = len(wins) / total
            avg_pct = sum(float(r.get("outcome_pct") or 0) for r in rows) / total
            if win_rate >= 0.6:
                category = "entry"
                content = f"近{total}次{code} {action}决策胜率{win_rate:.0%}，平均结果{avg_pct:+.1f}%，同类信号可提高关注。"
            elif win_rate <= 0.4:
                category = "risk"
                content = f"近{total}次{code} {action}决策胜率仅{win_rate:.0%}，平均结果{avg_pct:+.1f}%，同类信号需降权或等待确认。"
            else:
                continue

            evidence = {
                "decision_ids": [r.get("id") for r in rows],
                "win_count": len(wins),
                "lose_count": len(loses),
                "avg_outcome_pct": round(avg_pct, 2),
                "codes": [code],
            }
            score = 50 + min(40, abs(win_rate - 0.5) * 80 + total * 3)
            if self.save_memory_item(
                layer="medium", scope="stock", key=code, category=category,
                content=content, evidence=evidence, score=score,
                source="decision_pattern",
            ) > 0:
                medium_count += 1

            if total >= 3 and (win_rate >= 0.67 or win_rate <= 0.33):
                if self.save_memory_item(
                    layer="long", scope="stock", key=code, category=category,
                    content="长期验证: " + content, evidence=evidence,
                    score=min(100, score + 10), source="decision_pattern",
                ) > 0:
                    long_count += 1
        return medium_count, long_count

    def _consolidate_important_lessons(self, since: str) -> int:
        """将高重要度教训提升为长期记忆。"""
        db = self._get_db()
        count = 0
        for lesson in db.get_lessons(limit=200):
            if (lesson.get("date") or "") < since:
                continue
            importance = int(lesson.get("importance") or 3)
            if importance < 4:
                continue
            codes = self._loads_json(lesson.get("related_trades"), [])
            scope = "stock" if codes else ("regime" if lesson.get("market_regime") else "global")
            key = codes[0] if codes else lesson.get("market_regime", "") or ""
            if self.save_memory_item(
                layer="long",
                scope=scope,
                key=key,
                category=lesson.get("category") or "general",
                content=f"复盘高重要度教训: {lesson.get('content', '')}",
                evidence={"lesson_ids": [lesson.get("id")], "codes": codes},
                score=min(100, 55 + importance * 8),
                source="important_lesson",
            ) > 0:
                count += 1
        return count

    def _consolidate_prompt_hints(self) -> int:
        """将当前生效prompt建议同步为长期全局记忆。"""
        db = self._get_db()
        count = 0
        try:
            rows = db.conn.execute("""
                SELECT id, hint, source, created_at
                FROM active_prompt_hints
                WHERE active = 1
                ORDER BY id DESC LIMIT 3
            """).fetchall()
        except Exception:
            return 0
        for row in rows:
            if self.save_memory_item(
                layer="long",
                scope="global",
                key="",
                category="prompt",
                content=f"生效Prompt进化建议: {row['hint']}",
                evidence={"prompt_hint_ids": [row["id"]], "source": row["source"]},
                score=72,
                source="prompt_evolution",
            ) > 0:
                count += 1
        return count

    @staticmethod
    def _loads_json(value, default):
        """容错解析JSON字段。"""
        if value is None:
            return default
        if isinstance(value, (list, dict)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return default

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
