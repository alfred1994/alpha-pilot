"""
五维择时信号模块
====================================================================
参考"五维择时策略"，从市场级别判断当前仓位：
  1. 估值因子：ERP = 1/PE - 10年国债利率
  2. 资金因子：融资余额变化 + 北向资金流向
  3. 技术因子：沪深300布林带位置 + 市场广度
  4. 情绪因子：涨跌停比 + 市场恐贪
  5. 基本面因子：制造业PMI

每个因子输出 -1(看空) / 0(中性) / +1(看多)
最终投票 → 仓位比例 (100% / 50% / 0%)
====================================================================
数据源: 全部使用东方财富datacenter API + 腾讯API，不依赖AKShare
====================================================================
"""
import logging
import time
import requests
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger("signals.market_timing")

# 缓存（市场级数据，全局共享）
_CACHE: Dict[str, tuple] = {}  # key -> (value, timestamp)
_CACHE_TTL = 600  # 10分钟

# 东方财富 API 通用headers
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}


def _get_cached(key: str):
    if key in _CACHE:
        val, ts = _CACHE[key]
        if time.time() - ts < _CACHE_TTL:
            return val
    return None


def _set_cached(key: str, val):
    _CACHE[key] = (val, time.time())


@dataclass
class TimingSignal:
    """择时信号"""
    name: str
    signal: int           # -1, 0, +1
    value: float          # 原始值
    detail: str


@dataclass
class MarketTimingResult:
    """五维择时综合结果"""
    valuation: TimingSignal
    capital: TimingSignal
    technical: TimingSignal
    emotion: TimingSignal
    fundamental: TimingSignal
    final_signal: int     # -1, 0, +1
    position_pct: int     # 0, 50, 100
    vote_detail: str

    def __repr__(self):
        return f"MarketTiming(signal={self.final_signal}, position={self.position_pct}%)"


# ══════════════════════════════════════════════════════════════════
# 1. 估值因子：ERP = 1/PE - 10年国债利率
# 数据源: 腾讯API(PE) + 东方财富datacenter(国债收益率)
# ══════════════════════════════════════════════════════════════════

def _get_bond_yield() -> Optional[float]:
    """获取10年国债收益率（%），东方财富datacenter API"""
    cached = _get_cached("bond_yield")
    if cached is not None:
        return cached

    try:
        url = (
            "https://datacenter.eastmoney.com/api/data/get?"
            "type=RPTA_WEB_TREASURYYIELD&sty=ALL&st=SOLAR_DATE&sr=-1"
            "&p=1&ps=3&source=WEB&client=WEB"
        )
        resp = requests.get(url, headers=_EM_HEADERS, timeout=8)
        data = resp.json()
        if data.get("result") and data["result"].get("data"):
            latest = data["result"]["data"][0]
            # EMM00166466 = 中国国债收益率10年
            rate = latest.get("EMM00166466")
            if rate is not None:
                _set_cached("bond_yield", float(rate))
                return float(rate)
    except Exception as e:
        logger.warning(f"国债收益率获取失败: {e}")

    return None


def _calc_valuation_signal() -> TimingSignal:
    """
    股权风险溢价 (ERP)
    ERP = 1/PE - 10年国债收益率
    ERP越高 → 股票越便宜 → 看多
    """
    cached = _get_cached("valuation")
    if cached:
        return cached

    try:
        # 获取沪深300 PE（腾讯API）
        from data.realtime import get_realtime
        quotes = get_realtime(["sh000300"])  # 【Phase1-Task4】统一传入list参数
        pe = quotes[0].pe if quotes and quotes[0].pe > 0 else None

        # 获取10年国债收益率（东方财富datacenter）
        bond_rate = _get_bond_yield()
        bond_rate_decimal = bond_rate / 100 if bond_rate else None

        if pe and pe > 0 and bond_rate_decimal is not None:
            erp = 1.0 / pe - bond_rate_decimal
            # ERP历史范围约 -2% ~ 6%
            if erp > 0.04:
                signal = 1
                detail = f"ERP={erp:.2%} 股票极具吸引力"
            elif erp > 0.02:
                signal = 1
                detail = f"ERP={erp:.2%} 股票较有吸引力"
            elif erp > 0:
                signal = 0
                detail = f"ERP={erp:.2%} 中性"
            else:
                signal = -1
                detail = f"ERP={erp:.2%} 股票不如债券"
        else:
            signal = 0
            erp = 0
            detail = f"数据不足(PE={pe}, 国债={bond_rate})"

        result = TimingSignal("估值因子", signal, erp, detail)
        _set_cached("valuation", result)
        return result

    except Exception as e:
        logger.warning(f"估值因子计算失败: {e}")
        return TimingSignal("估值因子", 0, 0, f"计算异常: {e}")


