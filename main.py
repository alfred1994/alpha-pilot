"""
A股量化系统 - 主入口
====================================================================
命令行参数:
  --scan      扫描信号（选股 + 5维打分 + 决策）
  --execute   执行交易（风控 + 止损 + 模拟盘下单）
  --review    每日复盘
  --full      全链路（默认）
  --account   查看账户状态
  --risk      查看风控状态
  --after-market  收盘后分析（外围市场+新闻）
====================================================================
"""
import sys
import os
import json
import argparse
import logging
from datetime import datetime

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)


def cmd_scan():
    """扫描信号（快链路）"""
    from scheduler.pipeline import run_scan, format_pipeline_report, format_trade_plan_report

    print("快速扫描...")
    result = run_scan()
    print(format_pipeline_report(result))

    # 输出TradePlan报告
    import json as _json
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "signal_cache.json")
    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            plan_data = _json.load(f)
        print("\n" + format_trade_plan_report(plan_data))

    # 输出决策报告
    if result.decisions:
        from strategy.decision import format_decision_report
        print("\n" + format_decision_report(result.decisions))

    return result


def cmd_execute():
    """执行交易"""
    from scheduler.pipeline import execute_trades, format_pipeline_report

    print("执行交易...")
    result = execute_trades()
    print(format_pipeline_report(result))
    return result


def cmd_review():
    """每日复盘"""
    from scheduler.pipeline import run_review, format_pipeline_report

    print("每日复盘...")
    result = run_review()
    # 打印完整复盘报告（容错）
    try:
        from review.daily_review import run_daily_review
        review_text = run_daily_review()
        print(review_text)
    except Exception as e:
        print(f"详细复盘失败(不影响主流程): {e}")
    return result


def cmd_after_market():
    """收盘后分析：外围市场 + 新闻聚合 → 隔夜快照"""
    from data.overnight_snapshot import generate_overnight_snapshot, format_for_llm_prompt
    from signals.overnight import compute_overnight_signal

    print("收盘后分析...")
    print("-" * 65)

    # 1. 生成隔夜快照
    snap = generate_overnight_snapshot()
    prompt = format_for_llm_prompt(snap)
    print(prompt)

    # 2. 计算隔夜风险信号
    signal = compute_overnight_signal()
    print(f"\n隔夜风险信号: {signal.score:.0f}分 ({signal.risk_level})")
    print(f"  {signal.detail}")

    # 3. 保存结果摘要
    summary = {
        "date": snap.get("date", ""),
        "signal_score": signal.score,
        "risk_level": signal.risk_level,
        "detail": signal.detail,
        "prompt_preview": prompt[:500],
    }
    print(f"\n[OK] 隔夜快照已保存")
    return summary


def cmd_full():
    """全链路"""
    from scheduler.pipeline import run_daily_pipeline, format_pipeline_report

    print("全链路管道启动...")
    result = run_daily_pipeline()
    print(format_pipeline_report(result))
    return result


