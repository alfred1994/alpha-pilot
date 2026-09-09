"""
可转债T+0策略引擎
正股涨停→资金转向可转债→T+0日内套利

评分体系（参照OpenClaw文档）:
  溢价率    40%  <5%最优
  跟随度    20%  转债涨幅/正股涨幅 > 80%
  剩余规模  20%  <5亿最优
  换手率    15%  >20%最优
  量比       5%  >2最优

风控纪律:
  - 严禁隔夜持仓
  - 溢价>30%不追
  - 正股炸板立即止盈
  - 止损-3%
  - 单只仓位≤20%
"""
import logging
import math
import threading
import time
from typing import Dict, List, Optional
from datetime import datetime

from config import (
    CB_MAX_PREMIUM, CB_STOP_LOSS, CB_SINGLE_POSITION,
    CB_MIN_SCORE, CB_T0_ENABLED,
)

logger = logging.getLogger("strategy.cb_t0")


# 可转债列表一次返回转债、正股与溢价所需字段。退出巡检可能由多个入口在
# 一个轮询周期内调用，短期缓存只用于请求限流，不能当作源行情时间戳。
_EXIT_CONTEXT_CACHE = {"fetched_at": 0.0, "by_code": {}, "refreshing": False}
_EXIT_CONTEXT_LOCK = threading.Lock()


# ── 评分权重 ─────────────────────────────────────────────────
WEIGHTS = {
    "premium": 0.40,       # 溢价率
    "following": 0.20,     # 跟随度
    "scale": 0.20,         # 剩余规模
    "turnover": 0.15,      # 换手率
    "volume_ratio": 0.05,  # 量比
}


def score_cb(cb: dict) -> dict:
    """
    对单只可转债评分

    Args:
        cb: 可转债数据字典（来自 convertible_bond.get_cb_list）

    Returns:
        {total_score, details, signal, action}
    """
    details = {}

    # 1. 溢价率评分 (40%) — 越低越好，<5%满分
    premium = cb.get("premium_rate", 100)
    if premium <= 0:
        details["premium"] = 100  # 负溢价 = 折价，极好
    elif premium < 5:
        details["premium"] = 100
    elif premium < 10:
        details["premium"] = 90 - (premium - 5) * 8
    elif premium < 20:
        details["premium"] = 50 - (premium - 10) * 3
    elif premium < 30:
        details["premium"] = 20 - (premium - 20) * 1.5
    else:
        details["premium"] = 0  # 溢价>30%不追

    # 2. 跟随度评分 (20%) — 转债涨幅/正股涨幅，>80%满分
    stock_chg = abs(cb.get("stock_change_pct", 0))
    cb_chg = abs(cb.get("cb_change_pct", 0))
    if stock_chg > 0.5:  # 正股涨幅>0.5%才有意义
        following = min(cb_chg / stock_chg * 100, 150)  # 上限150%
        if following >= 80:
            details["following"] = 100
        elif following >= 50:
            details["following"] = 60 + (following - 50) * 1.33
        else:
            details["following"] = following * 1.2
    else:
        details["following"] = 50  # 正股没动，中性

    # 3. 剩余规模评分 (20%) — 越小越好，<5亿满分
    scale = cb.get("remaining_scale", 100)
    if scale <= 0:
        details["scale"] = 50  # 数据缺失
    elif scale < 2:
        details["scale"] = 100
    elif scale < 5:
        details["scale"] = 80 + (5 - scale) * 6.67
    elif scale < 10:
        details["scale"] = 50 + (10 - scale) * 6
    else:
        details["scale"] = max(0, 50 - (scale - 10) * 2)

    # 4. 换手率评分 (15%) — 越高越好，>20%满分
    turnover = cb.get("turnover_rate", 0)
    if turnover >= 20:
        details["turnover"] = 100
    elif turnover >= 10:
        details["turnover"] = 70 + (turnover - 10) * 3
    elif turnover >= 5:
        details["turnover"] = 50 + (turnover - 5) * 4
    elif turnover >= 1:
        details["turnover"] = 20 + (turnover - 1) * 7.5
    else:
        details["turnover"] = turnover * 20

    # 5. 量比评分 (5%) — >2满分（简化：用成交额/剩余规模估算）
    trade_amount = cb.get("trade_amount", 0)  # 万元
    remaining = cb.get("remaining_scale", 1) * 10000  # 转为万元
    if remaining > 0 and trade_amount > 0:
        vol_ratio = trade_amount / remaining
        if vol_ratio >= 0.02:  # 成交额占规模2%以上
            details["volume_ratio"] = 100
        elif vol_ratio >= 0.01:
            details["volume_ratio"] = 70
        else:
            details["volume_ratio"] = 40
    else:
        details["volume_ratio"] = 50

    # 加权总分
    total = sum(
        details.get(k, 0) * v
        for k, v in WEIGHTS.items()
    )

    # 信号判定
    if total >= 80:
        signal = "STRONG_BUY"
        action = "强烈买入"
    elif total >= CB_MIN_SCORE:
        signal = "BUY"
        action = "买入"
    elif total >= 50:
        signal = "WATCH"
        action = "观察"
    else:
        signal = "SKIP"
        action = "跳过"

    return {
        "cb_code": cb.get("cb_code", ""),
        "cb_name": cb.get("cb_name", ""),
        "stock_code": cb.get("stock_code", ""),
        "stock_name": cb.get("stock_name", ""),
        "total_score": round(total, 1),
        "details": {k: round(v, 1) for k, v in details.items()},
        "signal": signal,
        "action": action,
        "premium_rate": premium,
        "stock_change_pct": cb.get("stock_change_pct", 0),
        "cb_change_pct": cb.get("cb_change_pct", 0),
        "cb_price": cb.get("cb_price", 0),
        "remaining_scale": scale,
    }


