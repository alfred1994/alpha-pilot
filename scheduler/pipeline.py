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
import hashlib
import uuid
from datetime import datetime
from scheduler.market_calendar import _now_bj
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import concurrent.futures

from scheduler import logger
from config import (
    FAST_SCAN_BUDGET_SECONDS,
    FAST_SCAN_LLM_DECISION_TIMEOUT,
    FAST_SCAN_LLM_RESULT_TIMEOUT,
    FAST_SCAN_SELL_LLM_TIMEOUT,
    FAST_SCAN_STOCK_PICK_TIMEOUT,
)

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
    scan_id: str = ""
    regime: str = "sideways"
    regime_confidence: float = 0.0
    strategy_version: str = ""
    strategy_intent: str = ""
    deadline: str = "14:50"
    orders: list = field(default_factory=list)   # TradeOrder列表
    hold_reasons: dict = field(default_factory=dict)  # code → reason_code
    raw_scores: list = field(default_factory=list)    # 所有候选的打分结果
    cb_opportunities: list = field(default_factory=list)  # 可转债T+0机会
    candidate_pool: dict = field(default_factory=dict)  # 候选池版本/生成时间
    market_snapshot: dict = field(default_factory=dict)  # 市场数据来源/新鲜度
    elapsed: float = 0.0
    errors: list = field(default_factory=list)

    def to_dict(self):
        return {
            "date": self.date,
            "scan_id": self.scan_id,
            "regime": self.regime,
            "regime_confidence": self.regime_confidence,
            "strategy_version": self.strategy_version,
            "strategy_intent": self.strategy_intent,
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
                    "allow_t0": o.allow_t0,
                    "trade_unit": o.trade_unit,
                    "market_regime": o.market_regime,
                    "dimensions": o.dimensions,
                    "signal_detail": o.signal_detail,
                    "decision_id": o.decision_id,
                }
                for o in self.orders
            ],
            "hold_reasons": self.hold_reasons,
            "raw_scores": self.raw_scores,
            "cb_opportunities": self.cb_opportunities,
            "candidate_pool": self.candidate_pool,
            "market_snapshot": self.market_snapshot,
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
    allow_t0: bool = False   # 仅由证券元数据明确标记可当日回转
    trade_unit: int = 100    # 证券最小交易单位；可转债为10张
    market_regime: str = ""
    dimensions: dict = field(default_factory=dict)
    signal_detail: str = ""
    decision_id: int = None


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
    trade_plan: dict = field(default_factory=dict)
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
# 快链路：早盘快速扫描（480秒预算）
# ═══════════════════════════════════════════════════════════════════

