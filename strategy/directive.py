"""AI 日终策略指令。

盘后 LLM 负责基于当天事实决定下一交易日的策略参数；本模块只负责
约束结构、版本持久化和按生效日期读取，不用固定胜率规则替 AI 调参。
"""
import json
import logging
import re
from datetime import datetime
from typing import Callable, Dict, Optional

from scheduler.market_calendar import next_trading_day

logger = logging.getLogger("strategy.directive")

MIN_SCORE = 45.0
MAX_SCORE = 75.0
MIN_TOP_K = 1
MAX_TOP_K = 5
MIN_WEIGHT = 0.03
MAX_WEIGHT = 0.25


def _next_trading_date(date: str) -> str:
    """将交易日历返回的 YYYYMMDD 统一为数据库使用的 YYYY-MM-DD。"""
    calendar_date = str(date).replace("-", "")
    if len(calendar_date) != 8 or not calendar_date.isdigit():
        raise ValueError(f"无效复盘日期: {date}")
    value = str(next_trading_day(calendar_date))
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def _extract_json(text: str) -> Optional[dict]:
    """兼容 LLM 直接JSON或带 Markdown 代码块的返回。"""
    if not text:
        return None
    candidates = [text.strip()]
    candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL))
    candidates.extend(re.findall(r"(\{.*\})", text, re.DOTALL))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _number(value, field: str, minimum: float, maximum: float, integer: bool = False):
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"策略指令字段 {field} 必须是数值") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"策略指令字段 {field} 必须在 {minimum} 到 {maximum} 之间")
    return number


def normalize_strategy_directive(raw: Dict, review_date: str, effective_date: str,
                                 regime: str) -> Dict:
    """将 LLM 输出规范成可执行且可审计的策略指令。"""
    if not isinstance(raw, dict):
        raise ValueError("策略指令必须是对象")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("策略指令 params 必须是对象")

    summary = str(raw.get("summary") or "").strip()
    diagnosis = str(raw.get("diagnosis") or "").strip()
    rationale = str(raw.get("rationale") or "").strip()
    if not summary or not diagnosis or not rationale:
        raise ValueError("策略指令必须包含 summary、diagnosis 和 rationale")

    now = datetime.now().isoformat()
    version = f"directive-{effective_date.replace('-', '')}-{datetime.now().strftime('%H%M%S%f')}"
    return {
        "version": version,
        "review_date": review_date,
        "effective_date": effective_date,
        "created_at": now,
        "regime": str(raw.get("regime") or regime or "sideways"),
        "intent": str(raw.get("intent") or "平衡巡航").strip(),
        "summary": summary,
        "diagnosis": diagnosis,
        "rationale": rationale,
        "hypothesis": str(raw.get("hypothesis") or "观察该策略版本的决策质量与执行结果").strip(),
        "params": {
            "top_k": _number(params.get("top_k"), "params.top_k", MIN_TOP_K, MAX_TOP_K, integer=True),
            "min_score": _number(params.get("min_score"), "params.min_score", MIN_SCORE, MAX_SCORE),
            "max_weight": _number(params.get("max_weight"), "params.max_weight", MIN_WEIGHT, MAX_WEIGHT),
        },
    }


def _build_prompt(review_date: str, effective_date: str, review_data: Dict,
                  llm_review: str, regime: str, current_params: Dict) -> str:
    """要求 LLM 为下一交易日直接输出结构化策略版本。"""
    return f"""你是 AlphaPilot 的自主策略负责人。请在收盘后，为下一交易日生成一份可直接执行的策略指令。

复盘日期：{review_date}
生效日期：{effective_date}
市场环境：{regime}
账户总资产：{review_data.get('total_assets', 0):.2f}
当日盈亏：{review_data.get('daily_pnl', 0):.2f}
当日交易数：{len(review_data.get('trade_reviews') or [])}
当前策略参数：{json.dumps(current_params, ensure_ascii=False)}

已有日终复盘：
{llm_review or '无'}

自主决策要求：
1. 你可以保持、探索、收紧或放宽策略；不要机械地按连续天数或单一胜率规则行动。
2. 要区分“市场无机会”与“评分门槛让 LLM 没有机会判断”。
3. 只输出一个 JSON 对象，不要 Markdown 或额外文字。
4. params.top_k 必须是 1-5 的整数；params.min_score 必须为 45-75；params.max_weight 必须为 0.03-0.25。

JSON 格式：
{{
  "intent": "平衡巡航/谨慎探索/主动进攻/防守观察等简短意图",
  "regime": "bull/bear/sideways/rebound",
  "summary": "给用户的一句话驾驶结论",
  "diagnosis": "候选、评分、LLM判断、计划或执行链路的主要诊断",
  "rationale": "为什么选择这些参数，必须引用当天事实",
  "hypothesis": "明日怎样的结果会支持或否定这次调整",
  "params": {{"top_k": 3, "min_score": 58, "max_weight": 0.1}}
}}"""


def generate_and_save_strategy_directive(review_date: str, review_data: Dict,
                                         llm_review: str, regime: str,
                                         current_params: Dict,
                                         db_path: str = None,
                                         effective_date: str = None,
                                         llm_call: Callable[[str], Optional[str]] = None) -> Optional[Dict]:
    """生成并持久化下一交易日策略指令；失败时保留上一有效版本。"""
    effective_date = effective_date or _next_trading_date(review_date)
    prompt = _build_prompt(review_date, effective_date, review_data, llm_review, regime, current_params)
    if llm_call is None:
        from review.llm_review import _call_llm
        llm_call = _call_llm
    raw_text = llm_call(prompt)
    raw = _extract_json(raw_text or "")
    if raw is None:
        logger.warning("AI 未返回可解析的策略指令，沿用上一有效版本")
        return None
    try:
        directive = normalize_strategy_directive(raw, review_date, effective_date, regime)
    except ValueError as exc:
        logger.warning("AI 策略指令无效，沿用上一有效版本: %s", exc)
        return None

    from data.database import Database
    with Database(db_path=db_path) as db:
        db.save_strategy_directive(directive, review_text=llm_review)
    logger.info("AI 策略指令已保存: %s，%s 生效", directive["version"], effective_date)
    return directive


def get_effective_trade_policy(date: str, regime: str, db_path: str = None) -> Optional[Dict]:
    """返回当天生效的 AI 策略参数；不存在时由调用方走兼容默认策略。"""
    from data.database import Database
    with Database(db_path=db_path) as db:
        directive = db.get_effective_strategy_directive(date)
    if not directive:
        return None
    params = directive.get("params") or {}
    try:
        normalized = normalize_strategy_directive(
            directive,
            directive.get("review_date", date),
            directive.get("effective_date", date),
            directive.get("regime", regime),
        )
    except ValueError as exc:
        logger.warning("已保存策略指令无效，忽略: %s", exc)
        return None
    normalized["version"] = directive.get("version", normalized["version"])
    return normalized
