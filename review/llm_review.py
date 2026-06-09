"""
LLM 深度复盘分析
每日收盘后用 LLM 对当天战况做全面总结和策略建议
"""
import json
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("review.llm")

# MiMo API 配置
MIMO_API_KEY = os.environ.get("XIAOMI_API_KEY", "")
MIMO_BASE_URL = os.environ.get("XIAOMI_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")
LLM_MODEL = "mimo-v2.5-pro"


def _call_llm(prompt: str, max_tokens: int = 2000) -> Optional[str]:
    """调用 MiMo LLM"""
    if not MIMO_API_KEY:
        logger.warning("XIAOMI_API_KEY 未设置")
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
                    "你是一位资深A股量化交易复盘分析师。"
                    "请基于提供的交易数据和市场信息，给出专业、具体、可操作的分析和建议。"
                    "用中文回答，语气专业但易懂。"
                    "分点列出，每条建议要具体可执行，不要空话套话。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }

    try:
        url = f"{MIMO_BASE_URL}/chat/completions"
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = (msg.get("content", "") or "").strip()
        if not content:
            content = (msg.get("reasoning_content", "") or "").strip()
        return content if content else None
    except Exception as e:
        logger.error(f"LLM调用失败: {e}")
        return None


def generate_llm_review(date: str, review_data: dict, adaptive_data: dict = None, market_data: dict = None) -> str:
    """
    生成 LLM 深度复盘分析

    Args:
        date: 日期
        review_data: 当日复盘数据
        adaptive_data: 自适应分析数据
        market_data: 市场行情数据
    """
    # ── 构建分析提示词 ──
    prompt_parts = [
        f"# {date} A股量化系统每日复盘",
        "",
        "请基于以下数据，给出当日战况全面总结和次日操作建议。",
        "",
    ]

    # 1. 账户概况
    prompt_parts.append("## 一、账户概况")
    prompt_parts.append(f"- 初始资金: {review_data.get('initial_capital', 0):,.0f} 元")
    prompt_parts.append(f"- 当前总资产: {review_data.get('total_assets', 0):,.0f} 元")
    prompt_parts.append(f"- 当日盈亏: {review_data.get('daily_pnl', 0):+,.0f} 元 ({review_data.get('daily_pnl_pct', 0):+.2%})")
    prompt_parts.append(f"- 累计盈亏: {review_data.get('cumulative_pnl', 0):+,.0f} 元 ({review_data.get('cumulative_pnl_pct', 0):+.2%})")
    prompt_parts.append(f"- 可用现金: {review_data.get('cash', 0):,.0f} 元")
    prompt_parts.append(f"- 持仓市值: {review_data.get('market_value', 0):,.0f} 元")
    prompt_parts.append(f"- 持仓数量: {review_data.get('position_count', 0)} 只")
    prompt_parts.append("")

    # 2. 持仓明细
    positions = review_data.get("position_pnls", [])
    if positions:
        prompt_parts.append("## 二、持仓明细")
        prompt_parts.append("| 股票 | 买入价 | 现价 | 持仓 | 日盈亏 | 累计盈亏 | 仓位占比 |")
        prompt_parts.append("|------|--------|------|------|--------|----------|----------|")
        for p in positions:
            prompt_parts.append(
                f"| {p.get('name','')}({p.get('code','')}) "
                f"| {p.get('buy_price',0):.2f} "
                f"| {p.get('current_price',0):.2f} "
                f"| {p.get('shares',0)}股 "
                f"| {p.get('daily_pnl_pct',0):+.2%} "
                f"| {p.get('total_pnl_pct',0):+.2%} "
                f"| {p.get('weight',0):.1%} |"
            )
        prompt_parts.append("")

    # 3. 当日交易
    trades = review_data.get("trade_reviews", [])
    if trades:
        prompt_parts.append("## 三、当日交易记录")
        for t in trades:
            action_icon = "🟢买入" if t.get("action") == "BUY" else "🔴卖出"
            pnl_str = f" 盈亏{t.get('result_pct',0):+.2%}" if t.get("action") == "SELL" else ""
            hit_str = "✅命中" if t.get("hit") else "❌未命中"
            prompt_parts.append(
                f"- {action_icon} {t.get('name','')}({t.get('code','')}) "
                f"{t.get('shares',0)}股@{t.get('price',0):.2f} "
                f"{hit_str}{pnl_str} | {t.get('reason','')}"
            )
        prompt_parts.append("")

    # 4. 信号命中率
    total_trades = review_data.get("total_trades", 0)
    win_trades = review_data.get("win_trades", 0)
    lose_trades = review_data.get("lose_trades", 0)
    win_rate = review_data.get("win_rate", 0)
    prompt_parts.append("## 四、信号命中统计")
    prompt_parts.append(f"- 总交易: {total_trades}笔 | 盈利: {win_trades}笔 | 亏损: {lose_trades}笔")
    prompt_parts.append(f"- 胜率: {win_rate:.1%}")
    prompt_parts.append("")

    # 5. 自适应分析
    if adaptive_data and adaptive_data.get("status") == "ok":
        prompt_parts.append("## 五、策略自适应分析")
        overall = adaptive_data.get("overall", {})
        prompt_parts.append(f"- 近期胜率: {overall.get('win_rate', 0):.1%}")
        prompt_parts.append(f"- 盈亏比: {overall.get('profit_factor', 0):.2f}")
        prompt_parts.append(f"- 期望收益: {overall.get('expectancy', 0):+.2%}")

        # 信号维度
        signal_stats = adaptive_data.get("signal_stats", {})
        if signal_stats:
            prompt_parts.append("- 信号维度准确率:")
            for dim, stats in signal_stats.items():
                if dim != "overall":
                    prompt_parts.append(f"  - {dim}: {stats.get('accuracy', 0):.0%}")

        # 来源有效性
        source_stats = adaptive_data.get("source_stats", {})
        if source_stats:
            prompt_parts.append("- 选股来源有效性:")
            for src, stats in source_stats.items():
                prompt_parts.append(f"  - {src}: 胜率{stats.get('win_rate', 0):.0%} 均盈{stats.get('avg_pnl', 0):+.2%}")

        # 参数调整
        adjustments = adaptive_data.get("adjustments", [])
        if adjustments:
            prompt_parts.append("- 系统自动调整:")
            for adj in adjustments:
                prompt_parts.append(f"  - {adj.get('reason', '')}")
        prompt_parts.append("")

    # 6. 市场概况
    if market_data:
        prompt_parts.append("## 六、市场行情概况")
        for k, v in market_data.items():
            prompt_parts.append(f"- {k}: {v}")
        prompt_parts.append("")

    # 7. 分析要求
    prompt_parts.append("## 七、请分析以下内容")
    prompt_parts.append("""
1. **当日战况总结**: 用2-3句话概括今天的表现，是赢是亏，主要原因是什么
2. **持仓诊断**: 逐只分析持仓股票的状态，哪些该持有，哪些需要注意
3. **交易反思**: 今天的买卖决策是否合理，有没有明显的错误
4. **策略评估**: 当前策略的胜率和盈亏比是否健康，需要怎么调整
5. **风险提示**: 当前持仓和市场环境中存在的风险点
6. **次日操作建议**: 明天开盘前应该关注什么，具体的操作建议
7. **策略优化建议**: 基于近期表现，有什么可以改进的地方

请用简洁专业的语言，分点列出，每条建议要具体可执行。
""")

    prompt = "\n".join(prompt_parts)

    logger.info("调用LLM进行深度复盘分析...")
    result = _call_llm(prompt, max_tokens=2000)

    if result:
        logger.info("LLM复盘分析完成")
        return result
    else:
        logger.warning("LLM分析失败，返回基础摘要")
        return _generate_fallback_summary(review_data)


