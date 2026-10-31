"""只读导出当前研究池的主板日线；stdout 支持 gzip 流，不写服务器数据库。"""
import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
import re
import sqlite3
import sys


def export_panel(db_path, universe_path, output, start, end, adjustment="unknown", max_rows=1_000_000):
    for day in (start, end):
        if datetime.fromisoformat(day).date().isoformat() != day:
            raise ValueError("日期必须为 YYYY-MM-DD")
    if start > end or max_rows < 1:
        raise ValueError("日期区间或行数上限无效")
    universe = json.loads(Path(universe_path).read_text(encoding="utf-8"))
    original = sorted({str(row["code"]) for row in universe.get("codes", [])})
    codes = [code for code in original if re.fullmatch(r"(?:000|001|002|003|600|601|603|605)\d{3}", code) and code != "000300"]
    if not codes:
        raise ValueError("研究池没有当前执行账本支持的主板股票")
    metadata = {
        "source": "existing_k_daily_readonly_snapshot", "adjustment": adjustment,
        "universe": "current_research_pool_mainboard_subset", "universe_generated_at": universe.get("generated_at", "unknown"),
        "snapshot_at_utc": datetime.now(timezone.utc).isoformat(), "requested_pool_size": len(original),
        "selected_mainboard_codes": len(codes), "excluded_unsupported_board_codes": sorted(set(original) - set(codes)),
        "selection_rule": "主板执行范围在评估收益前固定；不按收益、历史长度或最新日期筛选",
        "historical_membership_verified": False, "delisted_coverage_verified": False, "corporate_actions_verified": False,
    }
    # mode=ro 既防止修改，也防止路径错误时悄悄创建空数据库。
    db = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        db.row_factory = sqlite3.Row
        cursor = db.execute(
            "SELECT code,date,open,high,low,close,volume,source FROM k_daily WHERE code IN ("
            + ",".join("?" for _ in codes) + ") AND date>=? AND date<=? ORDER BY date,code", codes + [start, end])
        context = nullcontext(sys.stdout.buffer) if str(output) == "-" else Path(output).open("xb")
        count = 0
        with context as stream, gzip.GzipFile(fileobj=stream, mode="wb", compresslevel=3, mtime=0) as zipped:
            with io.TextIOWrapper(zipped, encoding="utf-8") as text:
                text.write('{"metadata":' + json.dumps(metadata, ensure_ascii=False)
                           + ',"expected_codes":' + json.dumps(codes) + ',"bars":[')
                while batch := cursor.fetchmany(2000):
                    if count + len(batch) > max_rows:
                        raise ValueError("快照超过预设行数上限；保留未完成输出，不能用于训练")
                    if count:
                        text.write(",")
                    # 每批编码/压缩一次，避免百万行逐字段触发 gzip.write 的开销。
                    text.write(json.dumps([dict(row) for row in batch], ensure_ascii=False,
                                          separators=(",", ":"), allow_nan=False)[1:-1])
                    count += len(batch)
                summary = {"rows": count, "source_pool": len(original), "mainboard_codes": len(codes),
                           "excluded_other_boards": len(original) - len(codes), "start": start, "end": end}
                text.write('],"export":' + json.dumps(summary) + "}")
        return summary
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--universe", required=True, type=Path)
    parser.add_argument("--output", required=True, help="新 JSON.gz 文件，或 - 输出二进制 gzip 到 stdout")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--adjustment", choices=("unknown", "raw", "qfq", "hfq"), default="unknown")
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    args = parser.parse_args(argv)
    result = export_panel(args.db, args.universe, args.output, args.start_date, args.end_date, args.adjustment, args.max_rows)
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