def scan_and_score() -> List[dict]:
    """
    扫描全市场可转债并评分，返回按分数排序的结果

    Returns:
        评分结果列表（降序）
    """
    if not CB_T0_ENABLED:
        logger.info("可转债T+0策略已禁用")
        return []

    from data.convertible_bond import get_cb_list

    cbs = get_cb_list()
    if not cbs:
        return []

    # 预筛选: 排除明显不合格的
    candidates = []
    for cb in cbs:
        # 排除溢价>30%
        if cb.get("premium_rate", 100) > CB_MAX_PREMIUM * 100:
            continue
        # 排除价格过低（<90）或过高（>200）
        price = cb.get("cb_price", 0)
        if price < 90 or price > 200:
            continue
        # 排除正股跌幅>5%的
        if cb.get("stock_change_pct", 0) < -5:
            continue
        candidates.append(cb)

    # 评分
    scored = []
    for cb in candidates:
        result = score_cb(cb)
        scored.append(result)

    # 按分数降序
    scored.sort(key=lambda x: x["total_score"], reverse=True)

    logger.info(f"可转债评分: {len(scored)}只候选, Top3={[(s['cb_name'], s['total_score']) for s in scored[:3]]}")
    return scored


def is_cb_code(code: str) -> bool:
    """判断是否为可转债代码（沪市 110/111/113/118，深市 123/127/128）。"""
    c = str(code or "").strip()
    return len(c) == 6 and c.isdigit() and c.startswith((
        "110", "111", "113", "118", "123", "127", "128",
    ))


