"""数据源：竞彩篮球盘口（500.com 官方 SP 镜像）与 ESPN 赛程/盘口/比分。

- 竞彩：``trade.500.com/jclq/?date=<销售日>&playid=275|277``（275 让分胜负，277 大小分）。
  每行 ``data-subactive`` 标明单关/过关是否开售，例如 ``rfsfdg:0,rfsfgg:1`` 表示让分只开过关。
- ESPN：``scoreboard?dates=<美国日期>``，内嵌一家国际博彩公司的让分与总分（通常为
  DraftKings），只是抓取时点的快照；开赛前多次抓取的最后一条作为“收盘线”近似。
竞彩销售日（北京时间）与 ESPN 的美国比赛日相同：北京 D 日销售的场次在美国 D 日开打。
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from lottery.teams import ABBR2KEY, CN2KEY

BEIJING = ZoneInfo("Asia/Shanghai")
JC_URL = "https://trade.500.com/jclq/"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}
TIMEOUT = 25
PLAY_SPREAD, PLAY_TOTAL = 275, 277


def _float(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_home_line(title: str) -> Optional[float]:
    """把“主队让3.5分 / 客队受让1.5分”转成主队让分（负数=主队让分）。"""
    match = re.match(r"(主队|客队)(让|受让)([\d.]+)分", title or "")
    if not match:
        return None
    who, kind, num = match.group(1), match.group(2), float(match.group(3))
    gives = kind == "让"
    if who == "主队":
        return -num if gives else num
    return num if gives else -num


def parse_subactive(text: str) -> Dict[str, bool]:
    out = {}
    for part in (text or "").split(","):
        key, _, value = part.partition(":")
        if key:
            out[key.strip()] = value.strip() == "1"
    return out


def parse_jc_page(html: str) -> List[dict]:
    """解析竞彩篮球列表页，只保留 NBA（美职篮且双方都能映射到 NBA 球队）。"""
    rows = re.findall(r'<tr class="bet-tb-tr[^"]*"(.*?)</tr>', html or "", re.S)
    out = []
    for row in rows:
        def attr(name):
            match = re.search(rf'data-{name}="([^"]*)"', row)
            return match.group(1) if match else None

        if "美职" not in (attr("simpleleague") or ""):
            continue
        home_cn, away_cn = attr("homesxname"), attr("awaysxname")
        home, away = CN2KEY.get(home_cn or ""), CN2KEY.get(away_cn or "")
        if not home or not away:
            continue
        odds = {(typ, val): float(sp) for typ, val, sp in
                re.findall(r'data-type="(\w+)" data-value="(\d)" data-sp="([\d.]+)"', row)}
        message = re.search(r'<p class="betmsg">(.*?)</p>', row, re.S)
        title = re.search(r'title="([^"]*)"', message.group(1)) if message else None
        out.append({
            "matchnum": attr("matchnum"), "home": home, "away": away,
            "home_cn": home_cn, "away_cn": away_cn,
            "tip_at": f"{attr('matchdate')}T{attr('matchtime')}:00+08:00",
            "buy_end": (attr("buyendtime") or "").replace(" ", "T") + "+08:00" if attr("buyendtime") else None,
            "line_title": title.group(1) if title else "",
            "yszf": _float(attr("yszf")),
            "subactive": parse_subactive(attr("subactive") or ""),
            "odds": odds,
        })
    return out


def fetch_jc(day: str, playid: int, session=None) -> List[dict]:
    response = (session or requests).get(JC_URL, params={"date": day, "playid": playid},
                                         headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = "gbk"
    return parse_jc_page(response.text)


def merge_jc(spread_rows: List[dict], total_rows: List[dict]) -> List[dict]:
    """合并让分页与大小分页为每场一行。"""
    totals = {row["matchnum"]: row for row in total_rows}
    games = []
    for row in spread_rows:
        total = totals.get(row["matchnum"], {})
        active = row["subactive"]
        games.append({
            "matchnum": row["matchnum"], "home": row["home"], "away": row["away"],
            "home_cn": row["home_cn"], "away_cn": row["away_cn"],
            "tip_at": row["tip_at"], "buy_end": row["buy_end"],
            "jc_line": parse_home_line(row["line_title"]),
            "jc_sp_home": row["odds"].get(("rfsf", "1")), "jc_sp_away": row["odds"].get(("rfsf", "2")),
            "jc_total": total.get("yszf"),
            "jc_sp_over": (total.get("odds") or {}).get(("dxf", "1")),
            "jc_sp_under": (total.get("odds") or {}).get(("dxf", "2")),
            "single_spread": active.get("rfsfdg"), "single_total": active.get("dxfdg"),
        })
    return games


def parse_espn(payload: dict) -> List[dict]:
    out = []
    for event in (payload or {}).get("events", []):
        competition = (event.get("competitions") or [{}])[0]
        teams = competition.get("competitors") or []
        home = next((x for x in teams if x.get("homeAway") == "home"), None)
        away = next((x for x in teams if x.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        home_key = ABBR2KEY.get((home.get("team") or {}).get("abbreviation", ""))
        away_key = ABBR2KEY.get((away.get("team") or {}).get("abbreviation", ""))
        if not home_key or not away_key:
            continue
        status = (competition.get("status") or event.get("status") or {}).get("type") or {}
        item = {
            "home": home_key, "away": away_key, "state": status.get("state") or "",
            "completed": bool(status.get("completed")),
            "tip_utc": competition.get("date") or event.get("date"),
            "home_score": None, "away_score": None,
            "ref_provider": None, "ref_line": None, "ref_total": None, "ref_details": "",
        }
        if item["completed"]:
            try:
                item["home_score"] = int(float(home.get("score")))
                item["away_score"] = int(float(away.get("score")))
            except (TypeError, ValueError):
                item["completed"] = False
        odds = (competition.get("odds") or [None])[0]
        if odds:
            details = (odds.get("details") or "").strip()
            line = None
            match = re.match(r"([A-Z]{2,4})\s+(-?[\d.]+)", details)
            if match:
                favorite, number = ABBR2KEY.get(match.group(1)), abs(float(match.group(2)))
                line = -number if favorite == home_key else number if favorite == away_key else None
            elif details.upper().startswith(("PK", "EVEN")):
                line = 0.0
            item.update(ref_provider=((odds.get("provider") or {}).get("name") or "ESPN"),
                        ref_line=line, ref_total=_float(odds.get("overUnder")), ref_details=details)
        out.append(item)
    return out


def fetch_espn(day: str, session=None) -> List[dict]:
    response = (session or requests).get(ESPN_URL, params={"dates": day.replace("-", "")},
                                         headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return parse_espn(response.json())


def now_beijing() -> datetime:
    return datetime.now(timezone.utc).astimezone(BEIJING)
