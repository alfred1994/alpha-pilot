"""
组合管理与回测模块

功能:
    - 持仓管理: 买入/卖出/调仓，止损-5%，止盈+10%
    - 回测引擎: 历史验证策略有效性
    - 报告生成: 盈亏统计、信号命中率

规则:
    - 最多持有5只股票
    - 单只持仓上限20%
    - 计算: 总收益率、年化收益、夏普比率、最大回撤、胜率

使用方法:
    from portfolio.position import PositionManager
    from portfolio.backtest import BacktestEngine
    from portfolio.report import generate_full_report
"""
from portfolio.position import PositionManager, Position, TradeRecord
from portfolio.backtest import BacktestEngine, SimpleBacktestEngine, BacktestResult
from portfolio.report import generate_full_report, format_backtest_report
