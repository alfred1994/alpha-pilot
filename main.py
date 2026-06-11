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
    review_text = getattr(result, "review_text", "")
    if review_text:
        print(review_text)
    print(format_pipeline_report(result))
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


def cmd_auto(args):
    """自动盯盘交易员"""
    from scheduler.auto_trader import run_auto_loop

    print("模拟盘自动盯盘交易员启动...")
    result = run_auto_loop(
        loop_interval=args.loop_interval,
        scan_interval=args.scan_interval,
        once=args.auto_once,
        force_scan=args.force_scan,
    )
    return result


def cmd_health():
    """系统健康检查"""
    from scheduler.health import run_health_check, format_health_report

    items = run_health_check()
    print(format_health_report(items))
    return {"errors": [i.name for i in items if (not i.ok and i.required)]}


def cmd_watchdog():
    """自动盯盘运行态检查"""
    from scheduler.watchdog import run_auto_watchdog, format_watchdog_report

    items = run_auto_watchdog()
    print(format_watchdog_report(items))
    return {"errors": [i.name for i in items if i.severity == "critical"]}


def cmd_doctor(args):
    """自动盯盘巡检自愈"""
    from scheduler.doctor import run_auto_doctor, format_doctor_report

    result = run_auto_doctor(
        recover=not args.no_recover,
        force_scan=args.force_scan,
        auto_pause_on_failure=not args.no_auto_pause_on_failure,
        repair_closure=not args.no_closure_repair,
    )
    print(format_doctor_report(result))
    return {"errors": result.get("after_critical", []) + result.get("closure_after_critical", [])}


def cmd_windows_tasks(args):
    """生成Windows无人值守计划任务脚本"""
    from scheduler.windows_tasks import (
        format_windows_task_summary,
        generate_windows_task_scripts,
    )

    paths = generate_windows_task_scripts(
        project_dir=os.path.dirname(os.path.abspath(__file__)),
        output_dir=args.task_output_dir,
        python_cmd=args.python_cmd,
        task_prefix=args.task_prefix,
        report_days=args.report_days,
    )
    print(format_windows_task_summary(paths))
    return {"paths": paths}


def cmd_unattended_status(args):
    """检查Windows无人值守状态"""
    from scheduler.windows_tasks import format_unattended_status, run_unattended_status

    items = run_unattended_status(
        project_dir=os.path.dirname(os.path.abspath(__file__)),
        output_dir=args.task_output_dir,
        task_prefix=args.task_prefix,
    )
    print(format_unattended_status(items))
    return {"errors": [i.name for i in items if i.severity == "critical"]}


def cmd_linux_tasks(args):
    """生成Ubuntu/Hermes无人值守systemd用户任务脚本"""
    from scheduler.linux_tasks import (
        format_linux_task_summary,
        generate_linux_task_scripts,
    )

    paths = generate_linux_task_scripts(
        project_dir=os.path.dirname(os.path.abspath(__file__)),
        output_dir=args.task_output_dir,
        python_cmd=args.python_cmd,
        service_prefix=args.service_prefix,
        report_days=args.report_days,
        hermes_env_file=args.hermes_env_file,
    )
    print(format_linux_task_summary(paths))
    return {"paths": paths}


def cmd_linux_unattended_status(args):
    """检查Ubuntu/Hermes无人值守状态"""
    from scheduler.linux_tasks import (
        format_linux_unattended_status,
        run_linux_unattended_status,
    )

    items = run_linux_unattended_status(
        project_dir=os.path.dirname(os.path.abspath(__file__)),
        output_dir=args.task_output_dir,
        service_prefix=args.service_prefix,
    )
    print(format_linux_unattended_status(items))
    return {"errors": [i.name for i in items if i.severity == "critical"]}


def cmd_paper_ready(args):
    """模拟盘AI交易员就绪检查"""
    from scheduler.readiness import format_paper_readiness, run_paper_readiness

    result = run_paper_readiness(
        report_days=args.report_days,
        report_end_date=args.report_end_date,
        rehearsal_days=args.rehearsal_days,
        task_output_dir=args.task_output_dir,
        task_prefix=args.task_prefix,
        service_prefix=args.service_prefix,
        unattended_platform=args.unattended_platform,
        project_dir=os.path.dirname(os.path.abspath(__file__)),
    )
    print(format_paper_readiness(result))
    return {"errors": result["overall"].get("critical", [])}


