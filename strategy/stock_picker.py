"""
AI选股器
从涨停板 + 龙虎榜 + 北向资金三个维度筛选候选股票
输出 Top N 候选列表供决策引擎使用
"""
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime

import pandas as pd
import requests

from config import PICKER_TOP_N, PICKER_MIN_SCORE, PICKER_MAX_PE, PICKER_MIN_AMOUNT

logger = logging.getLogger("strategy.picker")


@dataclass
class Candidate:
    """候选股票"""
    code: str
    name: str
    source: List[str]          # 来源: ["涨停板", "龙虎榜", "北向"]
    score: float = 0.0         # 综合候选分 0-100
    limit_up_days: int = 0     # 连板天数
    lhb_net_buy: float = 0.0   # 龙虎榜净买额（万）
    north_flow: float = 0.0    # 北向净买入（万）
    change_pct: float = 0.0    # 涨跌幅
    amount: float = 0.0        # 成交额（万）
    industry: str = ""         # 所属行业

    def __repr__(self):
        return f"{self.code} {self.name} score={self.score:.1f} src={','.join(self.source)}"


def _get_limit_up_candidates() -> Dict[str, Candidate]:
    """从涨停板获取候选"""
    from data.market import get_limit_up

    candidates = {}
    df = get_limit_up()
    if df is None or df.empty:
        return candidates

    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if not code:
            continue
        name = str(row.get("名称", ""))
        c = Candidate(
            code=code,
            name=name,
            source=["涨停板"],
            limit_up_days=int(row.get("连板数", 1) or 1),
            change_pct=float(row.get("涨跌幅", 0) or 0),
            amount=float(row.get("成交额", 0) or 0) / 10000,  # 转万元
            industry=str(row.get("所属行业", "")),
        )
        # 连板加分: 基础分40, 每板+15分, 上限80
        c.score = min(80, 40 + c.limit_up_days * 15)
        candidates[code] = c

    logger.info(f"涨停板候选: {len(candidates)}只")
    return candidates


def _get_dragon_tiger_candidates() -> Dict[str, Candidate]:
    """从龙虎榜获取候选"""
    from data.market import get_dragon_tiger

    candidates = {}
    df = get_dragon_tiger()
    if df is None or df.empty:
        return candidates

    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if not code:
            continue
        name = str(row.get("名称", ""))
        net_buy = float(row.get("龙虎榜净买额", 0) or 0) / 10000  # 转万元
        c = Candidate(
            code=code,
            name=name,
            source=["龙虎榜"],
            lhb_net_buy=net_buy,
            change_pct=float(row.get("涨跌幅", 0) or 0),
        )
        # 净买额为正加分, 为负减分
        if net_buy > 0:
            c.score = min(25, 15 + net_buy / 1000)  # 净买1000万+1分
        else:
            c.score = max(0, 10 + net_buy / 1000)
        candidates[code] = c

    logger.info(f"龙虎榜候选: {len(candidates)}只")
    return candidates


def _get_performance_candidates() -> Dict[str, Candidate]:
    """
    从业绩预告获取候选 (净利润预增)
    数据源: 东方财富 datacenter RPT_PUBLIC_OP_NEWPREDICT
    """
    candidates = {}

    try:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "reportName=RPT_PUBLIC_OP_NEWPREDICT"
            "&columns=SECURITY_CODE,SECURITY_NAME_ABBR,NOTICE_DATE,PREDICT_FINANCE_CODE,PREDICT_FINANCE,ADD_AMP_LOWER,ADD_AMP_UPPER"
            "&filter=(PREDICT_FINANCE_CODE='004')"
            "&pageSize=100&pageNumber=1&sortColumns=NOTICE_DATE&sortTypes=-1"
            "&source=WEB&client=WEB"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()

        if data.get("result") and data["result"].get("data"):
            items = data["result"]["data"]
            for item in items:
                code = str(item.get("SECURITY_CODE", "")).strip()
                if not code:
                    continue
                name = str(item.get("SECURITY_NAME_ABBR", ""))
                amp_lower = float(item.get("ADD_AMP_LOWER") or 0)
                amp_upper = float(item.get("ADD_AMP_UPPER") or 0)
                avg_amp = (amp_lower + amp_upper) / 2

                if avg_amp <= 0:
                    continue

                c = Candidate(
                    code=code, name=name, source=["业绩预增"],
                    industry=f"增幅{amp_lower:.0f}%-{amp_upper:.0f}%",
                )
                if avg_amp >= 200:
                    c.score = 35
                elif avg_amp >= 100:
                    c.score = 25 + (avg_amp - 100) / 100 * 10
                elif avg_amp >= 50:
                    c.score = 15 + (avg_amp - 50) / 50 * 10
                elif avg_amp >= 20:
                    c.score = 8 + (avg_amp - 20) / 30 * 7
                else:
                    c.score = max(3, avg_amp / 20 * 8)

                candidates[code] = c

            logger.info(f"业绩预增候选: {len(candidates)}只")
        else:
            logger.warning(f"业绩预增API无数据: {data.get('message', 'unknown')}")

    except Exception as e:
        logger.warning(f"业绩预增获取失败: {e}")

    return candidates


