"""
长桥数据源模块（CLI模式）
通过 longbridge CLI 获取行情，使用 OAuth token，永不过期。

核心类: LongbridgeDataSource
工具函数: to_longport_code / from_longport_code
"""
import os
import json
import logging
import subprocess
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict

import pandas as pd

logger = logging.getLogger("data.longbridge")

# CLI 环境：unset 旧的 LONGPORT_* 环境变量，让 CLI 用 OAuth token
_CLI_ENV = None


def _get_cli_env():
    """获取 CLI 运行环境（去掉 LONGPORT_* 避免冲突）"""
    global _CLI_ENV
    if _CLI_ENV is None:
        _CLI_ENV = {k: v for k, v in os.environ.items()
                    if not k.startswith("LONGPORT_")}
    return _CLI_ENV


def _run_cli(args: list, timeout: int = 30) -> Optional[str]:
    """
    调用 longbridge CLI 并返回 stdout

    Args:
        args: CLI 参数列表，如 ["quote", "get", "600519.SH", "--format", "json"]
        timeout: 超时秒数

    Returns:
        stdout 字符串，失败返回 None
    """
    cmd = ["longbridge"] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=_get_cli_env(),
        )
        if result.returncode != 0:
            logger.error(f"CLI 失败: {' '.join(cmd)} → {result.stderr.strip()}")
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.error(f"CLI 超时({timeout}s): {' '.join(cmd)}")
        return None
    except Exception as e:
        logger.error(f"CLI 异常: {' '.join(cmd)} → {e}")
        return None


def _parse_json(stdout: str):
    """从 CLI 输出中提取 JSON（可能有 hint 尾部文本）"""
    if not stdout:
        return None
    text = stdout.strip()
    # 找到 JSON 起始位置
    for i, ch in enumerate(text):
        if ch in ('[', '{'):
            # 从后往前找对应的结束符
            target = ']' if ch == '[' else '}'
            for j in range(len(text) - 1, i, -1):
                if text[j] == target:
                    try:
                        return json.loads(text[i:j + 1])
                    except json.JSONDecodeError:
                        continue
            break
    return None


# ══════════════════════════════════════════════════════════════════
# 代码格式转换
# ══════════════════════════════════════════════════════════════════

def to_longport_code(code: str) -> str:
    """
    转换为长桥格式: 600519 → 600519.SH, 000001 → 000001.SZ

    支持输入:
        "600519"      → "600519.SH"
        "sh600519"    → "600519.SH"
        "sh.600519"   → "600519.SH"
        "600519.SH"   → "600519.SH" (原样返回)
        "sz000001"    → "000001.SZ"
    """
    code = code.strip()

    # 已经是长桥格式
    if "." in code and code.endswith((".SH", ".SZ")):
        return code

    # baostock格式: sh.600519
    if "." in code:
        parts = code.split(".")
        num = parts[-1]
    else:
        num = code

    # 去掉可能的前缀
    num = num.lstrip("shszSHSZ")

    # 判断市场: 6/5/9开头 → 上海, 其余 → 深圳
    if num.startswith(("6", "5", "9")):
        return f"{num}.SH"
    return f"{num}.SZ"


def from_longport_code(code: str) -> str:
    """
    从长桥格式转换为系统格式: 600519.SH → 600519

    同时兼容其他格式输入，统一返回纯数字代码
    """
    code = code.strip()
    if "." in code:
        parts = code.split(".")
        # 长桥格式: 600519.SH → parts[0]="600519"
        # baostock格式: sh.600519 → parts[1]="600519"
        num = parts[1] if parts[0].lower() in ("sh", "sz") else parts[0]
        return num
    return code.lstrip("shszSHSZ")


# 兼容旧代码
to_longbridge_code = to_longport_code
to_system_code = from_longport_code


# ══════════════════════════════════════════════════════════════════
# LongbridgeDataSource 核心类
# ══════════════════════════════════════════════════════════════════

