#!/usr/bin/env python3
"""标记并纠正回放成交对模拟账户的污染。

背景
----
生产账户 ``data/quant.db`` 的 ``trades`` 表最早 7 条记录写于 2026-06-25
凌晨 05:12~07:35，当时 A 股尚未开盘：

    id=1/4 603459 红板科技  52.50 -> 94.96  (+80.9%)  两分钟内往返
    id=2/5 002990 盛视科技  38.80 -> 72.60  (+87.1%)  两分钟内往返
    id=3/6 603698 航天工程  18.20 -> 39.45  (+116.8%) 两分钟内往返
    id=7    002990 盛视科技  72.60 买入 1200 股

涨跌停是 ±10%，且 09:30 才开盘，这三笔不可能是真实成交，只可能是引导期/回放
脚本写进了生产账户。它们带来 156,491.13 元虚假盈利，把账户从 1,000,000 抬到
1,159,156（首日 +15.9%），此后所有绩效、熔断基准、日终快照与 LLM 复盘都建立
在这个虚高基准上。

本工具做两件互相独立的事
------------------------
1. ``--mark``  把指定成交标记 ``is_replay=1``。``Database.get_trades()`` 默认
   排除这类记录，胜率、逐笔统计、复盘与看板因此不再把它们当交易样本。
2. ``--unwind`` 把账户现金与总资产按虚假盈利净额下调，使收益口径回到真实值
   （表观 +12.76% -> 真实 -2.89%）。

安全性
------
- 默认只报告不写盘，必须显式 ``--apply``。
- 写入前强制备份 quant.db（含 -wal/-shm）与 paper_account.json。
- 成交必须用 ``--trade-ids`` 显式列出，工具不做任何模糊匹配。
- 每次执行写审计记录到 data/replay_quarantine.json。

用法
----
    python scripts/replay_quarantine.py                       # 只报告
    python scripts/replay_quarantine.py --mark                # 只标记
    python scripts/replay_quarantine.py --unwind              # 只纠正账户
    python scripts/replay_quarantine.py --mark --unwind --apply
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATA_DIR  # noqa: E402

AUDIT_FILE = "replay_quarantine.json"
#: 引导期写入的成交（见模块 docstring）。必须显式确认，不做自动识别。
DEFAULT_REPLAY_IDS = [1, 2, 3, 4, 5, 6, 7]


def _db_path() -> str:
    return os.path.join(DATA_DIR, "quant.db")


def _backup(stamp: str) -> str:
    """复制数据库与 JSON 镜像；WAL 模式下一致性依赖 -wal/-shm 一起复制。"""
    target_dir = os.path.join(DATA_DIR, "backups", f"replay-quarantine-{stamp}")
    os.makedirs(target_dir, exist_ok=True)
    for name in ("quant.db", "quant.db-wal", "quant.db-shm", "paper_account.json"):
        source = os.path.join(DATA_DIR, name)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(target_dir, name))
    return target_dir


def _inspect(conn, trade_ids):
    rows = []
    for trade_id in trade_ids:
        row = conn.execute(
            "SELECT id, code, name, action, price, shares, pnl, commission, created_at "
            "FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if row is None:
            print(f"  警告: 成交 id={trade_id} 不存在，已跳过")
            continue
        rows.append(row)
    return rows


def _replay_net_gain(rows):
    """回放成交带来的虚假净盈利。

    只累计已平仓(SELL)的 pnl：未平仓的买入只占用现金、不产生盈利，unwind 时
    只需下调现金与总资产，不必动持仓。
    """
    return sum(float(row[6] or 0.0) for row in rows if row[3] == "SELL")


def _report(conn, rows, net_gain):
    state = conn.execute(
        "SELECT initial_capital, cash, total_assets, position_count "
        "FROM account_state WHERE id = 1").fetchone()
    total_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    flagged = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE COALESCE(is_replay, 0) = 1").fetchone()[0]

    print("回放成交明细：")
    for row in rows:
        print(f"  id={row[0]:<4} {row[1]} {row[2] or '':<8} {row[3]:<4} "
              f"@{row[4]:<9} x{row[5]:<6} pnl={row[6]}  {row[8]}")
    print()
    print(f"虚假净盈利      : {net_gain:,.2f}")
    if state is None:
        print("账户状态        : 无 account_state 记录")
        return
    initial, cash, total_assets = state[0], state[1], state[2]
    print(f"initial_capital : {initial:,.2f}")
    print(f"当前 cash       : {cash:,.2f}")
    print(f"当前 total_assets: {total_assets:,.2f}")
    print(f"成交总数        : {total_trades}（已标记回放 {flagged}）")
    if initial:
        print(f"表观收益率      : {(total_assets / initial - 1) * 100:+.2f}%")
        print(f"纠正后收益率    : {((total_assets - net_gain) / initial - 1) * 100:+.2f}%")
    print(f"纠正后 total_assets: {total_assets - net_gain:,.2f}")


def _apply(conn, rows, net_gain, do_mark, do_unwind, account_path):
    changed = {"marked_ids": [], "unwound_amount": 0.0}
    if do_mark:
        ids = [row[0] for row in rows]
        conn.execute(
            f"UPDATE trades SET is_replay = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
        changed["marked_ids"] = ids
        print(f"已标记回放成交: {ids}")

    if do_unwind and abs(net_gain) > 1e-9:
        conn.execute(
            "UPDATE account_state SET cash = cash - ?, total_assets = total_assets - ? "
            "WHERE id = 1", (net_gain, net_gain))
        changed["unwound_amount"] = net_gain
        print(f"已下调账户 {net_gain:,.2f}（cash 与 total_assets）")
    elif do_unwind:
        print("虚假净盈利为 0，无需下调账户")

    if do_unwind and os.path.exists(account_path):
        with open(account_path, encoding="utf-8") as handle:
            mirror = json.load(handle)
        if "cash" in mirror:
            mirror["cash"] = float(mirror["cash"]) - net_gain
            with open(account_path, "w", encoding="utf-8") as handle:
                json.dump(mirror, handle, ensure_ascii=False, indent=2)
            print("已同步 paper_account.json 的 cash")
        else:
            print("警告: paper_account.json 无 cash 字段，未同步")

    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trade-ids", default=",".join(str(i) for i in DEFAULT_REPLAY_IDS),
                        help="逗号分隔的回放成交 id")
    parser.add_argument("--mark", action="store_true", help="标记 is_replay=1")
    parser.add_argument("--unwind", action="store_true", help="按虚假盈利下调账户")
    parser.add_argument("--apply", action="store_true", help="真正写盘（默认只报告）")
    args = parser.parse_args()

    try:
        trade_ids = [int(part) for part in args.trade_ids.split(",") if part.strip()]
    except ValueError:
        parser.error("--trade-ids 必须是逗号分隔的整数")
    if not trade_ids:
        parser.error("--trade-ids 不能为空")

    db_file = _db_path()
    if not os.path.exists(db_file):
        parser.error(f"找不到数据库 {db_file}")
    account_path = os.path.join(DATA_DIR, "paper_account.json")

    conn = sqlite3.connect(db_file)
    try:
        rows = _inspect(conn, trade_ids)
        if not rows:
            print("没有可处理的成交")
            return 1
        net_gain = _replay_net_gain(rows)
        _report(conn, rows, net_gain)

        if not (args.mark or args.unwind):
            print("\n仅报告。加 --mark / --unwind 指定动作，再加 --apply 才会写盘。")
            return 0
        if not args.apply:
            print("\n未指定 --apply，不做任何写盘。")
            return 0

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = _backup(stamp)
        print(f"\n已备份到 {backup_dir}")
        changed = _apply(conn, rows, net_gain, args.mark, args.unwind, account_path)
        conn.commit()
    finally:
        conn.close()

    record = {
        "applied_at": datetime.now().isoformat(),
        "backup_dir": backup_dir,
        "trade_ids": trade_ids,
        "replay_net_gain": net_gain,
        **changed,
    }
    audit_path = os.path.join(DATA_DIR, AUDIT_FILE)
    history = []
    if os.path.exists(audit_path):
        try:
            with open(audit_path, encoding="utf-8") as handle:
                history = json.load(handle).get("history", [])
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(record)
    with open(audit_path, "w", encoding="utf-8") as handle:
        json.dump({"history": history}, handle, ensure_ascii=False, indent=2)
    print(f"审计记录已写入 {audit_path}")
    print("\n注意: daily_snapshots / circuit_breaker / system_risk 的历史序列"
          "仍含污染值，本工具未改动它们——纠正序列需要去重并按同一偏移重算，"
          "属于独立决策，详见 docs/architecture-audit-2026-09-28.md A 域。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
