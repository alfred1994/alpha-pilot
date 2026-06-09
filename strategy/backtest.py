"""
双风格策略回测框架
=================================================================
用Baostock历史数据回测科技成长+价值超跌策略
输出: 胜率、盈亏比、最大回撤、年化收益等
=================================================================
"""
import sys
import os
import json
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional

import numpy as np
import pandas as pd

# 项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logger = logging.getLogger("backtest")


@dataclass
class Trade:
    """交易记录"""
    code: str
    name: str
    style: str           # "growth" | "value"
    entry_date: str
    entry_price: float
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""  # "止损" | "止盈" | "持有到期" | "信号消失"
    holding_days: int = 0
    pnl_pct: float = 0.0
    signal_score: float = 0.0
    reasons: List[str] = field(default_factory=list)


@dataclass
class BacktestResult:
    """回测结果"""
    strategy_name: str
    start_date: str
    end_date: str
    total_trades: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_loss_ratio: float = 0.0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_holding_days: float = 0.0
    trades: List[Trade] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"\n{'='*60}\n"
            f"📊 {self.strategy_name} 回测结果\n"
            f"{'='*60}\n"
            f"期间: {self.start_date} ~ {self.end_date}\n"
            f"总交易: {self.total_trades}笔\n"
            f"胜率: {self.win_rate:.1%} ({self.win_trades}胜/{self.loss_trades}负)\n"
            f"平均盈利: {self.avg_win_pct:+.2f}%\n"
            f"平均亏损: {self.avg_loss_pct:+.2f}%\n"
            f"盈亏比: {self.profit_loss_ratio:.2f}\n"
            f"总收益: {self.total_return_pct:+.2f}%\n"
            f"最大回撤: {self.max_drawdown_pct:.2f}%\n"
            f"平均持仓: {self.avg_holding_days:.1f}天\n"
            f"{'='*60}"
        )


def fetch_stock_data(code: str, start_date: str, end_date: str, bs_session=None) -> Optional[pd.DataFrame]:
    """从Baostock获取历史K线数据"""
    import baostock as bs

    # 转换代码格式: 600519 -> sh.600519
    if '.' not in code:
        if code.startswith(('6', '5')):
            bs_code = f"sh.{code}"
        else:
            bs_code = f"sz.{code}"
    else:
        bs_code = code

    need_logout = False
    if bs_session is None:
        bs.login()
        need_logout = True

    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,code,open,high,low,close,volume,amount,turn,pctChg",
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="2"  # 前复权
    )

    rows = []
    while (rs.error_code == '0') and rs.next():
        rows.append(rs.get_row_data())

    if need_logout:
        bs.logout()

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=rs.fields)
    for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'turn', 'pctChg']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['close'])
    df = df[df['close'] > 0]
    df = df.reset_index(drop=True)
    return df