class LongbridgeDataSource:
    """
    长桥数据源（CLI模式，OAuth认证，永不过期）

    方法:
        get_daily_kline(code, start_date, end_date)  - 日K线，自动存入SQLite
        get_minute_kline(code, period, count)         - 分钟K线
        get_realtime_quote(codes)                     - 实时行情
        get_intraday(code)                            - 分时数据
        get_capital_flow(code)                        - 资金流向
    """

    # ── 日K线 ──

    def get_daily_kline(self, code: str, start_date: str = None,
                        end_date: str = None) -> pd.DataFrame:
        """
        获取日K线，自动存入SQLite

        Args:
            code: 股票代码，如 "600519"
            start_date: "YYYY-MM-DD"，默认30天前
            end_date:   "YYYY-MM-DD"，默认今天

        Returns:
            DataFrame: date, open, high, low, close, volume, amount, turn, pctChg
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        # 标准化日期
        start_date = _normalize_date(start_date)
        end_date = _normalize_date(end_date)

        system_code = from_longport_code(code)

        # 1. 先查SQLite缓存
        try:
            from data.database import Database
            with Database() as db:
                cached = db.get_k_daily(system_code, start_date, end_date)
                if cached:
                    df = pd.DataFrame(cached)
                    for col in ["source"]:
                        if col in df.columns:
                            df = df.drop(columns=[col])
                    logger.info(f"日K线命中缓存: {system_code} {len(df)}条")
                    return df
        except Exception as e:
            logger.debug(f"缓存查询失败: {e}")

        # 2. 调CLI获取
        df = self._fetch_daily(code, start_date, end_date)

        # 3. 存入SQLite
        if df is not None and not df.empty:
            try:
                from data.database import Database
                records = df.to_dict("records")
                for r in records:
                    r["code"] = system_code
                with Database() as db:
                    db.insert_k_daily(records, source="longport")
            except Exception as e:
                logger.debug(f"缓存保存失败: {e}")

        return df if df is not None else pd.DataFrame()

    def _fetch_daily(self, code: str, start_date: str,
                     end_date: str) -> Optional[pd.DataFrame]:
        """从 CLI 获取日K线"""
        lb_code = to_longport_code(code)

        stdout = _run_cli([
            "kline", "history", lb_code,
            "--start", start_date,
            "--end", end_date,
            "--period", "day",
            "--format", "json",
        ])
        data = _parse_json(stdout)

        if not data:
            logger.warning(f"长桥日K线无数据: {lb_code}")
            return None

        records = []
        for c in data:
            # time 格式: "2026-06-01T16:00:00Z" → 取日期部分（UTC，A股收盘=次日凌晨UTC）
            raw_time = c.get("time", "")
            dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            # A股日K的 timestamp 是交易日当天 16:00 UTC = 北京次日 00:00
            # 所以日期取 UTC 日期 + 1 天 = 交易日
            trade_date = (dt + timedelta(hours=8)).strftime("%Y-%m-%d")

            if trade_date < start_date or trade_date > end_date:
                continue
            records.append({
                "date": trade_date,
                "open": float(c.get("open", 0)),
                "high": float(c.get("high", 0)),
                "low": float(c.get("low", 0)),
                "close": float(c.get("close", 0)),
                "volume": float(c.get("volume", 0)),
                "amount": float(c.get("turnover", 0)),
                "turn": 0,
                "pctChg": 0,
            })

        if not records:
            return None

        df = pd.DataFrame(records)
        df = df.sort_values("date").reset_index(drop=True)

        # 计算涨跌幅
        if len(df) > 1:
            df["pctChg"] = (df["close"].pct_change() * 100).round(2)

        logger.info(f"长桥日K线: {lb_code} {len(df)}条")
        return df

    # ── 分钟K线 ──

    def get_minute_kline(self, code: str, period: str = "5m",
                         count: int = 100) -> pd.DataFrame:
        """
        获取分钟K线

        Args:
            code: 股票代码
            period: 周期 "1m"/"5m"/"15m"/"30m"/"60m"
            count: 返回条数

        Returns:
            DataFrame: datetime, open, high, low, close, volume, amount
        """
        lb_code = to_longport_code(code)

        # CLI period 格式: 1m/5m/15m/30m/1h
        cli_period = period.replace("60m", "1h")

        stdout = _run_cli([
            "kline", lb_code,
            "--period", cli_period,
            "--count", str(min(count, 1000)),
            "--format", "json",
        ])
        data = _parse_json(stdout)

        if not data:
            logger.warning(f"长桥分钟K线无数据: {lb_code}")
            return pd.DataFrame()

        records = []
        for c in data:
            raw_time = c.get("time", "")
            dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            dt_local = dt + timedelta(hours=8)
            records.append({
                "datetime": dt_local.strftime("%Y-%m-%d %H:%M"),
                "open": float(c.get("open", 0)),
                "high": float(c.get("high", 0)),
                "low": float(c.get("low", 0)),
                "close": float(c.get("close", 0)),
                "volume": float(c.get("volume", 0)),
                "amount": float(c.get("turnover", 0)),
            })

        df = pd.DataFrame(records).sort_values("datetime").reset_index(drop=True)
        logger.info(f"长桥分钟K线: {lb_code} {period} {len(df)}条")
        return df

    # ── 实时行情 ──

    def get_realtime_quote(self, codes: List[str]) -> List[Dict]:
        """
        获取实时行情

        Args:
            codes: 股票代码列表，如 ["600519", "000001"]

        Returns:
            行情数据列表
        """
        lb_codes = [to_longport_code(c) for c in codes]

        stdout = _run_cli(["quote", "get"] + lb_codes + ["--format", "json"])
        data = _parse_json(stdout)

        if not data:
            logger.warning(f"长桥实时行情无数据: {lb_codes}")
            return []

        results = []
        for q in data:
            last_done = float(q.get("last", 0) or 0)
            prev_close = float(q.get("prev_close", 0) or 0)
            results.append({
                "code": from_longport_code(q.get("symbol", "")),
                "name": "",
                "price": last_done,
                "open": float(q.get("open", 0) or 0),
                "high": float(q.get("high", 0) or 0),
                "low": float(q.get("low", 0) or 0),
                "prev_close": prev_close,
                "volume": int(float(q.get("volume", 0) or 0)),
                "amount": float(q.get("turnover", 0) or 0),
                "change": round(last_done - prev_close, 2) if last_done and prev_close else 0,
                "change_pct": round((last_done - prev_close) / prev_close * 100, 2) if last_done and prev_close and prev_close > 0 else 0,
                "timestamp": "",
            })

        logger.info(f"长桥实时行情: {len(results)}只")
        return results

    # ── 分时数据 ──

    def get_intraday(self, code: str) -> pd.DataFrame:
        """
        获取分时数据

        Args:
            code: 股票代码

        Returns:
            DataFrame: time, price, volume, avg_price
        """
        lb_code = to_longport_code(code)

        stdout = _run_cli(["intraday", lb_code, "--format", "json"])
        data = _parse_json(stdout)

        if not data:
            return pd.DataFrame()

        records = []
        total_amount = 0.0
        total_volume = 0
        for c in data:
            vol = int(float(c.get("volume", 0) or 0))
            amt = float(c.get("turnover", 0) or 0)
            total_volume += vol
            total_amount += amt
            avg_price = total_amount / total_volume if total_volume > 0 else float(c.get("price", 0))

            raw_time = c.get("time", "")
            # intraday time 格式可能是 ISO 或 HH:MM
            if "T" in raw_time:
                dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                time_str = (dt + timedelta(hours=8)).strftime("%H:%M")
            else:
                time_str = raw_time[:5]

            records.append({
                "time": time_str,
                "price": float(c.get("price", 0) or 0),
                "volume": vol,
                "avg_price": round(avg_price, 2),
            })

        df = pd.DataFrame(records)
        logger.info(f"长桥分时数据: {lb_code} {len(df)}条")
        return df

    # ── 资金流向 ──

    def get_capital_flow(self, code: str) -> Dict:
        """
        获取资金流向

        Args:
            code: 股票代码

        Returns:
            资金流向数据 {inflow, outflow, net_flow, ...}
        """
        lb_code = to_longport_code(code)

        stdout = _run_cli(["capital", lb_code, "--format", "json"])
        data = _parse_json(stdout)

        if not data:
            return {}

        cap_in = data.get("capital_in", {})
        cap_out = data.get("capital_out", {})

        inflow = float(cap_in.get("large", 0)) + float(cap_in.get("medium", 0)) + float(cap_in.get("small", 0))
        outflow = float(cap_out.get("large", 0)) + float(cap_out.get("medium", 0)) + float(cap_out.get("small", 0))

        result = {
            "code": from_longport_code(code),
            "inflow": inflow,
            "outflow": outflow,
            "net_flow": inflow - outflow,
            "capital_in_large": float(cap_in.get("large", 0)),
            "capital_in_medium": float(cap_in.get("medium", 0)),
            "capital_in_small": float(cap_in.get("small", 0)),
            "capital_out_large": float(cap_out.get("large", 0)),
            "capital_out_medium": float(cap_out.get("medium", 0)),
            "capital_out_small": float(cap_out.get("small", 0)),
        }

        logger.info(f"长桥资金流向: {code}")
        return result


# ══════════════════════════════════════════════════════════════════
# 模块级便捷函数（兼容旧代码）
# ══════════════════════════════════════════════════════════════════

_default_source = None


def _get_default_source():
    global _default_source
    if _default_source is None:
        _default_source = LongbridgeDataSource()
    return _default_source


def get_daily_kline(code, start_date=None, end_date=None,
                    adjust="qfq", use_cache=True):
    """模块级日K线获取（兼容旧代码）"""
    return _get_default_source().get_daily_kline(code, start_date, end_date)


def get_minute_kline(code, period="5m", count=200, use_cache=True):
    """模块级分钟K线获取"""
    return _get_default_source().get_minute_kline(code, period, count)


def get_realtime_quote(codes):
    """模块级实时行情"""
    return _get_default_source().get_realtime_quote(codes)


def get_single_quote(code):
    """获取单只股票行情"""
    results = get_realtime_quote([code])
    return results[0] if results else None


def get_intraday_data(code):
    """模块级分时数据"""
    return _get_default_source().get_intraday(code)


def get_capital_flow(code):
    """模块级资金流向"""
    return _get_default_source().get_capital_flow(code)


# ══════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════

def _normalize_date(d: str) -> str:
    """标准化日期为 YYYY-MM-DD 格式"""
    d = d.strip().replace("/", "-")
    if len(d) == 8:  # YYYYMMDD
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def is_market_open() -> bool:
    """判断当前是否为交易时间"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    from datetime import time as dtime
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


