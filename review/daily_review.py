"""
每日复盘模块
盈亏归因、信号命中率、持仓检视、次日建议
"""
import json
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional
from collections import defaultdict
from dataclasses import dataclass, field, asdict

from config import DATA_DIR, INITIAL_CAPITAL

logger = logging.getLogger("review.daily")

# 复盘数据持久化目录
REVIEW_DIR = os.path.join(DATA_DIR, "reviews")


@dataclass
class DailyPnL:
    """单只股票日盈亏"""
    code: str
    name: str
    buy_price: float
    prev_price: float      # 昨收
    current_price: float   # 今收
    shares: int
    daily_pnl: float       # 日盈亏额
    daily_pnl_pct: float   # 日盈亏率
    total_pnl: float       # 累计盈亏
    total_pnl_pct: float   # 累计盈亏率
    weight: float          # 仓位占比
    reason: str = ""       # 归因


@dataclass
class TradeReview:
    """单笔交易复盘"""
    code: str
    name: str
    action: str
    price: float
    shares: int
    reason: str
    signal_score: float = 0.0
    result_pct: float = 0.0  # 如果是卖出，记录盈亏
    hit: bool = True         # 信号是否命中（盈利=命中）


@dataclass
class DailyReviewResult:
    """每日复盘结果"""
    date: str
    initial_capital: float
    total_assets: float
    daily_pnl: float
    daily_pnl_pct: float
    cumulative_pnl: float
    cumulative_pnl_pct: float
    cash: float
    market_value: float
    position_count: int

    # 持仓明细
    position_pnls: List[DailyPnL] = field(default_factory=list)
    # 当日交易
    trade_reviews: List[TradeReview] = field(default_factory=list)

    # 信号命中统计
    total_trades: int = 0
    win_trades: int = 0
    lose_trades: int = 0
    win_rate: float = 0.0

    # 次日建议
    suggestions: List[str] = field(default_factory=list)


