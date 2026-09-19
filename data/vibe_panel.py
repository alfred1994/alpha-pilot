"""
Vibe-Trading 因子基准的 AlphaPilot 数据面板
====================================================================
为外置 vibe-trading 的 alpha_bench 提供本地日线面板，替代其内置的
Tushare 数据源：

  宇宙代码 = 研究池(data/research_universe.json)或显式代码文件
  日线来源 = data.history.get_daily（k_daily 缓存 → 同花顺 → 长桥 → Baostock）
  产物     = 长表 CSV（date,code,open,high,low,close,volume,amount,vwap）

CSV 由 scripts/vibe/vibe_tool_driver.py 在 vibe venv 内读入并 pivot 成
alpha_bench 需要的宽表 panel（open/high/low/close/volume/amount/vwap 各一张
date×code 的 DataFrame）。前复权口径与 k_daily 一致。

使用方法:
    from data.vibe_panel import resolve_universe_codes, export_panel_csv
    codes = resolve_universe_codes("pool", pool_limit=100)
    path, stats = export_panel_csv(codes, "2024-2026")
====================================================================
"""
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd

from config import VIBE_DATA_DIR
from data import history as data_history
from data.research_universe import UNIVERSE_FILE

PANEL_DIR = os.path.join(VIBE_DATA_DIR, "panel")
PANEL_COLUMNS = ["date", "code", "open", "high", "low", "close", "volume", "amount", "vwap"]

_YEAR_PERIOD = re.compile(r"^(\d{4})-(\d{4})$")
_DATE_PERIOD = re.compile(r"^(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})$")


def period_bounds(period: str) -> Tuple[str, str]:
    """把 YYYY-YYYY 或 YYYY-MM-DD/YYYY-MM-DD 解析为 (start, end)。"""
    if not isinstance(period, str):
        raise ValueError(f"period 必须是字符串，得到 {type(period).__name__}")
    m = _DATE_PERIOD.match(period)
    if m:
        start, end = m.group(1), m.group(2)
    else:
        m = _YEAR_PERIOD.match(period)
        if not m:
            raise ValueError(f"period {period!r} 必须是 YYYY-YYYY 或 YYYY-MM-DD/YYYY-MM-DD")
        start, end = f"{m.group(1)}-01-01", f"{m.group(2)}-12-31"
    if pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError(f"period 起止倒置: {period}")
    return start, end


def _pool_codes(payload) -> List[str]:
    """研究池 codes 兼容两种形态: 纯字符串或 {"code": "600519", ...} 字典。"""
    codes = []
    for item in (payload or {}).get("codes") or []:
        code = str(item.get("code") if isinstance(item, dict) else item).strip()
        if re.fullmatch(r"\d{6}", code):
            codes.append(code)
    return codes


def resolve_universe_codes(spec: str, pool_limit: int = 100) -> List[str]:
    """
    解析宇宙代码列表。

    - "pool": 研究池 UNIVERSE_FILE 的 codes（按存储顺序截取 pool_limit 只，
      pool_limit<=0 表示不截断）；
    - "file:<path>": 每行一个 6 位代码，# 开头为注释。

    pool 为空通常意味着尚未运行 refresh_research_universe / sync。
    """
    spec = str(spec or "").strip()
    if spec == "pool":
        if not os.path.isfile(UNIVERSE_FILE):
            raise ValueError(
                f"研究池不存在: {UNIVERSE_FILE}（先运行 data.research_universe.refresh_research_universe）"
            )
        try:
            with open(UNIVERSE_FILE, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"研究池文件不可读: {exc}") from exc
        codes = _pool_codes(payload)
        if pool_limit and pool_limit > 0:
            codes = codes[:pool_limit]
    elif spec.startswith("file:"):
        path = spec[5:].strip()
        if not os.path.isfile(path):
            raise ValueError(f"代码文件不存在: {path}")
        codes = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                token = line.split("#", 1)[0].strip()
                if re.fullmatch(r"\d{6}", token):
                    codes.append(token)
    else:
        raise ValueError('universe 规格必须是 "pool" 或 "file:<path>"')
    if not codes:
        raise ValueError("宇宙代码列表为空")
    return codes