def backtest_strategy(
    pool: Dict[str, str],
    signal_func,
    style_name: str,
    start_date: str = "2026-01-01",
    end_date: str = "2026-06-06",
    lookback_days: int = 60,    # 信号计算需要的前置数据天数
    hold_days: int = 5,          # 最大持有天数
    stop_loss: float = -0.05,    # 止损线 -5%
    take_profit: float = 0.15,   # 止盈线 +15%
    min_score: float = 40,       # 最低信号分数
) -> BacktestResult:
    """
    回测指定策略

    逻辑:
    1. 每个交易日，对股票池中每只股票计算信号
    2. 满足条件则买入（信号日收盘价）
    3. 持有期间按止损/止盈/到期规则卖出
    """
    import baostock as bs

    # 为每只股票获取完整数据
    stock_data = {}
    extended_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")

    import baostock as bs
    bs.login()
    for code, name in pool.items():
        df = fetch_stock_data(code, extended_start, end_date, bs_session=True)
        if df is not None and len(df) >= 30:
            stock_data[code] = df
            logger.info(f"  加载 {code} {name}: {len(df)}条")
        else:
            logger.warning(f"  跳过 {code} {name}: 数据不足")
    bs.logout()

    logger.info(f"可用股票: {len(stock_data)}/{len(pool)}")

    # 生成交易日列表
    all_dates = set()
    for df in stock_data.values():
        all_dates.update(df['date'].tolist())
    trade_dates = sorted([d for d in all_dates if d >= start_date])

    trades = []
    open_positions = {}  # code -> Trade

    for date in trade_dates:
        # === 检查持仓止损/止盈 ===
        to_close = []
        for code, trade in open_positions.items():
            if code not in stock_data:
                continue
            df = stock_data[code]
            day_data = df[df['date'] == date]
            if day_data.empty:
                continue

            cur_close = float(day_data['close'].iloc[0])
            trade.holding_days += 1
            pnl = (cur_close - trade.entry_price) / trade.entry_price

            exit_reason = None
            if pnl <= stop_loss:
                exit_reason = "止损"
            elif pnl >= take_profit:
                exit_reason = "止盈"
            elif trade.holding_days >= hold_days:
                exit_reason = "持有到期"

            if exit_reason:
                trade.exit_date = date
                trade.exit_price = cur_close
                trade.exit_reason = exit_reason
                trade.pnl_pct = pnl * 100
                trades.append(trade)
                to_close.append(code)

        for code in to_close:
            del open_positions[code]

        # === 扫描新信号 ===
        for code, name in pool.items():
            if code in open_positions:
                continue  # 已持仓
            if code not in stock_data:
                continue

            df = stock_data[code]
            # 取到当前日期的数据
            idx = df[df['date'] == date].index
            if len(idx) == 0:
                continue
            cur_idx = idx[0]
            if cur_idx < 30:
                continue  # 数据不够

            sub_df = df.iloc[:cur_idx + 1].copy()

            try:
                signal = signal_func(sub_df, code, name)
            except Exception as e:
                continue

            if signal and signal.score >= min_score:
                entry_price = float(sub_df['close'].iloc[-1])
                trade = Trade(
                    code=code, name=name, style=style_name,
                    entry_date=date, entry_price=entry_price,
                    signal_score=signal.score, reasons=signal.reasons,
                )
                open_positions[code] = trade

    # 未平仓的按最后价格平仓
    for code, trade in open_positions.items():
        df = stock_data.get(code)
        if df is not None and len(df) > 0:
            last_close = float(df['close'].iloc[-1])
            trade.exit_date = df['date'].iloc[-1]
            trade.exit_price = last_close
            trade.exit_reason = "回测结束"
            trade.pnl_pct = (last_close - trade.entry_price) / trade.entry_price * 100
            trades.append(trade)

    # === 统计 ===
    result = BacktestResult(
        strategy_name=style_name,
        start_date=start_date,
        end_date=end_date,
        trades=trades,
    )

    if not trades:
        return result

    result.total_trades = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    result.win_trades = len(wins)
    result.loss_trades = len(losses)
    result.win_rate = len(wins) / len(trades) if trades else 0
    result.avg_win_pct = np.mean([t.pnl_pct for t in wins]) if wins else 0
    result.avg_loss_pct = np.mean([t.pnl_pct for t in losses]) if losses else 0
    result.profit_loss_ratio = abs(result.avg_win_pct / result.avg_loss_pct) if result.avg_loss_pct != 0 else 0
    result.avg_holding_days = np.mean([t.holding_days for t in trades])

    # 总收益（简化: 每笔等仓位）
    result.total_return_pct = np.mean([t.pnl_pct for t in trades])

    # 最大回撤（按交易序列计算）
    cumulative = np.cumsum([t.pnl_pct for t in trades])
    peak = np.maximum.accumulate(cumulative)
    drawdown = cumulative - peak
    result.max_drawdown_pct = abs(drawdown.min()) if len(drawdown) > 0 else 0

    return result


