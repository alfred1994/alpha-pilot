"""
全链路管道 v2 - 快慢链路分离
====================================================================
架构改造：

慢链路（盘前/盘后）:
  - prefetch(): 数据预热，拉取全市场数据存快照
  - run_review(): LLM复盘，教训总结，参数建议

快链路（盘中，90秒预算）:
  - fast_scan(): 读缓存+实时价格 → 快速打分 → 出TradePlan
    0-10s: 读昨日候选池 + 盘前缓存
    10-30s: 更新实时价格
    30-60s: 并发打分+排序
    60-75s: 风控+仓位计算
    75-90s: 输出TradePlan

所有外部API调用都有超时降级，不会阻塞主链路。
====================================================================
"""
import json
import os
import time
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import concurrent.futures

from scheduler import logger

# P2-8: LLM连续超时自动关闭计数器
_llm_timeout_count = 0
_LLM_AUTO_DISABLE_THRESHOLD = 3  # 连续3次超时自动关闭LLM
from scheduler.market_calendar import is_trading_day, get_market_status
from scheduler.notifier import (
    send_signal_report,
    send_decision_report,
    send_daily_summary,
    send_error_alert,
)

# ── 信号缓存路径 ──
SIGNAL_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "signal_cache.json",
)


@dataclass
class TradePlan:
    """交易计划（替代旧的信号缓存）"""
    date: str = ""
    regime: str = "sideways"
    regime_confidence: float = 0.0
    deadline: str = "14:50"
    orders: list = field(default_factory=list)   # TradeOrder列表
    hold_reasons: dict = field(default_factory=dict)  # code → reason_code
    raw_scores: list = field(default_factory=list)    # 所有候选的打分结果
    cb_opportunities: list = field(default_factory=list)  # 可转债T+0机会
    elapsed: float = 0.0
    errors: list = field(default_factory=list)

    def to_dict(self):
        return {
            "date": self.date,
            "regime": self.regime,
            "regime_confidence": self.regime_confidence,
            "deadline": self.deadline,
            "orders": [
                {
                    "code": o.code,
                    "name": o.name,
                    "action": o.action,
                    "priority": o.priority,
                    "target_weight": o.target_weight,
                    "max_price": o.max_price,
                    "reason": o.reason,
                    "score": o.score,
                    "conviction": o.conviction,
                }
                for o in self.orders
            ],
            "hold_reasons": self.hold_reasons,
            "raw_scores": self.raw_scores,
            "cb_opportunities": self.cb_opportunities,
            "elapsed": self.elapsed,
            "errors": self.errors,
        }


@dataclass
class TradeOrder:
    """交易订单"""
    code: str
    name: str
    action: str        # BUY / SELL / HOLD
    priority: int      # 1=最优先
    target_weight: float  # 目标仓位比例 0.0-1.0
    max_price: float   # 最高买入价/最低卖出价
    reason: str = ""
    score: float = 0.0
    conviction: float = 0.0  # 置信度


@dataclass
class StepResult:
    """单步执行结果"""
    name: str
    success: bool
    elapsed: float = 0.0
    detail: str = ""
    error: str = ""


@dataclass
class PipelineResult:
    """管道执行结果"""
    date: str = ""
    market_status: str = ""
    steps: List[StepResult] = field(default_factory=list)
    candidates: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    executed_orders: list = field(default_factory=list)
    risk_triggered: list = field(default_factory=list)
    order_audit: list = field(default_factory=list)
    total_elapsed: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    @property
    def buy_count(self) -> int:
        return sum(1 for d in self.decisions if d.action == "BUY")

    @property
    def sell_count(self) -> int:
        return sum(1 for d in self.decisions if d.action == "SELL")


# ═══════════════════════════════════════════════════════════════════
# 慢链路：盘前数据预热
# ═══════════════════════════════════════════════════════════════════

def prefetch(candidate_codes: List[str] = None):
    """
    盘前数据预热（慢链路）

    1. 拉取市场级数据（涨停板/北向/两融）
    2. 如果有候选代码，预取K线和新闻
    3. 批量舆情分析
    4. 结果存入快照文件
    """
    from data.snapshot import run_prefetch
    return run_prefetch(candidate_codes)


# ═══════════════════════════════════════════════════════════════════
# 快链路：早盘快速扫描（90秒预算）
# ═══════════════════════════════════════════════════════════════════