class DailyReviewer:
    """
    每日复盘器

    功能:
        1. 计算日盈亏和累计盈亏
        2. 持仓盈亏归因
        3. 当日交易信号命中率
        4. 生成次日操作建议
        5. 持久化复盘记录
    """

    def __init__(self, review_dir: str = None, db_path: str = None):
        self.review_dir = review_dir or REVIEW_DIR
        self.db_path = db_path
        os.makedirs(self.review_dir, exist_ok=True)

    def run_review(
        self,
        date: str,
        positions: Dict[str, dict],
        trades: List[dict],
        prices: Dict[str, float],
        prev_prices: Dict[str, float],
        cash: float,
        initial_capital: float = None,
    ) -> DailyReviewResult:
        """
        执行每日复盘

        Args:
            date: 复盘日期
            positions: 当前持仓 {code: pos_dict}
            trades: 当日交易记录列表
            prices: 今日收盘价 {code: price}
            prev_prices: 昨日收盘价 {code: price}
            cash: 可用现金
            initial_capital: 初始资金

        Returns:
            DailyReviewResult
        """
        if initial_capital is None:
            initial_capital = INITIAL_CAPITAL

        # 1. 计算持仓市值和日盈亏
        market_value = 0.0
        position_pnls = []
        total_assets = cash

        for code, pos in positions.items():
            current = prices.get(code, pos["buy_price"])
            prev = prev_prices.get(code, pos["buy_price"])
            shares = pos["shares"]
            buy_price = pos["buy_price"]

            mv = current * shares
            market_value += mv
            total_assets += mv

            daily_pnl = (current - prev) * shares
            daily_pnl_pct = (current - prev) / prev if prev > 0 else 0
            total_pnl = (current - buy_price) * shares
            total_pnl_pct = (current - buy_price) / buy_price if buy_price > 0 else 0

            # 归因分析
            reason = self._analyze_pnl_reason(daily_pnl_pct)

            position_pnls.append(DailyPnL(
                code=code,
                name=pos.get("name", code),
                buy_price=buy_price,
                prev_price=prev,
                current_price=current,
                shares=shares,
                daily_pnl=daily_pnl,
                daily_pnl_pct=daily_pnl_pct,
                total_pnl=total_pnl,
                total_pnl_pct=total_pnl_pct,
                weight=mv / total_assets if total_assets > 0 else 0,
                reason=reason,
            ))

        # 2. 复盘当日交易
        trade_reviews = []
        win_count = 0
        lose_count = 0

        for t in trades:
            action = t.get("action", "")
            code = t.get("code", "")
            profit_pct = t.get("profit_pct", 0)
            hit = profit_pct >= 0 if action == "SELL" else True

            if action == "SELL":
                if hit:
                    win_count += 1
                else:
                    lose_count += 1

            trade_reviews.append(TradeReview(
                code=code,
                name=t.get("name", code),
                action=action,
                price=t.get("price", 0),
                shares=t.get("shares", 0),
                reason=t.get("reason", ""),
                signal_score=0,
                result_pct=profit_pct,
                hit=hit,
            ))

        total_trades = win_count + lose_count
        win_rate = win_count / total_trades if total_trades > 0 else 0

        # 3. 计算日盈亏
        daily_pnl = total_assets - initial_capital  # 简化，实际应与昨日比
        # 更准确: 用持仓日盈亏总和
        pos_daily_pnl = sum(p.daily_pnl for p in position_pnls)
        daily_pnl = pos_daily_pnl  # 不含已卖出的盈亏
        daily_pnl_pct = daily_pnl / (total_assets - daily_pnl) if (total_assets - daily_pnl) > 0 else 0

        cumulative_pnl = total_assets - initial_capital
        cumulative_pnl_pct = cumulative_pnl / initial_capital if initial_capital > 0 else 0

        # 4. 生成次日建议
        suggestions = self._generate_suggestions(
            position_pnls, trade_reviews, win_rate, total_assets, initial_capital
        )

        result = DailyReviewResult(
            date=date,
            initial_capital=initial_capital,
            total_assets=total_assets,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            cumulative_pnl=cumulative_pnl,
            cumulative_pnl_pct=cumulative_pnl_pct,
            cash=cash,
            market_value=market_value,
            position_count=len(positions),
            position_pnls=position_pnls,
            trade_reviews=trade_reviews,
            total_trades=total_trades,
            win_trades=win_count,
            lose_trades=lose_count,
            win_rate=win_rate,
            suggestions=suggestions,
        )

        # 持久化
        self._save_review(result)

        return result

    def _analyze_pnl_reason(self, pnl_pct: float) -> str:
        """盈亏归因分析"""
        if pnl_pct >= 0.05:
            return "大幅上涨"
        elif pnl_pct >= 0.02:
            return "上涨"
        elif pnl_pct > -0.02:
            return "横盘"
        elif pnl_pct > -0.05:
            return "下跌"
        else:
            return "大幅下跌"

    def _generate_suggestions(
        self,
        position_pnls: List[DailyPnL],
        trade_reviews: List[TradeReview],
        win_rate: float,
        total_assets: float,
        initial_capital: float,
    ) -> List[str]:
        """生成次日操作建议"""
        suggestions = []

        # 回撤预警
        drawdown = (total_assets - initial_capital) / initial_capital
        if drawdown < -0.10:
            suggestions.append("回撤超过10%，建议降低仓位或暂停交易")
        elif drawdown < -0.05:
            suggestions.append("回撤超过5%，建议关注止损位")

        # 连续亏损
        if win_rate < 0.3 and len(trade_reviews) >= 3:
            suggestions.append("近期胜率偏低，建议减少交易频率")

        # 仓位过重的股票
        for p in position_pnls:
            if p.weight > 0.18:
                suggestions.append(f"{p.name}仓位{p.weight:.1%}接近上限，注意风险")
            if p.total_pnl_pct < -0.03:
                suggestions.append(f"{p.name}累计亏损{p.total_pnl_pct:+.1%}，关注止损")

        # 涨幅过大
        for p in position_pnls:
            if p.total_pnl_pct > 0.08:
                suggestions.append(f"{p.name}涨幅{p.total_pnl_pct:+.1%}，可考虑部分止盈")

        if not suggestions:
            suggestions.append("持仓状态良好，继续持有观察")

        return suggestions

    def _save_review(self, result: DailyReviewResult):
        """保存复盘记录到JSON"""
        filepath = os.path.join(self.review_dir, f"review_{result.date}.json")
        data = {
            "date": result.date,
            "initial_capital": result.initial_capital,
            "total_assets": result.total_assets,
            "daily_pnl": result.daily_pnl,
            "daily_pnl_pct": result.daily_pnl_pct,
            "cumulative_pnl": result.cumulative_pnl,
            "cumulative_pnl_pct": result.cumulative_pnl_pct,
            "cash": result.cash,
            "market_value": result.market_value,
            "position_count": result.position_count,
            "position_pnls": [
                {
                    "code": p.code, "name": p.name,
                    "buy_price": p.buy_price, "prev_price": p.prev_price,
                    "current_price": p.current_price, "shares": p.shares,
                    "daily_pnl": p.daily_pnl, "daily_pnl_pct": p.daily_pnl_pct,
                    "total_pnl": p.total_pnl, "total_pnl_pct": p.total_pnl_pct,
                    "weight": p.weight, "reason": p.reason,
                }
                for p in result.position_pnls
            ],
            "trade_reviews": [
                {
                    "code": t.code, "name": t.name,
                    "action": t.action, "price": t.price,
                    "shares": t.shares, "reason": t.reason,
                    "result_pct": t.result_pct, "hit": t.hit,
                }
                for t in result.trade_reviews
            ],
            "win_rate": result.win_rate,
            "suggestions": result.suggestions,
            "created_at": datetime.now().isoformat(),
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"复盘记录已保存: {filepath}")

        # 双写：复盘快照同步到 SQLite
        try:
            from data.database import Database
            with Database(db_path=self.db_path) as db:
                db.save_review_snapshot(result.date, data)
        except Exception as e:
            logger.warning(f"SQLite双写失败(不影响JSON): {e}")

    def load_review(self, date: str) -> Optional[dict]:
        """加载历史复盘记录"""
        filepath = os.path.join(self.review_dir, f"review_{date}.json")
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def backfill_decision_outcomes(self, date: str = None) -> int:
        """
        回填llm_decisions表的outcome和outcome_pct

        逻辑:
        1. 查找指定日期所有llm_decisions记录（outcome为空的）
        2. 对BUY决策：通过code关联trades表，找到对应的卖出交易
        3. 计算盈亏百分比，更新outcome='WIN'/'LOSS'和outcome_pct
        4. 对SELL决策：通过code关联trades表，找到卖出交易的profit_pct

        Args:
            date: 回填日期，默认今天

        Returns:
            更新的记录数
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        updated_count = 0
        try:
            from data.database import Database
            with Database() as db:
                # 1. 查找今日outcome为空的决策记录
                decisions = db.get_llm_decisions(start_date=date, end_date=date, limit=200)
                # 过滤出outcome为空的
                pending = [d for d in decisions if not d.get("outcome")]
                if not pending:
                    logger.info(f"[outcome回填] {date} 无待回填决策")
                    return 0

                # 2. 获取今日所有交易记录
                trades = db.get_trades(start_date=date, end_date=date, limit=500)

                # 按code分组交易
                trades_by_code = {}
                for t in trades:
                    code = t.get("code", "")
                    if code not in trades_by_code:
                        trades_by_code[code] = []
                    trades_by_code[code].append(t)

                # 3. 逐条回填
                for dec in pending:
                    dec_id = dec["id"]
                    dec_code = dec.get("code", "")
                    dec_action = dec.get("action", "")
                    dec_date = dec.get("date", date)

                    code_trades = trades_by_code.get(dec_code, [])
                    if not code_trades:
                        continue

                    outcome = None
                    outcome_pct = None

                    if dec_action == "BUY":
                        # BUY决策：找对应的卖出交易，计算盈亏
                        buy_trade = None
                        sell_trade = None
                        for t in code_trades:
                            if t.get("action") == "BUY" and not buy_trade:
                                buy_trade = t
                            if t.get("action") == "SELL":
                                sell_trade = t  # 取最后一笔卖出

                        if buy_trade and sell_trade:
                            buy_price = buy_trade.get("price", 0)
                            sell_price = sell_trade.get("price", 0)
                            if buy_price > 0:
                                outcome_pct = (sell_price - buy_price) / buy_price * 100
                                outcome = "WIN" if outcome_pct > 0 else ("LOSS" if outcome_pct < 0 else "BREAKEVEN")

                    elif dec_action == "SELL":
                        # SELL决策：直接用交易记录的profit_pct
                        sell_trade = None
                        for t in code_trades:
                            if t.get("action") == "SELL":
                                sell_trade = t
                                break
                        if sell_trade:
                            outcome_pct = sell_trade.get("profit_pct", 0)
                            outcome = "WIN" if outcome_pct > 0 else ("LOSS" if outcome_pct < 0 else "BREAKEVEN")

                    if outcome is not None:
                        db.update_llm_decision(dec_id, {
                            "outcome": outcome,
                            "outcome_pct": round(outcome_pct, 2) if outcome_pct is not None else None,
                        })
                        updated_count += 1
                        logger.debug(f"[outcome回填] {dec_code} {dec_action} → {outcome} ({outcome_pct:+.1f}%)")

                logger.info(f"[outcome回填] {date} 更新{updated_count}/{len(pending)}条决策")

        except Exception as e:
            logger.warning(f"[outcome回填] 失败(非致命): {e}")

        return updated_count

    def backfill_outcomes(self) -> int:
        """
        回填所有未结算的LLM决策outcome（跨日期全量扫描）

        与 backfill_decision_outcomes 的区别:
          - 不限日期，扫描所有 outcome IS NULL 的决策记录
          - 通过 code + date 双键匹配交易记录，提高关联准确度
          - 适合批量补填历史遗漏数据

        逻辑:
          1. 查询 llm_decisions 表中 outcome 为空的所有记录
          2. 按决策日期范围，批量拉取对应的交易记录
          3. 对每条决策，按 code+date 匹配交易，计算盈亏百分比
          4. 根据 action 类型（BUY/SELL）采用不同的盈亏计算策略
          5. 将结果写回 llm_decisions 的 outcome 和 outcome_pct 字段

        Returns:
            成功更新的记录数
        """
        updated_count = 0
        try:
            from data.database import Database
            with Database() as db:
                # 1. 查询所有 outcome 为空的决策记录（不限日期，最多500条）
                c = db.conn.cursor()
                c.execute("""
                    SELECT id, code, date, action
                    FROM llm_decisions
                    WHERE outcome IS NULL
                    ORDER BY date ASC
                    LIMIT 500
                """)
                pending = [dict(row) for row in c.fetchall()]

                if not pending:
                    logger.info("[backfill_outcomes] 无待回填的决策记录")
                    return 0

                # 2. 确定日期范围，批量拉取交易记录（减少DB查询次数）
                min_date = min(d["date"] for d in pending if d.get("date"))
                max_date = max(d["date"] for d in pending if d.get("date"))
                # 交易记录的 created_at 是 ISO 格式，用日期前缀匹配
                trades = db.get_trades(
                    start_date=min_date,
                    end_date=max_date,
                    limit=2000,
                )

                # 3. 按 (code, date) 双键建立交易索引，方便快速查找
                #    key = "code|YYYY-MM-DD"，value = 该股票当天的交易列表
                trades_by_code_date = {}
                for t in trades:
                    code = t.get("code", "")
                    # created_at 格式如 "2026-06-08T10:30:00"，取日期部分
                    trade_date = (t.get("created_at") or "")[:10]
                    key = f"{code}|{trade_date}"
                    trades_by_code_date.setdefault(key, []).append(t)

                # 4. 逐条决策进行匹配和回填
                for dec in pending:
                    dec_id = dec["id"]
                    dec_code = dec.get("code", "")
                    dec_date = dec.get("date", "")
                    dec_action = dec.get("action", "")

                    # 用 code+date 精确匹配当天的交易记录
                    key = f"{dec_code}|{dec_date}"
                    matched_trades = trades_by_code_date.get(key, [])
                    if not matched_trades:
                        # 当天无交易记录，跳过（可能是决策未执行）
                        continue

                    outcome = None
                    outcome_pct = None

                    if dec_action == "BUY":
                        # ── BUY决策的盈亏计算 ──
                        # 逻辑: 找到买入价和卖出价，计算价差百分比
                        # 如果只有买入没有卖出，说明仍持仓，无法计算outcome
                        buy_price = 0
                        sell_price = 0
                        for t in matched_trades:
                            act = t.get("action", "")
                            if act == "BUY" and buy_price == 0:
                                buy_price = t.get("price", 0)
                            if act == "SELL":
                                # 取最后一笔卖出价格
                                sell_price = t.get("price", 0)

                        if buy_price > 0 and sell_price > 0:
                            # 已清仓：用卖出价与买入价计算盈亏
                            outcome_pct = (sell_price - buy_price) / buy_price * 100
                            outcome = "WIN" if outcome_pct > 0 else (
                                "LOSS" if outcome_pct < 0 else "BREAKEVEN"
                            )

                    elif dec_action == "SELL":
                        # ── SELL决策的盈亏计算 ──
                        # 逻辑: SELL决策的好坏看卖出是否及时
                        # 直接使用交易记录中已计算好的 profit_pct
                        sell_trade = None
                        for t in matched_trades:
                            if t.get("action") == "SELL":
                                sell_trade = t
                                # 取最后一笔卖出
                        if sell_trade:
                            outcome_pct = sell_trade.get("profit_pct", 0)
                            if outcome_pct is not None:
                                outcome = "WIN" if outcome_pct > 0 else (
                                    "LOSS" if outcome_pct < 0 else "BREAKEVEN"
                                )

                    # 5. 写回数据库
                    if outcome is not None:
                        db.update_llm_decision(dec_id, {
                            "outcome": outcome,
                            "outcome_pct": round(outcome_pct, 2) if outcome_pct is not None else None,
                        })
                        updated_count += 1
                        logger.debug(
                            f"[backfill_outcomes] {dec_code} {dec_action} "
                            f"@{dec_date} → {outcome} ({outcome_pct:+.1f}%)"
                        )

                logger.info(
                    f"[backfill_outcomes] 扫描{len(pending)}条, "
                    f"成功回填{updated_count}条"
                )

        except Exception as e:
            logger.warning(f"[backfill_outcomes] 回填失败(非致命): {e}")

        return updated_count

    def format_review(self, result: DailyReviewResult) -> str:
        """格式化复盘报告"""
        lines = [
            "=" * 65,
            f"每日复盘 {result.date}",
            "=" * 65,
            f"初始资金:   {result.initial_capital:>14,.0f}",
            f"总资产:     {result.total_assets:>14,.0f}",
            f"可用现金:   {result.cash:>14,.0f}",
            f"持仓市值:   {result.market_value:>14,.0f}",
            f"日盈亏:     {result.daily_pnl:>+14,.0f} ({result.daily_pnl_pct:+.2%})",
            f"累计盈亏:   {result.cumulative_pnl:>+14,.0f} ({result.cumulative_pnl_pct:+.2%})",
            "-" * 65,
        ]

        # 持仓盈亏
        if result.position_pnls:
            lines.append("持仓盈亏:")
            lines.append(
                f"  {'代码':<8} {'名称':<8} {'昨收':>8} {'今收':>8} "
                f"{'日盈亏%':>8} {'累计%':>8} {'仓位%':>6}"
            )
            lines.append("  " + "-" * 60)
            for p in result.position_pnls:
                lines.append(
                    f"  {p.code:<8} {p.name:<8} {p.prev_price:>8.2f} "
                    f"{p.current_price:>8.2f} {p.daily_pnl_pct:>+7.1%} "
                    f"{p.total_pnl_pct:>+7.1%} {p.weight:>5.1%}"
                )

        # 当日交易
        if result.trade_reviews:
            lines.append("-" * 65)
            lines.append("当日交易:")
            for t in result.trade_reviews:
                hit_str = "OK" if t.hit else "MISS"
                pnl_str = f" {t.result_pct:+.1%}" if t.action == "SELL" else ""
                lines.append(
                    f"  [{t.action}] {t.code} {t.name} {t.shares}股@{t.price:.2f} "
                    f"{hit_str}{pnl_str} | {t.reason}"
                )

        # 命中率
        if result.total_trades > 0:
            lines.append("-" * 65)
            lines.append(
                f"信号命中率: {result.win_rate:.0%} "
                f"(盈利{result.win_trades}/亏损{result.lose_trades})"
            )

        # 次日建议
        if result.suggestions:
            lines.append("-" * 65)
            lines.append("次日建议:")
            for s in result.suggestions:
                lines.append(f"  - {s}")

        lines.append("=" * 65)
        return "\n".join(lines)


def run_daily_review(return_data: bool = False):
    """包装函数：自动从模拟账户收集数据并执行复盘"""
    from execution.broker import get_broker_adapter
    from data.realtime import get_realtime

    broker = get_broker_adapter()
    account = broker.account if hasattr(broker, "account") else broker
    today = datetime.now().strftime("%Y-%m-%d")

    # 当前持仓
    positions = broker.get_positions() or {}
    # 当日交易记录
    trades = getattr(account, "trades", []) or []
    today_trades = [
        t for t in trades
        if (t.get("timestamp") or t.get("created_at") or t.get("date") or "").startswith(today)
    ]

    # 获取当前/最新价格
    codes = list(positions.keys())
    prices = {}
    prev_prices = {}
    if codes:
        quotes = get_realtime(codes)
        for q in quotes:
            prices[q.code] = q.price
            prev_prices[q.code] = q.close_prev

    reviewer = DailyReviewer()
    result = reviewer.run_review(
        date=today,
        positions=positions,
        trades=today_trades,
        prices=prices,
        prev_prices=prev_prices,
        cash=broker.get_cash(),
        initial_capital=getattr(account, "initial_capital", INITIAL_CAPITAL),
    )

    # 【TaskA】收盘复盘时回填LLM决策的outcome
    try:
        reviewer.backfill_decision_outcomes(today)
    except Exception as e:
        logger.warning(f"outcome回填失败(非致命): {e}")

    text = reviewer.format_review(result)
    if return_data:
        return {"text": text, "data": asdict(result)}
    return text


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    reviewer = DailyReviewer()

    # 模拟复盘
    positions = {
        "600519": {"name": "贵州茅台", "buy_price": 1800, "shares": 100},
        "300750": {"name": "宁德时代", "buy_price": 200, "shares": 500},
    }
    trades = [
        {"action": "SELL", "code": "000858", "name": "五粮液", "price": 160,
         "shares": 200, "reason": "止盈", "profit_pct": 8.5},
    ]
    prices = {"600519": 1850, "300750": 195}
    prev_prices = {"600519": 1820, "300750": 200}

    result = reviewer.run_review(
        date="2024-01-15",
        positions=positions,
        trades=trades,
        prices=prices,
        prev_prices=prev_prices,
        cash=400_000,
    )
    print(reviewer.format_review(result))