def cmd_paper_bootstrap(args):
    """模拟盘AI交易员启动引导"""
    from scheduler.bootstrap import format_paper_bootstrap, run_paper_bootstrap

    result = run_paper_bootstrap(
        project_dir=os.path.dirname(os.path.abspath(__file__)),
        task_output_dir=args.task_output_dir,
        python_cmd=args.python_cmd,
        task_prefix=args.task_prefix,
        service_prefix=args.service_prefix,
        unattended_platform=args.unattended_platform,
        hermes_env_file=args.hermes_env_file,
        report_days=args.report_days,
        report_end_date=args.report_end_date,
        rehearsal_days=args.rehearsal_days,
        rehearsal_start_date=args.rehearsal_start_date,
    )
    print(format_paper_bootstrap(result))
    return {"errors": result.get("errors", [])}


def cmd_auto_control(args, action: str):
    """自动盯盘暂停/恢复/状态"""
    from scheduler.control import (
        format_auto_control_state,
        get_auto_control_state,
        pause_auto_trader,
        resume_auto_trader,
    )

    if action == "pause":
        state = pause_auto_trader(reason=args.reason or "manual pause")
    elif action == "resume":
        state = resume_auto_trader(reason=args.reason or "manual resume")
    else:
        state = get_auto_control_state()
    print(format_auto_control_state(state))
    return {"state": state}


def cmd_ai_report(args):
    """AI交易员模拟盘试运行报告"""
    from review.ai_trader_report import generate_ai_trader_report

    result = generate_ai_trader_report(
        days=args.report_days,
        end_date=args.report_end_date,
        save=True,
    )
    print(result["markdown"])
    paths = result.get("paths") or {}
    if paths.get("markdown"):
        print(f"\n报告已保存: {paths['markdown']}")
    if paths.get("json"):
        print(f"JSON已保存: {paths['json']}")
    return result


def cmd_ops_status(args):
    """自动盯盘运维状态汇总"""
    from scheduler.ops_status import format_ops_status, run_ops_status

    result = run_ops_status(
        report_days=args.report_days,
        report_end_date=args.report_end_date,
        save_report=True,
    )
    print(format_ops_status(result, include_sections=args.verbose))
    return {"errors": ["ops_status"] if result["overall"]["level"] == "critical" else []}


def cmd_closure_check(args):
    """正式模拟盘日内闭环缺口诊断"""
    from scheduler.closure_check import format_closure_check, run_closure_check

    result = run_closure_check(date=args.closure_date)
    print(format_closure_check(result))
    return {"errors": result["overall"].get("critical", [])}


def cmd_closure_repair(args):
    """正式模拟盘闭环缺口自愈"""
    from scheduler.closure_repair import format_closure_repair, run_closure_repair

    result = run_closure_repair(
        date=args.closure_date,
        recover=not args.no_recover,
    )
    print(format_closure_repair(result))
    return {"errors": result["after"]["overall"].get("critical", [])}


def cmd_realtime():
    """事件驱动交易员"""
    import asyncio
    from realtime.event_driven_trader import main

    print("事件驱动交易员启动...")
    print("实时行情监控 + 异步事件处理")
    print("按 Ctrl+C 退出")
    asyncio.run(main())


def cmd_auto_rehearsal(args):
    """自动盯盘连续演练"""
    from scheduler.rehearsal import run_auto_rehearsal

    result = run_auto_rehearsal(
        days=args.rehearsal_days,
        start_date=args.rehearsal_start_date,
        save_report=True,
    )
    print(result["report"]["markdown"])
    print(f"\n演练数据库: {result['db_path']}")
    paths = result["report"].get("paths") or {}
    if paths.get("markdown"):
        print(f"演练报告: {paths['markdown']}")
    return result