def fast_scan(budget_seconds: int = 90) -> TradePlan:
    """
    快链路：早盘快速扫描

    严格时间预算，到点必须出结果。
    所有外部调用有超时降级，不阻塞。

    Args:
        budget_seconds: 总时间预算(秒)

    Returns:
        TradePlan
    """
    global _llm_timeout_count
    from data.snapshot import (
        get_market_snapshot, get_sentiment_snapshot,
        get_candidate_pool, with_timeout,
    )

    plan = TradePlan(date=datetime.now().strftime("%Y-%m-%d"))
    t0 = time.time()

    def elapsed():
        return time.time() - t0

    def remaining():
        return max(0, budget_seconds - elapsed())

    # ── 0-10s: 读取缓存 ──
    logger.info(f"[快链路] 启动 | 预算={budget_seconds}s")

    # 读候选池(优先用缓存)
    candidates = []
    pool = get_candidate_pool(max_age=14400)  # 4小时内有效
    if pool and pool.get("candidates"):
        for c in pool["candidates"]:
            from strategy.stock_picker import Candidate
            candidates.append(Candidate(
                code=c["code"], name=c["name"],
                source=c.get("source", []), score=c.get("score", 0),
            ))
        logger.info(f"[快链路] 从缓存加载候选池: {len(candidates)}只")
    else:
        # 没有缓存，快速选股(带超时)
        logger.info("[快链路] 无候选池缓存，快速选股...")
        from strategy.stock_picker import pick_stocks_by_htsc
        candidates = with_timeout(
            lambda: pick_stocks_by_htsc(max_candidates=10),
            timeout=30,
            fallback=[],
            desc="华泰选股",
        )
        if not candidates:
            logger.warning("[快链路] 华泰选股失败/超时，无候选")
            plan.errors.append("选股失败，无候选股票")
            plan.elapsed = elapsed()
            return plan

    if remaining() < 10:
        logger.warning(f"[快链路] 时间不足({remaining():.0f}s)，跳过打分")
        plan.elapsed = elapsed()
        return plan

    # ── 10-30s: 市场环境 + 实时数据 ──
    # 市场环境(读缓存，不重新计算)
    regime = "sideways"
    regime_conf = 0.5
    market_snap = get_market_snapshot(max_age=1800)  # 30分钟有效
    if market_snap:
        limit_up_count = len(market_snap.get("limit_up", []))
        limit_down_count = len(market_snap.get("limit_down", []))
        logger.info(f"[快链路] 市场快照: 涨停{limit_up_count} 跌停{limit_down_count}")
    else:
        logger.info("[快链路] 无市场快照，用默认值")

    # 尝试读取已有的市场环境
    try:
        from strategy.market_regime import get_regime_history
        history = get_regime_history(days=1)
        if history:
            regime = history[0].regime
            regime_conf = history[0].confidence
            logger.info(f"[快链路] 市场环境: {regime} conf={regime_conf:.0%}")
    except Exception:
        pass

    plan.regime = regime
    plan.regime_confidence = regime_conf

    if remaining() < 20:
        logger.warning(f"[快链路] 时间不足({remaining():.0f}s)，跳过打分")
        plan.elapsed = elapsed()
        return plan

    # ── 30-60s: 并发打分 ──
    sentiment_snap = get_sentiment_snapshot(max_age=3600)
    sentiment_scores = sentiment_snap.get("scores", {}) if sentiment_snap else {}

    scored = _parallel_score(candidates, sentiment_scores, timeout=remaining())

    # 排序
    scored.sort(key=lambda x: x["composite"], reverse=True)
    plan.raw_scores = scored

    if remaining() < 10:
        logger.warning(f"[快链路] 时间不足({remaining():.0f}s)，跳过决策")
        plan.elapsed = elapsed()
        return plan

    # ── 60-75s: 动态TopK决策 ──
    from strategy.regime_config import get_trade_params
    params = get_trade_params(regime)
    top_k = params.get("top_k", 3)
    min_score = params.get("min_score", 58)
    max_weight = params.get("max_weight", 0.20)

    # 动态TopK: 取前K只，但必须过最低质量线
    top_candidates = []
    for s in scored[:top_k]:
        if s["composite"] >= min_score:
            top_candidates.append(s)
        else:
            plan.hold_reasons[s["code"]] = f"HOLD_SCORE_LOW({s['composite']:.0f}<{min_score})"

    # 不在TopK的标记原因
    for s in scored[top_k:]:
        plan.hold_reasons[s["code"]] = f"HOLD_NOT_TOP{top_k}(score={s['composite']:.0f})"

    logger.info(f"[快链路] Top{top_k}候选: {len(top_candidates)}只过线(阈值{min_score})")

    # === P1-4: 快链路集成 LLM 决策（超时降级） ===
    # P2-8: 动态检查LLM可用性（环境变量 + 连续超时计数）
    USE_LLM_IN_FAST = os.environ.get("USE_LLM_IN_FAST", "1") == "1" and _llm_timeout_count < _LLM_AUTO_DISABLE_THRESHOLD

    if USE_LLM_IN_FAST and top_candidates:
        from concurrent.futures import ThreadPoolExecutor as _TPE, TimeoutError as _FTE

        # 尝试加载账户信息（用于LLM决策上下文）
        _positions = {}
        _total_assets = 0.0
        _cash = 0.0
        try:
            from execution.paper_account import PaperAccount
            _acct = PaperAccount()
            _positions = _acct.positions
            _total_assets = _acct.total_assets()
            _cash = _acct.cash
        except Exception as _e:
            logger.debug(f"[快链路] 加载账户信息失败(可忽略): {_e}")

        # P2-12: 创建共享 TradeMemory 实例，避免每次决策都创建新连接
        _shared_memory = None
        try:
            from strategy.memory import TradeMemory
            _shared_memory = TradeMemory()
            _shared_memory.__enter__()
        except Exception as _e:
            logger.debug(f"[快链路] 创建共享记忆失败(可忽略): {_e}")

        llm_count = 0
        for _i, _s in enumerate(top_candidates[:5]):  # Top 5
            try:
                from strategy.decision import DimensionScore as _DS
                _dims = {}
                for _dn, _dv in _s.get("dimensions", {}).items():
                    _dims[_dn] = _DS(
                        name=_dn, score=_dv["score"],
                        confidence=_dv["confidence"], detail=_dv.get("detail", ""),
                    )

                from strategy.llm_trader import make_decision
                with _TPE(max_workers=1) as _executor:
                    _future = _executor.submit(
                        make_decision,
                        code=_s["code"],
                        name=_s["name"],
                        dimensions=_dims,
                        regime=regime,
                        current_positions=_positions,
                        total_assets=_total_assets,
                        cash=_cash,
                        memory=_shared_memory,  # P2-12: 复用共享记忆连接
                    )
                    _decision = _future.result(timeout=25)  # 25秒超时

                if _decision and _decision.action:
                    _llm_timeout_count = 0  # P2-8: LLM成功则重置超时计数
                    # LLM决策成功：用LLM结果覆盖加权打分的action
                    _s["llm_action"] = _decision.action
                    _s["llm_confidence"] = _decision.confidence
                    _s["llm_reason"] = _decision.reason
                    llm_count += 1
                    if _decision.action == "HOLD":
                        # LLM认为该持有，从top_candidates中移除
                        plan.hold_reasons[_s["code"]] = f"HOLD_LLM({ _decision.reason[:50]})"
                    elif _decision.action == "SELL":
                        # LLM建议卖出（持仓股），标记为SELL
                        _s["llm_action"] = "SELL"
            except _FTE:
                _llm_timeout_count += 1
                logger.warning(f"[快链路] LLM决策超时 {_s.get('code', '?')} ({_llm_timeout_count}/{_LLM_AUTO_DISABLE_THRESHOLD})，默认HOLD")
                _s["llm_action"] = "HOLD"  # 超时默认HOLD，不盲目买入
                _s["llm_confidence"] = 0.0
                _s["llm_reason"] = "LLM超时"
                if _llm_timeout_count >= _LLM_AUTO_DISABLE_THRESHOLD:
                    logger.error("[快链路] LLM连续超时，本次扫描自动关闭LLM")
            except Exception as _e:
                logger.warning(f"[快链路] LLM决策失败 {_s.get('code', '?')}: {_e}，默认HOLD")
                _s["llm_action"] = "HOLD"  # 失败默认HOLD
                _s["llm_confidence"] = 0.0
                _s["llm_reason"] = f"LLM失败: {str(_e)[:50]}"

        logger.info(f"[快链路] LLM决策: {llm_count}/{min(5, len(top_candidates))} 成功")

        # P2-12: 关闭共享记忆连接
        try:
            if _shared_memory is not None:
                _shared_memory.__exit__(None, None, None)
                _shared_memory = None
        except Exception:
            pass

        # 过滤：移除LLM判定为HOLD的候选（不再生成BUY订单）
        _llm_hold_codes = {
            _s["code"] for _s in top_candidates
            if _s.get("llm_action") == "HOLD"
        }
        if _llm_hold_codes:
            top_candidates = [_s for _s in top_candidates if _s["code"] not in _llm_hold_codes]
            # 兜底：如果全部被移除，保留分数最高的1只
            if not top_candidates and scored:
                logger.warning("[快链路] LLM判定全部HOLD，降级保留最高分候选")
                top_candidates = scored[:1]
            logger.info(f"[快链路] LLM过滤后剩余: {len(top_candidates)}只")

    # === P1-4 END ===

    # 生成TradeOrder（尊重LLM决策）
    for i, s in enumerate(top_candidates):
        weight = max_weight * (s["composite"] / 100)  # 按分数比例分配仓位
        weight = min(weight, max_weight)
        llm_action = s.get("llm_action", "HOLD")  # LLM失败时默认HOLD，不盲目买入
        llm_reason = s.get("llm_reason", "")
        plan.orders.append(TradeOrder(
            code=s["code"],
            name=s["name"],
            action=llm_action if llm_action in ("BUY", "SELL") else "BUY",
            priority=i + 1,
            target_weight=round(weight, 3),
            max_price=s.get("latest_price", 0) * 1.02,  # 最高价=现价+2%
            reason=llm_reason or s.get("top_signal", ""),
            score=s["composite"],
            conviction=s.get("llm_confidence", s.get("avg_confidence", 0.5)),
        ))

    # === P1-5: 持仓股卖出信号 LLM 决策 ===
    USE_LLM_SELL = os.environ.get("USE_LLM_IN_FAST", "1") == "1"
    if USE_LLM_SELL and remaining() > 10:
        from concurrent.futures import ThreadPoolExecutor as _TPE2, TimeoutError as _FTE2

        # 加载账户获取持仓
        _positions_sell = {}
        _total_assets_sell = 0.0
        _cash_sell = 0.0
        try:
            from execution.paper_account import PaperAccount
            _acct_sell = PaperAccount()
            _positions_sell = _acct_sell.positions
            _total_assets_sell = _acct_sell.total_assets()
            _cash_sell = _acct_sell.cash
        except Exception as _e:
            logger.debug(f"[快链路-卖出] 加载账户失败(可忽略): {_e}")

        if _positions_sell:
            # 已经在top_candidates中的持仓股（已在P1-4中评估），跳过
            _evaluated_codes = {s["code"] for s in top_candidates}
            _sell_eval_count = 0
            _sell_signal_count = 0

            for _pos_code, _pos_info in _positions_sell.items():
                if _pos_code in _evaluated_codes:
                    continue  # 已经评估过
                if remaining() < 5:
                    logger.warning("[快链路-卖出] 时间不足，跳过剩余持仓评估")
                    break

                try:
                    from strategy.decision import compute_dimension_scores, DimensionScore as _DS2
                    _df_sell = None
                    try:
                        from data.history import get_daily
                        _df_sell = get_daily(_pos_code, start_date="20240101")
                    except Exception:
                        pass

                    _dims_sell = compute_dimension_scores(_pos_code, _df_sell)
                    _pos_name = _pos_info.get("name", _pos_code)

                    from strategy.llm_trader import make_decision
                    with _TPE2(max_workers=1) as _ex2:
                        _fut2 = _ex2.submit(
                            make_decision,
                            code=_pos_code,
                            name=_pos_name,
                            dimensions=_dims_sell,
                            regime=regime,
                            current_positions=_positions_sell,
                            total_assets=_total_assets_sell,
                            cash=_cash_sell,
                        )
                        _sell_decision = _fut2.result(timeout=25)  # 25秒超时

                    _sell_eval_count += 1
                    if _sell_decision and _sell_decision.action == "SELL":
                        # LLM建议卖出持仓股，添加SELL订单
                        _pos_shares = _pos_info.get("shares", 0)
                        plan.orders.append(TradeOrder(
                            code=_pos_code,
                            name=_pos_name,
                            action="SELL",
                            priority=len(plan.orders) + 1,
                            target_weight=0,  # 清仓
                            max_price=0,
                            reason=f"LLM卖出: {_sell_decision.reason[:80]}",
                            score=_sell_decision.composite_score,
                            conviction=_sell_decision.confidence,
                        ))
                        _sell_signal_count += 1
                        logger.info(f"[快链路-卖出] LLM建议卖出: {_pos_code} {_pos_name} {_sell_decision.reason[:50]}")
                    elif _sell_decision and _sell_decision.action == "HOLD":
                        plan.hold_reasons[_pos_code] = f"HOLD_LLM_SELL({_sell_decision.reason[:50]})"

                except _FTE2:
                    logger.warning(f"[快链路-卖出] LLM评估超时 {_pos_code}")
                except Exception as _e:
                    logger.warning(f"[快链路-卖出] LLM评估失败 {_pos_code}: {_e}")

            if _sell_eval_count > 0:
                logger.info(f"[快链路-卖出] 持仓评估: {_sell_eval_count}只, 卖出信号: {_sell_signal_count}只")

    # === P1-5 END ===

    # === 可转债T+0扫描 ===
    try:
        from config import CB_T0_ENABLED
        if CB_T0_ENABLED and remaining() > 5:
            from strategy.cb_t0_strategy import scan_and_score, should_buy
            cb_results = scan_and_score()
            cb_buys = []
            for cb in cb_results:
                decision = should_buy(cb)
                if decision["buy"]:
                    cb_buys.append(cb)
                    logger.info(
                        f"[快链路-可转债] 买入信号: {cb['cb_name']}({cb['cb_code']}) "
                        f"分数={cb['total_score']} {decision['reason']}"
                    )
            plan.cb_opportunities = cb_results[:5]  # 保存Top5供报告展示
            if cb_buys:
                logger.info(f"[快链路-可转债] {len(cb_buys)}只买入信号")
    except Exception as e:
        logger.warning(f"[快链路-可转债] 扫描失败(非致命): {e}")

    # ── 75-90s: 输出 ──
    plan.elapsed = elapsed()
    logger.info(f"[快链路] 完成 | 耗时={plan.elapsed:.1f}s | 买入={len(plan.orders)} | HOLD={len(plan.hold_reasons)}")

    # 保存TradePlan
    _save_trade_plan(plan)
    return plan