def should_buy(cb: dict, max_single_weight: Optional[float] = None) -> dict:
    """
    买入决策

    Args:
        cb: 评分后的可转债数据
        max_single_weight: 单票仓位硬上限（可选，结合全系统策略指令统一风控预算）

    Returns:
        {buy: bool, reason: str, position_pct: float}
    """
    score = cb.get("total_score", 0)
    premium = cb.get("premium_rate", 100)

    # 溢价>30%不追
    if premium > CB_MAX_PREMIUM * 100:
        return {"buy": False, "reason": f"溢价率{premium:.1f}%>{CB_MAX_PREMIUM*100}%", "position_pct": 0}

    # 分数不够
    if score < CB_MIN_SCORE:
        return {"buy": False, "reason": f"评分{score}<{CB_MIN_SCORE}", "position_pct": 0}

    # 正股涨幅需要>=7%（接近涨停）
    stock_chg = cb.get("stock_change_pct", 0)
    if stock_chg < 7:
        return {"buy": False, "reason": f"正股涨幅{stock_chg:.1f}%<7%", "position_pct": 0}

    base_cap = CB_SINGLE_POSITION
    if max_single_weight is not None and max_single_weight > 0:
        base_cap = min(base_cap, float(max_single_weight))

    # 计算仓位（根据分数动态调整）
    if score >= 85:
        position_pct = base_cap
    elif score >= 75:
        position_pct = base_cap * 0.75
    else:
        position_pct = base_cap * 0.5

    return {
        "buy": True,
        "reason": f"评分{score} 正股+{stock_chg:.1f}% 溢价{premium:.1f}%",
        "position_pct": position_pct,
    }


def should_sell(cb_code: str, current_data, buy_price: float) -> dict:
    """
    卖出决策（包含止损、正股炸板、溢价扩大）

    Args:
        cb_code: 转债代码
        current_data: 当前数据 dict {cb_price, stock_change_pct, premium_rate} 或当前浮点价格
        buy_price: 买入价格

    Returns:
        {sell: bool, reason: str}
    """
    if isinstance(current_data, (int, float)):
        current_price = _optional_float(current_data)
        stock_chg = None
        premium = None
    elif isinstance(current_data, dict):
        current_price = _optional_float(current_data.get("cb_price"))
        # 不能用 ``or`` 设默认值：真实的 0% 正是正股炸板的退出信号。
        # 字段缺失时只跳过相应的附加规则，保留价格止损。
        stock_chg = _optional_float(current_data.get("stock_change_pct"))
        premium = _optional_float(current_data.get("premium_rate"))
    else:
        return {"sell": False, "reason": "数据格式异常"}

    if current_price is None or current_price <= 0 or buy_price <= 0:
        return {"sell": False, "reason": "价格数据缺失"}

    pnl = (current_price - buy_price) / buy_price

    # 止损 -3% (CB_STOP_LOSS, 默认 -0.03)
    if pnl <= CB_STOP_LOSS:
        return {"sell": True, "reason": f"可转债止损触发: {pnl:+.1%} <= {CB_STOP_LOSS:.0%}"}

    # 正股炸板（涨幅回落到<3%）
    if stock_chg is not None and stock_chg < 3:
        return {"sell": True, "reason": f"可转债正股炸板: 涨幅回落至{stock_chg:.1f}%"}

    # 溢价扩大到>30%
    if premium is not None and premium > 30:
        return {"sell": True, "reason": f"可转债溢价扩大: {premium:.1f}%>30%"}

    return {"sell": False, "reason": "继续持有"}