# ══════════════════════════════════════════════════════════════════
# 2. 资金因子：融资余额变化 + 北向资金
# 数据源: 东方财富datacenter API (RPTA_WEB_RZRQ_GGMX + RPT_MUTUAL_DEAL_HISTORY)
# ══════════════════════════════════════════════════════════════════

def _calc_capital_signal() -> TimingSignal:
    """
    资金因子
    - 融资余额变化：连续增长→看多
    - 北向资金：净流入→看多
    """
    cached = _get_cached("capital")
    if cached:
        return cached

    score = 0
    details = []

    # 融资余额变化（东方财富datacenter）
    try:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "reportName=RPTA_WEB_RZRQ_GGMX&columns=DATE,RZYE"
            "&pageSize=10&pageNumber=1&sortTypes=-1&sortColumns=DATE"
            "&source=WEB&client=WEB"
        )
        resp = requests.get(url, headers=_EM_HEADERS, timeout=8)
        data = resp.json()
        if data.get("result") and data["result"].get("data"):
            items = data["result"]["data"]
            # 按日期分组，取每天的总融资余额
            date_balance = {}
            for item in items:
                date = str(item.get("DATE", ""))[:10]
                balance = float(item.get("RZYE", 0) or 0)
                if date not in date_balance:
                    date_balance[date] = 0
                date_balance[date] += balance

            dates = sorted(date_balance.keys(), reverse=True)
            if len(dates) >= 3:
                b0 = date_balance[dates[0]]
                b1 = date_balance[dates[1]]
                b2 = date_balance[dates[2]]
                if b0 > b1 > b2:
                    score += 1
                    details.append("融资余额连续增长")
                elif b0 < b1 < b2:
                    score -= 1
                    details.append("融资余额连续下降")
                else:
                    details.append("融资余额震荡")
    except Exception as e:
        details.append(f"融资数据异常: {e}")

    # 北向资金（东方财富datacenter）
    try:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "reportName=RPT_MUTUAL_DEAL_HISTORY"
            "&columns=MUTUAL_TYPE,TRADE_DATE,NET_DEAL_AMT"
            "&pageSize=10&pageNumber=1&sortColumns=TRADE_DATE&sortTypes=-1"
            "&source=WEB&client=WEB"
        )
        resp = requests.get(url, headers=_EM_HEADERS, timeout=8)
        data = resp.json()
        if data.get("result") and data["result"].get("data"):
            # MUTUAL_TYPE: 006=北向合计, 004=沪股通, 002=深股通
            north_net = None
            for item in data["result"]["data"]:
                if item.get("MUTUAL_TYPE") == "006":
                    north_net = item.get("NET_DEAL_AMT")
                    break

            if north_net is not None:
                north_net = float(north_net)
                if north_net > 50:
                    score += 1
                    details.append(f"北向大幅流入{north_net:.0f}亿")
                elif north_net > 0:
                    details.append(f"北向小幅流入{north_net:.0f}亿")
                elif north_net < -50:
                    score -= 1
                    details.append(f"北向大幅流出{north_net:.0f}亿")
                else:
                    details.append(f"北向小幅流出{north_net:.0f}亿")
    except Exception as e:
        details.append(f"北向数据异常: {e}")

    signal = max(-1, min(1, score))
    result = TimingSignal("资金因子", signal, score, " | ".join(details))
    _set_cached("capital", result)
    return result


# ══════════════════════════════════════════════════════════════════
# 3. 技术因子：沪深300布林带 + 市场广度
# 数据源: Baostock(历史行情) + 东方财富(涨跌停)
# ══════════════════════════════════════════════════════════════════