def _get_survey_candidates() -> Dict[str, Candidate]:
    """
    从机构调研获取候选
    数据源: 东方财富 datacenter RPT_ORG_SURVEYNEW
    近期被机构调研次数越多，分数越高
    """
    from collections import Counter

    candidates = {}

    try:
        code_counter = Counter()
        org_types = {}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        }

        for page in range(1, 8):
            url = (
                f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
                f"reportName=RPT_ORG_SURVEYNEW"
                f"&columns=SECURITY_CODE,SECURITY_NAME_ABBR,RECEIVE_END_DATE,ORG_TYPE"
                f"&pageSize=50&pageNumber={page}&sortColumns=RECEIVE_END_DATE&sortTypes=-1"
                f"&source=WEB&client=WEB"
            )
            resp = requests.get(url, headers=headers, timeout=10)
            data = resp.json()

            if data.get("result") and data["result"].get("data"):
                for item in data["result"]["data"]:
                    code = str(item.get("SECURITY_CODE", "")).strip()
                    name = str(item.get("SECURITY_NAME_ABBR", ""))
                    org_type = str(item.get("ORG_TYPE", ""))
                    if code:
                        code_counter[(code, name)] += 1
                        if code not in org_types:
                            org_types[code] = set()
                        org_types[code].add(org_type)

        for (code, name), cnt in code_counter.items():
            c = Candidate(
                code=code, name=name, source=["机构调研"],
                industry=f"调研{cnt}次",
            )
            if cnt >= 20:
                c.score = 35
            elif cnt >= 10:
                c.score = 25 + (cnt - 10) / 10 * 10
            elif cnt >= 5:
                c.score = 15 + (cnt - 5) / 5 * 10
            elif cnt >= 3:
                c.score = 10 + (cnt - 3) / 2 * 5
            else:
                c.score = max(3, cnt * 3)

            orgs = org_types.get(code, set())
            if any("基金" in o or "保险" in o for o in orgs):
                c.score = min(40, c.score + 5)

            candidates[code] = c

        logger.info(f"机构调研候选: {len(candidates)}只")

    except Exception as e:
        logger.warning(f"机构调研获取失败: {e}")

    return candidates


def _get_financing_candidates() -> Dict[str, Candidate]:
    """
    融资融券候选（新增维度）
    融资余额增长 = 看好信号
    """
    from data.a_stock_data import get_margin_detail, normalize_code

    candidates = {}

    # 从涨停板/龙虎榜已有候选中筛选（避免全市场扫描）
    # 先获取活跃股池
    try:
        active_stocks = _get_active_stocks(min_amount=5000, limit=100)
        logger.info(f"融资融券扫描池: {len(active_stocks)}只")

        count = 0
        for code, name in list(active_stocks.items())[:50]:  # 限制50只，避免超时
            try:
                margin = get_margin_detail(normalize_code(code))
                if not margin:
                    continue

                # 融资余额趋势打分
                if margin["trend"] == "快速增长":
                    score = 30
                elif margin["trend"] == "增长":
                    score = 20
                elif margin["trend"] == "持平":
                    score = 10
                else:
                    continue  # 下降趋势不加入候选

                # 融资买入活跃度加分
                if margin["financing_buy"] > 50_000_000:  # >5000万
                    score += 5

                c = Candidate(
                    code=code, name=name, source=["融资融券"],
                    industry=f"{margin['trend']}",
                )
                c.score = score
                candidates[code] = c
                count += 1

                if count >= 10:  # 最多返回10只
                    break

            except Exception as e:
                logger.debug(f"融资融券扫描失败 {code}: {e}")

        logger.info(f"融资融券候选: {len(candidates)}只")
    except Exception as e:
        logger.warning(f"融资融券获取失败: {e}")

    return candidates