def to_baostock_code(code: str) -> str:
    """转换为Baostock格式: 600519 → sh.600519"""
    code = code.strip()
    if "." in code and code.startswith(("sh.", "sz.")):
        return code
    num = from_longport_code(code)
    if num.startswith(("6", "5", "9")):
        return f"sh.{num}"
    return f"sz.{num}"


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    print("=" * 60)
    print("长桥数据源测试 (CLI模式)")
    print("=" * 60)

    # 代码转换测试
    print("\n[1] 代码格式转换:")
    test_codes = ["600519", "sh.600519", "000001", "sz.000001", "600519.SH", "300750"]
    for c in test_codes:
        lp = to_longport_code(c)
        sys_code = from_longport_code(c)
        bs = to_baostock_code(c)
        print(f"  {c:<12} → 长桥:{lp:<10} 系统:{sys_code:<8} Baostock:{bs}")

    source = LongbridgeDataSource()

    # 实时行情
    print("\n[2] 实时行情:")
    quotes = source.get_realtime_quote(["600519", "000001"])
    for q in quotes:
        print(f"  {q['code']} 现价:{q['price']:.2f} 涨跌:{q['change_pct']:.2f}%")

    # 日K线
    print("\n[3] 日K线:")
    df = source.get_daily_kline("600519")
    if not df.empty:
        print(df.tail(5).to_string(index=False))

    print("\n" + "=" * 60)
    print("测试完成")
