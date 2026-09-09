"""同花顺财务研究上下文：按披露日筛选，不产生交易动作。"""
import argparse
import json
import math
from datetime import datetime, timedelta, timezone

from data.hithink import get_client, normalize_code

TZ = timezone(timedelta(hours=8))
STATEMENTS = ("income-statements", "balance-sheets", "cash-flow-statements")


def build_research_context(codes, as_of=None, client=None):
    """as_of 表示上海时区当日收盘后；不承诺历史修订版本可复原。"""
    today = datetime.now(TZ).date()
    day = datetime.strptime(as_of, "%Y-%m-%d").date() if as_of else today
    if day > today:
        raise ValueError("as_of must not be in the future")
    codes = list(dict.fromkeys(normalize_code(code) for code in codes))
    if not 1 <= len(codes) <= 20:
        raise ValueError("research request requires one to twenty stocks")
    cutoff = int(datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ).timestamp() * 1000)
    client = client if client is not None else get_client()
    result = {"source": "hithink", "as_of": day.isoformat(),
              "as_of_semantics": "Asia/Shanghai end of day; research only",
              "historical_revisions_verified": False, "stocks": []}
    for code in codes:
        item = {"code": code, "financials": {}, "valuation": None, "missing": {}}
        for statement in STATEMENTS:
            if client is None:
                item["missing"][statement] = "disabled_or_missing_key"
                continue
            try:
                # Limited latest-period window: explicitly report missing historical coverage.
                data = client.get_financials(code, statement=statement, period="quarterly", limit=20)
                visible = []
                for row in data.get("item", []):
                    stamp, period_end = row.get("report_date_ms"), row.get("period_end_ms")
                    if row.get("thscode") != code:
                        continue
                    if any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) or v <= 0 or v >= cutoff
                           for v in (stamp, period_end)):
                        continue
                    visible.append(row)
                visible.sort(key=lambda row: (row["period_end_ms"], row["report_date_ms"]), reverse=True)
                item["financials"][statement] = visible
                if not visible:
                    item["missing"][statement] = "no_disclosed_rows_in_latest_20_periods"
            except Exception as exc:
                item["missing"][statement] = type(exc).__name__
        # This provider exposes latest valuations only; never backfill historical PE/PB.
        if day != today:
            item["missing"]["valuation"] = "historical_valuation_not_supported"
        elif client is None:
            item["missing"]["valuation"] = "disabled_or_missing_key"
        else:
            try:
                valuation = client.get_valuations([code])
                rows = [row for row in valuation.get("item", []) if row.get("thscode") == code]
                item["valuation"] = {"item": rows, "batches": valuation.get("batches", []),
                                     "historical": False}
                if not rows:
                    item["missing"]["valuation"] = "no_data"
            except Exception as exc:
                item["missing"]["valuation"] = type(exc).__name__
        result["stocks"].append(item)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stocks", required=True, help="逗号分隔，最多20只")
    parser.add_argument("--as-of", help="上海时区YYYY-MM-DD盘后；默认今天")
    args = parser.parse_args()
    result = build_research_context(args.stocks.split(","), args.as_of)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