def fast_scan(
    budget_seconds: int = FAST_SCAN_BUDGET_SECONDS,
    candidate_codes: Optional[List[str]] = None,
    candidate_items: Optional[List[dict]] = None,
) -> TradePlan:
    """
    快链路：早盘快速扫描

    严格时间预算，到点必须出结果。
    所有外部调用有超时降级，不阻塞。

    时间分配(默认480s):
      0-90s:   选股(缓存优先)
      90-280s: 舆情分析(MiMo LLM，主备均允许完整返回)
      280-370s: 并发打分
      370-480s: LLM决策与计划生成

    Args:
        budget_seconds: 总时间预算(秒)
        candidate_codes: 候选代码白名单，仅扫描这些股票
        candidate_items: 外部传入的候选项，用于盘中救援扫描

    Returns:
        TradePlan
    """
    global _llm_timeout_count
    from data.snapshot import (
        get_market_snapshot_status, get_sentiment_snapshot,
        get_candidate_pool_status, save_candidate_pool, with_timeout,
    )

    scan_date = _now_bj().strftime("%Y-%m-%d")
    plan = TradePlan(
        date=scan_date,
        scan_id=f"{scan_date}T{datetime.now().strftime('%H%M%S%f')}-{uuid.uuid4().hex[:12]}",
    )
    t0 = time.time()

    def elapsed():
        return time.time() - t0

    def remaining():
        return max(0, budget_seconds - elapsed())

    # ── 0-10s: 读取缓存 ──
    logger.info(f"[快链路] 启动 | 预算={budget_seconds}s")

    candidate_filter = {
        str(code).strip()
        for code in (candidate_codes or [])
        if str(code).strip()
    }

    # 读候选池(优先用缓存)
    candidates = []
    if candidate_items:
        from strategy.stock_picker import Candidate
        for c in candidate_items:
            code = str(c.get("code", "")).strip()
            if not code:
                continue
            if candidate_filter and code not in candidate_filter:
                continue
            candidates.append(Candidate(
                code=code,
                name=str(c.get("name") or code),
                source=c.get("source") or ["盘中观察池"],
                score=float(c.get("score") or 0),
            ))
        plan.candidate_pool = {
            "source": "external_items",
            "fresh": True,
            "age_seconds": 0.0,
            "version": "",
            "generated_at": "",
            "candidate_count": len(candidates),
            "refresh_attempted": False,
            "refresh_error": "",
        }
        logger.info(f"[快链路] 使用外部候选白名单: {len(candidates)}只")
    else:
        pool_status = get_candidate_pool_status(max_age=1800)
        pool = pool_status.get("snapshot") or {}
        pool_is_fresh = bool(pool_status.get("fresh") and pool.get("candidates"))

        def _load_pool_rows(pool_data):
            loaded = []
            from strategy.stock_picker import Candidate
            for c in pool_data.get("candidates") or []:
                _code = str(c.get("code", "")).strip()
                # 防御: 过滤北交所/科创板(缓存可能含脏数据)
                if not _code or _code.startswith(("8", "4", "920", "688")):
                    continue
                loaded.append(Candidate(
                    code=_code, name=c.get("name", _code),
                    source=c.get("source", []), score=c.get("score", 0),
                ))
            return loaded

        if pool_is_fresh:
            candidates = _load_pool_rows(pool)
            plan.candidate_pool = {
                key: pool_status.get(key)
                for key in (
                    "source", "fresh", "age_seconds", "max_age_seconds",
                    "refresh_attempted", "refresh_error", "version",
                    "generated_at", "candidate_count",
                )
            }
            logger.info(
                "[快链路] 从候选池缓存加载: %s只 | 版本=%s 生成=%s 年龄=%ss",
                len(candidates), pool_status.get("version", "legacy"),
                pool_status.get("generated_at", "unknown"), pool_status.get("age_seconds"),
            )
        else:
            # 缓存过期或不存在时刷新候选池；刷新失败再显式回退到旧池。
            logger.info(
                "[快链路] 候选池需要刷新: source=%s age=%ss version=%s",
                pool_status.get("source"), pool_status.get("age_seconds"),
                pool_status.get("version", "legacy"),
            )
            # 低位潜力股选股 (多维数据源：龙虎榜/北向资金/业绩预增/机构调研/回撤反弹)
            from strategy.stock_picker import pick_stocks
            from strategy.stock_picker import get_sentiment_boost
            sentiment_boost = get_sentiment_boost()
            if sentiment_boost:
                logger.info(f"[快链路] 舆情加成: {sentiment_boost}")

            # 低位潜力股模式：专注低位股，避免追高
            candidates = with_timeout(
                lambda: pick_stocks(
                    top_n=10,
                    use_limit_up=False,       # 关闭涨停板（无法买入）
                    use_dragon_tiger=True,    # 启用龙虎榜
                    use_north_flow=True,      # 启用北向资金
                    use_performance=True,     # 启用业绩预增
                    use_survey=True,          # 启用机构调研
                    use_volume=False,         # 关闭异动放量
                    use_financing=False,      # 关闭融资融券
                    use_unlock_alert=False,   # 关闭解禁预警
                    use_low_position=True,    # 启用低位潜力股
                    low_position_mode=True,   # 低位模式
                    sentiment_boost=sentiment_boost,  # 舆情加成
                ),
                timeout=FAST_SCAN_STOCK_PICK_TIMEOUT,
                fallback=[],
                desc="多维选股",
            )
            if candidates:
                saved_pool = save_candidate_pool(candidates) or {}
                plan.candidate_pool = {
                    "source": "refreshed",
                    "fresh": True,
                    "age_seconds": 0.0,
                    "max_age_seconds": 1800,
                    "refresh_attempted": True,
                    "refresh_error": "",
                    "version": saved_pool.get("version", ""),
                    "generated_at": saved_pool.get("generated_at", ""),
                    "candidate_count": len(saved_pool.get("candidates") or candidates),
                }
                logger.info(
                    "[快链路] 候选池刷新完成: %s只 | 版本=%s",
                    len(candidates), plan.candidate_pool.get("version", ""),
                )
            elif pool.get("candidates"):
                candidates = _load_pool_rows(pool)
                plan.candidate_pool = {
                    key: pool_status.get(key)
                    for key in (
                        "source", "fresh", "age_seconds", "max_age_seconds",
                        "refresh_attempted", "refresh_error", "version",
                        "generated_at", "candidate_count",
                    )
                }
                plan.candidate_pool.update({
                    "source": "stale_fallback",
                    "refresh_attempted": True,
                    "refresh_error": "refresh_failed_or_timeout",
                })
                plan.errors.append(
                    "候选池刷新失败，使用过期候选池"
                    f"({pool_status.get('age_seconds')}s)"
                )
                logger.warning("[快链路] 候选池刷新失败，回退过期缓存: %s只", len(candidates))
            else:
                logger.warning("[快链路] 候选池刷新失败且无旧缓存")
                plan.candidate_pool = {
                    key: pool_status.get(key)
                    for key in (
                        "source", "fresh", "age_seconds", "max_age_seconds",
                        "refresh_attempted", "refresh_error", "version",
                        "generated_at", "candidate_count",
                    )
                }
                plan.candidate_pool["refresh_attempted"] = True
                plan.errors.append("选股失败，无候选股票")
                plan.elapsed = elapsed()
                _save_trade_plan(plan)
                return plan

    if candidate_filter:
        before_filter = len(candidates)
        candidates = [
            c for c in candidates
            if (c.code if hasattr(c, 'code') else c.get('code', '')) in candidate_filter
        ]
        logger.info(f"[快链路] 候选白名单过滤: {before_filter}->{len(candidates)}")
        if not candidates:
            plan.errors.append("候选白名单无匹配股票")
            plan.elapsed = elapsed()
            _save_trade_plan(plan)
            return plan

    if remaining() < 10:
        logger.warning(f"[快链路] 时间不足({remaining():.0f}s)，跳过打分")
        plan.elapsed = elapsed()
        _save_trade_plan(plan)
        return plan

    # ── 10-30s: 市场环境 + 实时数据 ──
    # 市场环境(读缓存，不重新计算)
    regime = "sideways"
    regime_conf = 0.5
    market_info = get_market_snapshot_status(
        max_age=1800,
        refresh=True,
        refresh_timeout=min(20, max(1, int(remaining()))),
    )
    market_snap = market_info.get("snapshot")
    plan.market_snapshot = {
        key: value for key, value in market_info.items() if key != "snapshot"
    }
    if market_snap:
        limit_up_count = len(market_snap.get("limit_up", []))
        limit_down_count = len(market_snap.get("limit_down", []))
        logger.info(
            "[快链路] 市场快照: 涨停%s 跌停%s | 来源=%s 新鲜=%s 年龄=%ss",
            limit_up_count,
            limit_down_count,
            market_info.get("source", "unknown"),
            market_info.get("fresh", False),
            market_info.get("age_seconds"),
        )
        if not market_info.get("fresh"):
            plan.errors.append(
                "市场快照刷新失败，当前使用过期缓存"
                f"({market_info.get('age_seconds')}s)"
            )
    else:
        logger.warning(
            "[快链路] 市场快照不可用，市场级字段保持未知，不伪装为空列表；来源=%s 错误=%s",
            market_info.get("source", "unavailable"),
            market_info.get("refresh_error", ""),
        )
        plan.errors.append("市场快照不可用，市场级数据未知，使用中性环境")

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
        _save_trade_plan(plan)
        return plan

    # ── 30-60s: 并发打分 ──
    sentiment_snap = get_sentiment_snapshot(max_age=3600)
    if not sentiment_snap:
        # 舆情缓存过期，批量预取（避免每只股票单独调LLM）
        logger.info("[快链路] 舆情缓存过期，批量预取...")
        from data.snapshot import prefetch_sentiment
        codes = [c.code if hasattr(c, 'code') else c.get('code', '') for c in candidates]
        sentiment_snap = prefetch_sentiment(codes)
    sentiment_scores = sentiment_snap.get("scores", {}) if sentiment_snap else {}
    logger.info(f"[快链路] 舆情覆盖: {len(sentiment_scores)}/{len(candidates)}只")
    data_quality = {
        "market_snapshot": {
            "source": market_info.get("source", "unavailable"),
            "fresh": bool(market_info.get("fresh")),
            "age_seconds": market_info.get("age_seconds"),
        },
        "candidate_pool": {
            "source": plan.candidate_pool.get("source", "unknown"),
            "fresh": bool(plan.candidate_pool.get("fresh")),
            "age_seconds": plan.candidate_pool.get("age_seconds"),
        },
        "sentiment": {
            "available": bool(sentiment_scores),
            "coverage": round(len(sentiment_scores) / len(candidates), 2) if candidates else 0,
        },
        "degraded": [],
    }
    if not market_info.get("fresh"):
        data_quality["degraded"].append("市场涨跌停/资金快照过期或缺失")
    if not plan.candidate_pool.get("fresh"):
        data_quality["degraded"].append("候选池过期或使用回退数据")
    if not sentiment_scores:
        data_quality["degraded"].append("舆情数据缺失")
    elif len(sentiment_scores) < len(candidates):
        data_quality["degraded"].append("部分股票舆情未返回")
    if data_quality["degraded"]:
        logger.warning("[快链路] 数据降级进入LLM上下文: %s", data_quality["degraded"])

    # ML信号批量预取：pooled影子模型可用时走本地库批量推理，避免逐股
    # 临时训练；不可用时各候选自动回退原逐股路径，行为与旧版一致。
    try:
        from strategy.qlib_signal import prefetch_pooled_signals
        prefetch_pooled_signals(
            [c.code if hasattr(c, "code") else c.get("code", "") for c in candidates]
        )
    except Exception as exc:
        logger.warning(f"[快链路] pooled批量预取跳过: {exc}")

    scored = _parallel_score(candidates, sentiment_scores, timeout=remaining())

    # 排序
    scored.sort(key=lambda x: x["composite"], reverse=True)
    plan.raw_scores = scored

    if remaining() < 30:
        logger.warning(f"[快链路] 时间不足({remaining():.0f}s)，跳过决策")
        plan.elapsed = elapsed()
        _save_trade_plan(plan)
        return plan

    # ── 60-75s: 读取日终 AI 策略指令并决定 TopK ──
    from strategy.regime_config import get_trade_params
    from strategy.directive import get_effective_trade_policy

    directive = get_effective_trade_policy(plan.date, regime)
    if directive:
        params = dict(directive["params"])
        plan.strategy_version = directive["version"]
        plan.strategy_intent = directive.get("intent", "")
        logger.info(
            "[快链路] AI策略指令: %s | %s | Top%s 最低%s 仓位上限%.0f%%",
            plan.strategy_version,
            plan.strategy_intent,
            params["top_k"],
            params["min_score"],
            params["max_weight"] * 100,
        )
    else:
        # 兼容首次运行或日终 LLM 不可用场景；一旦存在 AI 指令，旧自适应
        # 状态不会再直接支配盘中策略。
        params = get_trade_params(regime)
        plan.strategy_version = "legacy-adaptive"
        plan.strategy_intent = "兼容自适应策略"
    top_k = params.get("top_k", 3)
    min_score = params.get("min_score", 58)
    max_weight = params.get("max_weight", 0.20)

    # 影子策略：全部变体在同一候选池与同一份打分上推导决策并落库，
    # 复用反事实回填的净收益计算对比指标；晋级只产生候选行，不改变
    # 真实下单。旧 A/B 框架长期 running 的实验顺带过期清理。
    try:
        from strategy.shadow_traders import record_daily_decisions
        from strategy.shadow_eval import expire_stale_ab_tests
        from data.database import Database
        base_params = {"top_k": int(top_k), "min_score": float(min_score), "max_weight": float(max_weight)}
        with Database() as _shadow_db:
            record_daily_decisions(
                _shadow_db, plan.date, regime,
                plan.strategy_version or "legacy-adaptive", scored, base_params,
            )
            expire_stale_ab_tests(_shadow_db)
    except Exception as e:
        logger.debug(f"[影子] 决策记录失败(非致命): {e}")

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

        if _shared_memory is not None:
            for _s in top_candidates[:5]:
                try:
                    _s["memory_context"] = _shared_memory.recall(
                        stock_code=_s["code"],
                        regime=regime,
                    )
                except Exception as _e:
                    logger.debug(f"[快链路] 预取记忆失败 {_s.get('code', '?')}: {_e}")

        llm_count = 0
        # 并发LLM决策
        def _parallel_llm_decision(_s):
            try:
                from strategy.decision import DimensionScore as _DS
                _dims = {}
                for _dn, _dv in _s.get("dimensions", {}).items():
                    _dims[_dn] = _DS(
                        name=_dn, score=_dv["score"],
                        confidence=_dv["confidence"], detail=_dv.get("detail", ""),
                    )

                from strategy.llm_trader import make_decision
                return _s, make_decision(
                    code=_s["code"],
                    name=_s["name"],
                    dimensions=_dims,
                    regime=regime,
                    memory_context=_s.get("memory_context", ""),
                    current_positions=_positions,
                    total_assets=_total_assets,
                    cash=_cash,
                    strategy_directive=directive,
                    data_quality=data_quality,
                    llm_retries=0,
                    llm_timeout=FAST_SCAN_LLM_DECISION_TIMEOUT,
                )
            except Exception as _e:
                logger.warning(f"[快链路] LLM决策异常 {_s.get('code', '?')}: {_e}")
                return _s, None

        _llm_executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        try:
            _llm_futures = {
                _llm_executor.submit(_parallel_llm_decision, _s): _s
                for _s in top_candidates[:5]
            }
            for _future in concurrent.futures.as_completed(
                _llm_futures,
                timeout=FAST_SCAN_LLM_RESULT_TIMEOUT,
            ):
                try:
                    _s, _decision = _future.result()
                    if _decision and _decision.action:
                        # LLM决策成功：用LLM结果覆盖加权打分的action
                        _s["llm_action"] = _decision.action
                        _s["llm_confidence"] = _decision.confidence
                        _s["llm_reason"] = _decision.reason
                        _s["decision_id"] = getattr(_decision, "decision_id", None)
                        if _decision.confidence > 0:
                            _llm_timeout_count = 0  # P2-8: LLM成功则重置超时计数
                            llm_count += 1
                        else:
                            _llm_timeout_count += 1
                        if _decision.action == "HOLD":
                            plan.hold_reasons[_s["code"]] = f"HOLD_LLM({ _decision.reason[:50]})"
                        elif _decision.action == "SELL":
                            _s["llm_action"] = "SELL"
                except Exception as _e:
                    logger.warning(f"[快链路] LLM并发结果处理失败: {_e}")
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"[快链路] LLM决策超时({FAST_SCAN_LLM_RESULT_TIMEOUT}s)，已使用已有结果"
            )
        except Exception as _e:
            logger.warning(f"[快链路] LLM决策异常: {_e}")
        finally:
            # 不在收集超时后等待未完成的模型线程，防止其侵占后续执行窗口。
            _llm_executor.shutdown(wait=False, cancel_futures=True)

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
            logger.info(f"[快链路] LLM过滤后剩余: {len(top_candidates)}只")

    # === P1-4 END ===

    # 生成TradeOrder（尊重LLM决策）
    for i, s in enumerate(top_candidates):
        weight = max_weight * (s["composite"] / 100)  # 按分数比例分配仓位
        weight = min(weight, max_weight)
        llm_action = s.get("llm_action", "HOLD")  # LLM失败/未返回时默认HOLD，不盲目买入
        llm_reason = s.get("llm_reason", "")
        if llm_action not in ("BUY", "SELL"):
            plan.hold_reasons[s["code"]] = f"HOLD_NO_BUY_DECISION({llm_reason[:50]})"
            continue
        plan.orders.append(TradeOrder(
            code=s["code"],
            name=s["name"],
            action=llm_action,
            priority=i + 1,
            target_weight=round(weight, 3),
            max_price=s.get("latest_price", 0) * 1.02,  # 最高价=现价+2%
            reason=llm_reason or s.get("top_signal", ""),
            score=s["composite"],
            conviction=s.get("llm_confidence", s.get("avg_confidence", 0.5)),
            market_regime=regime,
            dimensions=s.get("dimensions", {}),
            signal_detail=s.get("top_signal", ""),
            decision_id=s.get("decision_id"),
        ))

    # === P1-5: 持仓股卖出信号 LLM 决策 ===
    USE_LLM_SELL = os.environ.get("USE_LLM_IN_FAST", "1") == "1"
    if USE_LLM_SELL and remaining() > 10:
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
            _sell_memory = None
            try:
                from strategy.memory import TradeMemory
                _sell_memory = TradeMemory()
                _sell_memory.__enter__()
            except Exception as _e:
                logger.debug(f"[快链路-卖出] 创建共享记忆失败(可忽略): {_e}")

            try:
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
                        _sell_decision = make_decision(
                            code=_pos_code,
                            name=_pos_name,
                            dimensions=_dims_sell,
                            regime=regime,
                            current_positions=_positions_sell,
                            total_assets=_total_assets_sell,
                            cash=_cash_sell,
                            strategy_directive=directive,
                            data_quality=data_quality,
                            memory=_sell_memory,
                            llm_retries=0,
                            llm_timeout=FAST_SCAN_SELL_LLM_TIMEOUT,
                        )

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
                            market_regime=regime,
                            dimensions={
                                k: {"score": v.score, "confidence": v.confidence, "detail": v.detail}
                                for k, v in (_sell_decision.dimensions or {}).items()
                            },
                            signal_detail=_sell_decision.reason,
                        ))
                            _sell_signal_count += 1
                            logger.info(f"[快链路-卖出] LLM建议卖出: {_pos_code} {_pos_name} {_sell_decision.reason[:50]}")
                        elif _sell_decision and _sell_decision.action == "HOLD":
                            plan.hold_reasons[_pos_code] = f"HOLD_LLM_SELL({_sell_decision.reason[:50]})"

                    except Exception as _e:
                        logger.warning(f"[快链路-卖出] LLM评估失败 {_pos_code}: {_e}")
            finally:
                try:
                    if _sell_memory is not None:
                        _sell_memory.__exit__(None, None, None)
                except Exception:
                    pass

            if _sell_eval_count > 0:
                logger.info(f"[快链路-卖出] 持仓评估: {_sell_eval_count}只, 卖出信号: {_sell_signal_count}只")

    # === P1-5 END ===

    # === 可转债T+0扫描 ===
    try:
        from config import CB_T0_ENABLED
        if CB_T0_ENABLED and remaining() > 5 and not (candidate_filter or candidate_items):
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
                # 可转债是明确允许T+0的独立品种，必须生成可执行订单，
                # 否则看板上的机会只是“展示信号”而不是交易计划。
                existing_codes = {o.code for o in plan.orders}
                for cb in cb_buys:
                    cb_code = str(cb.get("cb_code", "")).strip()
                    if not cb_code or cb_code in existing_codes:
                        continue
                    decision = should_buy(cb)
                    plan.orders.append(TradeOrder(
                        code=cb_code,
                        name=cb.get("cb_name", cb_code),
                        action="BUY",
                        priority=len(plan.orders) + 1,
                        target_weight=min(float(decision.get("position_pct", 0)), 0.20),
                        max_price=float(cb.get("cb_price", 0) or 0) * 1.02,
                        reason=f"可转债T+0: {decision.get('reason', '')}",
                        score=float(cb.get("total_score", 0) or 0),
                        conviction=min(1.0, float(cb.get("total_score", 0) or 0) / 100),
                        allow_t0=True,
                        trade_unit=10,
                        market_regime=regime,
                        dimensions={"convertible_bond": cb.get("details", {})},
                        signal_detail=f"CB评分={cb.get('total_score', 0)} 溢价={cb.get('premium_rate', 0)}%",
                    ))
                    existing_codes.add(cb_code)
                logger.info(f"[快链路-可转债] {len(cb_buys)}只买入信号, 已接入订单{len(plan.orders)}")
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
    from strategy.decision import compute_dimension_scores, get_effective_signal_weights
    from data.history import get_daily
    from config import SIGNAL_WEIGHTS

    def score_one(c):
        """给一只股票打分"""
        code = c.code if hasattr(c, "code") else c.get("code", "")
        name = c.name if hasattr(c, "name") else c.get("name", "")

        try:
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
            effective_weights = get_effective_signal_weights(dims, SIGNAL_WEIGHTS)
            total_w = sum(effective_weights.values())
            if total_w == 0:
                total_w = 1
            composite = sum(
                d.score * effective_weights.get(dn, 0)
                for dn, d in dims.items()
            ) / total_w
            degraded_dimensions = [
                name for name in dims
                if SIGNAL_WEIGHTS.get(name, 0) > 0 and name not in effective_weights
            ]

            # 找最强信号
            active_dims = [
                item for item in dims.items() if item[0] in effective_weights
            ]
            top_dim = max(active_dims, key=lambda x: x[1].score) if active_dims else None
            top_signal = f"{top_dim[0]}={top_dim[1].score:.0f}" if top_dim else ""

            avg_conf = sum(
                dim.confidence * effective_weights.get(name, 0)
                for name, dim in dims.items()
            ) / total_w

            return {
                "code": code,
                "name": name,
                "composite": round(composite, 1),
                "dimensions": {dn: {"score": d.score, "confidence": d.confidence, "detail": d.detail} for dn, d in dims.items()},
                "top_signal": top_signal,
                "avg_confidence": round(avg_conf, 2),
                "signal_coverage": {
                    "effective_weights": {
                        name: round(weight / total_w, 4)
                        for name, weight in effective_weights.items()
                    },
                    "degraded_dimensions": degraded_dimensions,
                },
                "latest_price": df["close"].iloc[-1] if df is not None and "close" in df.columns and len(df) > 0 else 0,
            }
        finally:
            # 清理当前线程的浏览器/事件循环，避免asyncio泄漏
            try:
                from data.eastmoney import cleanup_eastmoney
                cleanup_eastmoney()
            except Exception:
                pass

    results = []
    # 并发执行，总超时控制
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
    try:
        futures = {executor.submit(score_one, c): c for c in candidates}
        done, _ = concurrent.futures.wait(futures, timeout=timeout)
        for future in done:
            try:
                results.append(future.result())
            except Exception as e:
                c = futures[future]
                code = c.code if hasattr(c, "code") else c.get("code", "")
                logger.warning(f"打分失败 {code}: {e}")
    finally:
        # 与快链路总预算对齐：超时后不等待慢数据源线程完成。
        executor.shutdown(wait=False, cancel_futures=True)

    logger.info(f"并发打分完成: {len(results)}/{len(candidates)}只, 超时={timeout}s")
    return results


