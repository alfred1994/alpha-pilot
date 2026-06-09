"""
回测引擎
历史验证策略有效性，计算收益率/夏普比率/最大回撤
"""
import logging
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

from portfolio.position import PositionManager, TradeRecord
from signals.composite import CompositeSignal
from signals import Direction

logger = logging.getLogger("portfolio.backtest")


@dataclass
class DailySnapshot:
    """每日资产快照"""
    date: str
    cash: float
    market_value: float
    total_assets: float
    positions_count: int
    benchmark_value: float = 0.0


@dataclass
class BacktestResult:
    """回测结果"""
    start_date: str
    end_date: str
    initial_capital: float
    final_assets: float

    total_return: float          # 总收益率
    annualized_return: float     # 年化收益率
    sharpe_ratio: float          # 夏普比率
    max_drawdown: float          # 最大回撤
    max_drawdown_start: str = ""
    max_drawdown_end: str = ""
    win_rate: float = 0.0        # 胜率
    total_trades: int = 0
    win_trades: int = 0
    lose_trades: int = 0
    avg_hold_days: float = 0.0

    trades: List[TradeRecord] = field(default_factory=list)
    daily_snapshots: List[DailySnapshot] = field(default_factory=list)

    benchmark_return: float = 0.0    # 基准收益率
    excess_return: float = 0.0       # 超额收益