def _calc_technical_signal() -> TimingSignal:
    """
    技术因子
    - 布林带：价格在上轨之上→看多，下轨之下→看空
    - 市场广度：涨停股票数量
    """
    cached = _get_cached("technical")
    if cached:
        return cached

    score = 0
    details = []

    # 沪深300布林带（Baostock）
    try:
        from data.history import query_baostock_history_rows
        raw = query_baostock_history_rows(
            "sh.000300", "date,close",
            start_date="2025-01-01", frequency="d"
        )
        data = (raw or {}).get("data") or []

        if data and len(data) > 20:
            close = pd.Series([float(row[1]) for row in data])
            ma20 = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            upper = ma20 + 2 * std20
            lower = ma20 - 2 * std20
            cur = close.iloc[-1]

            if cur > upper.iloc[-1]:
                score += 1
                details.append(f"沪深300突破布林上轨({cur:.0f}>{upper.iloc[-1]:.0f})")
            elif cur < lower.iloc[-1]:
                score -= 1
                details.append(f"沪深300跌破布林下轨({cur:.0f}<{lower.iloc[-1]:.0f})")
            else:
                pct = (cur - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1]) * 100
                details.append(f"沪深300布林带位置{pct:.0f}%")
    except Exception as e:
        details.append(f"布林带异常: {e}")

    # 市场广度（东方财富涨停板）
    try:
        from data.eastmoney import get_limit_up
        zt = get_limit_up()
        zt_count = len(zt) if zt else 0
        if zt_count > 80:
            score += 1
            details.append(f"涨停{zt_count}只 市场活跃")
        elif zt_count > 30:
            details.append(f"涨停{zt_count}只 市场正常")
        else:
            score -= 1
            details.append(f"涨停{zt_count}只 市场冷淡")
    except Exception:
        details.append("市场广度数据异常")

    signal = max(-1, min(1, score))
    result = TimingSignal("技术因子", signal, score, " | ".join(details))
    _set_cached("technical", result)
    return result


# ══════════════════════════════════════════════════════════════════
# 4. 情绪因子：涨跌停比 + 恐贪指标
# 数据源: 东方财富(涨跌停) + 已有情绪模块
# ══════════════════════════════════════════════════════════════════

def _calc_emotion_signal() -> TimingSignal:
    """
    情绪因子（反向指标）
    - 涨跌停比过高 → 市场过热 → 看空
    - 涨跌停比过低 → 市场恐慌 → 看多（抄底机会）
    """
    cached = _get_cached("emotion")
    if cached:
        return cached

    try:
        from signals.sentiment import market_mood_signal
        mood = market_mood_signal()
        mood_score = mood.score  # 0-100

        # 情绪是反向指标
        if mood_score > 80:
            signal = -1
            detail = f"市场情绪{mood_score} 过度乐观(反向看空)"
        elif mood_score > 70:
            signal = 0
            detail = f"市场情绪{mood_score} 偏乐观"
        elif mood_score > 30:
            signal = 0
            detail = f"市场情绪{mood_score} 中性"
        elif mood_score > 20:
            signal = 1
            detail = f"市场情绪{mood_score} 偏悲观(机会)"
        else:
            signal = 1
            detail = f"市场情绪{mood_score} 极度恐慌(反向看多)"

        result = TimingSignal("情绪因子", signal, mood_score, detail)
        _set_cached("emotion", result)
        return result

    except Exception as e:
        logger.warning(f"情绪因子计算失败: {e}")
        return TimingSignal("情绪因子", 0, 50, f"计算异常: {e}")


# ══════════════════════════════════════════════════════════════════
# 5. 基本面因子：PMI
# 数据源: 东方财富datacenter API (RPT_ECONOMY_PMI)
# ══════════════════════════════════════════════════════════════════

