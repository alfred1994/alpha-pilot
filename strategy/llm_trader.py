"""
LLM决策引擎
====================================================================
用MiMo LLM替代加权打分做交易决策。

输入:
  - 5维信号分数 (技术/资金/舆情/情绪/基本面)
  - 市场环境 (bull/bear/sideways/rebound)
  - 历史记忆 (教训库 + 历史决策)

输出:
  - TradeDecision: action(BUY/SELL/HOLD) + confidence + reasoning

与原有 decision.py 并存，通过 USE_LLM_TRADER 配置切换。
====================================================================
"""
import json
import os
import logging
import requests
import threading
from typing import Dict, List, Optional
from datetime import datetime

from strategy.decision import TradeDecision, DimensionScore

logger = logging.getLogger("strategy.llm_trader")

# MiMo API 配置
MIMO_API_KEY = os.environ.get("XIAOMI_API_KEY", "")
MIMO_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
LLM_MODEL = "mimo-v2.5-pro"


def _call_llm(prompt: str, max_tokens: int = 2000, retries: int = 2,
              http_timeout: int = 60) -> Optional[str]:
    """调用 MiMo LLM（daemon线程硬超时 + 自动重试）"""
    if not MIMO_API_KEY:
        logger.warning("XIAOMI_API_KEY 未设置, LLM决策不可用")
        return None

    headers = {
        "Authorization": f"Bearer {MIMO_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一位资深A股量化AI交易员。"
                    "你基于数据和逻辑做决策，不受情绪影响。"
                    "你的目标是长期稳定盈利，而非短期暴利。"
                    "严格返回JSON格式，用中文分析。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,  # 低温度，决策要稳定
    }

    hard_timeout = http_timeout + 5  # 硬超时略大于HTTP超时

    for attempt in range(1 + retries):
        # 【Phase1-Task1】用daemon线程实现硬超时，避免ThreadPoolExecutor context manager等待线程完成
        result_box = [None]  # 用列表存储结果（闭包可修改）

        def _do_call():
            try:
                url = f"{MIMO_BASE_URL}/chat/completions"
                resp = requests.post(url, headers=headers, json=payload, timeout=http_timeout)
                resp.raise_for_status()
                msg = resp.json()["choices"][0]["message"]
                content = msg.get("content", "") or ""
                # MiMo模型: 当max_tokens不够时，内容可能只在reasoning_content里
                if not content.strip():
                    reasoning = msg.get("reasoning_content", "") or ""
                    if reasoning.strip():
                        logger.warning("LLM返回空content，使用reasoning_content兜底")
                        content = reasoning
                result_box[0] = content if content.strip() else None
            except Exception as e:
                logger.error(f"LLM调用失败(第{attempt+1}次): {e}")
                result_box[0] = None

        t = threading.Thread(target=_do_call, daemon=True)
        t.start()
        t.join(timeout=hard_timeout)

        if t.is_alive():
            logger.error(f"LLM调用超时({hard_timeout}s, 第{attempt+1}次)")
        elif result_box[0] is not None:
            return result_box[0]
        else:
            logger.warning(f"LLM第{attempt+1}次调用返回空，{'重试中...' if attempt < retries else '已用完重试次数'}")

    return None