def _parallel_score(candidates, sentiment_scores, timeout: int = 30) -> list:
    """
    并发打分（带超时）

    Args:
        candidates: 候选列表
        sentiment_scores: 预取的舆情分数
        timeout: 总超时秒数

    Returns:
        打分结果列表
    """
    from strategy.decision import compute_dimension_scores
    from data.history import get_daily
    from config import SIGNAL_WEIGHTS

    def score_one(c):
        """给一只股票打分"""
        code = c.code if hasattr(c, "code") else c.get("code", "")
        name = c.name if hasattr(c, "name") else c.get("name", "")

        # K线(优先用缓存)
        df = None
        try:
            df = get_daily(code, start_date="20240101")
        except Exception:
            pass

        # 5维打分
        dims = compute_dimension_scores(code, df)

        # 用预取的舆情覆盖
        if code in sentiment_scores:
            from strategy.decision import DimensionScore
            s = sentiment_scores[code]
            dims["sentiment"] = DimensionScore(
                name="sentiment", score=s["score"],
                confidence=s["confidence"], detail=s.get("detail", ""),
            )

        # 计算综合分
        total_w = sum(SIGNAL_WEIGHTS.get(k, 0) for k in dims)
        if total_w == 0:
            total_w = 1
        composite = sum(d.score * SIGNAL_WEIGHTS.get(dn, 0) for dn, d in dims.items()) / total_w

        # 找最强信号
        top_dim = max(dims.items(), key=lambda x: x[1].score) if dims else None
        top_signal = f"{top_dim[0]}={top_dim[1].score:.0f}" if top_dim else ""

        avg_conf = sum(d.confidence for d in dims.values()) / len(dims) if dims else 0

        return {
            "code": code,
            "name": name,
            "composite": round(composite, 1),
            "dimensions": {dn: {"score": d.score, "confidence": d.confidence, "detail": d.detail} for dn, d in dims.items()},
            "top_signal": top_signal,
            "avg_confidence": round(avg_conf, 2),
            "latest_price": df["close"].iloc[-1] if df is not None and "close" in df.columns and len(df) > 0 else 0,
        }

    results = []
    # 并发执行，总超时控制
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(score_one, c): c for c in candidates}
        done, _ = concurrent.futures.wait(futures, timeout=timeout)
        for future in done:
            try:
                results.append(future.result())
            except Exception as e:
                c = futures[future]
                code = c.code if hasattr(c, "code") else c.get("code", "")
                logger.warning(f"打分失败 {code}: {e}")

    logger.info(f"并发打分完成: {len(results)}/{len(candidates)}只, 超时={timeout}s")
    return results