def _get_unlock_alert_candidates() -> Dict[str, Candidate]:
    """
    限售解禁预警（反向筛选）
    未来30天有重大解禁（>5%）= 减分/排除
    """
    from data.a_stock_data import get_unlock_schedule, normalize_code

    candidates = {}

    # 从现有候选中检查解禁风险
    # 这个函数返回的是需要警惕的股票（负面信号）
    # 在merge时会降低它们的分数

    try:
        active_stocks = _get_active_stocks(min_amount=5000, limit=50)
        logger.info(f"解禁预警扫描池: {len(active_stocks)}只")

        for code, name in list(active_stocks.items())[:30]:
            try:
                unlock = get_unlock_schedule(normalize_code(code), days_ahead=30)
                if not unlock or not unlock["has_major_unlock"]:
                    continue

                # 有重大解禁，标记为风险
                c = Candidate(
                    code=code, name=name, source=["解禁预警"],
                    industry=f"{len(unlock['upcoming_unlocks'])}次解禁",
                )
                c.score = -20  # 负分，在merge时会降低总分
                candidates[code] = c

            except Exception as e:
                logger.debug(f"解禁预警扫描失败 {code}: {e}")

        logger.info(f"解禁预警候选: {len(candidates)}只（负面信号）")
    except Exception as e:
        logger.warning(f"解禁预警获取失败: {e}")

    return candidates


def _get_volume_candidates() -> Dict[str, Candidate]:
    """
    异动放量候选 (量比>3 + 涨幅不深跌)
    数据源: 东方财富推送API
    """
    candidates = {}

    try:
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            "pn=1&pz=100&po=1&np=1&fltt=2&invt=2"
            "&fields=f2,f3,f5,f6,f8,f12,f14,f18"
            "&fid=f18"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
            "&ut=fa5fd1943c7b386f172d6893dbbd1180"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()

        if data.get("data") and data["data"].get("diff"):
            items = data["data"]["diff"]
            for item in items:
                code = str(item.get("f12", "")).strip()
                name = str(item.get("f14", ""))
                price = float(item.get("f2") or 0)
                change_pct = float(item.get("f3") or 0)
                turnover = float(item.get("f8") or 0)
                volume_ratio = float(item.get("f18") or 0)

                if volume_ratio < 3 or change_pct < -5 or price <= 0:
                    continue

                c = Candidate(
                    code=code, name=name, source=["异动放量"],
                    change_pct=change_pct,
                    industry=f"量比{volume_ratio:.1f}",
                )

                if volume_ratio >= 10:
                    c.score = 25
                elif volume_ratio >= 5:
                    c.score = 15 + (volume_ratio - 5) / 5 * 10
                else:
                    c.score = 8 + (volume_ratio - 3) / 2 * 7

                if 2 <= change_pct <= 7:
                    c.score = min(35, c.score + 5)
                elif 0 < change_pct < 2:
                    c.score = min(35, c.score + 2)

                if 5 <= turnover <= 15:
                    c.score = min(35, c.score + 3)

                candidates[code] = c

            logger.info(f"异动放量候选: {len(candidates)}只 (量比>3)")
        else:
            logger.warning("异动放量API无数据")

    except Exception as e:
        logger.warning(f"异动放量获取失败: {e}")

    return candidates


