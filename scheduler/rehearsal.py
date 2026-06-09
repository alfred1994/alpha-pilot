"""
模拟盘连续演练器
====================================================================
在不调用真实行情、真实LLM、真实交易接口的前提下，驱动自动盯盘主循环
跑出多日闭环样本：
  盘前预热 → 市场识别 → 盘中扫描 → 模拟成交 → 盘后复盘 → 教训/参数进化

默认写入 data/rehearsal/rehearsal.db，避免污染正式模拟盘数据库。
====================================================================
"""
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Dict, List

from config import DATA_DIR
from data.database import Database
from review.ai_trader_report import generate_ai_trader_report
from scheduler.auto_trader import AutoTraderState, run_auto_cycle
from scheduler.pipeline import PipelineResult, StepResult


REHEARSAL_DIR = os.path.join(DATA_DIR, "rehearsal")
REHEARSAL_DB = os.path.join(REHEARSAL_DIR, "rehearsal.db")
REHEARSAL_REPORT_DIR = os.path.join(REHEARSAL_DIR, "reports")


def _date_list(days: int, start_date: str = None) -> List[str]:
    """生成连续演练日期列表"""
    days = max(1, int(days or 1))
    start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else datetime.now()
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def _pnl_for_day(day_index: int) -> float:
    """给演练样本生成可重复的盈亏序列，前三天偏弱以触发自适应收紧"""
    sequence = [-2.5, -1.8, -3.2, 2.1, 1.4]
    return sequence[day_index % len(sequence)]


def _make_scan_result(date: str) -> PipelineResult:
    """生成盘中扫描替身结果"""
    return PipelineResult(
        date=date,
        market_status="盘中",
        steps=[StepResult(name="rehearsal_scan", success=True, detail="演练扫描")],
        candidates=[
            {"code": "600519", "name": "贵州茅台", "score": 72},
            {"code": "000001", "name": "平安银行", "score": 64},
            {"code": "300750", "name": "宁德时代", "score": 61},
        ],
        decisions=[
            SimpleNamespace(code="600519", action="BUY"),
            SimpleNamespace(code="000001", action="HOLD"),
        ],
    )


def _save_rehearsal_account(db: Database, date: str, cumulative_pnl: float):
    """保存演练账户状态"""
    total_assets = 1000000 * (1 + cumulative_pnl / 100)
    db.save_account_state({
        "initial_capital": 1000000,
        "cash": total_assets,
        "total_assets": total_assets,
        "position_count": 0,
        "positions": {},
        "updated_at": f"{date}T15:10:00",
    })


def _save_rehearsal_trade_and_decision(db_path: str, date: str, day_index: int,
                                       cumulative_pnl: float) -> Dict:
    """写入一组演练成交和LLM决策"""
    pnl_pct = _pnl_for_day(day_index)
    buy_price = 10.0 + day_index * 0.1
    sell_price = round(buy_price * (1 + pnl_pct / 100), 2)
    outcome = "win" if pnl_pct > 0 else "loss"

    with Database(db_path=db_path) as db:
        buy_id = db.insert_trade({
            "code": "600519",
            "name": "贵州茅台",
            "action": "BUY",
            "price": buy_price,
            "shares": 1000,
            "amount": buy_price * 1000,
            "reason": "连续演练: LLM高置信买入",
            "signal_score": 72,
            "market_regime": "sideways",
            "created_at": f"{date}T10:01:00",
        })
        sell_id = db.insert_trade({
            "code": "600519",
            "name": "贵州茅台",
            "action": "SELL",
            "price": sell_price,
            "shares": 1000,
            "amount": sell_price * 1000,
            "reason": "连续演练: 收盘回放卖出",
            "signal_score": 72,
            "market_regime": "sideways",
            "created_at": f"{date}T14:50:00",
        })
        db.insert_llm_decision({
            "code": "600519",
            "date": date,
            "action": "BUY",
            "llm_prompt": "连续演练prompt",
            "llm_response": "BUY 600519",
            "reasoning": "演练样本: 趋势和资金信号较强",
            "confidence": 0.78,
            "outcome": outcome,
            "outcome_pct": pnl_pct,
            "trade_id": buy_id,
            "created_at": f"{date}T10:00:30",
        })
        _save_rehearsal_account(db, date, cumulative_pnl + pnl_pct)

    return {
        "buy_id": buy_id,
        "sell_id": sell_id,
        "pnl_pct": pnl_pct,
        "outcome": outcome,
    }


