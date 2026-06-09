"""
报告生成模块
盈亏统计、信号命中率、格式化输出
"""
from datetime import datetime
from typing import List, Optional
from collections import defaultdict

from portfolio.backtest import BacktestResult, DailySnapshot
from portfolio.position import TradeRecord


def format_backtest_report(result: BacktestResult) -> str:
    """格式化回测报告"""
    lines = []
    lines.append("=" * 60)
    lines.append("📈 回测报告")
    lines.append("=" * 60)

    # 基本信息
    lines.append(f"\n📅 回测区间: {result.start_date} ~ {result.end_date}")
    lines.append(f"💰 初始资金: {result.initial_capital:>14,.0f}")
    lines.append(f"💰 最终资产: {result.final_assets:>14,.0f}")

    # 收益指标
    lines.append("\n" + "-" * 40)
    lines.append("📊 收益指标")
    lines.append("-" * 40)
    lines.append(f"  总收益率:     {result.total_return:>+10.2%}")
    lines.append(f"  年化收益:     {result.annualized_return:>+10.2%}")
    lines.append(f"  基准收益:     {result.benchmark_return:>+10.2%}")
    lines.append(f"  超额收益:     {result.excess_return:>+10.2%}")

    # 风险指标
    lines.append("\n" + "-" * 40)
    lines.append("⚠️  风险指标")
    lines.append("-" * 40)
    lines.append(f"  夏普比率:     {result.sharpe_ratio:>10.2f}")
    lines.append(f"  最大回撤:     {result.max_drawdown:>10.2%}")
    if result.max_drawdown_start:
        lines.append(f"  回撤区间:     {result.max_drawdown_start} ~ {result.max_drawdown_end}")

    # 交易统计
    lines.append("\n" + "-" * 40)
    lines.append("📋 交易统计")
    lines.append("-" * 40)
    lines.append(f"  总交易次数:   {result.total_trades:>10d}")
    lines.append(f"  盈利次数:     {result.win_trades:>10d}")
    lines.append(f"  亏损次数:     {result.lose_trades:>10d}")
    lines.append(f"  胜率:         {result.win_rate:>10.2%}")
    lines.append(f"  平均持仓天数: {result.avg_hold_days:>10.1f}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


def format_trade_list(trades: List[TradeRecord]) -> str:
    """格式化交易记录列表"""
    if not trades:
        return "无交易记录"

    lines = []
    lines.append("-" * 70)
    lines.append(f"{'日期':^12} {'操作':^6} {'代码':^8} {'名称':^8} {'价格':>8} {'数量':>8} {'金额':>12} {'原因'}")
    lines.append("-" * 70)

    for t in trades:
        lines.append(
            f"{t.date:12s} {t.action:^6s} {t.code:8s} {t.name:8s} "
            f"{t.price:>8.2f} {t.shares:>8d} {t.amount:>12,.0f} {t.reason}"
        )

    lines.append("-" * 70)
    lines.append(f"共 {len(trades)} 笔交易")
    return "\n".join(lines)


def format_daily_nav(snapshots: List[DailySnapshot], last_n: int = 10) -> str:
    """格式化每日净值（默认只显示最后N天）"""
    if not snapshots:
        return "无净值数据"

    display = snapshots[-last_n:] if len(snapshots) > last_n else snapshots
    initial = snapshots[0].total_assets if snapshots else 1

    lines = []
    lines.append("-" * 55)
    lines.append(f"{'日期':^12} {'总资产':>14} {'日收益':>8} {'累计收益':>8} {'持仓数':>6}")
    lines.append("-" * 55)

    for i, snap in enumerate(display):
        prev_assets = display[i - 1].total_assets if i > 0 else snap.total_assets
        daily_ret = (snap.total_assets - prev_assets) / prev_assets if prev_assets > 0 else 0
        cum_ret = (snap.total_assets - initial) / initial if initial > 0 else 0
        lines.append(
            f"{snap.date:12s} {snap.total_assets:>14,.0f} "
            f"{daily_ret:>+7.2%} {cum_ret:>+7.2%} {snap.positions_count:>6d}"
        )

    lines.append("-" * 55)
    if len(snapshots) > last_n:
        lines.append(f"(显示最近 {last_n} 天，共 {len(snapshots)} 个交易日)")
    return "\n".join(lines)


def format_profit_loss_summary(trades: List[TradeRecord]) -> str:
    """盈亏统计汇总"""
    if not trades:
        return "无交易记录"

    # 配对买卖
    buy_map = {}
    results = []
    for t in trades:
        if t.action == "买入":
            buy_map[t.code] = t
        elif t.action == "卖出" and t.code in buy_map:
            buy_t = buy_map.pop(t.code)
            pnl = (t.price - buy_t.price) * t.shares
            pnl_pct = (t.price - buy_t.price) / buy_t.price
            results.append({
                "code": t.code,
                "name": t.name,
                "buy_price": buy_t.price,
                "sell_price": t.price,
                "shares": t.shares,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "buy_date": buy_t.date,
                "sell_date": t.date,
                "reason": t.reason,
            })

    if not results:
        return "无已平仓交易"

    # 按盈亏排序
    results.sort(key=lambda x: x["pnl"], reverse=True)

    lines = []
    lines.append("-" * 75)
    lines.append("📊 盈亏明细（按盈亏排序）")
    lines.append("-" * 75)
    lines.append(
        f"{'代码':^8} {'名称':^8} {'买入价':>8} {'卖出价':>8} "
        f"{'盈亏额':>12} {'盈亏比':>8} {'原因'}"
    )
    lines.append("-" * 75)

    total_pnl = 0
    for r in results:
        total_pnl += r["pnl"]
        emoji = "🟢" if r["pnl"] >= 0 else "🔴"
        lines.append(
            f"{emoji}{r['code']:>7s} {r['name']:8s} {r['buy_price']:>8.2f} "
            f"{r['sell_price']:>8.2f} {r['pnl']:>+12,.0f} {r['pnl_pct']:>+7.2%} {r['reason']}"
        )

    lines.append("-" * 75)
    total_pnl_pct = total_pnl / sum(r["buy_price"] * r["shares"] for r in results) if results else 0
    lines.append(f"合计盈亏: {total_pnl:>+,.0f}  加权收益率: {total_pnl_pct:>+.2%}")

    return "\n".join(lines)


def format_hit_rate(trades: List[TradeRecord]) -> str:
    """信号命中率统计"""
    if not trades:
        return "无交易记录"

    buy_map = {}
    reason_stats = defaultdict(lambda: {"win": 0, "lose": 0, "total_pnl": 0})

    for t in trades:
        if t.action == "买入":
            buy_map[t.code] = t
        elif t.action == "卖出" and t.code in buy_map:
            buy_t = buy_map.pop(t.code)
            pnl = (t.price - buy_t.price) / buy_t.price
            reason = t.reason.split("(")[0]  # 去掉百分比部分
            if pnl >= 0:
                reason_stats[reason]["win"] += 1
            else:
                reason_stats[reason]["lose"] += 1
            reason_stats[reason]["total_pnl"] += pnl

    if not reason_stats:
        return "无已平仓交易"

    lines = []
    lines.append("-" * 50)
    lines.append("🎯 信号命中率统计")
    lines.append("-" * 50)
    lines.append(f"{'卖出原因':<12} {'盈利':>6} {'亏损':>6} {'胜率':>8} {'平均盈亏':>10}")
    lines.append("-" * 50)

    for reason, stat in sorted(reason_stats.items()):
        total = stat["win"] + stat["lose"]
        wr = stat["win"] / total if total > 0 else 0
        avg_pnl = stat["total_pnl"] / total if total > 0 else 0
        lines.append(
            f"{reason:<12s} {stat['win']:>6d} {stat['lose']:>6d} "
            f"{wr:>7.1%} {avg_pnl:>+9.2%}"
        )

    lines.append("-" * 50)
    return "\n".join(lines)


def generate_full_report(result: BacktestResult, show_trades: bool = True) -> str:
    """生成完整回测报告"""
    parts = []

    parts.append(format_backtest_report(result))

    if show_trades and result.trades:
        parts.append("\n" + format_trade_list(result.trades))

    parts.append("\n" + format_profit_loss_summary(result.trades))
    parts.append("\n" + format_hit_rate(result.trades))

    if result.daily_snapshots:
        parts.append("\n" + format_daily_nav(result.daily_snapshots, last_n=10))

    return "\n".join(parts)