def cmd_paper_observe(args):
    """正式模拟盘有限轮观察"""
    os.environ["BROKER_MODE"] = "paper"
    from scheduler.paper_observer import format_paper_observer, run_paper_observer

    result = run_paper_observer(
        cycles=args.observe_cycles,
        interval=args.observe_interval,
        force_scan=args.force_scan,
        log_file=args.observe_log,
        save_report=not args.no_observe_report,
        report_days=args.report_days,
        report_end_date=args.report_end_date,
    )
    print(format_paper_observer(result))
    return {"errors": result.get("errors", [])}


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
  python main.py --auto        # 自动盯盘循环（模拟盘）
  python main.py --auto-once   # 自动盯盘单轮测试
  python main.py --auto-rehearse # 连续多日自动盯盘演练（隔离数据库）
  python main.py --paper-observe # 有限轮正式模拟盘观察（默认模拟盘）
  python main.py --health      # 检查自动盯盘运行条件
  python main.py --watchdog    # 检查自动盯盘是否停滞/漏扫/漏复盘
  python main.py --doctor      # Watchdog巡检并尝试触发一轮自愈
  python main.py --windows-tasks # 生成Windows计划任务脚本
  python main.py --unattended-status # 检查无人值守脚本/任务/日志
  python main.py --linux-tasks # 生成Ubuntu/Hermes systemd用户任务脚本
  python main.py --linux-unattended-status # 检查Ubuntu/Hermes无人值守状态
  python main.py --paper-ready --unattended-platform linux
  python main.py --paper-ready # 模拟盘AI交易员上线前就绪检查
  python main.py --paper-bootstrap # 生成脚本+演练+就绪门禁
  python main.py --auto-pause --reason "观察行情"
  python main.py --auto-resume
  python main.py --ai-report   # 最近5天AI交易员试运行报告
  python main.py --ops-status  # 健康/Watchdog/报告一页汇总
  python main.py --closure-check # 检查今日正式模拟盘闭环缺口
  python main.py --closure-repair # 在安全窗口内尝试补齐闭环缺口
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
    group.add_argument("--auto", action="store_true", help="自动盯盘交易员（循环运行，默认模拟盘）")
    group.add_argument("--auto-once", action="store_true", help="自动盯盘交易员（只运行一轮，用于测试）")
    group.add_argument("--auto-rehearse", action="store_true", help="连续多日自动盯盘演练（隔离数据库）")
    group.add_argument("--paper-observe", action="store_true", help="有限轮正式模拟盘观察（写入正式库，默认模拟盘）")
    group.add_argument("--health", action="store_true", help="系统健康检查")
    group.add_argument("--watchdog", action="store_true", help="自动盯盘运行态检查")
    group.add_argument("--doctor", action="store_true", help="自动盯盘巡检自愈")
    group.add_argument("--windows-tasks", action="store_true", help="生成Windows无人值守计划任务脚本")
    group.add_argument("--unattended-status", action="store_true", help="检查Windows无人值守脚本/任务/日志")
    group.add_argument("--linux-tasks", action="store_true", help="生成Ubuntu/Hermes systemd用户任务脚本")
    group.add_argument("--linux-unattended-status", action="store_true", help="检查Ubuntu/Hermes无人值守脚本/systemd/日志")
    group.add_argument("--paper-ready", action="store_true", help="模拟盘AI交易员上线前就绪检查")
    group.add_argument("--paper-bootstrap", action="store_true", help="模拟盘启动引导（脚本+演练+门禁）")
    group.add_argument("--auto-pause", action="store_true", help="暂停盘中自动交易动作")
    group.add_argument("--auto-resume", action="store_true", help="恢复盘中自动交易动作")
    group.add_argument("--auto-control", action="store_true", help="查看自动盯盘控制状态")
    group.add_argument("--ai-report", action="store_true", help="AI交易员模拟盘试运行报告")
    group.add_argument("--ops-status", action="store_true", help="自动盯盘运维状态汇总")
    group.add_argument("--closure-check", action="store_true", help="正式模拟盘日内闭环缺口诊断")
    group.add_argument("--closure-repair", action="store_true", help="正式模拟盘闭环缺口自愈")
    group.add_argument("--realtime", action="store_true", help="事件驱动交易员（实时行情+异步）")

    parser.add_argument("--start-date", default="2024-01-01", help="回测开始日期")
    parser.add_argument("--end-date", default="2024-12-31", help="回测结束日期")
    parser.add_argument("--capital", type=float, default=1000000, help="初始资金")
    parser.add_argument("--mode", choices=["weighted", "llm", "strategy"], default="weighted", help="决策模式")
    parser.add_argument("--stocks", nargs="+", help="回测股票列表")
    parser.add_argument("--strategy", default="zt_reversal", help="策略名称（用于 strategy 模式）")
    parser.add_argument("--rounds", type=int, default=3, help="优化轮数（用于 optimize 模式）")
    parser.add_argument("--loop-interval", type=int, default=None, help="自动盯盘主循环间隔秒数")
    parser.add_argument("--scan-interval", type=int, default=None, help="自动盯盘盘中扫描间隔秒数")
    parser.add_argument("--force-scan", action="store_true", help="自动盯盘单轮中强制执行盘中扫描")
    parser.add_argument("--no-recover", action="store_true", help="Doctor只巡检，不触发自愈")
    parser.add_argument("--no-auto-pause-on-failure", action="store_true", help="Doctor自愈失败后不自动暂停")
    parser.add_argument("--no-closure-repair", action="store_true", help="Doctor不执行闭环缺口自愈")
    parser.add_argument("--task-output-dir", default=None, help="无人值守脚本输出目录")
    parser.add_argument("--task-prefix", default="QuantPilot", help="Windows计划任务名前缀")
    parser.add_argument("--service-prefix", default="quant-pilot", help="Linux systemd用户任务名前缀")
    parser.add_argument("--hermes-env-file", default="$HOME/.hermes/.env", help="Hermes环境变量文件")
    parser.add_argument("--unattended-platform", choices=["windows", "linux"], default="windows", help="paper-ready/bootstrap使用的无人值守平台")
    parser.add_argument("--python-cmd", default="python3" if sys.platform != "win32" else "python", help="无人值守任务使用的Python命令")
    parser.add_argument("--reason", default="", help="暂停/恢复原因")
    parser.add_argument("--report-days", type=int, default=5, help="AI交易员报告回看天数")
    parser.add_argument("--report-end-date", default=None, help="AI交易员报告结束日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--rehearsal-days", type=int, default=5, help="自动盯盘演练天数")
    parser.add_argument("--rehearsal-start-date", default=None, help="自动盯盘演练起始日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--observe-cycles", type=int, default=3, help="正式模拟盘观察轮数")
    parser.add_argument("--observe-interval", type=int, default=60, help="正式模拟盘观察轮间隔秒数")
    parser.add_argument("--observe-log", default=None, help="正式模拟盘观察日志路径")
    parser.add_argument("--no-observe-report", action="store_true", help="正式模拟盘观察结束后不生成报告")
    parser.add_argument("--closure-date", default=None, help="闭环缺口诊断日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--verbose", action="store_true", help="输出详细分段报告")

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
    elif args.auto or args.auto_once:
        result = cmd_auto(args)
    elif args.auto_rehearse:
        result = cmd_auto_rehearsal(args)
    elif args.paper_observe:
        result = cmd_paper_observe(args)
    elif args.health:
        result = cmd_health()
    elif args.watchdog:
        result = cmd_watchdog()
    elif args.doctor:
        result = cmd_doctor(args)
    elif args.windows_tasks:
        result = cmd_windows_tasks(args)
    elif args.unattended_status:
        result = cmd_unattended_status(args)
    elif args.linux_tasks:
        result = cmd_linux_tasks(args)
    elif args.linux_unattended_status:
        result = cmd_linux_unattended_status(args)
    elif args.paper_ready:
        result = cmd_paper_ready(args)
    elif args.paper_bootstrap:
        result = cmd_paper_bootstrap(args)
    elif args.auto_pause:
        result = cmd_auto_control(args, "pause")
    elif args.auto_resume:
        result = cmd_auto_control(args, "resume")
    elif args.auto_control:
        result = cmd_auto_control(args, "status")
    elif args.ai_report:
        result = cmd_ai_report(args)
    elif args.ops_status:
        result = cmd_ops_status(args)
    elif args.closure_check:
        result = cmd_closure_check(args)
    elif args.closure_repair:
        result = cmd_closure_repair(args)
    elif args.realtime:
        cmd_realtime()
        return
    else:
        result = cmd_full()

    # 退出码
    if hasattr(result, "errors") and result.errors:
        sys.exit(1)
    if isinstance(result, dict) and result.get("errors"):
        sys.exit(1)


if __name__ == "__main__":
    main()
