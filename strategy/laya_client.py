"""
Laya 影子决策客户端
====================================================================
调用本机 laya 常驻服务（scripts/laya/laya_server.py，独立 venv）对候选做
类型化决策（choice/score），与 MiMo 的既有判断做影子对照：

  - 影子模式：只打分落库，不拦截、不改变任何交易决策；
  - 状态只含特征（6 维分数、市场环境），绝不包含 MiMo 的答案，
    避免"对照"变成循环论证；
  - 任何失败（服务未起/超时/输出异常）都静默降级。

使用方法:
    from strategy.laya_client import is_available, run_shadow_comparison
    summary = run_shadow_comparison("2026-09-22")   # 返回摘要或 None
====================================================================
"""
import json
import math
import os
import re
import sys
from typing import Any, Dict, List, Optional

import httpx

from config import LAYA_BASE_URL, LAYA_DATA_DIR, LAYA_ENABLED, LAYA_TIMEOUT_SECONDS

# typed questions：choice=有界分类(带校准概率), score=有序评分。
# 措辞必须自包含：laya 没有对话上下文，问题里要写清评分语义。
SHADOW_QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": (
            "A-share intraday trading decision for this stock candidate. "
            "The state carries six signal-dimension scores (0-100; above 60 is "
            "bullish, below 40 is bearish, 50 is neutral) each with a "
            "confidence, plus the current market regime. Decide from the "
            "features only."
        ),
        "criteria": {
            "buy": "strongly bullish: most high-confidence dimensions above 60, no dominant bearish dimension, regime not bearish",
            "hold": "mixed or neutral: dimensions disagree or sit near 50, no clear edge",
            "sell": "strongly bearish: multiple high-confidence dimensions below 40 or dominant negative sentiment",
        },
    },
    "conviction": {
        "type": "score",
        "instructions": "How strong and consistent is the signal edge for this candidate?",
        "criteria": [
            "weak or conflicting signals",
            "moderate edge with some agreement across dimensions",
            "strong consistent edge across dimensions",
        ],
    },
}

ACTION_ALIASES = {
    "BUY": "buy", "SELL": "sell", "HOLD": "hold",
}


class LayaUnavailable(Exception):
    """Laya 服务不可用（未启用/未起/超时/输出异常）。"""


def is_available(timeout: float = 3.0) -> bool:
    """健康检查；服务未起或未启用时返回 False，不发交易侧副作用。"""
    if not LAYA_ENABLED:
        return False
    try:
        resp = httpx.get(f"{LAYA_BASE_URL}/health", timeout=timeout)
        return resp.status_code == 200 and bool(resp.json().get("ok"))
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        return False


