"""
快速回测模块 - VectorBT 参数扫描优化
基于 vectorbt 向量化回测引擎，支持参数网格搜索，比事件驱动回测快 100x+

用法:
    # 单次回测验证
    result = run_single_backtest("600519", "2023-01-01", "2024-12-31")

    # 参数网格搜索
    results = run_parameter_sweep("600519", "2023-01-01", "2024-12-31", top_n=10)
"""
import logging
import itertools
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("portfolio.fast_backtest")


# ══════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class SingleBacktestResult:
    """单次回测结果"""
    code: str
    start_date: str
    end_date: str
    # 策略参数
    short_ma: int
    long_ma: int
    rsi_period: int
    rsi_oversold: float
    rsi_overbought: float
    stop_loss: float
    take_profit: float
    # 回测指标
    total_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    final_value: float = 0.0


@dataclass
class SweepResult:
    """参数扫描结果"""
    code: str
    start_date: str
    end_date: str
    total_combinations: int
    results: List[SingleBacktestResult] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# 技术指标计算
# ══════════════════════════════════════════════════════════════════

def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """计算 RSI 指标"""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _apply_stop_loss_take_profit(
    close: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    stop_loss: float,
    take_profit: float,
) -> Tuple[pd.Series, pd.Series]:
    """
    在已有的入场/出场信号上叠加止损止盈逻辑。
    返回修正后的 (entries, exits)。

    逻辑: 在持仓期间，如果累计收益触及止损/止盈线，提前触发卖出。
    """
    if stop_loss >= 0 and take_profit <= 0:
        return entries, exits  # 未启用止损止盈

    prices = close.values
    n = len(prices)
    new_exits = exits.copy()
    in_position = False
    entry_price = 0.0

    for i in range(n):
        if entries.iloc[i] and not in_position:
            in_position = True
            entry_price = prices[i]
            continue

        if in_position:
            pct = (prices[i] - entry_price) / entry_price if entry_price > 0 else 0.0
            # 止损
            if stop_loss < 0 and pct <= stop_loss:
                new_exits.iloc[i] = True
                in_position = False
            # 止盈
            elif take_profit > 0 and pct >= take_profit:
                new_exits.iloc[i] = True
                in_position = False
            # 原始出场信号
            elif exits.iloc[i]:
                in_position = False

    return entries, new_exits


# ══════════════════════════════════════════════════════════════════
# 数据加载
# ══════════════════════════════════════════════════════════════════