def _save_trade_plan(plan: TradePlan):
    """保存TradePlan到文件"""
    path = SIGNAL_CACHE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)
    logger.info(f"TradePlan已保存: {path}")


def _load_trade_plan() -> Optional[dict]:
    """加载今天的TradePlan"""
    if not os.path.exists(SIGNAL_CACHE_FILE):
        return None
    try:
        with open(SIGNAL_CACHE_FILE, "r", encoding="utf-8") as f:
            plan = json.load(f)
        if plan.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return None
        return plan
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# 交易执行（读取TradePlan执行）
# ═══════════════════════════════════════════════════════════════════

def _default_realtime_func():
    """获取默认实时行情函数"""
    from data.realtime import get_realtime
    return get_realtime


def _audit_order(result: PipelineResult, order: dict, status: str,
                 reason: str, **details):
    """记录订单执行审计，便于复盘分析未成交原因"""
    result.order_audit.append({
        "code": order.get("code", ""),
        "name": order.get("name", ""),
        "action": order.get("action", ""),
        "status": status,
        "reason": reason,
        "score": order.get("score", 0),
        "conviction": order.get("conviction", 0),
        "target_weight": order.get("target_weight", 0),
        **details,
    })


def execute_trade_plan(
    plan_data: dict,
    *,
    broker=None,
    realtime_func=None,
    market_status: str = None,
    drawdown_controller=None,
    system_risk_controller=None,
    update_memory: bool = True,
) -> PipelineResult:
    """
    执行指定 TradePlan

    Args:
        plan_data: TradePlan字典
        broker: 交易通道，None时使用默认BrokerAdapter
        realtime_func: 行情函数，签名([code]) -> quote列表
        market_status: 市场状态覆盖，None时自动获取
        drawdown_controller: 回撤控制器，None时创建默认实例
        system_risk_controller: 系统风控控制器，None时创建默认实例
        update_memory: 是否执行决策结果回填

    Returns:
        PipelineResult
    """
    result = PipelineResult(date=datetime.now().strftime("%Y-%m-%d"))
    t0 = time.time()

    if not plan_data:
        result.errors.append("无TradePlan，无法执行")
        return result

    result.market_status = market_status or get_market_status()

    # 加载交易通道（默认模拟盘）
    if broker is None:
        from execution.broker import get_broker_adapter
        broker = get_broker_adapter()
    account = broker.account if hasattr(broker, "account") else broker
    realtime_func = realtime_func or _default_realtime_func()

    # 风控检查
    from risk.position import PositionManager
    from risk.drawdown import DrawdownController
    from risk.system_risk import SystemRiskController
    pm = PositionManager()
    dc = drawdown_controller or DrawdownController()
    sr = system_risk_controller or SystemRiskController()

    total_assets = broker.total_assets()
    dc.update(total_assets)

    # 系统级风控更新
    sr_result = sr.update(total_assets)
    logger.info(
        f"系统风控: 日盈亏={sr_result['daily_pnl']:+.2%} "
        f"连亏={sr_result['consecutive_loss']}天 "
        f"降仓={sr_result['reduce_position']}"
    )

    # 检查熔断
    trading_check = dc.is_trading_allowed()
    if not trading_check["allowed"]:
        result.risk_triggered.append(trading_check["reason"])
        result.errors.append(f"风控熔断: {trading_check['reason']}")
        return result

    # 检查系统级风控
    sr_health = sr.check_system_health()
    if sr_health["system_halted"]:
        result.risk_triggered.append(f"系统停机: {sr_health['issues']}")
        result.errors.append(f"系统停机，禁止交易")
        return result

    # 检查是否允许开新仓（单日亏损熔断）
    buy_allowed = sr.is_new_buy_allowed()
    if not buy_allowed["allowed"]:
        logger.warning(f"系统风控禁止开新仓: {buy_allowed['reason']}")
        result.risk_triggered.append(f"禁止开新仓: {buy_allowed['reason']}")

    # 获取仓位缩放系数
    position_scale = sr.get_position_scale()
    if position_scale < 1.0:
        logger.warning(f"仓位缩放: {position_scale:.0%}")
        result.risk_triggered.append(f"仓位缩放至{position_scale:.0%}")

    # === P0修复: 闭环止损执行 ===
    # 在处理买入订单之前，先检查所有持仓的止损条件并自动执行卖出
    stop_sold_codes = set()  # P1-3: 记录已被止损卖出的股票，避免与LLM卖出冲突
    try:
        from risk.stop_loss import StopLossManager
        stop_loss_manager = StopLossManager()
        positions = broker.get_positions()

        if positions:
            # 获取持仓股票实时价格
            prices = {}
            for code in positions:
                try:
                    quotes = realtime_func([code])
                    if quotes and quotes[0].price > 0:
                        prices[code] = quotes[0].price
                except Exception as e:
                    logger.debug(f"获取 {code} 实时价格失败: {e}")

            # 检查止损条件（内部会自动执行卖出）
            stop_trades = broker.check_stop_conditions(prices)

            # 记录止损执行结果
            for trade in stop_trades:
                stop_sold_codes.add(trade.get("code", ""))  # P1-3
                result.executed_orders.append({
                    "code": trade.get("code", ""),
                    "name": trade.get("name", ""),
                    "side": "SELL",
                    "shares": trade.get("shares", 0),
                    "price": trade.get("price", 0),
                    "reason": trade.get("reason", ""),
                    "profit_pct": trade.get("profit_pct", 0),
                    "type": "stop_loss",
                })
                result.risk_triggered.append(
                    f"自动止损: {trade.get('code', '')} {trade.get('name', '')} "
                    f"{trade.get('shares', 0)}股 @ {trade.get('price', 0):.2f} "
                    f"({trade.get('reason', '')})"
                )

            if stop_trades:
                logger.info(f"闭环止损执行: {len(stop_trades)}笔止损卖出")
                # 止损后总资产可能变化，更新风控
                total_assets = broker.total_assets()
    except Exception as e:
        logger.error(f"闭环止损执行异常: {e}")
        result.errors.append(f"止损检查异常: {e}")

    # 执行订单（SELL + BUY）
    from execution.order import OrderManager
    om = OrderManager(account if hasattr(broker, "account") else None)

    orders = plan_data.get("orders", [])
    for order in orders:
        code = order.get("code", "")
        action = order.get("action", "")

        if action == "SELL":
            # P0-2: 执行LLM建议的卖出订单
            # P1-3: 跳过已被止损卖出的
            if code in stop_sold_codes:
                logger.info(f"SELL跳过: {code} 已被止损卖出")
                _audit_order(result, order, "skipped", "已被止损卖出")
                continue
            if not broker.has_position(code):
                _audit_order(result, order, "skipped", "无持仓可卖")
                continue
            try:
                rt = realtime_func([code])
                price = rt[0].price if rt and rt[0].price > 0 else 0
                if price <= 0:
                    result.errors.append(f"{code} 卖出失败: 无法获取价格")
                    _audit_order(result, order, "failed", "无法获取卖出价格")
                    continue
                reason = order.get("reason", "LLM卖出建议")
                positions = broker.get_positions()
                trade = broker.sell(code, price, positions[code]["shares"], reason=reason)
                if trade:
                    result.executed_orders.append(trade)
                    _audit_order(
                        result, order, "filled", reason,
                        price=price, shares=trade.get("shares", 0),
                    )
                    logger.info(f"LLM卖出执行: {code} @ {price} ({reason})")
                else:
                    _audit_order(result, order, "failed", "卖出接口未返回成交", price=price)
            except Exception as e:
                result.errors.append(f"{code} 卖出失败: {e}")
                _audit_order(result, order, "failed", f"卖出异常: {e}")

        elif action == "BUY":
            code = order["code"]
            target_weight = order.get("target_weight", 0.10)
            max_price = order.get("max_price", 0)

            # 系统级风控: 禁止开新仓时跳过所有BUY
            if not buy_allowed["allowed"]:
                logger.info(f"BUY跳过: {code} - {buy_allowed['reason']}")
                _audit_order(result, order, "blocked", f"禁止开新仓: {buy_allowed['reason']}")
                continue

            # 获取实时价格
            try:
                realtime = realtime_func([code])
                current_price = realtime[0].price if realtime else 0
                if current_price <= 0 and realtime:
                    current_price = realtime[0].close_prev
            except Exception:
                current_price = 0

            if current_price <= 0:
                logger.warning(f"{code} 无法获取实时价格，跳过")
                result.errors.append(f"{code} 跳过: 无法获取价格")
                _audit_order(result, order, "failed", "无法获取买入价格")
                continue

            # 检查价格限制（允许适度偏离，避免盘后/波动误杀）
            if max_price > 0:
                hard_limit = max_price * 1.05
                if current_price > hard_limit:
                    result.errors.append(f"{code} 现价{current_price}超过硬限价{hard_limit:.2f}")
                    _audit_order(
                        result, order, "blocked", "超过硬限价",
                        price=current_price, hard_limit=round(hard_limit, 4),
                    )
                    continue

            # 计算可买金额（含系统级仓位缩放）
            buy_amount = total_assets * target_weight * position_scale
            available_cash = broker.get_cash()
            buy_amount = min(buy_amount, available_cash * 0.95)  # 留5%缓冲

            if buy_amount < 5000:  # 最小买入金额
                result.errors.append(f"{code} 可买金额不足({buy_amount:.0f})")
                _audit_order(
                    result, order, "blocked", "可买金额不足",
                    price=current_price,
                    buy_amount=round(buy_amount, 2),
                    available_cash=round(available_cash, 2),
                )
                continue

            # 计算可买股数(100股整数倍)
            shares = int(buy_amount / current_price / 100) * 100
            if shares < 100:
                _audit_order(
                    result, order, "blocked", "不足一手",
                    price=current_price,
                    buy_amount=round(buy_amount, 2),
                    shares=shares,
                )
                continue

            # 下单（默认模拟账户，经BrokerAdapter封装）
            try:
                buy_order = om.create_buy_order(
                    code=code,
                    name=order.get("name", code),
                    price=current_price,
                    shares=shares,
                    reason="TradePlan执行",
                )
                if hasattr(broker, "account"):
                    om.account = broker.account
                    om.execute_order(buy_order, current_price=current_price)
                else:
                    trade = broker.buy(
                        code=code,
                        name=order.get("name", code),
                        price=current_price,
                        shares=shares,
                        reason="TradePlan执行",
                    )
                    buy_order.status = "filled" if trade else "failed"
                result.executed_orders.append({
                    "order_id": buy_order.order_id,
                    "code": code,
                    "name": buy_order.name,
                    "side": buy_order.side,
                    "shares": buy_order.shares,
                    "price": current_price,
                    "status": buy_order.status,
                })
                _audit_order(
                    result, order, buy_order.status,
                    "模拟买入成交" if buy_order.status == "filled" else "买入未成交",
                    price=current_price,
                    shares=buy_order.shares,
                    buy_amount=round(buy_amount, 2),
                    available_cash=round(available_cash, 2),
                )
                logger.info(f"买入 {code} {shares}股 @ {current_price}")
            except Exception as e:
                result.errors.append(f"{code} 下单失败: {e}")
                _audit_order(result, order, "failed", f"下单异常: {e}")

    result.total_elapsed = time.time() - t0

    # === P0修复: 决策结果回填 ===
    # 执行完交易后，更新待验证决策的outcome
    if update_memory:
        try:
            from strategy.memory import TradeMemory
            with TradeMemory() as memory:
                # 收集已获取的实时价格（止损阶段已获取的）
                # 如果没有，不需要额外获取，update_pending_decisions会自行获取
                memory.update_pending_decisions()
        except Exception as e:
            logger.warning(f"决策结果回填失败(非致命): {e}")

    return result