def _calc_fundamental_signal() -> TimingSignal:
    """
    基本面因子
    - PMI > 50 → 经济扩张 → 看多
    - PMI < 50 → 经济收缩
    - PMI 极低后回升 → 强看多（经济见底回升）
    """
    cached = _get_cached("fundamental")
    if cached:
        return cached

    try:
        # 东方财富PMI数据
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "reportName=RPT_ECONOMY_PMI&columns=REPORT_DATE,MAKE_INDEX,MAKE_SAME,NMAKE_INDEX"
            "&pageSize=5&pageNumber=1&sortColumns=REPORT_DATE&sortTypes=-1"
            "&source=WEB&client=WEB"
        )
        resp = requests.get(url, headers=_EM_HEADERS, timeout=8)
        data = resp.json()

        if data.get("result") and data["result"].get("data"):
            items = data["result"]["data"][:3]
            pmi_values = [float(item.get("MAKE_INDEX", 50)) for item in items]
            latest_pmi = pmi_values[0]

            if latest_pmi > 52:
                signal = 1
                detail = f"PMI={latest_pmi:.1f} 经济强劲扩张"
            elif latest_pmi > 50:
                if len(pmi_values) >= 3 and pmi_values[0] > pmi_values[1] > pmi_values[2]:
                    signal = 1
                    detail = f"PMI={latest_pmi:.1f} 连续回升"
                else:
                    signal = 0
                    detail = f"PMI={latest_pmi:.1f} 温和扩张"
            elif latest_pmi > 48:
                signal = 0
                detail = f"PMI={latest_pmi:.1f} 荣枯线附近"
            else:
                if len(pmi_values) >= 3 and pmi_values[0] > pmi_values[1]:
                    signal = 1
                    detail = f"PMI={latest_pmi:.1f} 低位回升(见底信号)"
                else:
                    signal = -1
                    detail = f"PMI={latest_pmi:.1f} 经济收缩"
        else:
            signal = 0
            latest_pmi = 50
            detail = "PMI数据不足"

        result = TimingSignal("基本面因子", signal, latest_pmi, detail)
        _set_cached("fundamental", result)
        return result

    except Exception as e:
        logger.warning(f"基本面因子计算失败: {e}")
        return TimingSignal("基本面因子", 0, 50, f"计算异常: {e}")


# ══════════════════════════════════════════════════════════════════
# 综合投票
# ══════════════════════════════════════════════════════════════════

def compute_market_timing() -> MarketTimingResult:
    """
    五维择时主函数

    每个维度输出 -1/0/+1，五个维度投票决定仓位：
    - 总票 >= 3 → 看多 → 100%仓位
    - 总票 >= 1 → 中性 → 50%仓位
    - 总票 <= 0 → 看空 → 0%仓位
    """
    logger.info("五维择时计算开始...")

    val = _calc_valuation_signal()
    cap = _calc_capital_signal()
    tech = _calc_technical_signal()
    emo = _calc_emotion_signal()
    fund = _calc_fundamental_signal()

    total = val.signal + cap.signal + tech.signal + emo.signal + fund.signal

    if total >= 3:
        final_signal = 1
        position_pct = 100
    elif total >= 1:
        final_signal = 0
        position_pct = 50
    else:
        final_signal = -1
        position_pct = 0

    vote_detail = (
        f"估值{val.signal:+d} 资金{cap.signal:+d} 技术{tech.signal:+d} "
        f"情绪{emo.signal:+d} 基本面{fund.signal:+d} → 总分{total:+d}"
    )

    result = MarketTimingResult(
        valuation=val, capital=cap, technical=tech,
        emotion=emo, fundamental=fund,
        final_signal=final_signal, position_pct=position_pct,
        vote_detail=vote_detail,
    )

    logger.info(f"五维择时: {vote_detail} → 仓位{position_pct}%")
    return result


def format_timing_report(result: MarketTimingResult) -> str:
    """格式化择时报告"""
    signal_emoji = {1: "🟢看多", 0: "🟡中性", -1: "🔴看空"}
    lines = [
        "=" * 50,
        "五维择时报告",
        "=" * 50,
        f"  {result.valuation.name}: {signal_emoji.get(result.valuation.signal, '?')} {result.valuation.detail}",
        f"  {result.capital.name}: {signal_emoji.get(result.capital.signal, '?')} {result.capital.detail}",
        f"  {result.technical.name}: {signal_emoji.get(result.technical.signal, '?')} {result.technical.detail}",
        f"  {result.emotion.name}: {signal_emoji.get(result.emotion.signal, '?')} {result.emotion.detail}",
        f"  {result.fundamental.name}: {signal_emoji.get(result.fundamental.signal, '?')} {result.fundamental.detail}",
        "-" * 50,
        f"  投票: {result.vote_detail}",
        f"  建议仓位: {result.position_pct}%",
        "=" * 50,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    result = compute_market_timing()
    print(format_timing_report(result))