def _generate_fallback_summary(review_data: dict) -> str:
    """LLM不可用时的备用摘要"""
    daily_pnl = review_data.get('daily_pnl', 0)
    daily_pnl_pct = review_data.get('daily_pnl_pct', 0)
    emoji = "📈" if daily_pnl > 0 else "📉" if daily_pnl < 0 else "➡️"

    lines = [
        f"{emoji} 当日战况: 盈亏{daily_pnl:+,.0f}元({daily_pnl_pct:+.2%})",
        "",
        "持仓:",
    ]
    for p in review_data.get("position_pnls", []):
        pnl_emoji = "🟢" if p.get("daily_pnl", 0) > 0 else "🔴"
        lines.append(f"  {pnl_emoji} {p.get('name','')} 日盈亏{p.get('daily_pnl_pct',0):+.2%} 累计{p.get('total_pnl_pct',0):+.2%}")

    trades = review_data.get("trade_reviews", [])
    if trades:
        lines.append("交易:")
        for t in trades:
            icon = "🟢买" if t.get("action") == "BUY" else "🔴卖"
            lines.append(f"  {icon} {t.get('name','')} {t.get('shares',0)}股@{t.get('price',0):.2f}")

    lines.append("")
    lines.append("⚠️ LLM分析不可用，仅显示基础数据")
    return "\n".join(lines)


