"""只读获取冻结股票池的完整日线快照；全部返回后核验，不补造停牌记录。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import gzip
import json
import math
from pathlib import Path
import sys
import time
import urllib.request

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.kt_realtime import get_kt_client
from research.ml_forward import BJ, calendar_days, frame_records, latest_closed
from scripts.run_ml_forward import load
from scripts.run_ml_research import file_hash, write_json
from strategy.etf_research import BROAD_ETFS, parse_identity_adjustments
from strategy.factor_research import validate_panel


def identity_stock_adjustments(text, cutoff):
    """股票复权文件只有 d/f；不能套用 ETF 的 f/s/u 字段，也不能接受空响应。"""
    payload, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows or payload.get("total") != len(rows):
        raise ValueError("股票复权记录不完整")
    past = [row for row in rows if pd.Timestamp(row["d"]) <= pd.Timestamp(cutoff)]
    if not past or any(not math.isfinite(float(row["f"])) or float(row["f"]) != 1 for row in past):
        raise ValueError("股票复权记录不支持单位调整")
    return payload


def fetch_verified(code, target, *, start=None, kline=None, events=None, suspension=None, fallback=None):
    client = get_kt_client()
    frame = (kline or (lambda c: client.get_kline(c, period="d", count=1000)))(code)
    if frame is None or frame.empty:
        if start is None:
            return frame
        symbol = client.normalize_security(code)[0].lower() + "." + code
        def query_history():
            from data.history import query_baostock_history_rows
            return query_baostock_history_rows(symbol, "date,code,open,high,low,close,volume,tradestatus",
                                               start, target, "d", "2", timeout=25)
        payload = (fallback or query_history)()
        if not payload or payload.get("error_code") != "0" or not payload.get("data"):
            return frame
        raw = pd.DataFrame(payload["data"], columns=payload["fields"])
        if set(raw.code) != {symbol} or not raw.tradestatus.isin(["0", "1"]).all():
            raise ValueError("后备日线代码/状态无效")
        raw.loc[raw.tradestatus.eq("0") & raw.volume.eq(""), "volume"] = "0"
        frame = pd.DataFrame({"data_date": raw.date, **{key: pd.to_numeric(raw[key], errors="raise") for key in ("open", "high", "low", "close")},
                              "volume_raw": pd.to_numeric(raw.volume, errors="raise")})
        if (not np.isfinite(frame[["open", "high", "low", "close", "volume_raw"]]).all().all()
                or (frame[["open", "high", "low", "close"]] <= 0).any().any() or (frame.volume_raw < 0).any()):
            raise ValueError("后备日线价格/成交量缺失或无效")
        frame.attrs.update(adjustment="qfq", verification_evidence={"fallback_history": {"source": "baostock_adjustflag_2", "raw": payload}})
    frame = frame.copy()
    evidence = dict(frame.attrs.get("verification_evidence", {}))
    if frame.attrs.get("adjustment") == "unadjusted":
        symbol = client.normalize_security(code)[2]
        def download(kind):
            with urllib.request.urlopen(f"https://finance.sina.com.cn/realstock/company/{symbol}/{kind}.js", timeout=10) as stream:
                return stream.read().decode("utf-8")
        read = events or download
        parse = parse_identity_adjustments if code in BROAD_ETFS else identity_stock_adjustments
        evidence["identity_adjustments"] = {kind: parse(read(kind), target) for kind in ("qfq", "hfq")}
        frame.attrs["adjustment"] = "qfq"
    if target not in set(frame.data_date) and frame.data_date.max() < target:
        symbol = client.normalize_security(code)[0].lower() + "." + code
        def query():
            from data.history import query_baostock_history_rows
            return query_baostock_history_rows(symbol, "date,code,open,high,low,close,volume,tradestatus",
                                               target, target, "d", "2", timeout=20)
        payload = (suspension or query)()
        if payload and payload.get("error_code") == "0" and len(payload.get("data", [])) == 1:
            row = dict(zip(payload["fields"], payload["data"][0]))
            if row.get("date") == target and row.get("code") == symbol and row.get("tradestatus") == "0" and row.get("volume") in ("", "0", "0.0"):
                prices = {key: float(row[key]) for key in ("open", "high", "low", "close")}
                if (any(not math.isfinite(value) or value <= 0 for value in prices.values())
                        or prices["low"] > min(prices["open"], prices["close"])
                        or prices["high"] < max(prices["open"], prices["close"])):
                    raise ValueError("停牌行情价格无效")
                evidence["suspension"] = {"source": "baostock_adjustflag_2", "raw": payload,
                                          "normalization": "tradestatus=0 且空/零成交量映射为不可交易的零量行"}
                attrs = dict(frame.attrs)
                frame = pd.concat([frame, pd.DataFrame([dict(prices, data_date=target, volume_raw=0.0)])], ignore_index=True)
                frame.attrs.update(attrs)
    frame.attrs["verification_evidence"] = evidence
    return frame


def collect(seed, calendar_payload, directory, *, workers=2, fetch=None, now=None, progress=None, reuse=None):
    """只请求公开行情；不读取凭证、不写行情库、不更新任何账户。"""
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 4:
        raise ValueError("行情并发数须为 1 至 4")
    rows = seed.get("bars", []) if isinstance(seed, dict) else seed
    bars = validate_panel(pd.DataFrame(rows))
    expected = sorted(set(seed.get("expected_codes") or bars.code)) if isinstance(seed, dict) else sorted(set(bars.code))
    if not expected or len(expected) > 800 or set(expected) != set(bars.code):
        raise ValueError("冻结股票池不完整或超出 800 只上限")
    for code in expected:
        validate_panel(bars[bars.code == code].iloc[:1])
    instant = now or datetime.now(BJ)
    sessions = calendar_days(calendar_payload)
    target = latest_closed(sessions, instant)
    start = bars.date.min().strftime("%Y-%m-%d")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {"created_at": instant.isoformat(), "source": "tencent_gtimg_qfq", "expected_codes": expected,
                "start": start, "target_date": target, "workers": workers, "requested_bars": 1000,
                "read_only_providers": True, "promotion_allowed": False,
                "rule": "不按收益筛选；不伪造停牌/公司行动；全池成功后才产出 ready"}
    good, failures, proofs = [], [], {}
    if reuse is not None:
        reuse = Path(reuse)
        parent = load(reuse / "manifest.json")
        if parent["target_date"] != target or parent["start"] != start or parent["expected_codes"] != expected:
            raise ValueError("复用快照与当前日期/冻结股票池不一致")
        files = [reuse / name for name in ("stocks.json.gz", "benchmarks.json.gz", "manifest.json", "summary.json")]
        manifest["reused_files"] = {str(p.resolve()): file_hash(p) for p in files}
        for path in files[:2]:
            good.extend(load(path)["bars"])
        if set(r["code"] for r in good) - set(expected) - set(BROAD_ETFS):
            raise ValueError("复用快照包含未登记证券")
        proof_path = reuse / "provider-evidence.json"
        if proof_path.exists():
            proofs.update(load(proof_path))
            manifest["reused_files"][str(proof_path.resolve())] = file_hash(proof_path)
    write_json(directory / "manifest.json", manifest)
    write_json(directory / "calendar.json", calendar_payload)
    fetch = fetch or (lambda code: fetch_verified(code, target, start=start))
    current_codes = {r["code"] for r in good if r["date"] == target}
    codes = [code for code in expected + list(BROAD_ETFS) if code not in current_codes]
    good = [r for r in good if r["code"] in current_codes]
    emit = progress or (lambda event: None)

    def one(code):
        started = time.monotonic()
        try:
            frame = fetch(code)
            if frame is None or frame.empty:
                return code, None, "missing_history", {}
            if frame.attrs.get("adjustment") != "qfq":
                return code, None, "unverified_adjustment", {}
            if frame.data_date.duplicated().any():
                return code, None, "duplicate_dates", {}
            current = frame[(frame.data_date >= start) & (frame.data_date <= target)]
            if current.empty or target not in set(current.data_date):
                return code, None, "latest_closed_bar_missing_requires_suspension_evidence", {}
            result = pd.DataFrame({"date": current.data_date, "code": code, "open": current.open,
                                   "high": current.high, "low": current.low, "close": current.close,
                                   "volume": current.volume_raw, "source": "tencent_gtimg_qfq"})
            if (not np.isfinite(result[["open", "high", "low", "close", "volume"]]).all().all()
                    or (result[["open", "high", "low", "close"]] <= 0).any().any() or (result.volume < 0).any()
                    or (result.high < result[["open", "low", "close"]].max(axis=1)).any()
                    or (result.low > result[["open", "high", "close"]].min(axis=1)).any()):
                return code, None, "invalid_daily_prices_or_volume", {}
            evidence = frame.attrs.get("verification_evidence", {})
            if "fallback_history" in evidence:
                result["source"] = "baostock_qfq_fallback"
            if "suspension" in evidence:
                result.loc[result.date == target, "source"] = "baostock_qfq_suspension"
            return code, frame_records(result), None, evidence
        except Exception as exc:
            return code, None, type(exc).__name__, {}
        finally:
            # 小请求也限速，避免在数据源快速响应时形成突发压力。
            time.sleep(max(0, .15 - (time.monotonic() - started)))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, (code, result, error, evidence) in enumerate(pool.map(one, codes), 1):
            if error:
                failures.append({"code": code, "reason": error})
            else:
                good.extend(result)
                if evidence:
                    proofs[code] = evidence
            if index % 50 == 0 or index == len(codes):
                emit({"stage": "collect", "completed": index, "total": len(codes), "failures": len(failures)})
    end = now or datetime.now(BJ)
    if latest_closed(sessions, end) != target:
        failures.append({"code": None, "reason": "session_changed_during_collection"})
    if any(file_hash(path) != digest for path, digest in manifest.get("reused_files", {}).items()):
        failures.append({"code": None, "reason": "reused_snapshot_changed_during_collection"})
    metadata = {"source": "tencent_gtimg_qfq", "adjustment": "qfq", "universe": "current_frozen_research_pool",
                "calendar_source": "baostock_raw_trade_dates", "corporate_actions_verified": False,
                "snapshot_at": end.isoformat()}
    for filename, subset, code_list in (("stocks.json.gz", [r for r in good if r["code"] in expected], expected),
                                        ("benchmarks.json.gz", [r for r in good if r["code"] in BROAD_ETFS], list(BROAD_ETFS))):
        payload = {"bars": sorted(subset, key=lambda r: (r["date"], r["code"])), "metadata": metadata,
                   "expected_codes": code_list, "calendar": [d for d in sessions if start <= d <= target]}
        with (directory / filename).open("xb") as stream, gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as zipped:
            zipped.write(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
    summary = {"status": "waiting_data" if failures else "ready", "as_of": target, "rows": len(good),
               "symbols_expected": len(expected), "failures": failures, "promotion_allowed": False,
               "execution_status": "waiting_execution_data", "note": "只提供因子行情，未核验原价、实际涨跌停和公司行动账务"}
    write_json(directory / "summary.json", summary)
    write_json(directory / "provider-evidence.json", proofs)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-input", type=Path, required=True, help="此前固定股票池快照，股票范围不按收益改变")
    parser.add_argument("--calendar-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--reuse-snapshot", type=Path, help="复用同日完整成功的证券，仅重试缺口；仍写入新目录")
    args = parser.parse_args(argv)
    before = {str(p): file_hash(p) for p in (args.seed_input, args.calendar_input)}
    result = collect(load(args.seed_input), load(args.calendar_input), args.output, workers=args.workers,
                     reuse=args.reuse_snapshot,
                     progress=lambda item: print(json.dumps(item), flush=True))
    if any(file_hash(path) != value for path, value in before.items()):
        write_json(args.output / "failure.json", {"reason": "collection_inputs_changed", "promotion_allowed": False})
        return 2
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