def execute_trades() -> PipelineResult:
    """
    执行交易（从今日TradePlan文件执行）

    流程:
    1. 加载TradePlan
    2. 风控检查
    3. 下单执行
    """
    plan_data = _load_trade_plan()
    if not plan_data:
        result = PipelineResult(date=datetime.now().strftime("%Y-%m-%d"))
        result.errors.append("无今日TradePlan，请先运行扫描")
        return result
    return execute_trade_plan(plan_data)


# ═══════════════════════════════════════════════════════════════════
# 每日复盘（慢链路，LLM深度参与）

def _load_today_order_audit(date: str, db_path: str = None) -> list:
    """读取当天自动执行审计，供复盘沉淀未成交原因"""
    try:
        from data.database import Database
        with Database(db_path=db_path) as db:
            events = db.get_auto_events(date=date, event_type="auto_cycle", limit=200)
        audit = []
        for event in events:
            details = event.get("details") or {}
            audit.extend(details.get("order_audit") or [])
        return audit
    except Exception as e:
        logger.debug(f"读取订单执行审计失败(非致命): {e}")
        return []

def run_review() -> PipelineResult:
    """每日复盘（慢链路，LLM深度分析）"""
    result = PipelineResult(date=datetime.now().strftime("%Y-%m-%d"))
    t0 = time.time()

    try:
        from review.daily_review import run_daily_review
        review_payload = run_daily_review(return_data=True)
        review_text = review_payload.get("text", "")
        review_data = review_payload.get("data", {})
        review_data["order_audit"] = _load_today_order_audit(result.date)
        try:
            from data.database import Database
            with Database() as db:
                db.save_review_snapshot(result.date, review_data)
        except Exception as e:
            logger.debug(f"订单审计复盘快照回写失败(非致命): {e}")
        result.review_text = review_text
        result.steps.append(StepResult(
            name="复盘", success=True,
            elapsed=time.time() - t0,
            detail=str(review_text)[:500],
        ))

        adaptive_data = {}
        try:
            from strategy.adaptive import AdaptiveEngine
            adaptive = AdaptiveEngine()
            adaptive_data = adaptive.analyze_and_adjust(days=10)
            result.steps.append(StepResult(
                name="自适应分析", success=True,
                elapsed=time.time() - t0,
                detail=f"status={adaptive_data.get('status', 'unknown')}",
            ))
        except Exception as e:
            logger.warning(f"自适应分析失败(非致命): {e}")

        try:
            from review.llm_review import (
                generate_llm_review,
                extract_and_save_lessons,
                run_decision_evolution_analysis,
            )
            llm_analysis = generate_llm_review(
                result.date,
                review_data,
                adaptive_data=adaptive_data,
            )
            lesson_count = extract_and_save_lessons(
                review_data,
                llm_analysis=llm_analysis,
                adaptive_data=adaptive_data,
            )
            evolution = run_decision_evolution_analysis()
            result.steps.append(StepResult(
                name="LLM复盘进化", success=True,
                elapsed=time.time() - t0,
                detail=f"教训{lesson_count}条 决策样本{evolution.get('total_decisions', 0)}条",
            ))
        except Exception as e:
            logger.warning(f"LLM复盘进化失败(非致命): {e}")
    except Exception as e:
        result.errors.append(f"复盘失败: {e}")

    # 【TaskC】收盘时写入每日快照
    try:
        from execution.broker import get_broker_adapter
        broker = get_broker_adapter()
        account = broker.account if hasattr(broker, "account") else broker
        today = datetime.now().strftime("%Y-%m-%d")

        # 尝试获取市场环境
        market_regime = ""
        regime_confidence = 0.0
        try:
            from strategy.market_regime import get_regime_history
            history = get_regime_history(days=1)
            if history:
                market_regime = history[0].regime
                regime_confidence = history[0].confidence
        except Exception:
            pass

        # 获取持仓市值（用买入价估算）
        market_value = broker.market_value() if hasattr(broker, "market_value") else 0
        total_assets = broker.total_assets()

        from data.database import Database
        with Database() as db:
            db.save_daily_snapshot({
                "date": today,
                "cash": broker.get_cash(),
                "market_value": market_value,
                "total_assets": total_assets,
                "position_count": len(broker.get_positions()),
                "market_regime": market_regime,
                "regime_confidence": regime_confidence,
            })
        logger.info(f"[收盘] 每日快照已写入: 资产={total_assets:,.0f} 持仓={len(broker.get_positions())}只")
    except Exception as e:
        logger.warning(f"[收盘] 每日快照写入失败(非致命): {e}")

    result.total_elapsed = time.time() - t0
    return result