def format_llm_review_header(date: str, review_data: dict) -> str:
    """生成 LLM 分析前的简洁头部"""
    daily_pnl = review_data.get('daily_pnl', 0)
    daily_pct = review_data.get('daily_pnl_pct', 0)
    total_assets = review_data.get('total_assets', 0)
    cum_pnl = review_data.get('cumulative_pnl', 0)
    cum_pct = review_data.get('cumulative_pnl_pct', 0)

    emoji = "📈" if daily_pnl > 0 else "📉" if daily_pnl < 0 else "➡️"

    return (
        f"{emoji} {date} 复盘报告\n"
        f"{'=' * 40}\n"
        f"总资产: {total_assets:,.0f}元\n"
        f"当日盈亏: {daily_pnl:+,.0f}元 ({daily_pct:+.2%})\n"
        f"累计盈亏: {cum_pnl:+,.0f}元 ({cum_pct:+.2%})\n"
        f"{'=' * 40}"
    )


def extract_and_save_lessons(review_data: dict, llm_analysis: str = None,
                              adaptive_data: dict = None) -> int:
    """
    从亏损/盈利交易中提取教训，存入记忆系统（LLM深度分析 + 规则降级）

    Args:
        review_data: 复盘数据
        llm_analysis: LLM分析文本
        adaptive_data: 自适应分析数据

    Returns:
        保存的教训数量
    """
    trades = review_data.get("trade_reviews", [])
    if not trades:
        return 0

    # 分离亏损和盈利交易
    losing_trades = [t for t in trades if t.get("result_pct", 0) < -2]
    winning_trades = [t for t in trades if t.get("result_pct", 0) > 2]

    # 也从持仓中提取
    for p in review_data.get("position_pnls", []):
        pnl_pct = p.get("total_pnl_pct", 0)
        t = {
            "code": p.get("code", ""),
            "name": p.get("name", ""),
            "pnl_pct": pnl_pct,
            "buy_price": p.get("buy_price", 0),
            "sell_price": p.get("current_price", 0),
            "reason": f"持仓累计{pnl_pct:+.1%}",
        }
        if pnl_pct < -0.05:
            losing_trades.append(t)
        elif pnl_pct > 0.08:
            winning_trades.append(t)

    if not losing_trades and not winning_trades:
        return 0

    # 尝试 LLM 深度分析
    llm_lessons = _analyze_lessons_with_llm(losing_trades, winning_trades)

    # 保存教训
    saved_count = 0
    try:
        from strategy.memory import TradeMemory
        with TradeMemory() as memory:
            if llm_lessons:
                # LLM 分析结果
                for item in llm_lessons:
                    lesson_text = item.get("lesson", "")
                    category = item.get("category", "general")
                    if lesson_text:
                        memory.save_lesson(
                            category=category,
                            content=lesson_text,
                            importance=4 if category in ("risk", "exit") else 3,
                        )
                        saved_count += 1
                        logger.info(f"LLM教训保存: {lesson_text[:50]}...")
            else:
                # 规则降级提取
                saved_count = _extract_lessons_by_rules(
                    memory, losing_trades, winning_trades
                )

            # 从自适应分析中提取教训
            if adaptive_data and adaptive_data.get("status") == "ok":
                for s in adaptive_data.get("suggestions", [])[:3]:
                    if "⚠️" in s or "胜率" in s:
                        memory.save_lesson(category="general", content=s, importance=3)
                        saved_count += 1

            # LLM分析摘要
            if llm_analysis:
                summary = llm_analysis[:200].replace("\n", " ")
                memory.save_lesson(
                    category="general",
                    content=f"LLM复盘摘要: {summary}",
                    importance=2,
                )
                saved_count += 1

        logger.info(f"从复盘中提取并保存了{saved_count}条教训")
    except Exception as e:
        logger.warning(f"提取教训失败: {e}")

    return saved_count


