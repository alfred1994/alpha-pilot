"""
绩效统计模块
夏普比率、索提诺比率、卡尔玛比率、年化收益等
"""
import json
import os
import logging
import math
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from config import DATA_DIR, INITIAL_CAPITAL

logger = logging.getLogger("review.performance")

# 复盘目录（复用daily_review的目录）
REVIEW_DIR = os.path.join(DATA_DIR, "reviews")

# 无风险利率（年化，按1年期国债约2%）
RISK_FREE_RATE = 0.02


@dataclass
class PerformanceMetrics:
    """绩效指标"""
    # 收益指标
    total_return: float = 0.0          # 总收益率
    annualized_return: float = 0.0     # 年化收益率
    daily_avg_return: float = 0.0      # 日均收益率
    daily_return_std: float = 0.0      # 日收益率标准差

    # 风险调整指标
    sharpe_ratio: float = 0.0          # 夏普比率
    sortino_ratio: float = 0.0         # 索提诺比率
    calmar_ratio: float = 0.0          # 卡尔玛比率
    information_ratio: float = 0.0     # 信息比率（vs基准）

    # 风险指标
    max_drawdown: float = 0.0          # 最大回撤
    max_drawdown_duration: int = 0     # 最大回撤持续天数
    volatility: float = 0.0            # 年化波动率
    downside_volatility: float = 0.0   # 下行波动率

    # 交易指标
    total_trades: int = 0
    win_trades: int = 0
    lose_trades: int = 0
    win_rate: float = 0.0              # 胜率
    profit_factor: float = 0.0         # 盈亏比（总盈利/总亏损）
    avg_win: float = 0.0               # 平均盈利
    avg_loss: float = 0.0              # 平均亏损
    max_win: float = 0.0               # 最大单笔盈利
    max_loss: float = 0.0              # 最大单笔亏损
    avg_hold_days: float = 0.0         # 平均持仓天数

    # 时间区间
    start_date: str = ""
    end_date: str = ""
    trading_days: int = 0


