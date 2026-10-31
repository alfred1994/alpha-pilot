"""复现 lottery/signals.py 的点值表：相对收盘线有利 d 分时的赢/走盘比例。

用法：python scripts/lottery_calibrate.py <SBR games.csv>
输入需要列 home_margin、home_spread（收盘主队让分）、total_points、tot_close。
数据来自 nba-prediction-lab 的 SBR 归档（2007-08 → 2022-23，19,385 场），不入库。
"""
import json
import sys

import numpy as np
import pandas as pd


def table(residual: np.ndarray):
    rows = []
    for d in np.arange(0, 10.5, 0.5):
        win = 0.5 * (np.mean(residual + d > 0) + np.mean(-residual + d > 0))
        push = 0.5 * (np.mean(residual + d == 0) + np.mean(-residual + d == 0))
        rows.append((float(d), round(float(win), 4), round(float(push), 4)))
    return rows


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    games = pd.read_csv(sys.argv[1]).dropna(subset=["home_spread", "home_margin", "tot_close", "total_points"])
    spread = (games.home_margin + games.home_spread).to_numpy()
    total = (games.total_points - games.tot_close).to_numpy()
    print(json.dumps({"games": int(len(games)), "spread_sd": round(float(spread.std()), 2),
                      "total_sd": round(float(total.std()), 2),
                      "SPREAD_TABLE": table(spread), "TOTAL_TABLE": table(total)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