def _get_north_flow_candidates() -> Dict[str, Candidate]:
    """
    从北向资金获取候选
    使用东方财富 datacenter API (RPT_MUTUAL_HOLDSTOCKNORTH_STA)
    数据为季度持仓快照，按持股市值排序取前20
    """
    import requests

    candidates = {}

    try:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "reportName=RPT_MUTUAL_HOLDSTOCKNORTH_STA"
            "&columns=SECURITY_CODE,SECURITY_NAME,MUTUAL_TYPE,HOLD_SHARES,HOLD_MARKET_CAP,FREE_SHARES_RATIO"
            "&pageSize=30&pageNumber=1&sortColumns=HOLD_MARKET_CAP&sortTypes=-1"
            "&source=WEB&client=WEB"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()

        if data.get("result") and data["result"].get("data"):
            items = data["result"]["data"]
            for item in items:
                code = str(item.get("SECURITY_CODE", "")).strip()
                if not code:
                    continue
                name = str(item.get("SECURITY_NAME", ""))
                hold_cap = float(item.get("HOLD_MARKET_CAP", 0) or 0)
                free_ratio = float(item.get("FREE_SHARES_RATIO", 0) or 0)

                c = Candidate(
                    code=code, name=name, source=["北向"],
                    north_flow=hold_cap / 1e8,  # 亿
                )
                # 持股市值和占流通比加分
                # 持股市值>100亿+10分, >500亿+20分
                if hold_cap > 500e8:
                    c.score = 25
                elif hold_cap > 100e8:
                    c.score = 15 + (hold_cap - 100e8) / 400e8 * 10
                elif hold_cap > 10e8:
                    c.score = 5 + (hold_cap - 10e8) / 90e8 * 10
                else:
                    c.score = 2
                # 占流通比>10%额外加分
                if free_ratio > 10:
                    c.score = min(30, c.score + 5)
                elif free_ratio > 5:
                    c.score = min(30, c.score + 2)

                candidates[code] = c

            logger.info(f"北向资金候选: {len(candidates)}只 (东方财富datacenter)")
        else:
            logger.warning(f"北向资金API无数据: {data.get('message', 'unknown')}")

    except Exception as e:
        logger.warning(f"北向资金获取失败: {e}")

        # 备选: 用AKShare获取汇总数据
        try:
            import akshare as ak
            from data import rate_limit
            rate_limit("akshare", 2.5)
            df_summary = ak.stock_hsgt_fund_flow_summary_em()
            if df_summary is not None and not df_summary.empty:
                logger.info(f"北向资金汇总备选: {len(df_summary)}条 (仅汇总，无个股)")
        except Exception as e2:
            logger.warning(f"北向资金备选也失败: {e2}")

    return candidates


def _merge_candidates(
    limit_up: Dict[str, Candidate],
    dragon_tiger: Dict[str, Candidate],
    north_flow: Dict[str, Candidate],
    performance: Dict[str, Candidate] = None,
    survey: Dict[str, Candidate] = None,
    volume: Dict[str, Candidate] = None,
    financing: Dict[str, Candidate] = None,
    unlock_alert: Dict[str, Candidate] = None,
) -> List[Candidate]:
    """
    合并所有来源的候选, 多源叠加加分
    """
    if performance is None:
        performance = {}
    if survey is None:
        survey = {}
    if volume is None:
        volume = {}
    if financing is None:
        financing = {}
    if unlock_alert is None:
        unlock_alert = {}

    all_pools = [limit_up, dragon_tiger, north_flow, performance, survey, volume, financing, unlock_alert]
    all_codes = set()
    for pool in all_pools:
        all_codes.update(pool.keys())

    merged = []

    for code in all_codes:
        sources = []
        total_score = 0.0

        for pool in all_pools:
            if code in pool:
                c = pool[code]
                sources.extend(c.source)
                total_score += c.score

        # 多源叠加奖励: 2源+10, 3源+20, 4源+30, 5源+40
        unique_sources = list(set(sources))
        bonus = max(0, (len(unique_sources) - 1) * 10)
        total_score += bonus

        # 取任一来源的完整对象, 填充合并信息
        base = None
        for pool in all_pools:
            if code in pool:
                base = pool[code]
                break
        base.source = unique_sources
        base.score = min(100, total_score)
        merged.append(base)

    merged.sort(key=lambda x: x.score, reverse=True)
    return merged


