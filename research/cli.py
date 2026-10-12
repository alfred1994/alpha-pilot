"""研究 CLI；report 完全只读，audit/compare 仅写独立研究库。"""
import json
from pathlib import Path

import pandas as pd

from research.audit import audit_panel, fingerprint
from research.store import import_report, read_experiments, register, save_report


def register_existing(report, kind, parameters=None, hypothesis=None):
    """在既有研究 CLI 完成后登记；纯计算函数仍保持无 IO。"""
    manifest = {"kind": kind, "hypothesis": hypothesis or kind, "parameters": parameters or {},
                "input_sha256": report.get("input_sha256", "unknown"), "interval_role": "review",
                "audit_contract": (report.get("data_audit") or {}).get("metadata", "unknown"),
                "execution": (report.get("strategy") or {}).get("config", "unknown"),
                "intervals": report.get("folds", "unknown"),
                "benchmarks": [k for k in ("equal_weight_benchmark", "buy_hold_net", "cash_return") if k in report]}
    identity = register(manifest)
    audit = report.get("data_audit") or {}
    return save_report(identity, {"as_of": "unknown", "result": report,
                                 "reasons": audit.get("errors", []) + audit.get("gaps", [])},
                       status="waiting_data" if audit.get("status") in ("unavailable", "approximate_only") else "completed")["experiment_id"]


def register_failure(kind, parameters, reason, input_sha256="unknown"):
    identity = register({"kind": kind, "hypothesis": kind, "parameters": parameters,
                         "input_sha256": input_sha256, "interval_role": "review"})
    return save_report(identity, {"as_of": "unknown", "failure_reason": reason,
                                 "verdict": "inconclusive"}, status="waiting_data")["experiment_id"]


def run(args):
    path = args.research_db
    try:
        if args.research_archive and not args.research_audit:
            raise ValueError("--research-archive requires --research-audit")
        if args.research_import and not args.research_audit:
            raise ValueError("--research-import requires --research-audit")
        if args.research_archive and (args.research_input or args.research_import):
            raise ValueError("archive cannot also audit/import input")
        if args.research_report:
            from research.runtime import health
            result = {"experiments": read_experiments(path, args.research_id, limit=args.research_limit,
                                                       offset=args.research_offset), "health": health(path), "errors": []}
        else:
            payload = {}
            if args.research_input:
                with open(args.research_input, encoding="utf-8") as file:
                    payload = json.load(file)
            if args.research_audit:
                if args.research_archive:
                    from research.store import archive
                    if not args.research_id:
                        raise ValueError("--research-id is required")
                    result = archive(args.research_id, path)
                elif args.research_import:
                    with open(args.research_import, encoding="utf-8") as file:
                        result = import_report(json.load(file), path, args.research_hypothesis or "legacy-import", args.research_parent)
                else:
                    if not args.research_input:
                        raise ValueError("--research-input is required")
                    frame = pd.DataFrame(payload.get("bars", []) if isinstance(payload, dict) else payload)
                    meta = payload.get("metadata", {}) if isinstance(payload, dict) else {}
                    audit = audit_panel(frame, payload.get("calendar") if isinstance(payload, dict) else None, meta)
                    manifest = {"kind": "audit", "hypothesis": args.research_hypothesis or "data-quality",
                                "parent_id": args.research_parent, "input_sha256": fingerprint(payload),
                                "parameters": meta, "interval_role": "review"}
                    identity = register(manifest, path)
                    result = save_report(identity, {"as_of": args.research_as_of or "unknown", "audit": audit}, path,
                                         "completed" if audit["status"] == "strict_ready" else "waiting_data")
                    result["errors"] = [] if audit["status"] == "strict_ready" else ["data_incomplete"]
            else:
                from research.compare import clock, compare, new_experiment
                from datetime import time
                if not args.research_input:
                    raise ValueError("--research-input is required")
                identity = args.research_id or new_experiment(
                    path=path, input_path=str(Path(args.research_input).resolve()),
                    retrospective=args.research_retrospective, start=args.research_start,
                    hypothesis=args.research_hypothesis or "last-llm-filter-v1", parent_id=args.research_parent,
                    input_sha256=fingerprint(payload) if args.research_retrospective else None)
                if not args.research_id and not args.research_retrospective and clock().time() < time(15, 15):
                    result = {"experiment_id": identity, "status": "registered", "promotion_allowed": False,
                              "as_of": None, "limitations": ["已登记，等待现场扫描和新的已收盘行情。"]}
                else:
                    result = compare(identity, payload, as_of=args.research_as_of, path=path)
                result["errors"] = ["data_incomplete"] if result["status"] == "waiting_data" else []
        result.setdefault("errors", [])
    except Exception as exc:
        result = {"status": "error", "reason": str(exc), "errors": ["research_evidence"]}
        if "identity" in locals():
            result["experiment_id"] = identity
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return result