def _save_trade_plan(plan: TradePlan):
    """保存带稳定计划标识的 TradePlan 到文件。"""
    path = SIGNAL_CACHE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = plan.to_dict()
    plan_id, _ = _trade_plan_identity(payload)
    payload["plan_id"] = plan_id
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)
    logger.info(f"TradePlan已保存: {path}")


def _load_trade_plan() -> Optional[dict]:
    """加载今天的TradePlan"""
    if not os.path.exists(SIGNAL_CACHE_FILE):
        return None
    try:
        with open(SIGNAL_CACHE_FILE, "r", encoding="utf-8") as f:
            plan = json.load(f)
        if plan.get("date") != _now_bj().strftime("%Y-%m-%d"):
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


def _trade_plan_identity(plan_data: dict) -> tuple[str, str]:
    """校验计划结构并生成稳定标识，重试同一计划会得到相同的 id。"""
    if not isinstance(plan_data, dict):
        raise ValueError("TradePlan必须是对象")
    plan_date = plan_data.get("date")
    try:
        datetime.strptime(plan_date, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError("TradePlan缺少有效日期") from exc
    deadline = plan_data.get("deadline")
    if deadline:
        try:
            datetime.strptime(deadline, "%H:%M")
        except (TypeError, ValueError) as exc:
            raise ValueError("TradePlan截止时间格式必须为HH:MM") from exc
    orders = plan_data.get("orders")
    if not isinstance(orders, list):
        raise ValueError("TradePlan订单必须是列表")
    for index, order in enumerate(orders):
        if not isinstance(order, dict):
            raise ValueError(f"TradePlan订单{index}必须是对象")
        if not isinstance(order.get("code"), str) or not order["code"].strip():
            raise ValueError(f"TradePlan订单{index}缺少证券代码")
        if order.get("action") not in {"BUY", "SELL", "HOLD"}:
            raise ValueError(f"TradePlan订单{index}动作非法")
    canonical = {key: value for key, value in plan_data.items() if key != "plan_id"}
    payload_hash = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    plan_id = str(plan_data.get("plan_id") or f"{plan_date}-{payload_hash[:24]}")
    return plan_id, payload_hash


def execute_trade_plan(
    plan_data: dict,
    *,
    broker=None,
    realtime_func=None,
    market_status: str = None,
    drawdown_controller=None,
    system_risk_controller=None,
    update_memory: bool = True,
    allow_historical_plan: bool = True,
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
        allow_historical_plan: 是否允许历史计划重放；自动执行时必须为False

    Returns:
        PipelineResult
    """
    result = PipelineResult(date=_now_bj().strftime("%Y-%m-%d"))
    t0 = time.time()

    if not plan_data:
        result.errors.append("无TradePlan，无法执行")
        return result

    try:
        plan_id, payload_hash = _trade_plan_identity(plan_data)
    except ValueError as e:
        result.errors.append(str(e))
        return result

    if not allow_historical_plan:
        now = _now_bj()
        if plan_data.get("date") != now.strftime("%Y-%m-%d"):
            result.errors.append("TradePlan不是当前交易日计划")
            return result
        deadline = plan_data.get("deadline")
        if deadline and now.strftime("%H:%M") > deadline:
            result.errors.append("TradePlan已超过执行截止时间")
            return result

    result.market_status = market_status or get_market_status()
    result.trade_plan = {"plan_id": plan_id, "date": plan_data.get("date")}

    # 加载交易通道（默认模拟盘）
    if broker is None:
        from execution.broker import get_broker_adapter
        broker = get_broker_adapter()
    account = broker.account if hasattr(broker, "account") else broker
    realtime_func = realtime_func or _default_realtime_func()

    # 领取计划执行权。数据库中的唯一主键使重启、重试和并发调用不会重复下单。
    try:
        from data.database import Database
        with Database(db_path=getattr(account, "db_path", None)) as db:
            execution_claim = db.claim_trade_plan_execution(
                plan_id, plan_data["date"], payload_hash,
            )
    except Exception as e:
        result.errors.append(f"TradePlan执行权领取失败: {e}")
        return result
    if not execution_claim.get("claimed"):
        result.errors.append(
            f"TradePlan已被执行或正在执行: {plan_id} ({execution_claim.get('status', 'unknown')})"
        )
        return result

    def complete_plan_execution():
        try:
            with Database(db_path=getattr(account, "db_path", None)) as db:
                db.complete_trade_plan_execution(plan_id, "; ".join(result.errors))
        except Exception as e:
            logger.error(f"更新TradePlan执行状态失败 {plan_id}: {e}")

    def record_ab_sell(code: str, price: float, pnl_pct: float):
        """将真实卖出结果回填到仍在运行的影子A/B实验。"""
        try:
            from strategy.ab_test import ABTestManager
            from data.database import Database
            with Database(db_path=getattr(account, "db_path", None)) as _ab_db:
                _ab = ABTestManager(_ab_db)
                for test in _ab.get_running_tests():
                    for group in ("control", "treatment"):
                        prior = _ab_db.conn.execute(
                            "SELECT 1 FROM ab_test_trades WHERE test_id=? AND group_name=? AND code=? AND action='BUY' LIMIT 1",
                            (test["test_id"], group, code),
                        ).fetchone()
                        if prior:
                            _ab.record_trade(test["test_id"], group, code, "SELL", price, pnl_pct)
        except Exception as e:
            logger.debug(f"[A/B] 卖出结果回填失败(非致命): {e}")

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
        complete_plan_execution()
        return result

    # 检查系统级风控
    sr_health = sr.check_system_health()
    if sr_health["system_halted"]:
        result.risk_triggered.append(f"系统停机: {sr_health['issues']}")
        result.errors.append(f"系统停机，禁止交易")
        complete_plan_execution()
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
            stop_trades = broker.check_stop_conditions(
                prices, trade_date=plan_data["date"],
            )

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
            sellable_shares = broker.get_sellable_shares(
                code, trade_date=plan_data["date"],
            )
            if sellable_shares <= 0:
                reason = "T+1限制: 当日买入普通A股不可卖出"
                logger.info(f"SELL阻断: {code} - {reason}")
                _audit_order(result, order, "blocked", reason, sellable_shares=0)
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
                shares = min(int(positions[code]["shares"]), int(sellable_shares))
                trade = broker.sell(
                    code, price, shares, reason=reason,
                    trade_date=plan_data["date"],
                    market_regime=order.get("market_regime") or plan_data.get("regime", ""),
                    signal_score=order.get("score"),
                    signal_detail=order.get("signal_detail") or reason,
                    dimensions=order.get("dimensions"),
                )
                if trade:
                    result.executed_orders.append(trade)
                    record_ab_sell(code, price, trade.get("pnl_pct", 0))
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

            trade_unit = max(1, int(order.get("trade_unit", 100) or 100))
            # 计算可买数量：普通A股100股一手，可转债等T+0品种10张一手。
            shares = int(buy_amount / current_price / trade_unit) * trade_unit
            if shares < trade_unit:
                _audit_order(
                    result, order, "blocked", f"不足{trade_unit}单位",
                    price=current_price,
                    buy_amount=round(buy_amount, 2),
                    shares=shares,
                )
                continue

            # 下单（默认模拟账户，经BrokerAdapter封装）
            try:
                execution_reason = order.get("reason") or "TradePlan执行"
                buy_order = om.create_buy_order(
                    code=code,
                    name=order.get("name", code),
                    price=current_price,
                    shares=shares,
                    reason=execution_reason,
                    trade_date=plan_data["date"],
                    allow_t0=bool(order.get("allow_t0", False)),
                    trade_unit=trade_unit,
                    signal_score=order.get("score"),
                    signal_detail=order.get("signal_detail") or execution_reason,
                    market_regime=order.get("market_regime") or plan_data.get("regime", ""),
                    dimensions=order.get("dimensions"),
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
                        reason=execution_reason,
                        trade_date=plan_data["date"],
                        allow_t0=bool(order.get("allow_t0", False)),
                        trade_unit=trade_unit,
                        market_regime=order.get("market_regime") or plan_data.get("regime", ""),
                        signal_score=order.get("score"),
                        signal_detail=order.get("signal_detail") or execution_reason,
                        dimensions=order.get("dimensions"),
                    )
                    buy_order.status = "filled" if trade else "failed"
                if buy_order.status == "filled":
                    result.executed_orders.append({
                        "order_id": buy_order.order_id,
                        "code": code,
                        "name": buy_order.name,
                        "side": buy_order.side,
                        "shares": buy_order.shares,
                        "price": current_price,
                        "status": buy_order.status,
                        "reason": execution_reason,
                    })
                _audit_order(
                    result, order, buy_order.status,
                    execution_reason if buy_order.status == "filled" else "买入未成交",
                    price=current_price,
                    shares=buy_order.shares,
                    buy_amount=round(buy_amount, 2),
                    available_cash=round(available_cash, 2),
                )
                if buy_order.status == "filled":
                    if order.get("decision_id") and buy_order.trade_id:
                        try:
                            from data.database import Database
                            with Database(db_path=getattr(account, "db_path", None)) as db:
                                db.update_llm_decision(
                                    int(order["decision_id"]),
                                    {"trade_id": int(buy_order.trade_id)},
                                )
                        except Exception as e:
                            logger.warning(f"LLM决策关联成交失败 {code}: {e}")
                    logger.info(f"买入 {code} {shares}股 @ {current_price} ({execution_reason})")
                else:
                    logger.info(f"买入未成交 {code} {shares}股 @ {current_price}")
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

    complete_plan_execution()
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
        result = PipelineResult(date=_now_bj().strftime("%Y-%m-%d"))
        result.errors.append("无今日TradePlan，请先运行扫描")
        return result
    return execute_trade_plan(plan_data, allow_historical_plan=False)


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
    result = PipelineResult(date=_now_bj().strftime("%Y-%m-%d"))
    t0 = time.time()

    # 先回填已成熟的历史候选，再构建 daily_facts 和调用 LLM；否则本轮新
    # 标签至少要晚一天才能进入复盘上下文。
    try:
        from strategy.counterfactual import evaluate_candidate_outcomes
        counterfactual = evaluate_candidate_outcomes()
        result.steps.append(StepResult(
            name="候选反事实评估", success=True,
            elapsed=time.time() - t0,
            detail=(
                f"检查{counterfactual.get('checked', 0)}条 "
                f"回填{counterfactual.get('updated', 0)}条 "
                f"成熟{counterfactual.get('matured', 0)}条"
            ),
        ))
    except Exception as e:
        logger.warning(f"候选反事实评估失败(非致命): {e}")

    try:
        from review.daily_review import run_daily_review
        review_payload = run_daily_review(return_data=True)
        review_text = review_payload.get("text", "")
        review_data = review_payload.get("data", {})
        review_data["order_audit"] = _load_today_order_audit(result.date)
        try:
            from scheduler.trader_brief import build_daily_facts
            daily_facts = build_daily_facts(date=result.date)
            review_data["daily_facts"] = daily_facts
            # 每日事实层已经按订单结果去重，复盘和教训必须使用同一口径。
            review_data["order_audit"] = daily_facts.get("order_audit") or review_data["order_audit"]
        except Exception as e:
            logger.warning(f"每日决策事实聚合失败(非致命): {e}")
        try:
            from data.database import Database
            with Database() as db:
                db.save_review_snapshot(result.date, review_data)
        except Exception as e:
            logger.debug(f"订单审计复盘快照回写失败(非致命): {e}")
        result.review_text = review_text
        # 提取关键财务指标作为摘要
        daily_pnl = review_data.get("daily_pnl", 0)
        daily_pnl_pct = review_data.get("daily_pnl_pct", 0)
        total_assets = review_data.get("total_assets", 0)
        cumulative_pnl_pct = review_data.get("cumulative_pnl_pct", 0)
        position_count = len(review_data.get("position_pnls", []))
        trade_count = len(review_data.get("trade_reviews", []))
        win_rate = review_data.get("win_rate", 0)

        summary_parts = []
        if total_assets > 0:
            summary_parts.append(f"总资产{total_assets:,.0f}")
        if daily_pnl != 0:
            summary_parts.append(f"日盈亏{daily_pnl:+,.0f}({daily_pnl_pct:+.2%})")
        if cumulative_pnl_pct != 0:
            summary_parts.append(f"累计{cumulative_pnl_pct:+.2%}")
        if position_count > 0:
            summary_parts.append(f"持仓{position_count}只")
        if trade_count > 0:
            summary_parts.append(f"今日交易{trade_count}笔 命中率{win_rate:.0%}")

        detail_summary = " | ".join(summary_parts) if summary_parts else "暂无数据"
        result.steps.append(StepResult(
            name="复盘", success=True,
            elapsed=time.time() - t0,
            detail=detail_summary,
        ))

        adaptive_data = {}
        try:
            from strategy.adaptive import AdaptiveEngine
            adaptive = AdaptiveEngine()
            # 固定规则只负责生成绩效分析，不再直接调整明日参数；明日策略
            # 由下方的 LLM 日终策略指令统一决定。
            adaptive_data = adaptive.analyze_and_adjust(days=10, apply_adjustments=False)
            # 提取自适应分析关键信息
            adj_status = adaptive_data.get("status", "unknown")
            regime = adaptive_data.get("regime", {})
            regime_type = regime.get("type", "") if isinstance(regime, dict) else str(regime)
            adj_detail = f"状态={adj_status}"
            if regime_type:
                adj_detail += f" 市场环境={regime_type}"
            result.steps.append(StepResult(
                name="自适应分析", success=True,
                elapsed=time.time() - t0,
                detail=adj_detail,
            ))
        except Exception as e:
            logger.warning(f"自适应分析失败(非致命): {e}")

        try:
            from strategy.ab_test import ABTestManager
            from data.database import Database
            with Database() as _ab_db:
                concluded = ABTestManager(_ab_db).evaluate_all_running()
            result.steps.append(StepResult(
                name="A/B影子评估", success=True,
                elapsed=time.time() - t0,
                detail=f"本次完成{len(concluded)}个实验，未达样本的实验继续运行",
            ))
        except Exception as e:
            logger.warning(f"A/B影子评估失败(非致命): {e}")

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
            directive = None
            try:
                from strategy.directive import (
                    generate_and_save_strategy_directive,
                    get_effective_trade_policy,
                )
                from strategy.regime_config import get_trade_params
                from strategy.market_regime import get_regime_history

                current_regime = "sideways"
                history = get_regime_history(days=1)
                if history:
                    current_regime = history[0].regime
                active_policy = get_effective_trade_policy(result.date, current_regime)
                current_params = (
                    active_policy["params"] if active_policy else get_trade_params(current_regime)
                )
                directive = generate_and_save_strategy_directive(
                    review_date=result.date,
                    review_data=review_data,
                    llm_review=llm_analysis,
                    regime=current_regime,
                    current_params=current_params,
                    current_directive=active_policy,
                )
                if directive:
                    result.steps.append(StepResult(
                        name="AI明日策略指令", success=True,
                        elapsed=time.time() - t0,
                        detail=(
                            f"{directive['version']} {directive['effective_date']}生效 | "
                            f"{directive['intent']} | Top{directive['params']['top_k']} "
                            f"最低分{directive['params']['min_score']:.0f}"
                        ),
                    ))
                else:
                    result.steps.append(StepResult(
                        name="AI明日策略指令", success=False,
                        elapsed=time.time() - t0,
                        detail="LLM策略指令无效，沿用上一有效版本",
                    ))
            except Exception as e:
                logger.warning(f"AI策略指令生成失败(非致命): {e}")
                result.steps.append(StepResult(
                    name="AI明日策略指令", success=False,
                    elapsed=time.time() - t0,
                    detail="策略指令生成异常，沿用上一有效版本",
                    error=str(e),
                ))
            lesson_count = extract_and_save_lessons(
                review_data,
                llm_analysis=llm_analysis,
                adaptive_data=adaptive_data,
            )
            evolution = run_decision_evolution_analysis()
            memory_report = {}
            try:
                from strategy.memory import TradeMemory
                with TradeMemory() as memory:
                    memory_report = memory.consolidate_layers(
                        date=result.date,
                        lookback_days=10,
                    )
                result.steps.append(StepResult(
                    name="分层记忆进化", success=True,
                    elapsed=time.time() - t0,
                    detail=(
                        f"短期{memory_report.get('short', 0)}条 "
                        f"中期{memory_report.get('medium', 0)}条 "
                        f"长期{memory_report.get('long', 0)}条 "
                        f"失效{memory_report.get('expired', 0)}条"
                    ),
                ))
            except Exception as e:
                logger.warning(f"分层记忆进化失败(非致命): {e}")
            # 提取LLM分析关键信息
            total_decisions = evolution.get("total_decisions", 0)
            llm_detail = f"提取教训{lesson_count}条"
            if total_decisions > 0:
                llm_detail += f" 分析决策样本{total_decisions}条"
            # 如果有关键教训，添加摘要
            if llm_analysis and isinstance(llm_analysis, str):
                # 取第一行作为摘要
                first_line = llm_analysis.split("\n")[0][:100]
                if first_line:
                    llm_detail += f" | {first_line}"
            result.steps.append(StepResult(
                name="LLM复盘进化", success=True,
                elapsed=time.time() - t0,
                detail=llm_detail,
            ))
            # 复盘快照必须同时保留 AI 全文与可执行指令，供日报和策略版本回溯。
            review_data["llm_review"] = llm_analysis
            review_data["strategy_directive"] = directive
            try:
                from data.database import Database
                with Database() as db:
                    db.save_review_snapshot(result.date, review_data)
            except Exception as e:
                logger.warning(f"AI复盘快照回写失败(非致命): {e}")
        except Exception as e:
            logger.warning(f"LLM复盘进化失败(非致命): {e}")
    except Exception as e:
        result.errors.append(f"复盘失败: {e}")

    # 【TaskC】收盘时写入每日快照
    try:
        from execution.broker import get_broker_adapter
        broker = get_broker_adapter()
        account = broker.account if hasattr(broker, "account") else broker
        today = _now_bj().strftime("%Y-%m-%d")

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
        f"策略版本: {plan_data.get('strategy_version', 'legacy-adaptive')} {plan_data.get('strategy_intent', '')}",
        f"执行截止: {plan_data.get('deadline', 'N/A')}",
        f"耗时: {plan_data.get('elapsed', 0):.1f}s",
        "",
    ]

    market_snapshot = plan_data.get("market_snapshot") or {}
    if market_snapshot:
        age = market_snapshot.get("age_seconds")
        age_text = f"{age:.0f}s" if isinstance(age, (int, float)) else "未知"
        lines.append(
            "市场数据: "
            f"来源={market_snapshot.get('source', 'unknown')} "
            f"新鲜={market_snapshot.get('fresh', False)} 年龄={age_text}"
        )

    candidate_pool = plan_data.get("candidate_pool") or {}
    if candidate_pool:
        pool_age = candidate_pool.get("age_seconds")
        pool_age_text = f"{pool_age:.0f}s" if isinstance(pool_age, (int, float)) else "未知"
        lines.append(
            "候选池: "
            f"版本={candidate_pool.get('version') or 'legacy'} "
            f"来源={candidate_pool.get('source', 'unknown')} "
            f"数量={candidate_pool.get('candidate_count', '未知')} "
            f"年龄={pool_age_text}"
        )

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

def run_scan(
    budget_seconds: int = FAST_SCAN_BUDGET_SECONDS,
    candidate_codes: Optional[List[str]] = None,
    candidate_items: Optional[List[dict]] = None,
) -> PipelineResult:
    """扫描信号（兼容旧接口，内部调fast_scan）"""
    result = PipelineResult(date=_now_bj().strftime("%Y-%m-%d"))
    t0 = time.time()

    plan = fast_scan(
        budget_seconds=budget_seconds,
        candidate_codes=candidate_codes,
        candidate_items=candidate_items,
    )
    result.trade_plan = plan.to_dict()
    result.candidates = plan.raw_scores
    result.errors.extend(plan.errors)

    try:
        from strategy.counterfactual import record_trade_plan_candidates
        captured = record_trade_plan_candidates(plan)
        if captured:
            logger.info("候选反事实观察已记录: %s只", captured)
    except Exception as e:
        logger.debug("候选反事实观察记录失败(非致命): %s", e)

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