def cmd_stop_check():
    """盘中止损巡检"""
    import time as _time
    _t0 = _time.time()
    try:
        from execution.paper_account import PaperAccount

        account = PaperAccount()
        positions = account.positions
        print(f"[{_time.time()-_t0:.1f}s] 加载账户: 现金={account.cash:.0f} 持仓={len(positions)}只", flush=True)

        # 系统级风控检查
        from risk.system_risk import SystemRiskController
        sr = SystemRiskController()
        total_assets = account.total_assets()
        sr.update(total_assets)
        health = sr.check_system_health()
        if not health["healthy"]:
            print(f"[{_time.time()-_t0:.1f}s] ⚠ 系统风控告警:", flush=True)
            for issue in health["issues"]:
                print(f"  ⚠ {issue}", flush=True)
        if health["system_halted"]:
            print(f"[{_time.time()-_t0:.1f}s] 系统已停机，跳过止损检查", flush=True)
            return

        if not positions:
            print("无持仓，跳过止损检查")
            return

        # 获取实时价格
        prices = {}
        for code in positions:
            try:
                from data.realtime import get_realtime
                _tc = _time.time()
                quote = get_realtime([code])
                print(f"[{_time.time()-_t0:.1f}s] {code} 行情耗时 {_time.time()-_tc:.1f}s", flush=True)
                if quote and quote[0].price > 0:
                    prices[code] = quote[0].price
            except Exception as e:
                print(f"[{_time.time()-_t0:.1f}s] {code} 行情失败: {e}", flush=True)

        if not prices:
            print("无法获取价格，跳过")
            return

        # 检查止损
        print(f"[{_time.time()-_t0:.1f}s] 开始止损检查...", flush=True)
        stop_signals = account.check_stop_conditions(prices)

        if stop_signals:
            for signal in stop_signals:
                code = signal.get("code", "")
                action = signal.get("action", "")
                if action in ("stop_loss", "take_profit", "trailing_stop"):
                    shares = positions.get(code, {}).get("shares", 0)
                    price = prices.get(code, 0)
                    if shares > 0 and price > 0:
                        account.sell(code, price, shares,
                                     reason=signal.get("reason", action))
                        print(f"止损卖出: {code} {shares}股 @ {price} ({action})")
        else:
            print(f"持仓 {len(positions)} 只，全部正常")

        # 打印持仓状态
        for code, pos in positions.items():
            price = prices.get(code, 0)
            buy_price = pos.get("buy_price", 0)
            if price > 0 and buy_price > 0:
                pnl = (price - buy_price) / buy_price
                print(f"  {pos.get('name', code)}({code}): {price:.2f} ({pnl:+.2%})")

    except Exception as e:
        print(f"止损巡检异常: {e}")
        logging.getLogger().error(f"止损巡检异常: {e}", exc_info=True)


def cmd_account():
    """查看账户状态"""
    from execution.paper_account import PaperAccount

    account = PaperAccount()

    # 尝试获取实时价格
    prices = {}
    for code in account.positions:
        try:
            from data.realtime import get_realtime
            quote = get_realtime([code])  # 【Phase1-Task4】统一传入list参数
            if quote and quote[0].price > 0:
                prices[code] = quote[0].price
        except Exception:
            pass

    print(account.format_status(prices if prices else None))

    # 盈亏汇总
    pnl = account.pnl_summary(prices if prices else None)
    print(f"\n总盈亏: {pnl['pnl']:+,.0f} ({pnl['pnl_pct']:+.2%})")


def cmd_risk():
    """查看风控状态"""
    from risk.drawdown import DrawdownController
    from risk.stop_loss import StopLossManager
    from execution.paper_account import PaperAccount

    dc = DrawdownController()
    account = PaperAccount()

    # 回撤状态
    total_assets = account.total_assets()
    dc.update(total_assets)
    print(dc.format_status(total_assets))

    # 止损止盈概览
    if account.positions:
        prices = {}
        for code in account.positions:
            try:
                from data.realtime import get_realtime
                quote = get_realtime([code])  # 【Phase1-Task4】统一传入list参数
                if quote and quote[0].price > 0:
                    prices[code] = quote[0].price
            except Exception:
                pass

        if prices:
            slm = StopLossManager()
            print("\n" + slm.format_stop_summary(account.positions, prices))


def cmd_backtest(args):
    """运行回测"""
    from portfolio.backtest import SimpleBacktestEngine
    from portfolio.report import generate_full_report

    stock_codes = args.stocks or ["600519", "000001", "300750"]

    print(f"回测股票: {stock_codes}")
    print(f"回测区间: {args.start_date} ~ {args.end_date}")
    print(f"决策模式: {args.mode} | 初始资金: {args.capital:,.0f}")
    if args.mode == "strategy":
        print(f"策略: {args.strategy}")
    print("-" * 65)

    # 策略模式需要加载策略
    strategy = None
    if args.mode == "strategy":
        from strategy.strategies import get_strategy
        strategy = get_strategy(args.strategy)
        print(f"已加载策略: {strategy.describe()}")

    engine = SimpleBacktestEngine(
        initial_capital=args.capital,
        max_stocks=5,
        stop_loss=-0.08,
        take_profit=0.10,
        use_atr=True,
        signal_interval=5,
        strategy=strategy,
    )

    result = engine.run(
        stock_codes=stock_codes,
        start_date=args.start_date,
        end_date=args.end_date,
        decision_mode=args.mode,
    )

    print(generate_full_report(result))
    return result


