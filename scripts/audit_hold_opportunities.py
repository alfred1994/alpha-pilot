#!/usr/bin/env python3
"""只读审计近期空仓 HOLD；--public-prices 显式启用公开日线查询。"""
import argparse
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy.decision_audit import audit_flat_holds, build_hold_audit_from_db, load_hold_audit_inputs, select_flat_holds


def fetch_bars(code, start, end):
    symbol = ("sh" if code.startswith("6") or code == "000300" else "sz") + code
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,{start},{end},120,"
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.load(response)
    if payload.get("code") != 0:
        raise ValueError("行情业务响应失败")
    data = payload["data"][symbol]
    rows = data.get("day")
    if not rows:
        raise ValueError("无不复权日线")
    return [{"date": r[0], "open": float(r[1]), "close": float(r[2]),
             "high": float(r[3]), "low": float(r[4])} for r in rows], data.get("qt", {}).get(symbol, ["", code])[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--as-of", required=True, help="已收盘日期 YYYY-MM-DD")
    parser.add_argument("--sessions", type=int, default=10)
    parser.add_argument("--public-prices", action="store_true")
    parser.add_argument("--output", help="可选，本地报告路径；不修改数据库")
    args = parser.parse_args()
    if not 1 <= args.sessions <= 30:
        parser.error("sessions 必须为 1-30")
    from datetime import date
    date.fromisoformat(args.as_of)
    with sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not args.public_prices:
            result = build_hold_audit_from_db(conn, args.as_of, args.sessions)
        else:
            decisions, trades, days = load_hold_audit_inputs(conn, args.as_of, args.sessions)
            selected, _ = select_flat_holds(decisions, trades)
            codes = {r["code"] for r in selected} | {"000300"}
            bars, names, errors = {}, {}, {}
            if days:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    futures = {pool.submit(fetch_bars, code, min(days), args.as_of): code for code in codes}
                    for future in concurrent.futures.as_completed(futures):
                        code = futures[future]
                        try:
                            bars[code], names[code] = future.result()
                        except Exception as exc:
                            errors[code] = type(exc).__name__
            calendar = [r["date"] for r in bars.get("000300", [])]
            result = audit_flat_holds(decisions, trades, bars, calendar, args.as_of)
            result.update(decision_dates=days, price_source="Tencent unadjusted daily bars",
                          names=names, fetch_errors=errors, calendar=calendar,
                          promotion_evidence=False)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
