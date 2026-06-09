"""
Prompt 进化模块
====================================================================
定期用 LLM 分析自身决策准确率，生成 prompt 优化建议。
====================================================================
"""
import json
import os
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger("strategy.prompt_evolution")

# MiMo API 配置（与 llm_trader.py 一致）
MIMO_API_KEY = os.environ.get("XIAOMI_API_KEY", "")
MIMO_BASE_URL = os.environ.get("XIAOMI_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")
LLM_MODEL = "mimo-v2.5-pro"


def _call_llm(prompt: str, max_tokens: int = 1500) -> Optional[str]:
    """调用 MiMo LLM"""
    import requests
    if not MIMO_API_KEY:
        logger.warning("XIAOMI_API_KEY 未设置，无法调用LLM")
        return None
    headers = {"Authorization": f"Bearer {MIMO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是一位AI交易策略分析师。基于历史决策数据，分析决策质量并给出优化建议。用中文回答。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    try:
        resp = requests.post(f"{MIMO_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        return msg.get("content", "") or msg.get("reasoning_content", "")
    except Exception as e:
        logger.error(f"LLM调用失败: {e}")
        return None


def analyze_decision_accuracy(db, days: int = 7) -> dict:
    """
    分析最近N天的决策准确率

    Returns:
        {
            "total_decisions": 10,
            "win_count": 6,
            "lose_count": 4,
            "win_rate": 0.6,
            "avg_win_pnl": 0.03,
            "avg_lose_pnl": -0.02,
            "by_regime": {...},
            "common_mistakes": ["..."],
            "prompt_suggestions": ["..."]
        }
    """
    if not db:
        return {}

    # 查询已完成的决策
    cursor = db.conn.execute("""
        SELECT d.code, d.action, d.outcome, d.outcome_pct, d.reasoning,
               COALESCE(m.regime, 'unknown') AS regime,
               d.created_at
        FROM llm_decisions d
        LEFT JOIN market_regimes m ON m.date = d.date
        WHERE d.outcome IS NOT NULL
          AND LOWER(d.outcome) != 'pending'
        ORDER BY d.created_at DESC
        LIMIT 50
    """)
    decisions = cursor.fetchall()

    if not decisions:
        return {"total_decisions": 0, "message": "无已完成的决策数据"}

    # 统计
    total = len(decisions)
    wins = [d for d in decisions if str(d[2]).lower() in ("win", "wins")]
    loses = [d for d in decisions if str(d[2]).lower() in ("lose", "loss", "losses")]

    win_rate = len(wins) / total if total > 0 else 0
    avg_win_pnl = (sum((d[3] or 0) / 100 for d in wins) / len(wins)) if wins else 0
    avg_lose_pnl = (sum((d[3] or 0) / 100 for d in loses) / len(loses)) if loses else 0

    # 按市场环境分组
    by_regime = {}
    for d in decisions:
        regime = d[5] or "unknown"
        if regime not in by_regime:
            by_regime[regime] = {"wins": 0, "loses": 0}
        if str(d[2]).lower() in ("win", "wins"):
            by_regime[regime]["wins"] += 1
        else:
            by_regime[regime]["loses"] += 1

    # 构建 LLM 分析 prompt
    analysis_prompt = f"""分析以下AI交易员的决策记录，找出常见错误模式和优化建议。

决策统计:
- 总数: {total}, 胜: {len(wins)}, 负: {len(loses)}, 胜率: {win_rate:.1%}
- 平均盈利: {avg_win_pnl:+.2%}, 平均亏损: {avg_lose_pnl:+.2%}
- 按环境: {json.dumps(by_regime, ensure_ascii=False)}

最近5条亏损决策:
"""
    for d in loses[:5]:
        loss_pct = (d[3] or 0) / 100
        analysis_prompt += f"- {d[0]} {d[1]} | 亏损{loss_pct:+.1%} | 理由:{d[4][:80] if d[4] else '无'} | 环境:{d[5]}\n"

    analysis_prompt += """
请分析:
1. 常见亏损模式（什么情况下容易亏）
2. 决策逻辑的薄弱环节
3. 3条具体的prompt优化建议

返回JSON:
{"common_mistakes": ["..."], "weaknesses": ["..."], "prompt_suggestions": ["建议1", "建议2", "建议3"]}
"""

    # 调用 LLM 分析
    result = {
        "total_decisions": total,
        "win_count": len(wins),
        "lose_count": len(loses),
        "win_rate": round(win_rate, 3),
        "avg_win_pnl": round(avg_win_pnl, 4),
        "avg_lose_pnl": round(avg_lose_pnl, 4),
        "by_regime": by_regime,
    }

    # P2-11: 调用 LLM 分析（30秒超时保护）
    llm_response = None
    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTE
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_call_llm, analysis_prompt)
            llm_response = fut.result(timeout=30)
    except _FTE:
        logger.warning("Prompt进化分析超时(30s)，跳过LLM分析")
    except Exception as e:
        logger.warning(f"Prompt进化LLM调用失败: {e}")

    if llm_response:
        try:
            # 尝试提取 JSON
            json_match = re.search(r'\{.*\}', llm_response, re.DOTALL)
            if json_match:
                llm_analysis = json.loads(json_match.group())
                result.update(llm_analysis)
        except Exception:
            result["llm_raw_analysis"] = llm_response[:500]

    return result


def save_evolution_report(analysis: dict, db=None):
    """保存进化报告到数据库"""
    if not db:
        return

    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt_evolution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            analysis TEXT NOT NULL,
            applied INTEGER DEFAULT 0
        )
    """)
    db.conn.execute(
        "INSERT INTO prompt_evolution (created_at, analysis) VALUES (?, ?)",
        (datetime.now().isoformat(), json.dumps(analysis, ensure_ascii=False))
    )
    db.conn.commit()
    logger.info("进化报告已保存")


def format_evolution_report(analysis: dict) -> str:
    """格式化进化报告"""
    if not analysis:
        return "无分析数据"

    lines = [
        "【决策质量分析报告】",
        f"总决策: {analysis.get('total_decisions', 0)} | 胜率: {analysis.get('win_rate', 0):.1%}",
        f"平均盈利: {analysis.get('avg_win_pnl', 0):+.2%} | 平均亏损: {analysis.get('avg_lose_pnl', 0):+.2%}",
    ]

    mistakes = analysis.get("common_mistakes", [])
    if mistakes:
        lines.append("\n常见亏损模式:")
        for m in mistakes:
            lines.append(f"  - {m}")

    suggestions = analysis.get("prompt_suggestions", [])
    if suggestions:
        lines.append("\nPrompt优化建议:")
        for s in suggestions:
            lines.append(f"  - {s}")

    return "\n".join(lines)


def apply_evolution_suggestions(db, analysis: dict) -> int:
    """
    将进化建议应用到决策prompt中

    实现方式：将优化建议存入 active_prompt_hints 表，
    _build_decision_prompt() 读取这些 hints 并注入到 prompt 中。

    Args:
        db: Database实例
        analysis: 分析结果 dict（需包含 prompt_suggestions 字段）

    Returns:
        应用的建议数量
    """
    suggestions = analysis.get("prompt_suggestions", [])
    if not suggestions:
        return 0

    try:
        # 确保表存在
        db.conn.execute("""
            CREATE TABLE IF NOT EXISTS active_prompt_hints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hint TEXT NOT NULL,
                source TEXT DEFAULT 'evolution',
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)

        # 保存建议（替换旧的）
        db.conn.execute("UPDATE active_prompt_hints SET active = 0")
        applied = 0
        for hint in suggestions[:3]:  # 最多3条
            if hint and len(hint) > 10:
                db.conn.execute(
                    "INSERT INTO active_prompt_hints (hint, source, active, created_at) VALUES (?, 'evolution', 1, ?)",
                    (hint, datetime.now().isoformat())
                )
                applied += 1
        db.conn.commit()

        # 标记为已应用
        db.conn.execute(
            "UPDATE prompt_evolution SET applied = 1 WHERE applied = 0"
        )
        db.conn.commit()

        logger.info(f"进化建议已应用: {applied} 条")
        return applied

    except Exception as e:
        logger.warning(f"应用进化建议失败: {e}")
        return 0