def pick_stocks(
    top_n: int = None,
    min_score: float = None,
    use_limit_up: bool = True,
    use_dragon_tiger: bool = True,
    use_north_flow: bool = True,
    use_performance: bool = True,
    use_survey: bool = True,
    use_volume: bool = True,
    use_financing: bool = True,
    use_unlock_alert: bool = True,
) -> List[Candidate]:
    """
    AI选股主函数

    从8个维度筛选候选股票:
    涨停板、龙虎榜、北向资金、业绩预增、机构调研、异动放量、融资融券、解禁预警
    返回综合得分最高的 Top N 候选。
    """
    if top_n is None:
        top_n = PICKER_TOP_N
    if min_score is None:
        min_score = PICKER_MIN_SCORE

    logger.info(f"开始选股 top_n={top_n} min_score={min_score}")

    # ── 获取各池子 (并行化可优化) ──
    limit_up = _get_limit_up_candidates() if use_limit_up else {}
    dragon_tiger = _get_dragon_tiger_candidates() if use_dragon_tiger else {}
    north_flow = _get_north_flow_candidates() if use_north_flow else {}
    performance = _get_performance_candidates() if use_performance else {}
    survey = _get_survey_candidates() if use_survey else {}
    volume = _get_volume_candidates() if use_volume else {}
    financing = _get_financing_candidates() if use_financing else {}
    unlock_alert = _get_unlock_alert_candidates() if use_unlock_alert else {}

    merged = _merge_candidates(limit_up, dragon_tiger, north_flow, performance, survey, volume, financing, unlock_alert)

    # ── 过滤垃圾股 ──
    def _is_valid(c: Candidate) -> bool:
        name = c.name.upper()
        code = c.code.strip()
        # 过滤退市
        if "退" in name:
            return False
        # 过滤名称过短（可能是异常数据）
        if len(c.name.strip()) < 2:
            return False
        # 过滤北交所（8/4开头，无权限）
        if code.startswith(("8", "4")):
            return False
        # 过滤科创板（688开头，无权限）
        if code.startswith("688"):
            return False
        return True

    before_filter = len(merged)
    merged = [c for c in merged if _is_valid(c)]
    if len(merged) < before_filter:
        logger.info(f"过滤ST/退市股: {before_filter} → {len(merged)}只")

    # 按分数过滤
    filtered = [c for c in merged if c.score >= min_score]
    result = filtered[:top_n]

    logger.info(f"选股完成: 合并{len(merged)}只, 过滤后{len(filtered)}只, 输出{len(result)}只")
    for c in result:
        logger.info(f"  {c}")

    return result


def format_picker_report(candidates: List[Candidate]) -> str:
    """格式化选股报告"""
    if not candidates:
        return "无候选股票"

    lines = [
        "=" * 65,
        "AI选股结果",
        "=" * 65,
        f"{'排名':>4} {'代码':<8} {'名称':<8} {'分数':>6} {'来源':<16} {'行业':<8}",
        "-" * 65,
    ]

    for i, c in enumerate(candidates, 1):
        src = "+".join(c.source)
        lines.append(
            f"{i:>4} {c.code:<8} {c.name:<8} {c.score:>6.1f} {src:<16} {c.industry:<8}"
        )

    lines.append("=" * 65)
    return "\n".join(lines)


def pick_stocks_by_strategy(
    strategy_names: list = None,
    min_amount: float = 5000,     # 最低日成交额（万元）
    max_candidates: int = 200,    # 最多扫描多少只
    kline_days: int = 120,        # 取多少天K线
) -> List[Candidate]:
    """
    技术形态选股
    不依赖涨停板/龙虎榜，直接从市场数据中找策略信号

    流程:
    1. 从东方财富获取今日活跃股（成交额排序）
    2. 过滤: ST/北交所/科创板/成交额太低
    3. 对每只跑策略信号
    4. 有BUY信号的返回
    """
    from strategy.strategies import get_strategy, list_strategies

    if strategy_names is None:
        strategy_names = [s["name"] for s in list_strategies()]

    strategies = [get_strategy(name) for name in strategy_names]
    logger.info(f"技术扫描选股: {len(strategies)}个策略, 最低成交额{min_amount}万")

    # 1. 获取今日活跃股
    active_stocks = _get_active_stocks(min_amount, max_candidates * 2)
    logger.info(f"活跃股: {len(active_stocks)}只")

    # 2. 扫描策略信号
    from data.history import get_daily
    from datetime import datetime, timedelta

    start = (datetime.now() - timedelta(days=kline_days)).strftime("%Y%m%d")
    results = []
    scanned = 0
    errors = 0

    for code, name in active_stocks.items():
        if scanned >= max_candidates:
            break
        scanned += 1

        try:
            df = get_daily(code, start_date=start)
            if df is None or len(df) < 60:
                continue

            for strategy in strategies:
                try:
                    signal = strategy.generate_signals(code, df)
                    if signal.action == "BUY" and signal.score > 0:
                        results.append(Candidate(
                            code=code,
                            name=name,
                            source=[strategy.name],
                            score=signal.score,
                            change_pct=0,
                            amount=0,
                        ))
                        logger.info(f"  📈 {code} {name} | {strategy.name} score={signal.score}")
                        break  # 一只股票只取第一个信号
                except Exception:
                    pass
        except Exception:
            errors += 1
            if errors > 20:
                logger.warning("错误过多，跳过剩余")
                break

    logger.info(f"技术扫描完成: 扫描{scanned}只, 发现{len(results)}只有信号")
    return results