# ═══════════════════════════════════════════════════════════════════
# 格式化报告
# ═══════════════════════════════════════════════════════════════════

def format_trade_plan_report(plan_data: dict) -> str:
    """格式化TradePlan报告"""
    if not plan_data:
        return "无TradePlan数据"

    lines = [
        "=" * 60,
        f"📋 TradePlan | {plan_data.get('date', 'N/A')}",
        "=" * 60,
        f"市场环境: {plan_data.get('regime', 'N/A')} (置信度{plan_data.get('regime_confidence', 0):.0%})",
        f"执行截止: {plan_data.get('deadline', 'N/A')}",
        f"耗时: {plan_data.get('elapsed', 0):.1f}s",
        "",
    ]

    orders = plan_data.get("orders", [])
    if orders:
        lines.append("🟢 买入计划:")
        for o in orders:
            lines.append(f"  #{o['priority']} {o['code']} {o['name']} "
                         f"仓位{o['target_weight']:.1%} 最高价{o['max_price']:.2f} "
                         f"分数{o['score']:.0f} 置信度{o['conviction']:.0%}")
            lines.append(f"     理由: {o.get('reason', '')}")
    else:
        lines.append("⚪ 今日无买入计划")

    # HOLD原因统计
    hold_reasons = plan_data.get("hold_reasons", {})
    if hold_reasons:
        lines.append("")
        lines.append(f"🟡 HOLD原因 ({len(hold_reasons)}只):")
        reason_counts = {}
        for code, reason in hold_reasons.items():
            # 提取reason code
            rc = reason.split("(")[0] if "(" in reason else reason
            reason_counts[rc] = reason_counts.get(rc, 0) + 1
        for rc, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {rc}: {count}只")

    # 可转债T+0机会
    cb_opps = plan_data.get("cb_opportunities", [])
    if cb_opps:
        lines.append("")
        lines.append(f"🔵 可转债T+0机会 ({len(cb_opps)}只):")
        for cb in cb_opps[:3]:
            icon = "🟢" if cb.get("total_score", 0) >= 80 else "🔵" if cb.get("total_score", 0) >= 70 else "⚪"
            lines.append(
                f"  {icon} {cb.get('cb_name', '')}({cb.get('cb_code', '')}) "
                f"分数={cb.get('total_score', 0):.0f} "
                f"溢价率={cb.get('premium_rate', 0):.1f}% "
                f"正股{cb.get('stock_name', '')}({cb.get('stock_change_pct', 0):+.1f}%)"
            )

    # 错误
    errors = plan_data.get("errors", [])
    if errors:
        lines.append("")
        lines.append(f"🔴 错误 ({len(errors)}):")
        for e in errors[:5]:
            lines.append(f"  - {e}")

    lines.append("=" * 60)

    # 汇总
    buy_count = len(orders)
    hold_count = len(hold_reasons)
    lines.append(f"汇总: 买入计划{buy_count} 持有{hold_count}")

    return "\n".join(lines)