def _load_close_series(
    code: str,
    start_date: str,
    end_date: str,
) -> Optional[pd.Series]:
    """
    从 data.history.get_daily 加载收盘价，返回 DatetimeIndex 的 Series。
    """
    try:
        from data.history import get_daily
    except ImportError:
        logger.error("无法导入 data.history，确认项目路径正确")
        return None

    df = get_daily(code, start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        logger.warning(f"获取 {code} 数据为空")
        return None

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    if len(df) < 60:
        logger.warning(f"{code} 数据不足 60 条 ({len(df)}条)，跳过")
        return None

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df["close"]


# ══════════════════════════════════════════════════════════════════
# 单次回测
# ══════════════════════════════════════════════════════════════════

def run_single_backtest(
    code: str,
    start_date: str,
    end_date: str,
    short_ma: int = 5,
    long_ma: int = 20,
    rsi_period: int = 14,
    rsi_oversold: float = 30.0,
    rsi_overbought: float = 70.0,
    stop_loss: float = -0.08,
    take_profit: float = 0.10,
    initial_capital: float = 1_000_000,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
) -> Optional[SingleBacktestResult]:
    """
    单次快速回测 - MA 交叉 + RSI 过滤策略

    策略逻辑:
        买入: 短期MA上穿长期MA 且 RSI < rsi_oversold (超卖区)
        卖出: 短期MA下穿长期MA 或 RSI > rsi_overbought (超买区) 或 触发止损/止盈

    Args:
        code: 股票代码 (如 "600519")
        start_date: 开始日期 "YYYY-MM-DD"
        end_date: 结束日期
        short_ma: 短期均线周期
        long_ma: 长期均线周期
        rsi_period: RSI 计算周期
        rsi_oversold: RSI 超卖阈值 (低于此值考虑买入)
        rsi_overbought: RSI 超买阈值 (高于此值考虑卖出)
        stop_loss: 止损比例 (负数, 如 -0.08 = -8%)
        take_profit: 止盈比例 (正数, 如 0.10 = +10%)
        initial_capital: 初始资金
        commission_rate: 佣金费率 (A股万三 = 0.0003)
        stamp_tax_rate: 印花税率 (A股千一 = 0.001, 卖出收取)

    Returns:
        SingleBacktestResult 或 None
    """
    try:
        import vectorbt as vbt
    except ImportError:
        logger.error("vectorbt 未安装，请执行: pip install vectorbt")
        return None

    close = _load_close_series(code, start_date, end_date)
    if close is None:
        return None

    # 计算技术指标
    fast_ma = vbt.MA.run(close, short_ma)
    slow_ma = vbt.MA.run(close, long_ma)
    rsi = _calc_rsi(close, rsi_period)

    # ── 生成入场/出场信号 ──
    # 买入: 短均线上穿长均线 且 RSI 处于超卖区
    ma_cross_up = fast_ma.ma_crossed_above(slow_ma)
    ma_cross_down = fast_ma.ma_crossed_below(slow_ma)
    rsi_low = rsi < rsi_oversold
    rsi_high = rsi > rsi_overbought

    entries = ma_cross_up & rsi_low
    exits = ma_cross_down | rsi_high

    # 叠加止损止盈
    entries, exits = _apply_stop_loss_take_profit(close, entries, exits, stop_loss, take_profit)

    # 手续费: 买入佣金 + 卖出佣金 + 卖出印花税
    # vectorbt 的 fees 是单次交易费用占交易额的比例
    total_fee = commission_rate * 2 + stamp_tax_rate  # 买卖佣金 + 卖出印花税

    # ── 执行回测 ──
    pf = vbt.Portfolio.from_signals(
        close=close,
        entries=entries,
        exits=exits,
        init_cash=initial_capital,
        fees=total_fee,
        freq="1D",
    )

    # ── 提取指标 ──
    total_return = float(pf.total_return())
    sharpe = float(pf.sharpe_ratio()) if not np.isnan(pf.sharpe_ratio()) else 0.0
    max_dd = float(pf.max_drawdown())
    trades = pf.trades if hasattr(pf, "trades") else None
    win_rate = 0.0
    total_trades = 0
    if trades is not None:
        try:
            total_trades = int(trades.count())
            wins = int(trades.win_count())
            win_rate = wins / total_trades if total_trades > 0 else 0.0
        except Exception:
            pass

    final_value = float(pf.final_value())

    result = SingleBacktestResult(
        code=code,
        start_date=start_date,
        end_date=end_date,
        short_ma=short_ma,
        long_ma=long_ma,
        rsi_period=rsi_period,
        rsi_oversold=rsi_oversold,
        rsi_overbought=rsi_overbought,
        stop_loss=stop_loss,
        take_profit=take_profit,
        total_return=total_return,
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        win_rate=win_rate,
        total_trades=total_trades,
        final_value=final_value,
    )

    logger.info(
        f"回测完成 {code} MA({short_ma}/{long_ma}) RSI({rsi_period}) "
        f"收益={total_return:.2%} 夏普={sharpe:.2f} 回撤={max_dd:.2%} 胜率={win_rate:.2%}"
    )
    return result


# ══════════════════════════════════════════════════════════════════
# 参数网格搜索
# ══════════════════════════════════════════════════════════════════

# 默认参数搜索范围
DEFAULT_PARAM_GRID = {
    "short_ma": [3, 5, 10],
    "long_ma": [10, 20, 30, 60],
    "rsi_period": [6, 14],
    "rsi_oversold": [25, 30, 35],
    "rsi_overbought": [65, 70, 75],
    "stop_loss": [-0.05, -0.08, -0.10],
    "take_profit": [0.08, 0.10, 0.15],
}


def run_parameter_sweep(
    code: str,
    start_date: str,
    end_date: str,
    param_grid: Optional[Dict[str, List]] = None,
    top_n: int = 10,
    sort_by: str = "sharpe_ratio",
    initial_capital: float = 1_000_000,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
) -> SweepResult:
    """
    参数网格搜索 - 遍历参数组合，返回 Top N 最优策略

    Args:
        code: 股票代码
        start_date: 开始日期
        end_date: 结束日期
        param_grid: 参数搜索空间，默认使用 DEFAULT_PARAM_GRID
                    格式: {"short_ma": [3,5,10], "long_ma": [10,20,30], ...}
        top_n: 返回排名前 N 的参数组合
        sort_by: 排序字段，可选 "sharpe_ratio", "total_return", "max_drawdown", "win_rate"
        initial_capital: 初始资金
        commission_rate: 佣金费率
        stamp_tax_rate: 印花税率

    Returns:
        SweepResult
    """
    try:
        import vectorbt as vbt
    except ImportError:
        logger.error("vectorbt 未安装，请执行: pip install vectorbt")
        return SweepResult(code=code, start_date=start_date, end_date=end_date, total_combinations=0)

    if param_grid is None:
        param_grid = DEFAULT_PARAM_GRID

    # 预加载数据（只加载一次）
    close = _load_close_series(code, start_date, end_date)
    if close is None:
        return SweepResult(code=code, start_date=start_date, end_date=end_date, total_combinations=0)

    # 生成所有参数组合
    param_keys = list(param_grid.keys())
    param_values = list(param_grid.values())
    combinations = list(itertools.product(*param_values))
    total = len(combinations)

    # 过滤无效组合: short_ma 必须 < long_ma
    valid_combos = []
    for combo in combinations:
        params = dict(zip(param_keys, combo))
        if params.get("short_ma", 0) >= params.get("long_ma", 999):
            continue
        valid_combos.append(params)

    logger.info(f"参数扫描: {code} {start_date}~{end_date}，共 {len(valid_combos)}/{total} 个有效组合")

    # 手续费
    total_fee = commission_rate * 2 + stamp_tax_rate

    results: List[SingleBacktestResult] = []
    for idx, params in enumerate(valid_combos):
        try:
            fast_ma = vbt.MA.run(close, params["short_ma"])
            slow_ma = vbt.MA.run(close, params["long_ma"])
            rsi = _calc_rsi(close, params["rsi_period"])

            ma_cross_up = fast_ma.ma_crossed_above(slow_ma)
            ma_cross_down = fast_ma.ma_crossed_below(slow_ma)
            rsi_low = rsi < params["rsi_oversold"]
            rsi_high = rsi > params["rsi_overbought"]

            entries = ma_cross_up & rsi_low
            exits = ma_cross_down | rsi_high

            # 止损止盈
            entries, exits = _apply_stop_loss_take_profit(
                close, entries, exits, params["stop_loss"], params["take_profit"]
            )

            pf = vbt.Portfolio.from_signals(
                close=close,
                entries=entries,
                exits=exits,
                init_cash=initial_capital,
                fees=total_fee,
                freq="1D",
            )

            total_return = float(pf.total_return())
            sharpe = float(pf.sharpe_ratio()) if not np.isnan(pf.sharpe_ratio()) else 0.0
            max_dd = float(pf.max_drawdown())

            trades = pf.trades if hasattr(pf, "trades") else None
            win_rate = 0.0
            total_trades = 0
            if trades is not None:
                try:
                    total_trades = int(trades.count())
                    wins = int(trades.win_count())
                    win_rate = wins / total_trades if total_trades > 0 else 0.0
                except Exception:
                    pass

            result = SingleBacktestResult(
                code=code,
                start_date=start_date,
                end_date=end_date,
                short_ma=params["short_ma"],
                long_ma=params["long_ma"],
                rsi_period=params["rsi_period"],
                rsi_oversold=params["rsi_oversold"],
                rsi_overbought=params["rsi_overbought"],
                stop_loss=params["stop_loss"],
                take_profit=params["take_profit"],
                total_return=total_return,
                sharpe_ratio=sharpe,
                max_drawdown=max_dd,
                win_rate=win_rate,
                total_trades=total_trades,
                final_value=float(pf.final_value()),
            )
            results.append(result)

        except Exception as e:
            logger.debug(f"组合 {params} 回测失败: {e}")
            continue

        # 进度日志（每 100 个组合输出一次）
        if (idx + 1) % 100 == 0:
            logger.info(f"  进度: {idx + 1}/{len(valid_combos)}")

    if not results:
        logger.warning("所有参数组合回测均失败")
        return SweepResult(code=code, start_date=start_date, end_date=end_date, total_combinations=0)

    # 排序
    reverse = sort_by != "max_drawdown"  # 回撤越小越好
    if sort_by == "max_drawdown":
        results.sort(key=lambda r: getattr(r, sort_by), reverse=False)
    else:
        results.sort(key=lambda r: getattr(r, sort_by), reverse=True)

    top_results = results[:top_n]

    sweep = SweepResult(
        code=code,
        start_date=start_date,
        end_date=end_date,
        total_combinations=len(valid_combos),
        results=top_results,
    )

    # 输出 Top N 摘要
    logger.info(f"参数扫描完成，成功 {len(results)} 个，Top {top_n} 按 {sort_by} 排序:")
    for i, r in enumerate(top_results, 1):
        logger.info(
            f"  #{i} MA({r.short_ma}/{r.long_ma}) RSI({r.rsi_period}) "
            f"SL={r.stop_loss:.0%} TP={r.take_profit:.0%} | "
            f"收益={r.total_return:.2%} 夏普={r.sharpe_ratio:.2f} "
            f"回撤={r.max_drawdown:.2%} 胜率={r.win_rate:.2%} 交易={r.total_trades}笔"
        )

    return sweep


def sweep_results_to_df(sweep: SweepResult) -> pd.DataFrame:
    """将扫描结果转为 DataFrame，方便后续分析"""
    if not sweep.results:
        return pd.DataFrame()
    records = []
    for r in sweep.results:
        records.append({
            "code": r.code,
            "short_ma": r.short_ma,
            "long_ma": r.long_ma,
            "rsi_period": r.rsi_period,
            "rsi_oversold": r.rsi_oversold,
            "rsi_overbought": r.rsi_overbought,
            "stop_loss": r.stop_loss,
            "take_profit": r.take_profit,
            "total_return": r.total_return,
            "sharpe_ratio": r.sharpe_ratio,
            "max_drawdown": r.max_drawdown,
            "win_rate": r.win_rate,
            "total_trades": r.total_trades,
            "final_value": r.final_value,
        })
    return pd.DataFrame(records)


# ══════════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # 单次回测示例: 茅台
    print("=" * 60)
    print("单次回测: 贵州茅台 MA(5,20) + RSI(14)")
    print("=" * 60)
    r = run_single_backtest("600519", "2023-01-01", "2024-12-31")
    if r:
        print(f"  总收益: {r.total_return:.2%}")
        print(f"  夏普比: {r.sharpe_ratio:.2f}")
        print(f"  最大回撤: {r.max_drawdown:.2%}")
        print(f"  胜率: {r.win_rate:.2%}")
        print(f"  交易笔数: {r.total_trades}")
        print(f"  最终净值: {r.final_value:,.0f}")

    # 参数扫描示例（缩小范围，快速演示）
    print("\n" + "=" * 60)
    print("参数扫描: 贵州茅台 Top 5")
    print("=" * 60)
    small_grid = {
        "short_ma": [3, 5, 10],
        "long_ma": [20, 30],
        "rsi_period": [14],
        "rsi_oversold": [30],
        "rsi_overbought": [70],
        "stop_loss": [-0.08],
        "take_profit": [0.10],
    }
    sweep = run_parameter_sweep("600519", "2023-01-01", "2024-12-31", param_grid=small_grid, top_n=5)
    df = sweep_results_to_df(sweep)
    if not df.empty:
        print(df.to_string(index=False))
