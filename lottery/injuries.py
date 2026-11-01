"""伤停标签：出单时双方的伤停名单与两个标签，以及名单前后变化。

只记录、不参与选单。标签用于赛后分组统计，回答“竞彩没跟上伤停的场次，长期是不是更赚”：

- ``24小时内伤停更新``：双方任一伤停条目在出单前 24 小时内有更新（ESPN 的更新时间
  也会因为备注改写而变化，所以这是偏宽的口径）；
- ``有出战成疑球员``：双方有状态不是“缺阵/停赛”的条目。这类球员是否出战通常在竞彩
  停售之后才确定，出票时锁定的价格无法再调整。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

STATUS_CN = {"Out": "缺阵", "Suspension": "停赛", "Day-To-Day": "每日观察", "Questionable": "出战成疑",
             "Doubtful": "大概率缺阵", "Probable": "大概率出战", "Game-Time Decision": "赛前决定"}
CERTAIN_STATUSES = {"Out", "Suspension"}
RECENT_HOURS = 24
TAG_RECENT = "24小时内伤停更新"
TAG_UNCERTAIN = "有出战成疑球员"
TAGS = (TAG_RECENT, TAG_UNCERTAIN)


def parse_time(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def status_cn(status: str) -> str:
    return STATUS_CN.get(status, status)


def by_team(items: Iterable[dict]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for item in items or []:
        out.setdefault(item["team"], []).append(item)
    for rows in out.values():
        rows.sort(key=lambda r: (r["status"] in CERTAIN_STATUSES, r["player"]))
    return out


def digest(items: Iterable[dict]) -> str:
    """一支球队名单的指纹：只看球员、状态与伤病部位，备注或更新时间变化不算新名单。"""
    key = sorted((i["player"], i["status"], i.get("type") or "") for i in items)
    return hashlib.sha1(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()


def compact(item: dict) -> dict:
    return {k: item.get(k) for k in ("player", "status", "type", "updated", "return_date")}


def context(items: Optional[List[dict]], home: str, away: str, now: datetime) -> Optional[dict]:
    """出单时双方的伤停名单与标签；伤停数据不可用时返回 None（标签留空，不当作“无伤停”）。"""
    if items is None:
        return None
    teams = by_team(items)
    home_rows, away_rows = teams.get(home, []), teams.get(away, [])
    cutoff = now - timedelta(hours=RECENT_HOURS)
    tags = []
    if any((parse_time(r.get("updated")) or cutoff) > cutoff for r in home_rows + away_rows):
        tags.append(TAG_RECENT)
    if any(r["status"] not in CERTAIN_STATUSES for r in home_rows + away_rows):
        tags.append(TAG_UNCERTAIN)
    return {"tags": tags, "home": [compact(r) for r in home_rows], "away": [compact(r) for r in away_rows]}


def diff(before: List[dict], after: List[dict]) -> List[dict]:
    """名单变化：新增、状态改变、移出名单（视为恢复出战）。"""
    old = {r["player"]: r for r in before or []}
    new = {r["player"]: r for r in after or []}
    changes = []
    for player in sorted(set(old) | set(new)):
        a, b = old.get(player), new.get(player)
        if a and b and a["status"] == b["status"]:
            continue
        changes.append({"player": player, "from": a["status"] if a else None, "to": b["status"] if b else None,
                        "type": (b or a).get("type")})
    return changes


def tag_group(tags: Optional[str], tag: str) -> Optional[bool]:
    """注单的标签字段（逗号分隔）是否含某标签；未记录伤停（历史注单或抓取失败）返回 None。"""
    if tags is None:
        return None
    return tag in [t for t in tags.split(",") if t]