def _parse_decision_response(raw: Optional[str]) -> tuple:
    """
    解析LLM决策响应

    Returns:
        (action, confidence, reasoning) 或 ("HOLD", 0.0, "解析失败")
    """
    if not raw:
        return "HOLD", 0.0, "LLM无响应"

    try:
        content = raw.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        data = json.loads(content)

        action = str(data.get("action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"

        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        reasoning = str(data.get("reasoning", ""))
        if not reasoning:
            reasoning = str(data.get("reason", ""))

        return action, confidence, reasoning

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        # 尝试用正则从文本中提取JSON
        import re
        match = re.search(r'\{[^{}]*"action"\s*:\s*"[^"]+?"[^{}]*\}', raw)
        if match:
            try:
                data = json.loads(match.group())
                action = str(data.get("action", "HOLD")).upper()
                if action in ("BUY", "SELL", "HOLD"):
                    confidence = float(data.get("confidence", 0.5))
                    confidence = max(0.0, min(1.0, confidence))
                    reasoning = str(data.get("reasoning", data.get("reason", "")))
                    logger.info(f"正则提取JSON成功: {action} conf={confidence}")
                    return action, confidence, reasoning
            except Exception:
                pass

        # 最后兜底：从自然语言推理文本中推断决策
        text_lower = raw.lower()
        # 优先检查明确的卖出信号
        sell_keywords = ["建议卖出", "应卖出", "建议清仓", "卖出信号", "sell"]
        hold_keywords = ["建议持有", "建议观望", "不宜买入", "不建议", "置信度不足",
                         "风险较高", "谨慎", "hold", "暂时不买", "不建议买入"]
        buy_keywords = ["建议买入", "可以买入", "买入信号", "看多", "buy"]

        for kw in sell_keywords:
            if kw in text_lower:
                logger.info(f"从推理文本推断: SELL (关键词: {kw})")
                return "SELL", 0.3, f"推理推断: {raw[:100]}"
        for kw in hold_keywords:
            if kw in text_lower:
                logger.info(f"从推理文本推断: HOLD (关键词: {kw})")
                return "HOLD", 0.3, f"推理推断: {raw[:100]}"
        for kw in buy_keywords:
            if kw in text_lower:
                logger.info(f"从推理文本推断: BUY (关键词: {kw})")
                return "BUY", 0.3, f"推理推断: {raw[:100]}"

        # 完全无法推断，默认HOLD
        logger.warning(f"LLM决策解析失败，无法推断: {raw[:200]}")
        return "HOLD", 0.0, f"解析失败: {e}"


def _build_decision_prompt(
    code: str,
    name: str,
    dimensions: Dict[str, DimensionScore],
    regime: str = "sideways",
    memory_context: str = "",
    current_positions: dict = None,
    total_assets: float = 0,
    cash: float = 0,
    htsc_diagnosis: str = "",
    overnight_text: str = "",
) -> str:
    """
    构建LLM决策prompt

    Args:
        code: 股票代码
        name: 股票名称
        dimensions: 5维信号分数
        regime: 市场环境
        memory_context: 记忆上下文
        current_positions: 当前持仓
        total_assets: 总资产
        cash: 可用现金
        htsc_diagnosis: 华泰个股诊断（可选）

    Returns:
        prompt字符串
    """
    regime_label = {
        "bull": "牛市(上涨趋势)",
        "bear": "熊市(下跌趋势)",
        "sideways": "震荡(方向不明)",
        "rebound": "反弹(急跌后回升)",
    }.get(regime, regime)

    # 构建信号部分
    signal_lines = []
    dim_labels = {
        "technical": "技术面",
        "capital": "资金面",
        "sentiment": "舆情面",
        "emotion": "情绪面",
        "fundamental": "基本面",
    }
    for dim_name in ["technical", "capital", "sentiment", "emotion", "fundamental"]:
        dim = dimensions.get(dim_name)
        if dim:
            label = dim_labels.get(dim_name, dim_name)
            signal_lines.append(f"- {label}: {dim.score:.0f}分 (置信度{dim.confidence:.0%}) {dim.detail}")

    signals_text = "\n".join(signal_lines) if signal_lines else "无信号数据"

    # 持仓信息
    position_text = ""
    if current_positions:
        pos_lines = []
        for pcode, pos in current_positions.items():
            pos_lines.append(f"- {pos.get('name', pcode)}({pcode}): {pos.get('shares', 0)}股 成本{pos.get('buy_price', 0):.2f}")
        position_text = f"\n当前持仓:\n" + "\n".join(pos_lines)
        position_text += f"\n总资产: {total_assets:,.0f} 可用现金: {cash:,.0f}"

    # === P1-5: 已持仓股的卖出分析 ===
    has_position = current_positions and code in current_positions
    sell_analysis = ""
    if has_position:
        pos = current_positions[code]
        buy_price = pos.get('buy_price', 0)
        shares = pos.get('shares', 0)
        buy_date = pos.get('buy_date', '')
        # 尝试从dimensions中获取当前价格估算盈亏
        pnl_text = ""
        if buy_price > 0:
            # 用technical中的detail尝试提取价格信息，或者直接标注成本
            pnl_text = f"成本: {buy_price:.2f}"
            if buy_date:
                pnl_text += f"  买入日期: {buy_date}"
        sell_analysis = f"""

【⚠ 持仓卖出分析 - {name}({code})】
当前持有: {shares}股 {pnl_text}
** 请重点分析该持仓股是否应该卖出（止盈/减仓/清仓）**
"""

    # 华泰诊断信息
    htsc_text = ""
    if htsc_diagnosis:
        # 截取前500字，避免prompt过长
        htsc_trimmed = htsc_diagnosis[:500]
        htsc_text = f"\n【华泰专业诊断】\n{htsc_trimmed}"

    # 外围市场信息
    overnight_section = ""
    if overnight_text:
        overnight_section = f"\n【外围市场·隔夜快照】\n{overnight_text}\n"

    prompt = f"""请分析以下股票，做出交易决策。

【股票信息】
代码: {code}  名称: {name}

【市场环境】
{regime_label}
{overnight_section}
【5维信号】
{signals_text}
{position_text}{sell_analysis}{htsc_text}

【历史记忆】
{memory_context if memory_context else "暂无相关历史记忆。"}

【决策要求】
1. 综合考虑5维信号、外围市场环境、市场环境和历史记忆
2. 如果外围市场大跌（美股跌幅>1%或A50跌幅>1%），即使个股信号偏多也要谨慎
3. 如果市场环境是熊市，即使信号偏多也要谨慎
4. 如果历史记忆中有该股票的亏损教训，要特别注意
5. 只有在置信度>=60%时才建议买入
6. 如果已持有该股票，分析是否应该卖出（止盈/减仓/清仓）：当盈利达到目标、基本面恶化、技术面转空、或有更好的替代标的时，应考虑SELL
7. 持仓股如果信号仍强且无卖出理由，应返回HOLD继续持有

请返回JSON:
{{"action": "BUY/SELL/HOLD", "confidence": 0.0-1.0, "reasoning": "50字以内的决策理由"}}

只返回JSON，不要其他文字。"""

    # 读取进化优化建议
    try:
        from data.database import Database
        with Database() as db:
            cursor = db.conn.execute(
                "SELECT hint FROM active_prompt_hints WHERE active = 1 ORDER BY id DESC LIMIT 3"
            )
            hints = cursor.fetchall()
            if hints:
                evolution_hints = "\n".join([f"- {h[0]}" for h in hints])
                prompt += f"\n【历史优化建议】\n{evolution_hints}\n"
    except Exception:
        pass

    return prompt


def make_decision(
    code: str,
    name: str = "",
    dimensions: Dict[str, DimensionScore] = None,
    regime: str = "sideways",
    memory_context: str = "",
    current_positions: dict = None,
    total_assets: float = 0,
    cash: float = 0,
    weights: Dict[str, float] = None,  # 兼容旧接口，忽略
    buy_threshold: float = None,        # 兼容旧接口，忽略
    sell_threshold: float = None,       # 兼容旧接口，忽略
    htsc_diagnosis: str = "",
    overnight_text: str = "",
    memory=None,  # P2-12: 外部传入的 TradeMemory 实例（连接复用）
    llm_retries: int = 2,
    llm_timeout: int = 60,
) -> TradeDecision:
    """
    LLM决策主函数

    Args:
        code: 股票代码
        name: 股票名称
        dimensions: 5维信号分数（从decision.compute_dimension_scores获取）
        regime: 市场环境 (bull/bear/sideways/rebound)
        memory_context: 记忆上下文文本
        current_positions: 当前持仓
        total_assets: 总资产
        cash: 可用现金
        weights: 兼容旧接口，忽略
        buy_threshold: 兼容旧接口，忽略
        sell_threshold: 兼容旧接口，忽略

    Returns:
        TradeDecision
    """
    if dimensions is None:
        dimensions = {}

    # 计算综合分（先算，用于判断是否值得拉华泰诊断）
    from config import SIGNAL_WEIGHTS
    total_weight = sum(weights.get(k, 0) for k in dimensions) if weights else sum(SIGNAL_WEIGHTS.get(k, 0) for k in dimensions)
    if total_weight == 0:
        total_weight = 1

    composite = 0.0
    w = weights or SIGNAL_WEIGHTS
    for dim_name, dim in dimensions.items():
        composite += dim.score * w.get(dim_name, 0)
    composite /= total_weight

    # 获取华泰专业诊断（仅对高分top股票，避免每只都拉太慢）
    # P1-7: 给华泰诊断加5秒超时，避免阻塞LLM调用
    htsc_diagnosis = ""
    if composite >= 65 and name:
        try:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTE
            with ThreadPoolExecutor(max_workers=1) as ex:
                from data.htsc_client import diagnose_stock
                fut = ex.submit(diagnose_stock, name)
                htsc_diagnosis = fut.result(timeout=5) or ""
            if htsc_diagnosis:
                logger.info(f"华泰诊断获取成功: {code} {name}")
        except _FTE:
            logger.debug(f"华泰诊断超时(5s): {code} {name}")
            htsc_diagnosis = ""
        except Exception as e:
            logger.debug(f"华泰诊断跳过: {e}")
            htsc_diagnosis = ""

    # 加载隔夜快照
    overnight_text = ""
    try:
        from data.overnight_snapshot import load_overnight_snapshot, format_for_llm_prompt
        snap = load_overnight_snapshot()
        if snap:
            overnight_text = format_for_llm_prompt(snap)
    except Exception as e:
        logger.debug(f"加载隔夜快照失败(可忽略): {e}")

    # 自动注入交易记忆：复盘教训必须能反哺下一次决策
    if not memory_context:
        try:
            if memory is not None:
                memory_context = memory.recall(stock_code=code, regime=regime)
            else:
                from strategy.memory import TradeMemory
                with TradeMemory() as _memory:
                    memory_context = _memory.recall(stock_code=code, regime=regime)
        except Exception as e:
            logger.debug(f"加载交易记忆失败(可忽略): {e}")
            memory_context = ""

    # 构建prompt
    prompt = _build_decision_prompt(
        code, name, dimensions, regime, memory_context,
        current_positions, total_assets, cash,
        htsc_diagnosis=htsc_diagnosis,
        overnight_text=overnight_text,
    )

    # 调用LLM
    raw = _call_llm(prompt, retries=llm_retries, http_timeout=llm_timeout)
    action, confidence, reasoning = _parse_decision_response(raw)

    # 构建决策对象（composite已提前计算）
    decision = TradeDecision(
        code=code,
        name=name or code,
        action=action,
        composite_score=round(composite, 1),
        confidence=round(confidence, 2),
        dimensions=dimensions,
        reason=reasoning,
    )

    # P2-12: 保存到记忆系统（优先使用外部传入的实例，避免重复创建连接）
    try:
        if memory is not None:
            # 外部传入的实例，由调用方管理生命周期
            memory.save_decision(
                code=code,
                action=action,
                prompt=prompt[:2000],
                response=raw[:2000] if raw else "",
                reasoning=reasoning,
                confidence=confidence,
            )
        else:
            from strategy.memory import TradeMemory
            with TradeMemory() as _memory:
                _memory.save_decision(
                    code=code,
                    action=action,
                    prompt=prompt[:2000],
                    response=raw[:2000] if raw else "",
                    reasoning=reasoning,
                    confidence=confidence,
                )
    except Exception as e:
        logger.debug(f"保存LLM决策到记忆失败(可忽略): {e}")

    logger.info(f"LLM决策: {decision} | {reasoning[:50]}")
    return decision


def batch_decide(
    candidates: list,
    get_df_func=None,
    weights: Dict[str, float] = None,
    regime: str = "sideways",
    memory=None,
    account=None,
) -> List[TradeDecision]:
    """
    批量LLM决策

    Args:
        candidates: 候选列表
        get_df_func: 获取日线数据的函数
        weights: 兼容旧接口
        regime: 市场环境
        memory: TradeMemory实例
        account: PaperAccount实例（获取持仓信息）

    Returns:
        按置信度降序排列的决策列表
    """
    import time
    from strategy.decision import compute_dimension_scores

    t0 = time.time()
    logger.info(f"LLM批量决策开始: {len(candidates)}只候选, 市场环境={regime}")

    # 获取持仓信息
    current_positions = {}
    total_assets = 0
    cash = 0
    if account:
        current_positions = account.positions
        total_assets = account.total_assets()
        cash = account.cash

    # 预取共享数据
    from data.sentiment import get_weibo_finance
    from signals.sentiment import market_mood_signal, batch_sentiment_signal

    weibo_posts = []
    try:
        weibo_posts = get_weibo_finance(10)
    except Exception:
        pass

    mood_result = None
    try:
        mood_result = market_mood_signal()
    except Exception:
        pass

    # 批量舆情
    stock_list = []
    for c in candidates:
        code = c.code if hasattr(c, "code") else c.get("code", "")
        name = c.name if hasattr(c, "name") else c.get("name", "")
        stock_list.append({"code": code, "name": name})

    batch_sentiment_scores = {}
    try:
        batch_sentiment_scores = batch_sentiment_signal(stock_list, weibo_posts)
    except Exception:
        pass

    # 获取记忆上下文
    memory_context_cache = {}

    # ── 第一阶段: 快速打分（所有候选） ──
    scored = []  # (code, name, dims, composite, ctx)
    from config import SIGNAL_WEIGHTS
    for c in candidates:
        code = c.code if hasattr(c, "code") else c.get("code", "")
        name = c.name if hasattr(c, "name") else c.get("name", "")

        # 获取日线数据
        df = None
        if get_df_func:
            try:
                df = get_df_func(code)
            except Exception:
                pass

        # 计算5维分数
        dims = compute_dimension_scores(code, df, weibo_posts)
        # 用预取的舆情和情绪覆盖
        if code in batch_sentiment_scores:
            dims["sentiment"] = DimensionScore(
                name="sentiment",
                score=batch_sentiment_scores[code].score,
                confidence=batch_sentiment_scores[code].confidence,
                detail=batch_sentiment_scores[code].detail,
            )
        if mood_result:
            dims["emotion"] = DimensionScore(
                name="emotion",
                score=mood_result.score,
                confidence=mood_result.confidence,
                detail=mood_result.detail,
            )

        # 计算综合分
        total_w = sum(SIGNAL_WEIGHTS.get(k, 0) for k in dims)
        if total_w == 0:
            total_w = 1
        composite = sum(d.score * SIGNAL_WEIGHTS.get(d_name, 0) for d_name, d in dims.items()) / total_w

        # 记忆上下文
        ctx = ""
        if memory:
            try:
                ctx = memory.recall(stock_code=code, regime=regime)
            except Exception:
                pass

        scored.append((code, name, dims, composite, ctx))

    # 【Phase1-Task3】对当前持仓中未在候选列表的股票，也生成卖出分析
    # 确保LLM会评估是否应该卖出已有持仓
    scored_codes = {s[0] for s in scored}
    if current_positions:
        for pos_code, pos_info in current_positions.items():
            if pos_code not in scored_codes:
                pos_name = pos_info.get("name", pos_code)
                # 为持仓股计算5维分数
                df = None
                if get_df_func:
                    try:
                        df = get_df_func(pos_code)
                    except Exception:
                        pass
                pos_dims = compute_dimension_scores(pos_code, df, weibo_posts)
                if pos_code in batch_sentiment_scores:
                    pos_dims["sentiment"] = DimensionScore(
                        name="sentiment",
                        score=batch_sentiment_scores[pos_code].score,
                        confidence=batch_sentiment_scores[pos_code].confidence,
                        detail=batch_sentiment_scores[pos_code].detail,
                    )
                if mood_result:
                    pos_dims["emotion"] = DimensionScore(
                        name="emotion",
                        score=mood_result.score,
                        confidence=mood_result.confidence,
                        detail=mood_result.detail,
                    )
                pos_total_w = sum(SIGNAL_WEIGHTS.get(k, 0) for k in pos_dims)
                if pos_total_w == 0:
                    pos_total_w = 1
                pos_composite = sum(d.score * SIGNAL_WEIGHTS.get(d_name, 0) for d_name, d in pos_dims.items()) / pos_total_w
                pos_ctx = ""
                if memory:
                    try:
                        pos_ctx = memory.recall(stock_code=pos_code, regime=regime)
                    except Exception:
                        pass
                scored.append((pos_code, pos_name, pos_dims, pos_composite, pos_ctx))
                logger.info(f"持仓股 {pos_name}({pos_code}) 加入卖出分析, 综合分={pos_composite:.0f}")

    # 按综合分排序，取 Top N
    scored.sort(key=lambda x: x[3], reverse=True)
    top_n = min(5, len(scored))
    top_candidates = scored[:top_n]
    rest_candidates = scored[top_n:]

    logger.info(f"快速打分完成: {len(scored)}只, Top{top_n}进入LLM决策, 其余{len(rest_candidates)}只直接HOLD")

    # ── 第二阶段: LLM精细决策（仅Top N） ──
    decisions = []
    for code, name, dims, composite, ctx in top_candidates:
        try:
            decision = make_decision(
                code=code,
                name=name,
                dimensions=dims,
                regime=regime,
                memory_context=ctx,
                current_positions=current_positions,
                total_assets=total_assets,
                cash=cash,
            )
            decisions.append(decision)
        except Exception as e:
            logger.error(f"LLM决策失败 {code}: {e}")
            # fallback: 用加权分生成HOLD决策
            decisions.append(TradeDecision(
                code=code, name=name, action="HOLD",
                composite_score=round(composite, 1), confidence=0.3,
                dimensions=dims, reason=f"LLM失败, 加权分{composite:.0f}",
            ))

    # 其余股票直接HOLD（不浪费LLM调用）
    for code, name, dims, composite, ctx in rest_candidates:
        decisions.append(TradeDecision(
            code=code, name=name, action="HOLD",
            composite_score=round(composite, 1), confidence=0.2,
            dimensions=dims, reason=f"综合分{composite:.0f}未进Top{top_n}, 跳过LLM",
        ))

    # 按置信度降序排列
    decisions.sort(key=lambda x: x.confidence, reverse=True)

    logger.info(f"LLM批量决策完成: {len(decisions)}只, 耗时{time.time()-t0:.1f}s")
    return decisions


def format_llm_decision_report(decisions: List[TradeDecision]) -> str:
    """格式化LLM决策报告"""
    if not decisions:
        return "无LLM决策结果"

    lines = [
        "=" * 70,
        "LLM决策报告",
        "=" * 70,
    ]

    for d in decisions:
        action_emoji = {"BUY": "🟢买入", "SELL": "🔴卖出", "HOLD": "🟡持有"}.get(d.action, "⚪?")
        lines.append(f"\n{action_emoji} {d.code} {d.name}")
        lines.append(f"  综合分: {d.composite_score:.1f} | LLM置信度: {d.confidence:.0%} | 决策: {d.action}")
        for dim_name in ["technical", "capital", "sentiment", "emotion", "fundamental"]:
            dim = d.dimensions.get(dim_name)
            if dim:
                label = {"technical": "技术面", "capital": "资金面", "sentiment": "舆情面",
                         "emotion": "情绪面", "fundamental": "基本面"}.get(dim_name, dim_name)
                lines.append(f"    {label}: {dim.score:.1f} (conf={dim.confidence:.0%}) {dim.detail}")
        lines.append(f"  推理: {d.reason}")

    lines.append("\n" + "=" * 70)
    buy_count = sum(1 for d in decisions if d.action == "BUY")
    sell_count = sum(1 for d in decisions if d.action == "SELL")
    hold_count = sum(1 for d in decisions if d.action == "HOLD")
    lines.append(f"汇总: 买入{buy_count} 卖出{sell_count} 持有{hold_count}")
    return "\n".join(lines)


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # 测试单只股票
    from signals import Direction
    dims = {
        "technical": DimensionScore("technical", 65, 0.7, "MACD金叉"),
        "capital": DimensionScore("capital", 55, 0.5, "资金中性"),
        "sentiment": DimensionScore("sentiment", 70, 0.6, "舆情偏多"),
        "emotion": DimensionScore("emotion", 50, 0.4, "情绪中性"),
        "fundamental": DimensionScore("fundamental", 60, 0.5, "PE合理"),
    }

    decision = make_decision("600519", "贵州茅台", dimensions=dims, regime="sideways")
    print(format_llm_decision_report([decision]))