def cmd_optimize(args):
    """策略优化模式"""
    from strategy.strategies import get_strategy
    from strategy.optimizer import StrategyOptimizer
    from portfolio.backtest import SimpleBacktestEngine

    stock_codes = args.stocks or ["600519", "000001", "300750"]

    print(f"策略优化模式")
    print(f"策略: {args.strategy}")
    print(f"优化轮数: {args.rounds}")
    print(f"回测股票: {stock_codes}")
    print(f"回测区间: {args.start_date} ~ {args.end_date}")
    print("-" * 65)

    # 加载策略
    strategy = get_strategy(args.strategy)
    print(f"已加载策略: {strategy.describe()}")

    # 创建回测引擎
    engine = SimpleBacktestEngine(
        initial_capital=args.capital,
        max_stocks=5,
        stop_loss=-0.08,
        take_profit=0.10,
        use_atr=True,
        signal_interval=5,
        strategy=strategy,
    )

    # 创建优化器
    optimizer = StrategyOptimizer(strategy, engine)

    # 运行优化
    result = optimizer.optimize(
        stock_codes=stock_codes,
        start_date=args.start_date,
        end_date=args.end_date,
        max_rounds=args.rounds,
    )

    # 输出报告
    print(optimizer.format_optimization_report(result))
    return result


def main():
    parser = argparse.ArgumentParser(
        description="A股量化系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                # 全链路（默认）
  python main.py --full         # 全链路
  python main.py --scan         # 只扫描信号
  python main.py --execute      # 只执行交易
  python main.py --review       # 每日复盘
  python main.py --account      # 查看账户
  python main.py --risk         # 查看风控
  python main.py --backtest --stocks 600519 000001 --start-date 2024-01-01 --end-date 2024-12-31
  python main.py --backtest --mode strategy --strategy zt_reversal --stocks 600519 000001
  python main.py --optimize --strategy zt_reversal --stocks 600519 000001 --rounds 3
  python main.py --after-market
        """,
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--scan", action="store_true", help="扫描信号（快链路90秒）")
    group.add_argument("--prefetch", action="store_true", help="盘前数据预热（慢链路）")
    group.add_argument("--execute", action="store_true", help="执行交易（风控+模拟盘）")
    group.add_argument("--review", action="store_true", help="每日复盘")
    group.add_argument("--full", action="store_true", help="全链路（默认）")
    group.add_argument("--account", action="store_true", help="查看账户状态")
    group.add_argument("--risk", action="store_true", help="查看风控状态")
    group.add_argument("--backtest", action="store_true", help="运行回测")
    group.add_argument("--optimize", action="store_true", help="策略优化模式")
    group.add_argument("--after-market", action="store_true", help="收盘后分析（外围市场+新闻）")
    group.add_argument("--stop-check", action="store_true", help="盘中止损巡检")

    parser.add_argument("--start-date", default="2024-01-01", help="回测开始日期")
    parser.add_argument("--end-date", default="2024-12-31", help="回测结束日期")
    parser.add_argument("--capital", type=float, default=1000000, help="初始资金")
    parser.add_argument("--mode", choices=["weighted", "llm", "strategy"], default="weighted", help="决策模式")
    parser.add_argument("--stocks", nargs="+", help="回测股票列表")
    parser.add_argument("--strategy", default="zt_reversal", help="策略名称（用于 strategy 模式）")
    parser.add_argument("--rounds", type=int, default=3, help="优化轮数（用于 optimize 模式）")

    args = parser.parse_args()

    print(f"A股量化系统 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # 默认全链路
    if args.scan:
        result = cmd_scan()
    elif args.prefetch:
        from scheduler.pipeline import prefetch, format_pipeline_report
        print("盘前数据预热...")
        result = prefetch()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    elif args.execute:
        result = cmd_execute()
    elif args.after_market:
        result = cmd_after_market()
    elif args.review:
        result = cmd_review()
    elif args.account:
        cmd_account()
        return
    elif args.risk:
        cmd_risk()
        return
    elif args.backtest:
        result = cmd_backtest(args)
    elif args.optimize:
        result = cmd_optimize(args)
    elif args.stop_check:
        cmd_stop_check()
        return
    else:
        result = cmd_full()

    # 退出码
    if hasattr(result, "errors") and result.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