def _analyze_lessons_with_llm(losing_trades: list, winning_trades: list) -> list:
    """
    用 LLM 深度分析亏损/盈利交易，提取教训

    Returns:
        教训列表 [{"lesson": "...", "category": "..."}] 或空列表（失败时）
    """
    if not losing_trades and not winning_trades:
        return []

    # 构建分析 prompt
    analysis_prompt = "分析以下A股交易记录，提取关键教训。\n\n"

    if losing_trades:
        analysis_prompt += "亏损交易:\n"
        for t in losing_trades[:5]:
            analysis_prompt += (
                f"- {t.get('code', '')} {t.get('name', '')} "
                f"亏损{t.get('pnl_pct', t.get('result_pct', 0)):+.1f}% "
                f"买入价{t.get('buy_price', 0):.2f} "
                f"卖出价{t.get('sell_price', t.get('price', 0)):.2f}\n"
            )

    if winning_trades:
        analysis_prompt += "\n盈利交易:\n"
        for t in winning_trades[:5]:
            analysis_prompt += (
                f"- {t.get('code', '')} {t.get('name', '')} "
                f"盈利{t.get('pnl_pct', t.get('result_pct', 0)):+.1f}% "
                f"买入价{t.get('buy_price', 0):.2f} "
                f"卖出价{t.get('sell_price', t.get('price', 0)):.2f}\n"
            )

    analysis_prompt += """
请分析：
1. 亏损的共同原因（入场时机、止损位置、信号误判等）
2. 盈利的共同原因（选股、持仓策略等）
3. 3条最重要的可执行教训

返回JSON:
{"losing_patterns": ["..."], "winning_patterns": ["..."], "lessons": [{"lesson": "...", "category": "entry/exit/risk/signal"}]}
"""

    # 调用 LLM（30秒超时保护）
    response = None
    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTE
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_call_llm_for_review, analysis_prompt)
            response = fut.result(timeout=30)
    except Exception:
        response = None

    # 解析 JSON
    if response:
        try:
            import re
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                analysis = json.loads(json_match.group())
                lessons = analysis.get("lessons", [])
                if lessons:
                    return lessons
        except (json.JSONDecodeError, ValueError):
            pass

    return []


def _call_llm_for_review(prompt: str) -> Optional[str]:
    """调用 MiMo LLM 用于教训分析（独立函数，方便线程池调用）"""
    if not MIMO_API_KEY:
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
                    "你是一位资深A股量化交易复盘分析师。"
                    "分析交易记录，提取可执行的教训。"
                    "返回JSON格式，用中文。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1500,
        "temperature": 0.3,
    }

    try:
        url = f"{MIMO_BASE_URL}/chat/completions"
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = (msg.get("content", "") or "").strip()
        if not content:
            content = (msg.get("reasoning_content", "") or "").strip()
        return content if content else None
    except Exception as e:
        logger.debug(f"教训分析LLM调用失败: {e}")
        return None


def _extract_lessons_by_rules(memory, losing_trades: list, winning_trades: list) -> int:
    """规则提取教训（LLM不可用时的降级方案）"""
    saved = 0

    for t in losing_trades:
        code = t.get("code", "")
        name = t.get("name", "")
        pnl = t.get("pnl_pct", t.get("result_pct", 0))
        reason = t.get("reason", "未知")
        importance = 4 if abs(pnl) > 5 else 3
        memory.save_lesson(
            category="sell",
            content=f"亏损卖出: {name}({code}) 盈亏{pnl:+.1f}% 原因: {reason}",
            importance=importance,
            related_trades=[code],
        )
        saved += 1

    for t in winning_trades:
        code = t.get("code", "")
        name = t.get("name", "")
        pnl = t.get("pnl_pct", t.get("result_pct", 0))
        reason = t.get("reason", "未知")
        memory.save_lesson(
            category="sell",
            content=f"盈利卖出: {name}({code}) 盈亏{pnl:+.1f}% 原因: {reason}",
            importance=3,
            related_trades=[code],
        )
        saved += 1

    return saved


def run_decision_evolution_analysis(db=None) -> dict:
    """
    运行决策准确率分析并保存进化报告（P2-7 Prompt进化）

    在每日复盘流程中调用，分析LLM决策表现并生成优化建议。

    Args:
        db: Database实例，如果为None则自动创建

    Returns:
        分析结果 dict
    """
    try:
        from strategy.prompt_evolution import (
            analyze_decision_accuracy,
            save_evolution_report,
            format_evolution_report,
        )

        if db is None:
            try:
                from data.database import Database
                db = Database().__enter__()
            except Exception:
                logger.warning("无法获取数据库实例，跳过决策进化分析")
                return {}

        # 分析决策准确率
        analysis = analyze_decision_accuracy(db, days=7)

        if analysis.get("total_decisions", 0) > 0:
            # 保存进化报告
            save_evolution_report(analysis, db)
            report_text = format_evolution_report(analysis)
            logger.info(f"决策进化分析完成:\n{report_text}")

            # 自动应用进化建议到prompt
            if analysis.get("prompt_suggestions"):
                try:
                    from strategy.prompt_evolution import apply_evolution_suggestions
                    applied = apply_evolution_suggestions(db, analysis)
                    if applied:
                        logger.info(f"进化建议已自动应用: {applied}条")
                except Exception as e:
                    logger.debug(f"自动应用进化建议失败(非致命): {e}")
        else:
            logger.info("无决策数据，跳过进化分析")

        return analysis

    except Exception as e:
        logger.warning(f"决策进化分析失败: {e}")
        return {}