class PerformanceAnalyzer:
    """
    绩效分析器

    功能:
        1. 从复盘记录或净值序列计算各项绩效指标
        2. 夏普/索提诺/卡尔玛比率
        3. 交易统计（胜率、盈亏比等）
        4. 格式化绩效报告
    """

    def __init__(self, risk_free_rate: float = None, review_dir: str = None):
        self.risk_free_rate = risk_free_rate if risk_free_rate is not None else RISK_FREE_RATE
        self.review_dir = review_dir or REVIEW_DIR

    def analyze_from_nav_series(
        self,
        nav_series: List[dict],
        trades: List[dict] = None,
        initial_capital: float = None,
    ) -> PerformanceMetrics:
        """
        从净值序列计算绩效

        Args:
            nav_series: [{date, total_assets}] 按日期排序的净值序列
            trades: 交易记录列表 [{action, price, shares, ...}]
            initial_capital: 初始资金

        Returns:
            PerformanceMetrics
        """
        if initial_capital is None:
            initial_capital = INITIAL_CAPITAL
        if not nav_series or len(nav_series) < 2:
            return PerformanceMetrics()

        metrics = PerformanceMetrics()
        metrics.start_date = nav_series[0]["date"]
        metrics.end_date = nav_series[-1]["date"]
        metrics.trading_days = len(nav_series)

        # ── 收益计算 ──────────────────────────────────────────────
        final_assets = nav_series[-1]["total_assets"]
        metrics.total_return = (final_assets - initial_capital) / initial_capital

        # 日收益率序列
        daily_returns = []
        for i in range(1, len(nav_series)):
            prev = nav_series[i - 1]["total_assets"]
            curr = nav_series[i]["total_assets"]
            if prev > 0:
                daily_returns.append((curr - prev) / prev)

        if not daily_returns:
            return metrics

        metrics.daily_avg_return = sum(daily_returns) / len(daily_returns)

        # 年化收益（假设一年250个交易日）
        years = len(daily_returns) / 250
        if years > 0 and metrics.total_return > -1:
            metrics.annualized_return = (1 + metrics.total_return) ** (1 / years) - 1

        # ── 波动率 ───────────────────────────────────────────────
        if len(daily_returns) >= 2:
            mean_ret = metrics.daily_avg_return
            variance = sum((r - mean_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            metrics.daily_return_std = math.sqrt(variance)
            metrics.volatility = metrics.daily_return_std * math.sqrt(250)

            # 下行波动率（只计算负收益）
            negative_returns = [r for r in daily_returns if r < 0]
            if len(negative_returns) >= 2:
                downside_var = sum(r ** 2 for r in negative_returns) / len(negative_returns)
                metrics.downside_volatility = math.sqrt(downside_var) * math.sqrt(250)

        # ── 最大回撤 ─────────────────────────────────────────────
        peak = nav_series[0]["total_assets"]
        max_dd = 0
        dd_start = 0
        max_dd_duration = 0
        current_dd_duration = 0

        for i, nav in enumerate(nav_series):
            value = nav["total_assets"]
            if value > peak:
                peak = value
                current_dd_duration = 0
            else:
                current_dd_duration += 1
                dd = (value - peak) / peak
                if dd < max_dd:
                    max_dd = dd
                    max_dd_duration = current_dd_duration

        metrics.max_drawdown = max_dd
        metrics.max_drawdown_duration = max_dd_duration

        # ── 风险调整比率 ─────────────────────────────────────────
        daily_rf = self.risk_free_rate / 250

        # 夏普比率 = (年化收益 - 无风险利率) / 年化波动率
        if metrics.volatility > 0:
            metrics.sharpe_ratio = (
                (metrics.annualized_return - self.risk_free_rate) / metrics.volatility
            )

        # 索提诺比率 = (年化收益 - 无风险利率) / 下行波动率
        if metrics.downside_volatility > 0:
            metrics.sortino_ratio = (
                (metrics.annualized_return - self.risk_free_rate) / metrics.downside_volatility
            )

        # 卡尔玛比率 = 年化收益 / |最大回撤|
        if metrics.max_drawdown < 0:
            metrics.calmar_ratio = metrics.annualized_return / abs(metrics.max_drawdown)

        # ── 交易统计 ─────────────────────────────────────────────
        if trades:
            self._compute_trade_metrics(metrics, trades)

        return metrics

    def analyze_from_review_files(
        self,
        start_date: str = None,
        end_date: str = None,
    ) -> PerformanceMetrics:
        """
        从本地复盘文件计算绩效

        Args:
            start_date: 起始日期（None=全部）
            end_date: 结束日期（None=全部）

        Returns:
            PerformanceMetrics
        """
        nav_series = []
        all_trades = []

        # 读取所有复盘文件
        if not os.path.exists(self.review_dir):
            return PerformanceMetrics()

        files = sorted(os.listdir(self.review_dir))
        for fname in files:
            if not fname.startswith("review_") or not fname.endswith(".json"):
                continue

            date = fname.replace("review_", "").replace(".json", "")
            if start_date and date < start_date:
                continue
            if end_date and date > end_date:
                continue

            filepath = os.path.join(self.review_dir, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                nav_series.append({
                    "date": data["date"],
                    "total_assets": data["total_assets"],
                })

                for t in data.get("trade_reviews", []):
                    all_trades.append(t)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"读取复盘文件失败 {fname}: {e}")

        initial_capital = INITIAL_CAPITAL
        if nav_series:
            # 第一天的总资产作为参考
            pass

        return self.analyze_from_nav_series(nav_series, all_trades, initial_capital)

    def _compute_trade_metrics(self, metrics: PerformanceMetrics, trades: List[dict]):
        """计算交易相关指标"""
        # 配对买卖
        buy_map = {}
        results = []

        for t in trades:
            action = t.get("action", "")
            code = t.get("code", "")

            if action in ("BUY", "买入"):
                buy_map[code] = t
            elif action in ("SELL", "卖出") and code in buy_map:
                buy_t = buy_map.pop(code)
                buy_price = buy_t.get("price", 0)
                sell_price = t.get("price", 0)
                if buy_price > 0:
                    pnl_pct = (sell_price - buy_price) / buy_price
                    results.append(pnl_pct)

        if not results:
            return

        metrics.total_trades = len(results)
        wins = [r for r in results if r >= 0]
        losses = [r for r in results if r < 0]

        metrics.win_trades = len(wins)
        metrics.lose_trades = len(losses)
        metrics.win_rate = len(wins) / len(results) if results else 0

        metrics.avg_win = sum(wins) / len(wins) if wins else 0
        metrics.avg_loss = sum(losses) / len(losses) if losses else 0
        metrics.max_win = max(results)
        metrics.max_loss = min(results)

        total_win = sum(wins)
        total_loss = abs(sum(losses))
        metrics.profit_factor = total_win / total_loss if total_loss > 0 else float("inf")

    def format_report(self, metrics: PerformanceMetrics) -> str:
        """格式化绩效报告"""
        lines = [
            "=" * 60,
            "绩效统计报告",
            "=" * 60,
            f"统计区间: {metrics.start_date} ~ {metrics.end_date} ({metrics.trading_days}个交易日)",
        ]

        # 收益指标
        lines.append("-" * 60)
        lines.append("收益指标")
        lines.append("-" * 60)
        lines.append(f"  总收益率:       {metrics.total_return:>+10.2%}")
        lines.append(f"  年化收益:       {metrics.annualized_return:>+10.2%}")
        lines.append(f"  日均收益:       {metrics.daily_avg_return:>+10.4%}")

        # 风险指标
        lines.append("-" * 60)
        lines.append("风险指标")
        lines.append("-" * 60)
        lines.append(f"  年化波动率:     {metrics.volatility:>10.2%}")
        lines.append(f"  下行波动率:     {metrics.downside_volatility:>10.2%}")
        lines.append(f"  最大回撤:       {metrics.max_drawdown:>+10.2%}")
        lines.append(f"  回撤持续天数:   {metrics.max_drawdown_duration:>10d}")

        # 风险调整指标
        lines.append("-" * 60)
        lines.append("风险调整指标")
        lines.append("-" * 60)
        lines.append(f"  夏普比率:       {metrics.sharpe_ratio:>10.2f}")
        lines.append(f"  索提诺比率:     {metrics.sortino_ratio:>10.2f}")
        lines.append(f"  卡尔玛比率:     {metrics.calmar_ratio:>10.2f}")

        # 交易统计
        if metrics.total_trades > 0:
            lines.append("-" * 60)
            lines.append("交易统计")
            lines.append("-" * 60)
            lines.append(f"  总交易次数:     {metrics.total_trades:>10d}")
            lines.append(f"  盈利次数:       {metrics.win_trades:>10d}")
            lines.append(f"  亏损次数:       {metrics.lose_trades:>10d}")
            lines.append(f"  胜率:           {metrics.win_rate:>10.1%}")
            lines.append(f"  盈亏比:         {metrics.profit_factor:>10.2f}")
            lines.append(f"  平均盈利:       {metrics.avg_win:>+10.2%}")
            lines.append(f"  平均亏损:       {metrics.avg_loss:>+10.2%}")
            lines.append(f"  最大单笔盈利:   {metrics.max_win:>+10.2%}")
            lines.append(f"  最大单笔亏损:   {metrics.max_loss:>+10.2%}")

        lines.append("=" * 60)

        # 评级
        rating = self._rate_performance(metrics)
        lines.append(f"综合评级: {rating}")
        lines.append("=" * 60)

        return "\n".join(lines)

    def _rate_performance(self, metrics: PerformanceMetrics) -> str:
        """综合评级"""
        score = 0

        # 夏普比率评分
        if metrics.sharpe_ratio >= 2.0:
            score += 3
        elif metrics.sharpe_ratio >= 1.0:
            score += 2
        elif metrics.sharpe_ratio >= 0.5:
            score += 1

        # 胜率评分
        if metrics.win_rate >= 0.6:
            score += 2
        elif metrics.win_rate >= 0.5:
            score += 1

        # 回撤评分
        if metrics.max_drawdown > -0.10:
            score += 2
        elif metrics.max_drawdown > -0.15:
            score += 1

        # 年化收益评分
        if metrics.annualized_return >= 0.15:
            score += 2
        elif metrics.annualized_return >= 0.08:
            score += 1

        if score >= 8:
            return "A (优秀)"
        elif score >= 6:
            return "B (良好)"
        elif score >= 4:
            return "C (一般)"
        elif score >= 2:
            return "D (较差)"
        else:
            return "E (很差)"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    analyzer = PerformanceAnalyzer()

    # 模拟净值序列
    import random
    random.seed(42)
    nav = 1_000_000
    nav_series = []
    for i in range(60):
        date = f"2024-01-{i+1:02d}" if i < 31 else f"2024-02-{i-30:02d}"
        nav *= (1 + random.uniform(-0.02, 0.025))
        nav_series.append({"date": date, "total_assets": nav})

    # 模拟交易
    trades = [
        {"action": "BUY", "code": "600519", "price": 1800, "shares": 100},
        {"action": "SELL", "code": "600519", "price": 1880, "shares": 100},
        {"action": "BUY", "code": "300750", "price": 200, "shares": 500},
        {"action": "SELL", "code": "300750", "price": 190, "shares": 500},
    ]

    metrics = analyzer.analyze_from_nav_series(nav_series, trades)
    print(analyzer.format_report(metrics))