def format_pipeline_report(result: PipelineResult) -> str:
    """格式化管道报告"""
    lines = [
        "=" * 60,
        f"A股量化系统 | {result.date}",
        "=" * 60,
    ]

    for step in result.steps:
        status = "OK" if step.success else "FAIL"
        lines.append(f"  [{status}] {step.name:20s} {step.elapsed:.1f}s")
        if step.detail:
            lines.append(f"        {step.detail[:100]}")

    if result.errors:
        lines.append(f"\n错误({len(result.errors)}):")
        for e in result.errors[:5]:
            lines.append(f"  - {e}")

    lines.append(f"\n总耗时: {result.total_elapsed:.1f}s")
    lines.append("=" * 60)
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 旧接口兼容
# ═══════════════════════════════════════════════════════════════════

def run_scan() -> PipelineResult:
    """扫描信号（兼容旧接口，内部调fast_scan）"""
    result = PipelineResult(date=datetime.now().strftime("%Y-%m-%d"))
    t0 = time.time()

    plan = fast_scan(budget_seconds=90)
    result.candidates = plan.raw_scores

    from strategy.decision import TradeDecision, DimensionScore
    for s in plan.raw_scores:
        dims = {}
        for dn, dv in s.get("dimensions", {}).items():
            dims[dn] = DimensionScore(name=dn, score=dv["score"], confidence=dv["confidence"], detail=dv.get("detail", ""))
        action = "BUY" if any(o.code == s["code"] for o in plan.orders) else "HOLD"
        result.decisions.append(TradeDecision(
            code=s["code"], name=s["name"], action=action,
            composite_score=s["composite"],
            confidence=s.get("avg_confidence", 0.5),
            dimensions=dims,
            reason=plan.hold_reasons.get(s["code"], ""),
        ))

    result.total_elapsed = time.time() - t0
    return result


def run_daily_pipeline() -> PipelineResult:
    """全链路"""
    scan = run_scan()
    exec_result = execute_trades()
    scan.executed_orders = exec_result.executed_orders
    scan.risk_triggered = exec_result.risk_triggered
    scan.errors.extend(exec_result.errors)
    return scan