def _save_rehearsal_review(db_path: str, date: str, day_index: int,
                           trade_result: Dict):
    """写入演练复盘、教训和自适应状态"""
    pnl_pct = trade_result["pnl_pct"]
    lesson = (
        "连续演练教训: 亏损样本出现，下一轮应减少TopK并降低仓位。"
        if pnl_pct <= 0
        else "连续演练教训: 盈利样本出现，继续观察该信号组合的稳定性。"
    )
    with Database(db_path=db_path) as db:
        db.save_review_snapshot(date, {
            "date": date,
            "trade_reviews": [{
                "code": "600519",
                "name": "贵州茅台",
                "action": "SELL",
                "result_pct": pnl_pct,
                "hit": pnl_pct > 0,
                "source": "auto_rehearsal",
                "dimensions": {"technical": 72, "capital": 66, "sentiment": 60},
            }],
            "summary": "连续演练复盘样本",
        })
        db.insert_lesson({
            "date": date,
            "category": "risk" if pnl_pct <= 0 else "buy",
            "content": lesson,
            "importance": 4 if pnl_pct <= 0 else 3,
            "market_regime": "sideways",
            "related_trades": '["600519"]',
            "created_at": f"{date}T15:12:00",
        })

        # 第三天起写入自适应状态，模拟复盘样本足够后系统开始纠偏。
        if day_index >= 2:
            db.save_adaptive_state({
                "last_update": f"{date}T15:13:00",
                "lookback_days": day_index + 1,
                "current_buy_threshold": 62,
                "current_min_score": 57,
                "current_top_k_delta": -1,
                "current_position_scale": 0.8,
                "adjustments": [{
                    "date": date,
                    "overall": {"win_rate": 0.0, "sell_trades": day_index + 1},
                    "adjustments": [
                        {"type": "buy_threshold", "old": 60, "new": 62, "reason": "演练低胜率，提高买入门槛"},
                        {"type": "top_k_delta", "old": 0, "new": -1, "reason": "演练低胜率，减少下一轮候选数量"},
                        {"type": "position_scale", "old": 1.0, "new": 0.8, "reason": "演练低胜率，降低下一轮单笔仓位"},
                    ],
                }],
            })


def run_auto_rehearsal(days: int = 5, start_date: str = None,
                       db_path: str = None, output_dir: str = None,
                       save_report: bool = True, reset: bool = True) -> Dict:
    """
    运行连续多日模拟盘演练

    Args:
        days: 演练天数
        start_date: 起始日期，默认今天
        db_path: 演练SQLite路径，默认data/rehearsal/rehearsal.db
        output_dir: 报告输出目录，默认data/rehearsal/reports
        save_report: 是否保存报告
        reset: 是否在演练前清空隔离数据库，默认True以保证报告可重复

    Returns:
        {"cycles": [...], "report": {...}, "db_path": "..."}
    """
    db_path = db_path or REHEARSAL_DB
    output_dir = output_dir or REHEARSAL_REPORT_DIR
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if reset:
        for candidate in [db_path, f"{db_path}-wal", f"{db_path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)

    dates = _date_list(days, start_date)
    cycles = []
    cumulative_pnl = 0.0

    def fake_prefetch():
        return {"ok": True, "source": "auto_rehearsal"}

    def fake_detect_regime():
        return SimpleNamespace(regime="sideways", confidence=0.66)

    for day_index, date in enumerate(dates):
        state = AutoTraderState(date=date)
        base_ts = 100000 + day_index * 10000
        trade_box: Dict = {}

        def fake_check_stops_once():
            return {"checked": 1 if day_index else 0, "sold": 0, "trades": []}

        def fake_run_scan():
            return _make_scan_result(date)

        def fake_execute_trades():
            nonlocal cumulative_pnl
            trade_result = _save_rehearsal_trade_and_decision(
                db_path=db_path,
                date=date,
                day_index=day_index,
                cumulative_pnl=cumulative_pnl,
            )
            cumulative_pnl += trade_result["pnl_pct"]
            trade_box.update(trade_result)
            return PipelineResult(
                date=date,
                market_status="盘中",
                steps=[StepResult(name="rehearsal_execute", success=True)],
                executed_orders=[{
                    "code": "600519",
                    "action": "BUY",
                    "trade_id": trade_result["buy_id"],
                    "outcome": trade_result["outcome"],
                }],
            )

        def fake_run_review():
            _save_rehearsal_review(db_path, date, day_index, trade_box)
            return PipelineResult(
                date=date,
                market_status="盘后",
                steps=[
                    StepResult(name="daily_review", success=True),
                    StepResult(name="lesson_extract", success=True),
                    StepResult(name="adaptive_evolution", success=day_index >= 2),
                ],
            )

        services = {
            "prefetch": fake_prefetch,
            "detect_regime": fake_detect_regime,
            "check_stops_once": fake_check_stops_once,
            "run_scan": fake_run_scan,
            "execute_trades": fake_execute_trades,
            "run_review": fake_run_review,
        }

        for status, hour, minute, ts, after_review in [
            ("盘前", 8, 50, base_ts, False),
            ("盘中", 10, 0, base_ts + 100, False),
            ("盘后", 15, 10, base_ts + 200, True),
        ]:
            result = run_auto_cycle(
                state=state,
                force_scan=status == "盘中",
                status_override=status,
                trading_day_override=True,
                today_override=date,
                now_override=datetime.strptime(f"{date} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M"),
                now_ts_override=ts,
                after_review_time_override=after_review,
                services=services,
                persist_state=False,
                record_event=True,
                db_path=db_path,
                notify=False,
            )
            cycles.append(result)
            state = AutoTraderState(**result["state"])

    report = generate_ai_trader_report(
        days=len(dates),
        end_date=dates[-1],
        db_path=db_path,
        output_dir=output_dir,
        save=save_report,
    )
    return {
        "status": "ok",
        "days": len(dates),
        "start_date": dates[0],
        "end_date": dates[-1],
        "db_path": db_path,
        "cycles": cycles,
        "report": report,
    }


if __name__ == "__main__":
    result = run_auto_rehearsal()
    print(result["report"]["markdown"])