def score_candidates(states: List[Dict[str, Any]],
                     timeout: int = None,
                     batch_size: int = 10) -> Optional[List[Dict[str, Any]]]:
    """
    分批调用 /predict；失败返回 None（不抛异常，调用方静默降级）。

    分批原因：ARM CPU 上单条状态实测 ~3s，一次性 40 条要 ~128s，会顶穿
    HTTP 超时导致整批作废。每批 10 条把单次请求压在 ~35s，且失败只损失
    已算完的部分不会拖垮整批。
    """
    if not states:
        return []
    results: List[Dict[str, Any]] = []
    for start in range(0, len(states), max(1, batch_size)):
        chunk = states[start:start + batch_size]
        try:
            resp = httpx.post(
                f"{LAYA_BASE_URL}/predict",
                json={"states": chunk, "questions": SHADOW_QUESTIONS},
                timeout=timeout or LAYA_TIMEOUT_SECONDS,
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return None
        if not payload.get("ok"):
            return None
        chunk_results = payload.get("results")
        if not isinstance(chunk_results, list) or len(chunk_results) != len(chunk):
            return None
        results.extend(chunk_results)
    return results


def build_candidate_state(decision: Dict[str, Any], name: str = "") -> Dict[str, Any]:
    """从 llm_decisions 行构建 laya 状态：只含特征，绝不含 MiMo 答案。"""
    dimensions = {}
    raw_dims = decision.get("dimensions")
    if isinstance(raw_dims, str):
        try:
            raw_dims = json.loads(raw_dims)
        except json.JSONDecodeError:
            raw_dims = None
    if isinstance(raw_dims, dict):
        for dim_name, dim in raw_dims.items():
            if not isinstance(dim, dict):
                continue
            try:
                score = round(float(dim.get("score") or 50.0), 1)
                confidence = round(float(dim.get("confidence") or 0.0), 3)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(score) and math.isfinite(confidence)):
                # "nan"/无穷分数进不了 laya 的输入（float("nan") 不抛异常）
                continue
            dimensions[str(dim_name)] = {
                "score": score,
                "confidence": confidence,
            }
    state = {
        # 固定中文市场标签：Router 按脚本路由 checkpoint（见 laya 实测），
        # 纯数字代码状态会被路由到有 sell 偏置的 english checkpoint；
        # 这个标签保证所有状态确定性地走 multilingual checkpoint。
        "market": "A股（沪深市场）",
        "code": str(decision.get("code") or ""),
        "date": str(decision.get("date") or ""),
        "dimensions": dimensions,
    }
    if name and name != state["code"]:
        state["name"] = str(name)
    return state


def _extract_name_from_prompt(prompt: str, code: str) -> str:
    """从 MiMo 提示词的股票信息段提取真实股票名（与 web 路由同一规则）。

    研究池里的 name 字段经常就是代码本身（纯数字），而提示词里由信号
    链路写入的「名称:」是真实中文名——真实名字既帮 Router 路由到
    multilingual checkpoint，也是模型可用的特征（如 ST 前缀）。
    """
    if not prompt:
        return ""
    match = re.search(r"名称[:：]\s*([^\s\n\r，,]+)", prompt)
    if not match:
        return ""
    name = match.group(1).strip()
    return name if name and name != code else ""


def _resolve_names(decisions: List[Dict[str, Any]]) -> Dict[str, str]:
    """从研究池补股票名（跳过 name==code 的占位条目；失败返回空映射）。"""
    try:
        from data.research_universe import UNIVERSE_FILE
        if not os.path.isfile(UNIVERSE_FILE):
            return {}
        with open(UNIVERSE_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        names = {}
        for item in payload.get("codes") or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "")
            name = str(item.get("name") or "")
            if code and name and name != code:
                names[code] = name
        return names
    except (OSError, json.JSONDecodeError):
        return {}


def _laya_answer(result: Dict[str, Any], question: str) -> Optional[Dict[str, Any]]:
    """解析 laya 答案（契约见 laya 0.3.x 实测输出）：

    choice -> {"type","choice","probabilities": {...}, "confidence"}
    score  -> {"type","score"(连续值), "legend": {级别: 措辞}, "probabilities"}
    """
    answers = (result or {}).get("answers") or {}
    answer = answers.get(question)
    if not isinstance(answer, dict):
        return None
    out: Dict[str, Any] = {}
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict):
        try:
            out["probabilities"] = {
                str(k): round(float(v), 4) for k, v in probabilities.items()
            }
        except (TypeError, ValueError):
            pass
    if question == "action":
        choice = answer.get("choice")
        out["choice"] = str(choice) if choice is not None else None
        if choice in (out.get("probabilities") or {}):
            out["probability"] = out["probabilities"][choice]
        confidence = answer.get("confidence")
        if confidence is not None:
            try:
                out["confidence"] = round(float(confidence), 4)
            except (TypeError, ValueError):
                pass
    else:
        score = answer.get("score")
        if score is not None:
            try:
                out["score"] = round(float(score), 4)
            except (TypeError, ValueError):
                pass
        legend = answer.get("legend")
        if isinstance(legend, dict):
            out["legend"] = {str(k): str(v) for k, v in legend.items()}
    return out or None