def print_top_trades(result: BacktestResult, top_n: int = 10):
    """打印最佳和最差交易"""
    if not result.trades:
        print("无交易记录")
        return

    sorted_trades = sorted(result.trades, key=lambda t: t.pnl_pct, reverse=True)

    print(f"\n🏆 最佳交易 TOP {min(top_n, len(sorted_trades))}:")
    for t in sorted_trades[:top_n]:
        print(f"  {t.code} {t.name:　<6} {t.entry_date}→{t.exit_date} "
              f"{t.pnl_pct:+.2f}% {t.exit_reason} 评分{t.signal_score:.0f} [{','.join(t.reasons[:3])}]")

    print(f"\n💀 最差交易 TOP {min(top_n, len(sorted_trades))}:")
    for t in sorted_trades[-top_n:]:
        print(f"  {t.code} {t.name:　<6} {t.entry_date}→{t.exit_date} "
              f"{t.pnl_pct:+.2f}% {t.exit_reason} 评分{t.signal_score:.0f} [{','.join(t.reasons[:3])}]")


def print_exit_analysis(result: BacktestResult):
    """按退出原因分析"""
    if not result.trades:
        return

    print(f"\n📋 退出原因分析:")
    reasons = {}
    for t in result.trades:
        r = t.exit_reason
        if r not in reasons:
            reasons[r] = {"count": 0, "pnl_list": []}
        reasons[r]["count"] += 1
        reasons[r]["pnl_list"].append(t.pnl_pct)

    for r, data in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
        pnl = data["pnl_list"]
        print(f"  {r}: {data['count']}笔  "
              f"平均{np.mean(pnl):+.2f}%  "
              f"胜率{len([p for p in pnl if p > 0])/len(pnl):.0%}")


if __name__ == "__main__":
    from strategy.dual_style import (
        calc_growth_signals, calc_value_signals,
        get_growth_pool, get_value_pool
    )

    print("=" * 60)
    print("双风格策略回测")
    print("=" * 60)

    # === 回测科技成长策略 ===
    print("\n🚀 科技成长策略回测中...")
    growth_result = backtest_strategy(
        pool=get_growth_pool(),
        signal_func=calc_growth_signals,
        style_name="科技成长(半导体/CPO/PCB)",
        start_date="2026-01-01",
        end_date="2026-06-06",
        hold_days=5,
        stop_loss=-0.05,
        take_profit=0.15,
        min_score=40,
    )
    print(growth_result.summary())
    print_top_trades(growth_result)
    print_exit_analysis(growth_result)

    # === 回测价值超跌策略 ===
    print("\n💎 价值超跌策略回测中...")
    value_result = backtest_strategy(
        pool=get_value_pool(),
        signal_func=calc_value_signals,
        style_name="价值超跌(低估值+超跌)",
        start_date="2026-01-01",
        end_date="2026-06-06",
        hold_days=5,
        stop_loss=-0.05,
        take_profit=0.15,
        min_score=35,
    )
    print(value_result.summary())
    print_top_trades(value_result)
    print_exit_analysis(value_result)

    # === 保存结果 ===
    output = {
        "growth": {
            "summary": {
                "total_trades": growth_result.total_trades,
                "win_rate": round(growth_result.win_rate, 4),
                "avg_return": round(growth_result.total_return_pct, 2),
                "max_drawdown": round(growth_result.max_drawdown_pct, 2),
                "profit_loss_ratio": round(growth_result.profit_loss_ratio, 2),
            },
            "trades": [
                {"code": t.code, "name": t.name, "entry": t.entry_date, "exit": t.exit_date,
                 "pnl": round(t.pnl_pct, 2), "reason": t.exit_reason, "score": t.signal_score,
                 "signals": t.reasons}
                for t in growth_result.trades
            ]
        },
        "value": {
            "summary": {
                "total_trades": value_result.total_trades,
                "win_rate": round(value_result.win_rate, 4),
                "avg_return": round(value_result.total_return_pct, 2),
                "max_drawdown": round(value_result.max_drawdown_pct, 2),
                "profit_loss_ratio": round(value_result.profit_loss_ratio, 2),
            },
            "trades": [
                {"code": t.code, "name": t.name, "entry": t.entry_date, "exit": t.exit_date,
                 "pnl": round(t.pnl_pct, 2), "reason": t.exit_reason, "score": t.signal_score,
                 "signals": t.reasons}
                for t in value_result.trades
            ]
        }
    }

    output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "backtest_result.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n💾 结果已保存: {output_path}")