def _normalize_daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    """统一 history.get_daily 返回的列与日期格式，附加 vwap。"""
    if df is None or df.empty:
        return pd.DataFrame()
    frame = pd.DataFrame(df).copy()
    required = {"date", "open", "high", "low", "close", "volume", "amount"}
    missing = required - set(frame.columns)
    if missing:
        return pd.DataFrame()
    dates = frame["date"].astype(str).str.strip()
    if dates.str.fullmatch(r"\d{8}").all():
        frame["date"] = pd.to_datetime(dates, format="%Y%m%d", errors="coerce")
    else:
        frame["date"] = pd.to_datetime(dates, errors="coerce")
    frame = frame.dropna(subset=["date"])
    frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["close"])
    frame = frame[frame["close"] > 0]
    volume = frame["volume"].where(frame["volume"] > 0)
    frame["vwap"] = (frame["amount"] / volume).astype(float)  # volume<=0 处为 NaN
    return frame.reset_index(drop=True)


def build_panel_long(codes: List[str], period: str, min_rows: int = 60) -> Tuple[pd.DataFrame, Dict]:
    """
    逐只取日线并汇成长表；来源复用 data.history.get_daily 的既有链路
    （k_daily 缓存 → 同花顺 → 长桥 → Baostock），不新增外部数据依赖。

    返回 (long_df, stats)；rows 少于 min_rows 的代码按覆盖不足跳过并记录。
    """
    start_date, end_date = period_bounds(period)
    # 请求区间可能延伸到未来；未来没有行情，只会把每只代码都判成"缓存过期"
    # 而触发外部源重试链（同花顺 1s/只、Baostock 75s 超时），因此收敛到今天。
    today = datetime.now().strftime("%Y-%m-%d")
    if end_date > today:
        end_date = today
    frames = []
    skipped: Dict[str, str] = {}
    used = 0
    for code in codes:
        try:
            # require_full_range=False：因子面板用长历史算 IC，允许尾部 ≤3 天
            # 的缓存陈旧；True 会让"终点=今天盘中"的每只代码都触发完整外部
            # 源重试链（Baostock 75s 超时/只），100 只串行要 2 小时以上。
            raw = data_history.get_daily(
                str(code), start_date=start_date, end_date=end_date,
                adjust="qfq", require_full_range=False,
            )
            frame = _normalize_daily_frame(raw)
        except Exception as exc:  # noqa: BLE001 —— 单只失败不拖垮整个面板
            skipped[str(code)] = f"error:{type(exc).__name__}"
            continue
        if frame.empty:
            skipped[str(code)] = "empty"
            continue
        if len(frame) < min_rows:
            skipped[str(code)] = f"rows<{min_rows}({len(frame)})"
            continue
        # 缓存帧可能自带 code 列（SELECT * 或 baostock 源），直接覆盖而不是 insert
        frame["code"] = str(code)
        frames.append(frame[PANEL_COLUMNS])
        used += 1

    stats = {
        "requested": len(codes),
        "used": used,
        "skipped": skipped,
        "start_date": start_date,
        "end_date": end_date,
        "source_chain": "k_daily_cache -> hithink -> longport -> baostock",
    }
    if not frames:
        return pd.DataFrame(columns=PANEL_COLUMNS), stats
    long_df = pd.concat(frames, ignore_index=True)
    long_df = long_df.sort_values(["date", "code"]).reset_index(drop=True)
    return long_df, stats


def export_panel_csv(codes: List[str], period: str,
                     out_dir: str = None, min_rows: int = 60) -> Tuple[str, Dict]:
    """构建面板并写出长表 CSV，返回 (csv路径, stats)。"""
    long_df, stats = build_panel_long(codes, period, min_rows=min_rows)
    if long_df.empty:
        raise ValueError("面板为空：所有代码均无有效日线（检查数据源与缓存）")
    out_dir = out_dir or PANEL_DIR
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"panel_{stats['start_date']}_{stats['end_date']}.csv")
    long_df.to_csv(path, index=False)
    stats["panel_csv"] = path
    stats["rows"] = int(len(long_df))
    return path, stats