def _get_active_stocks(min_amount: float = 5000, limit: int = 500) -> Dict[str, str]:
    """
    从东方财富获取今日活跃股（按成交额排序）
    返回 {code: name} 字典

    过滤: ST/北交所/科创板/成交额低于阈值
    """
    stocks = {}
    try:
        # 东方财富实时行情接口（按成交额排序）
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1,
            "pz": limit,
            "po": 0,           # 降序
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f6",       # 按成交额排序
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",  # 沪深A股
            "fields": "f12,f14,f6",  # 代码,名称,成交额
        }
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        items = data.get("data", {}).get("diff", [])
        if not items:
            return stocks

        for item in items:
            code = str(item.get("f12", ""))
            name = str(item.get("f14", ""))
            amount = item.get("f6", 0)  # 成交额（元）

            if not code or not name:
                continue

            # 成交额转万元
            amount_wan = amount / 10000 if amount else 0
            if amount_wan < min_amount:
                continue

            # 过滤退市
            if "退" in name:
                continue
            if code.startswith(("8", "4")):  # 北交所
                continue
            if code.startswith("688"):       # 科创板
                continue

            stocks[code] = name

    except Exception as e:
        logger.warning(f"获取活跃股失败: {e}")

    return stocks


def pick_stocks_by_htsc(
    queries: list = None,
    max_candidates: int = 200,
) -> List[Candidate]:
    """
    华泰条件选股
    使用华泰 select-stock skill 通过自然语言条件筛选股票

    Args:
        queries: 选股条件列表，每条是自然语言描述
        max_candidates: 最大候选数

    Returns:
        候选股票列表
    """
    from data.htsc_client import select_stock_parsed

    if queries is None:
        # 默认选股条件（单条，控制在10只以内）
        queries = [
            "主力净流入超过3000万且换手率大于3%的沪深主板股票",
        ]

    all_candidates = {}  # code -> Candidate
    for query in queries:
        try:
            stocks = select_stock_parsed(query)
            logger.info(f"华泰选股 '{query[:20]}...' → {len(stocks)}只")
            for s in stocks:
                code = s.get("code", "")
                name = s.get("name", "")
                if not code:
                    continue
                # 过滤北交所/科创板/退市
                if code.startswith(("8", "4")):
                    continue
                if code.startswith("688"):
                    continue
                if "退" in name:
                    continue
                if code not in all_candidates:
                    all_candidates[code] = Candidate(
                        code=code,
                        name=name,
                        source=["华泰选股"],
                        score=60,  # 华泰筛选出的基础分
                    )
                else:
                    # 多条件命中加分
                    all_candidates[code].score += 10
                    if "华泰选股" not in all_candidates[code].source:
                        all_candidates[code].source.append("华泰选股")
        except Exception as e:
            logger.warning(f"华泰选股失败 ({query[:30]}): {e}")

    candidates = list(all_candidates.values())
    candidates.sort(key=lambda c: c.score, reverse=True)
    result = candidates[:max_candidates]
    logger.info(f"华泰选股汇总: {len(result)}只候选")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    candidates = pick_stocks(top_n=10)
    print(format_picker_report(candidates))