def get_cb_exit_market_context(cb_codes, max_age_seconds: float = 30.0) -> Dict[str, dict]:
    """批量获取可转债退出所需的正股涨幅与溢价上下文。

    返回值按转债代码索引，数据源失败、字段无效或某只转债缺失时不伪造正股、
    溢价字段；调用方仍可将已有的转债现价作为 ``cb_price`` 传给
    :func:`should_sell` 来执行严格价格止损。数据源未提供逐标的时间戳，缓存
    时间只用于限制请求频率，不能被用于声明源行情新鲜度。
    """
    requested = {
        code for code in (str(value or "").strip() for value in cb_codes)
        if is_cb_code(code)
    }
    if not requested:
        return {}

    try:
        max_age = max(0.0, float(max_age_seconds))
    except (TypeError, ValueError):
        max_age = 30.0
    now = time.monotonic()
    with _EXIT_CONTEXT_LOCK:
        cache = _EXIT_CONTEXT_CACHE
        needs_refresh = now - cache["fetched_at"] > max_age
        if needs_refresh and not cache.get("refreshing"):
            # 外部 AkShare 调用没有可靠的调用方超时契约。止损巡检不能等待它：
            # 后台单飞刷新，当前轮降级为价格止损，待有效缓存可用后再补足规则。
            cache["refreshing"] = True
            threading.Thread(
                target=_refresh_cb_exit_context,
                name="cb-exit-context-refresh",
                daemon=True,
            ).start()
        # 已过期的数据不能在刷新期间继续驱动正股炸板或溢价退出；当前轮
        # 安全降级为价格止损，只有刷新成功后的下一轮才恢复扩展规则。
        records_by_code = {} if needs_refresh else dict(cache["by_code"])

    contexts = {}
    for code in requested:
        record = records_by_code.get(code)
        if not record:
            continue
        context = dict(record)
        # data.convertible_bond 保留 0.0 的兼容值，同时附带有效性标志。
        # 无效字段必须缺席，令 should_sell 安全降级为价格止损。
        for field in ("stock_change_pct", "premium_rate"):
            if context.get(f"{field}_valid") is False:
                context.pop(field, None)
        contexts[code] = context
    return contexts


def _refresh_cb_exit_context() -> None:
    """异步刷新退出上下文；任何失败均使附加退出规则安全降级。"""
    records_by_code = {}
    try:
        from data.convertible_bond import get_cb_list

        records = get_cb_list()
        records_by_code = {
            str(record.get("cb_code", "")).strip(): dict(record)
            for record in records
            if str(record.get("cb_code", "")).strip()
        }
    except Exception as exc:
        logger.warning("可转债退出上下文获取失败，仅保留价格止损: %s", exc)
    finally:
        with _EXIT_CONTEXT_LOCK:
            # 无逐标的源时间戳时，这里只记录本地刷新完成时刻用于限流。
            _EXIT_CONTEXT_CACHE["by_code"] = records_by_code
            _EXIT_CONTEXT_CACHE["fetched_at"] = time.monotonic()
            _EXIT_CONTEXT_CACHE["refreshing"] = False


def _optional_float(value) -> Optional[float]:
    """将存在且可解析的数值转换为 float；保留缺失语义。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def format_cb_report(scored: List[dict], top_n: int = 5) -> str:
    """格式化可转债扫描报告"""
    if not scored:
        return "无可转债机会"

    lines = [
        "=" * 55,
        "可转债T+0扫描报告",
        "=" * 55,
    ]

    for i, cb in enumerate(scored[:top_n], 1):
        signal_icon = {"STRONG_BUY": "🟢", "BUY": "🔵", "WATCH": "🟡", "SKIP": "⚪"}.get(cb["signal"], "⚪")
        lines.append(
            f"{signal_icon} #{i} {cb['cb_name']}({cb['cb_code']}) "
            f"分数={cb['total_score']} {cb['action']}"
        )
        lines.append(
            f"   正股: {cb['stock_name']}({cb['stock_code']}) "
            f"涨幅={cb['stock_change_pct']:+.1f}%"
        )
        lines.append(
            f"   转债: 现价={cb['cb_price']:.2f} "
            f"涨幅={cb['cb_change_pct']:+.1f}% "
            f"溢价率={cb['premium_rate']:.1f}%"
        )
        details = cb.get("details", {})
        lines.append(
            f"   评分: 溢价={details.get('premium', 0):.0f} "
            f"跟随={details.get('following', 0):.0f} "
            f"规模={details.get('scale', 0):.0f} "
            f"换手={details.get('turnover', 0):.0f} "
            f"量比={details.get('volume_ratio', 0):.0f}"
        )
        lines.append("")

    lines.append("=" * 55)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    print("=== 可转债T+0策略测试 ===\n")
    scored = scan_and_score()
    print(format_cb_report(scored))
