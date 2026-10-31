"""旁路比较的本地输入：独立原始日历 + 已保存行情；不编造严格成交字段。"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def prepare(*, evidence_db, market_db, calendar_file, output, now=None):
    from research.store import BJ, connect, read_experiments
    from research.ml_forward import calendar_days, clock, latest_closed
    from scheduler.persistence import atomic_json_write
    instant = clock(now)
    raw_calendar = json.loads(Path(calendar_file).read_text(encoding="utf-8"))
    sessions = calendar_days(raw_calendar)
    closed = latest_closed(sessions, instant)
    items = [i for i in read_experiments(evidence_db) if i["kind"] == "comparison" and i["status"] != "archived"]
    start = min((i["manifest"]["start"] for i in items), default=closed)
    codes = {"510300", "510500"}
    with connect(evidence_db, readonly=True) as db:
        for row in db.execute("SELECT payload FROM scans WHERE day>=? AND day<=?", (start, closed)):
            codes.update(item["code"] for item in json.loads(row[0])["candidates"])
    db = sqlite3.connect(Path(market_db).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        bars = [dict(row) for code in sorted(codes) for row in db.execute(
            "SELECT code,date,open,high,low,close,volume,source FROM k_daily WHERE code=? AND date>=? AND date<=? ORDER BY date",
            (code, start, closed))]
    finally:
        db.close()
    payload = {"calendar": [d for d in sessions if start <= d <= closed], "bars": bars,
               "metadata": {"calendar_source": "baostock_raw_trade_dates", "source": "local_k_daily_mixed_sources",
                            "adjustment": "mixed_documented", "universe": "forward_observed",
                            "corporate_actions_verified": False},
               "prepared_at": instant.isoformat(),
               "input_limitations": ["原价、实际涨跌停、交易状态、公司行动证据未齐；不计算严格账户收益"]}
    atomic_json_write(str(output), payload)
    return {"as_of": closed, "rows": len(bars), "codes_requested": len(codes),
            "status": "waiting_execution_data", "calendar_source": payload["metadata"]["calendar_source"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("evidence-db", "market-db", "calendar-file", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(**vars(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()