class BacktestEngine:
    """
    回测引擎

    使用方法:
        engine = BacktestEngine(initial_capital=1_000_000)
        result = engine.run(
            signals_func=my_signals_func,
            price_data=price_data,
            start_date="2024-01-01",
            end_date="2024-12-31",
        )
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        max_stocks: int = 5,
        max_single_pct: float = 0.20,
        stop_loss: float = -0.05,
        take_profit: float = 0.10,
        commission_rate: float = 0.001,  # 手续费率 0.1%
        stamp_tax_rate: float = 0.001,   # 印花税 0.1%（卖出时）
    ):
        self.initial_capital = initial_capital
        self.max_stocks = max_stocks
        self.max_single_pct = max_single_pct
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate

    def run(
        self,
        signals_func: Callable[[str], List[CompositeSignal]],
        price_data: pd.DataFrame,
        start_date: str,
        end_date: str,
        benchmark_code: str = "000300",
        signal_interval: int = 5,
    ) -> BacktestResult:
        """
        运行回测

        Args:
            signals_func: 信号生成函数，接收日期字符串，返回 CompositeSignal 列表
            price_data: 行情数据 DataFrame，需包含 date, code, close 列
                        多只股票纵向拼接，用 code 列区分
            start_date: 回测开始日期 "YYYY-MM-DD"
            end_date: 回测结束日期
            benchmark_code: 基准指数代码（用于对比）
            signal_interval: 每隔几个交易日调仓一次

        Returns:
            BacktestResult
        """
        pm = PositionManager(
            initial_capital=self.initial_capital,
            max_stocks=self.max_stocks,
            max_single_pct=self.max_single_pct,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
        )

        # 预处理行情数据
        price_data = price_data.copy()
        price_data["date"] = pd.to_datetime(price_data["date"]).dt.strftime("%Y-%m-%d")

        # 获取所有交易日
        all_dates = sorted(price_data["date"].unique())
        trade_dates = [d for d in all_dates if start_date <= d <= end_date]

        if not trade_dates:
            raise ValueError(f"在 {start_date} ~ {end_date} 期间无交易日数据")

        # 构建每只股票每日收盘价映射
        price_map: Dict[str, Dict[str, float]] = {}
        for _, row in price_data.iterrows():
            code = row["code"]
            date = row["date"]
            close = row["close"]
            if code not in price_map:
                price_map[code] = {}
            price_map[code][date] = close

        # 基准数据（如有）
        benchmark_prices = price_map.get(benchmark_code, {})

        snapshots: List[DailySnapshot] = []
        day_count = 0

        for date in trade_dates:
            # 当日所有股票价格
            current_prices = {}
            for code, dates in price_map.items():
                if date in dates:
                    current_prices[code] = dates[date]

            # 检查止损止盈
            pm.check_stop_loss_take_profit(current_prices, date)

            # 定期调仓
            if day_count % signal_interval == 0:
                try:
                    signals = signals_func(date)
                    pm.rebalance(signals, current_prices, date)
                except Exception as e:
                    pass  # 信号生成失败时跳过

            # 记录快照
            mv = pm.market_value(current_prices)
            total = pm.cash + mv

            # 基准值
            bm_value = 0.0
            if benchmark_prices:
                bm_price = benchmark_prices.get(date)
                if bm_price and trade_dates:
                    first_bm = benchmark_prices.get(trade_dates[0], 1)
                    bm_value = self.initial_capital * (bm_price / first_bm) if first_bm else 0

            snapshots.append(DailySnapshot(
                date=date,
                cash=pm.cash,
                market_value=mv,
                total_assets=total,
                positions_count=len(pm.positions),
                benchmark_value=bm_value,
            ))
            day_count += 1

        # 回测结束，强制平仓
        final_prices = {}
        last_date = trade_dates[-1]
        for code, dates in price_map.items():
            if last_date in dates:
                final_prices[code] = dates[last_date]
        for code in list(pm.positions.keys()):
            price = final_prices.get(code, pm.positions[code].buy_price)
            pm.sell(code, price, last_date, reason="回测结束平仓")

        final_assets = pm.cash

        return self._calculate_metrics(
            snapshots=snapshots,
            trades=pm.trades,
            initial_capital=self.initial_capital,
            final_assets=final_assets,
            start_date=start_date,
            end_date=end_date,
        )

    def _calculate_metrics(
        self,
        snapshots: List[DailySnapshot],
        trades: List[TradeRecord],
        initial_capital: float,
        final_assets: float,
        start_date: str,
        end_date: str,
    ) -> BacktestResult:
        """计算回测指标"""
        # 总收益率
        total_return = (final_assets - initial_capital) / initial_capital

        # 年化收益率
        if snapshots:
            days = len(snapshots)
            years = days / 252  # A股约252个交易日
            if years > 0 and total_return > -1:
                annualized_return = (1 + total_return) ** (1 / years) - 1
            else:
                annualized_return = total_return
        else:
            annualized_return = 0.0
            days = 0

        # 日收益率序列
        daily_returns = []
        for i in range(1, len(snapshots)):
            prev = snapshots[i - 1].total_assets
            curr = snapshots[i].total_assets
            if prev > 0:
                daily_returns.append((curr - prev) / prev)

        # 夏普比率（无风险利率按年化2%计算）
        risk_free_daily = 0.02 / 252
        if daily_returns and len(daily_returns) > 1:
            excess = np.array(daily_returns) - risk_free_daily
            std = np.std(excess, ddof=1)
            sharpe_ratio = (np.mean(excess) / std * np.sqrt(252)) if std > 0 else 0.0
        else:
            sharpe_ratio = 0.0

        # 最大回撤
        max_dd = 0.0
        dd_start = ""
        dd_end = ""
        peak = 0.0
        peak_date = ""
        for snap in snapshots:
            if snap.total_assets > peak:
                peak = snap.total_assets
                peak_date = snap.date
            dd = (peak - snap.total_assets) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                dd_start = peak_date
                dd_end = snap.date

        # 胜率
        sell_trades = [t for t in trades if t.action == "卖出" and t.reason != "回测结束平仓"]
        win_trades = 0
        lose_trades = 0
        hold_days_list = []

        # 配对买卖计算胜率
        buy_map = {}
        for t in trades:
            if t.action == "买入":
                buy_map[t.code] = t
            elif t.action == "卖出" and t.code in buy_map:
                buy_t = buy_map.pop(t.code)
                if t.price > buy_t.price:
                    win_trades += 1
                else:
                    lose_trades += 1
                # 持仓天数
                try:
                    bd = datetime.strptime(buy_t.date, "%Y-%m-%d")
                    sd = datetime.strptime(t.date, "%Y-%m-%d")
                    hold_days_list.append((sd - bd).days)
                except ValueError:
                    pass

        total_closed = win_trades + lose_trades
        win_rate = win_trades / total_closed if total_closed > 0 else 0.0
        avg_hold = np.mean(hold_days_list) if hold_days_list else 0.0

        # 基准收益
        benchmark_return = 0.0
        if snapshots and snapshots[0].benchmark_value > 0:
            benchmark_return = (snapshots[-1].benchmark_value - initial_capital) / initial_capital

        return BacktestResult(
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            final_assets=final_assets,
            total_return=total_return,
            annualized_return=annualized_return,
            sharpe_ratio=sharpe_ratio,
            max_drawdown=max_dd,
            max_drawdown_start=dd_start,
            max_drawdown_end=dd_end,
            win_rate=win_rate,
            total_trades=total_closed,
            win_trades=win_trades,
            lose_trades=lose_trades,
            avg_hold_days=avg_hold,
            trades=trades,
            daily_snapshots=snapshots,
            benchmark_return=benchmark_return,
            excess_return=total_return - benchmark_return,
        )


class SimpleBacktestEngine:
    """
    简化回测引擎 - 直接从SQLite/长桥获取数据，复用现有决策系统

    与原有 BacktestEngine 的区别:
    - 不需要预先准备 price_data DataFrame，自动从数据源获取
    - 内置止损止盈（含ATR动态止损）
    - 支持 weighted（加权打分）、llm（LLM决策）和 strategy（策略模式）三种模式
    - 自动获取基准（沪深300）数据做对比

    使用方法:
        engine = SimpleBacktestEngine(initial_capital=1_000_000)
        result = engine.run(
            stock_codes=["600519", "000001"],
            start_date="2024-01-01",
            end_date="2024-12-31",
            decision_mode="weighted",
        )

        # 策略模式
        from strategy.strategies import get_strategy
        strategy = get_strategy("zt_reversal")
        engine = SimpleBacktestEngine(initial_capital=1_000_000, strategy=strategy)
        result = engine.run(
            stock_codes=["600519", "000001"],
            start_date="2024-01-01",
            end_date="2024-12-31",
            decision_mode="strategy",
        )
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        max_stocks: int = 5,
        stop_loss: float = -0.05,
        take_profit: float = 0.10,
        atr_multiplier: float = 2.0,
        use_atr: bool = True,
        signal_interval: int = 5,
        commission_rate: float = 0.0003,   # 佣金万三
        stamp_tax_rate: float = 0.001,     # 印花税千一
        strategy=None,                      # 策略实例（用于 strategy 模式）
    ):
        self.initial_capital = initial_capital
        self.max_stocks = max_stocks
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.atr_multiplier = atr_multiplier
        self.use_atr = use_atr
        self.signal_interval = signal_interval
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.strategy = strategy

    def run(
        self,
        stock_codes: List[str],
        start_date: str,
        end_date: str,
        decision_mode: str = "weighted",
        benchmark_code: str = "000300",
    ) -> BacktestResult:
        """
        运行回测

        Args:
            stock_codes: 要回测的股票代码列表（如 ['600519', '000001']）
            start_date: "2024-01-01"
            end_date: "2024-12-31"
            decision_mode: "weighted"（加权打分，快）或 "llm"（LLM决策，慢）
            benchmark_code: 基准指数代码，默认沪深300

        Returns:
            BacktestResult
        """
        from data.history import get_daily
        from data.atr import calc_atr
        from risk.stop_loss import StopLossManager

        logger.info(f"回测启动: {stock_codes} {start_date}~{end_date} 模式={decision_mode}")

        # ── 1. 获取所有股票的历史K线 ──
        stock_data: Dict[str, pd.DataFrame] = {}
        for code in stock_codes:
            try:
                df = get_daily(code, start_date=start_date, end_date=end_date)
                if df is not None and len(df) > 30:
                    # 统一列名为小写
                    df.columns = [c.lower() for c in df.columns]
                    # 确保数值类型
                    for col in ["open", "high", "low", "close", "volume"]:
                        if col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors="coerce")
                    df = df.dropna(subset=["close"])
                    stock_data[code] = df
                    logger.info(f"  {code}: {len(df)}条K线")
                else:
                    logger.warning(f"  {code}: 数据不足，跳过")
            except Exception as e:
                logger.warning(f"  {code}: 获取数据失败 - {e}")

        if not stock_data:
            raise ValueError("所有股票数据获取失败，无法回测")

        # ── 2. 获取基准数据 ──
        benchmark_df = None
        try:
            bm_code = benchmark_code.replace(".SH", "").replace(".SZ", "")
            benchmark_df = get_daily(bm_code, start_date=start_date, end_date=end_date)
            if benchmark_df is not None:
                benchmark_df.columns = [c.lower() for c in benchmark_df.columns]
                benchmark_df["close"] = pd.to_numeric(benchmark_df["close"], errors="coerce")
                logger.info(f"  基准{benchmark_code}: {len(benchmark_df)}条K线")
        except Exception as e:
            logger.warning(f"  基准数据获取失败: {e}")

        # ── 3. 构建交易日历（取所有股票的交集日期） ──
        date_sets = [set(df["date"].astype(str).tolist()) for df in stock_data.values()]
        all_dates = sorted(set.intersection(*date_sets))
        trade_dates = [d for d in all_dates if start_date <= d <= end_date]

        if not trade_dates:
            raise ValueError(f"在 {start_date} ~ {end_date} 期间无公共交易日")

        logger.info(f"  公共交易日: {len(trade_dates)}天")

        # 构建价格映射: {code: {date: {open, high, low, close}}}
        price_map: Dict[str, Dict[str, dict]] = {}
        for code, df in stock_data.items():
            price_map[code] = {}
            for _, row in df.iterrows():
                d = str(row["date"])
                price_map[code][d] = {
                    "open": float(row.get("open", row["close"])),
                    "high": float(row.get("high", row["close"])),
                    "low": float(row.get("low", row["close"])),
                    "close": float(row["close"]),
                }

        # 基准价格映射
        bm_price_map: Dict[str, float] = {}
        if benchmark_df is not None:
            for _, row in benchmark_df.iterrows():
                bm_price_map[str(row["date"])] = float(row["close"])

        # ── 4. 初始化管理器 ──
        pm = PositionManager(
            initial_capital=self.initial_capital,
            max_stocks=self.max_stocks,
            max_single_pct=0.20,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
        )
        slm = StopLossManager(
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            atr_multiplier=self.atr_multiplier,
            use_atr=self.use_atr,
        )

        # ── 5. 遍历每个交易日 ──
        snapshots: List[DailySnapshot] = []
        day_count = 0

        # 为每只股票缓存近期K线（用于ATR和技术信号）
        df_cache: Dict[str, pd.DataFrame] = {}
        for code, df in stock_data.items():
            df_cache[code] = df.copy()

        for date in trade_dates:
            # 当日所有股票价格
            current_prices: Dict[str, float] = {}
            for code in stock_data:
                if date in price_map[code]:
                    current_prices[code] = price_map[code][date]["close"]

            # ── 5a. 更新持仓最高价 ──
            for code, pos in pm.positions.items():
                price = current_prices.get(code)
                if price and price > pos.highest_price:
                    pos.highest_price = price

            # ── 5b. 检查止损止盈（含ATR） ──
            for code in list(pm.positions.keys()):
                pos = pm.positions[code]
                price = current_prices.get(code)
                if price is None:
                    continue

                # 计算ATR
                atr_val = 0.0
                if self.use_atr:
                    hist_df = df_cache.get(code)
                    if hist_df is not None:
                        hist_up_to = hist_df[hist_df["date"].astype(str) <= date]
                        if len(hist_up_to) >= 15:
                            atr_val = calc_atr(hist_up_to)

                signal = slm.check_stop(
                    code=code,
                    name=pos.name,
                    buy_price=pos.buy_price,
                    current_price=price,
                    highest_price=pos.highest_price,
                    atr=atr_val if atr_val > 0 else None,
                )
                if signal:
                    # 手续费: 佣金 + 印花税
                    sell_amount = pos.shares * price
                    commission = sell_amount * self.commission_rate
                    stamp_tax = sell_amount * self.stamp_tax_rate
                    net_amount = sell_amount - commission - stamp_tax

                    pm.sell(code, price, date, reason=signal.reason)
                    logger.debug(f"  {date} 卖出 {code} @ {price:.2f} {signal.reason}")

            # ── 5c. 定期选股+决策 ──
            if day_count % self.signal_interval == 0:
                self._run_decision(
                    pm=pm,
                    stock_codes=list(stock_data.keys()),
                    current_prices=current_prices,
                    df_cache=df_cache,
                    date=date,
                    decision_mode=decision_mode,
                )

            # ── 5d. 记录每日快照 ──
            mv = pm.market_value(current_prices)
            total = pm.cash + mv

            # 基准值
            bm_value = 0.0
            if bm_price_map and trade_dates:
                first_bm = bm_price_map.get(trade_dates[0], 0)
                curr_bm = bm_price_map.get(date, 0)
                if first_bm > 0 and curr_bm > 0:
                    bm_value = self.initial_capital * (curr_bm / first_bm)

            snapshots.append(DailySnapshot(
                date=date,
                cash=pm.cash,
                market_value=mv,
                total_assets=total,
                positions_count=len(pm.positions),
                benchmark_value=bm_value,
            ))
            day_count += 1

        # ── 6. 回测结束，强制平仓 ──
        last_date = trade_dates[-1]
        for code in list(pm.positions.keys()):
            price = current_prices.get(code, pm.positions[code].buy_price)
            pm.sell(code, price, last_date, reason="回测结束平仓")

        final_assets = pm.cash

        # ── 7. 计算回测指标 ──
        result = self._calculate_metrics(
            snapshots=snapshots,
            trades=pm.trades,
            initial_capital=self.initial_capital,
            final_assets=final_assets,
            start_date=start_date,
            end_date=end_date,
        )

        logger.info(
            f"回测完成: 总收益={result.total_return:+.2%} "
            f"年化={result.annualized_return:+.2%} "
            f"夏普={result.sharpe_ratio:.2f} "
            f"最大回撤={result.max_drawdown:.2%} "
            f"胜率={result.win_rate:.0%}"
        )

        return result

    def _run_decision(
        self,
        pm: PositionManager,
        stock_codes: List[str],
        current_prices: Dict[str, float],
        df_cache: Dict[str, pd.DataFrame],
        date: str,
        decision_mode: str,
    ):
        """
        运行选股+决策逻辑

        Args:
            pm: 持仓管理器
            stock_codes: 候选股票代码列表
            current_prices: 当日价格 {code: close}
            df_cache: K线缓存 {code: DataFrame}
            date: 当前日期
            decision_mode: "weighted"、"llm" 或 "strategy"
        """
        # 策略模式使用独立的处理逻辑
        if decision_mode == "strategy":
            self._run_strategy_decision(
                pm=pm,
                stock_codes=stock_codes,
                current_prices=current_prices,
                df_cache=df_cache,
                date=date,
            )
            return

        from signals import Direction

        # 对每只候选股票做决策
        buy_candidates = []

        for code in stock_codes:
            price = current_prices.get(code)
            if price is None:
                continue

            # 获取截至当前日期的K线
            full_df = df_cache.get(code)
            if full_df is None:
                continue
            df = full_df[full_df["date"].astype(str) <= date].copy()
            if len(df) < 30:
                continue

            try:
                if decision_mode == "llm":
                    decision = self._llm_decision(code, df)
                else:
                    decision = self._weighted_decision(code, df)

                if decision.action == "BUY" and code not in pm.positions:
                    buy_candidates.append((code, decision))
                elif decision.action == "SELL" and code in pm.positions:
                    pm.sell(code, price, date, reason=f"决策卖出(score={decision.composite_score:.0f})")
            except Exception as e:
                logger.debug(f"  决策失败 {code}: {e}")

        # 按综合分降序，买入前N只
        buy_candidates.sort(key=lambda x: x[1].composite_score, reverse=True)
        for code, decision in buy_candidates:
            if not pm.can_buy():
                break
            price = current_prices.get(code)
            if price is None:
                continue

            # 买入（扣除佣金）
            trade = pm.buy(code, decision.name, price, date)
            if trade:
                commission = trade.amount * self.commission_rate
                pm.cash -= commission  # 佣金从现金扣除
                logger.debug(
                    f"  {date} 买入 {code} @ {price:.2f} "
                    f"score={decision.composite_score:.0f} conf={decision.confidence:.0%}"
                )

    def _run_strategy_decision(
        self,
        pm: PositionManager,
        stock_codes: List[str],
        current_prices: Dict[str, float],
        df_cache: Dict[str, pd.DataFrame],
        date: str,
    ):
        """
        运行策略模式决策

        使用策略对象的 generate_signals() 方法生成交易信号

        Args:
            pm: 持仓管理器
            stock_codes: 候选股票代码列表
            current_prices: 当日价格 {code: close}
            df_cache: K线缓存 {code: DataFrame}
            date: 当前日期
        """
        if self.strategy is None:
            logger.warning("策略模式未设置 strategy 参数，跳过决策")
            return

        buy_candidates = []

        for code in stock_codes:
            price = current_prices.get(code)
            if price is None:
                continue

            # 获取截至当前日期的K线
            full_df = df_cache.get(code)
            if full_df is None:
                continue
            df = full_df[full_df["date"].astype(str) <= date].copy()
            if len(df) < 30:
                continue

            try:
                signal = self.strategy.generate_signals(code, df)

                if signal.action == "BUY" and code not in pm.positions:
                    buy_candidates.append((code, signal))
                elif signal.action == "SELL" and code in pm.positions:
                    pm.sell(code, price, date, reason=f"策略卖出(score={signal.score:.0f})")
            except Exception as e:
                logger.debug(f"  策略信号生成失败 {code}: {e}")

        # 按信号分数降序，买入前N只
        buy_candidates.sort(key=lambda x: x[1].score, reverse=True)
        for code, signal in buy_candidates:
            if not pm.can_buy():
                break
            price = current_prices.get(code)
            if price is None:
                continue

            # 买入（扣除佣金）
            trade = pm.buy(code, code, price, date)
            if trade:
                commission = trade.amount * self.commission_rate
                pm.cash -= commission  # 佣金从现金扣除
                logger.debug(
                    f"  {date} 买入 {code} @ {price:.2f} "
                    f"score={signal.score:.0f} reason={signal.reason}"
                )

    def _weighted_decision(self, code: str, df: pd.DataFrame):
        """加权打分决策（回测模式，仅用历史数据，不调实时API）"""
        from signals.technical import (
            ichimoku_signal, volume_profile_signal, macd_signal
        )
        from strategy.decision import TradeDecision, DimensionScore
        from config import SIGNAL_WEIGHTS, DECISION_BUY_THRESHOLD

        if df is None or len(df) < 30:
            return TradeDecision(code=code, name=code, action="HOLD",
                                composite_score=0, confidence=0, dimensions={},
                                reason="数据不足")

        # 仅用技术面信号（不调任何实时API）
        dims = {}
        try:
            dims["technical"] = ichimoku_signal(df)
        except Exception:
            dims["technical"] = DimensionScore("technical", 50, 0.3, "计算异常")
        try:
            dims["capital"] = volume_profile_signal(df)
        except Exception:
            dims["capital"] = DimensionScore("capital", 50, 0.3, "计算异常")
        try:
            dims["emotion"] = macd_signal(df)
        except Exception:
            dims["emotion"] = DimensionScore("emotion", 50, 0.3, "计算异常")

        # 舆情和基本面用中性分（回测无法获取）
        dims["sentiment"] = DimensionScore("sentiment", 50, 0.0, "回测跳过")
        dims["fundamental"] = DimensionScore("fundamental", 50, 0.0, "回测跳过")

        # 计算综合分
        total_w = sum(SIGNAL_WEIGHTS.get(k, 0) for k in dims)
        composite = sum(dims[k].score * SIGNAL_WEIGHTS.get(k, 0) for k in dims) / total_w if total_w > 0 else 50

        # 决策
        if composite >= DECISION_BUY_THRESHOLD:
            action, reason = "BUY", f"综合分{composite:.0f}达标"
        elif composite <= 40:
            action, reason = "SELL", f"综合分{composite:.0f}偏低"
        else:
            action, reason = "HOLD", f"综合分{composite:.0f}观望"

        conf = max(d.confidence for d in dims.values())
        return TradeDecision(code=code, name=code, action=action,
                            composite_score=round(composite, 1),
                            confidence=round(conf, 2), dimensions=dims, reason=reason)

    def _llm_decision(self, code: str, df: pd.DataFrame):
        """LLM决策（慢速，调MiMo API）"""
        from strategy.decision import compute_dimension_scores
        from strategy.llm_trader import make_decision as llm_make_decision

        dimensions = compute_dimension_scores(code, df)
        return llm_make_decision(
            code=code,
            name=code,
            dimensions=dimensions,
            regime="sideways",
        )

    def _calculate_metrics(
        self,
        snapshots: List[DailySnapshot],
        trades: List[TradeRecord],
        initial_capital: float,
        final_assets: float,
        start_date: str,
        end_date: str,
    ) -> BacktestResult:
        """计算回测指标（复用已有逻辑）"""
        # 总收益率
        total_return = (final_assets - initial_capital) / initial_capital

        # 年化收益率
        if snapshots:
            days = len(snapshots)
            years = days / 252
            if years > 0 and total_return > -1:
                annualized_return = (1 + total_return) ** (1 / years) - 1
            else:
                annualized_return = total_return
        else:
            annualized_return = 0.0
            days = 0

        # 日收益率序列
        daily_returns = []
        for i in range(1, len(snapshots)):
            prev = snapshots[i - 1].total_assets
            curr = snapshots[i].total_assets
            if prev > 0:
                daily_returns.append((curr - prev) / prev)

        # 夏普比率（无风险利率按年化2%计算）
        risk_free_daily = 0.02 / 252
        if daily_returns and len(daily_returns) > 1:
            excess = np.array(daily_returns) - risk_free_daily
            std = np.std(excess, ddof=1)
            sharpe_ratio = (np.mean(excess) / std * np.sqrt(252)) if std > 0 else 0.0
        else:
            sharpe_ratio = 0.0

        # 最大回撤
        max_dd = 0.0
        dd_start = ""
        dd_end = ""
        peak = 0.0
        peak_date = ""
        for snap in snapshots:
            if snap.total_assets > peak:
                peak = snap.total_assets
                peak_date = snap.date
            dd = (peak - snap.total_assets) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                dd_start = peak_date
                dd_end = snap.date

        # 胜率（配对买卖计算）
        buy_map = {}
        win_trades = 0
        lose_trades = 0
        hold_days_list = []

        for t in trades:
            if t.action == "买入":
                buy_map[t.code] = t
            elif t.action == "卖出" and t.code in buy_map:
                buy_t = buy_map.pop(t.code)
                if t.price > buy_t.price:
                    win_trades += 1
                else:
                    lose_trades += 1
                try:
                    bd = datetime.strptime(buy_t.date, "%Y-%m-%d")
                    sd = datetime.strptime(t.date, "%Y-%m-%d")
                    hold_days_list.append((sd - bd).days)
                except ValueError:
                    pass

        total_closed = win_trades + lose_trades
        win_rate = win_trades / total_closed if total_closed > 0 else 0.0
        avg_hold = float(np.mean(hold_days_list)) if hold_days_list else 0.0

        # 基准收益
        benchmark_return = 0.0
        if snapshots and snapshots[0].benchmark_value > 0:
            benchmark_return = (snapshots[-1].benchmark_value - initial_capital) / initial_capital

        return BacktestResult(
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            final_assets=final_assets,
            total_return=total_return,
            annualized_return=annualized_return,
            sharpe_ratio=sharpe_ratio,
            max_drawdown=max_dd,
            max_drawdown_start=dd_start,
            max_drawdown_end=dd_end,
            win_rate=win_rate,
            total_trades=total_closed,
            win_trades=win_trades,
            lose_trades=lose_trades,
            avg_hold_days=avg_hold,
            trades=trades,
            daily_snapshots=snapshots,
            benchmark_return=benchmark_return,
            excess_return=total_return - benchmark_return,
        )