def run_shadow_comparison(date: str = None,
                          db_path: str = None) -> Optional[Dict[str, Any]]:
    """
    对指定日期 MiMo 已判断过的候选跑 laya 影子对照。

    返回摘要 dict（含逐只对照明细）；未启用/服务不可用/无候选返回 None。
    结果落盘 data/laya/shadow_<date>.json 并写一条 auto_event
    （event_type=laya_shadow），供 trader brief 与人工检查。
    """
    if not is_available():
        return None

    from data.database import Database

    with Database(db_path=db_path) as db:
        rows = [dict(r) for r in db.conn.execute(
            "SELECT code, date, action, confidence, dimensions, llm_prompt "
            "FROM llm_decisions "
            "WHERE date=? AND action IS NOT NULL", (date or "",),
        ).fetchall()]
    if not rows:
        return None

    # 名字优先级：提示词里的真实中文名 > 研究池。研究池的 name 经常是
    # 代码本身（纯数字），而提示词「名称:」由信号链路写入，是真实名字。
    pool_names = _resolve_names(rows)
    states = []
    for row in rows:
        code = str(row.get("code") or "")
        name = _extract_name_from_prompt(
            str(row.get("llm_prompt") or ""), code,
        ) or pool_names.get(code, "")
        states.append(build_candidate_state(row, name))
    results = score_candidates(states)
    if results is None:
        return None

    details = []
    agree = 0
    confusion: Dict[str, int] = {}
    for row, state, result in zip(rows, states, results):
        laya = {
            "action": _laya_answer(result, "action"),
            "conviction": _laya_answer(result, "conviction"),
        }
        mimo_action = ACTION_ALIASES.get(
            str(row.get("action") or "").upper(), str(row.get("action") or ""),
        )
        laya_action = (laya["action"] or {}).get("choice")
        matched = laya_action == mimo_action
        agree += 1 if matched else 0
        key = f"{mimo_action}->{laya_action or 'unknown'}"
        confusion[key] = confusion.get(key, 0) + 1
        detail = {
            "code": str(row.get("code") or ""),
            "laya": laya,
            "mimo": {
                "action": mimo_action,
                "confidence": round(float(row.get("confidence") or 0.0), 3),
            },
            "match": bool(matched),
        }
        routing = (result or {}).get("routing")
        if isinstance(routing, dict):
            # 路由信息用于诊断 checkpoint 选择（如误路由到 english 偏置）
            detail["routing"] = {
                str(k): str(v) for k, v in routing.items()
            }
        details.append(detail)

    summary = {
        "date": date,
        "n_candidates": len(rows),
        "agreement": f"{agree}/{len(rows)}",
        "agreement_pct": round(agree / len(rows), 4),
        "confusion": confusion,
        "details": details,
    }

    os.makedirs(LAYA_DATA_DIR, exist_ok=True)
    path = os.path.join(LAYA_DATA_DIR, f"shadow_{date}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    summary["path"] = path

    try:
        with Database(db_path=db_path) as db:
            db.insert_auto_event({
                "date": date,
                "event_type": "laya_shadow",
                "status": "盘后",
                "actions": ["Laya 影子对照完成"],
                "details": {
                    "n_candidates": summary["n_candidates"],
                    "agreement": summary["agreement"],
                    "confusion": confusion,
                },
            })
    except Exception:  # noqa: BLE001 —— 事件写入失败不影响摘要返回
        pass
    return summary


if __name__ == "__main__":
    # 手工自检: python -m strategy.laya_client [YYYY-MM-DD]
    from datetime import datetime
    target = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    print(f"enabled={LAYA_ENABLED} base_url={LAYA_BASE_URL} "
          f"available={is_available()}")
    result = run_shadow_comparison(target)
    print(json.dumps(result, ensure_ascii=False, indent=2) if result
          else "无结果（未启用/服务未起/当日无候选）")
    sys.exit(0)
