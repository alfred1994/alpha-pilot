"""隔离ETF前向实验：初始化、盘后记录或只读状态；无Broker/LLM调用。"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

import pandas as pd
import requests

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from data.kt_realtime import get_kt_client
from strategy.etf_research import BROAD_ETFS, SECTOR_ETFS, parse_identity_adjustments
from scheduler.etf_forward import SHANGHAI, closed_panel, initialize, advance, read_status


def collect(now):
    client=get_kt_client()
    codes=sorted(set(BROAD_ETFS)|{c for m in SECTOR_ETFS.values() for c in m})
    frames,adjustments={},{}
    for code in codes:
        frame=client.get_kline(code,period="d",count=1000)
        if frame.empty:
            raise ValueError(f"{code} KT日线缺失")
        symbol=client.normalize_security(code)[2]
        response=requests.get(f"https://finance.sina.com.cn/realstock/company/{symbol}/hfq.js",timeout=10)
        response.raise_for_status()
        payload,_=json.JSONDecoder().raw_decode(response.text[response.text.index("{"):])
        records=payload.get("data")
        if not records or payload.get("total")!=len(records):
            raise ValueError(f"{code} 公司行动元数据缺失")
        if frame.attrs.get("adjustment")!="qfq":
            parse_identity_adjustments(response.text,now.date().isoformat())
        frames[code]=pd.DataFrame({"date":frame.data_date,"code":code,"open":frame.open,"close":frame.close,
                                   "high":frame.high,"low":frame.low,"volume":frame.volume_raw})
        adjustments[code]=records
    # 固定共同历史起点；不删除中间缺失的日期。
    start=max(frame.date.min() for frame in frames.values())
    panel=pd.concat([frame[frame.date>=start] for frame in frames.values()],ignore_index=True).sort_values(["date","code"])
    panel=closed_panel(panel,now)
    latest=panel.date.max().strftime("%Y-%m-%d")
    ticks=client.get_realtime_tick(codes)
    if ticks.empty or set(ticks.code)!=set(codes) or ticks.code.duplicated().any():
        raise ValueError("原始收盘快照覆盖不足")
    if not (ticks.data_date==latest).all() or not ticks.time.str[-8:].ge("15:00:00").all():
        raise ValueError("只接受与已收盘日线同日、15点之后的原始快照")
    raw=pd.DataFrame({"date":ticks.data_date,"code":ticks.code,"open":ticks.open,"close":ticks.price,
                      "high":ticks.high,"low":ticks.low,"volume":ticks.volume_raw})
    # 仅保存截至已收盘日的事件，未来预告不能悄悄进入信号/账户。
    adjustments={code:sorted([r for r in rows if str(r.get("d",""))<=latest],key=lambda r:r["d"])
                 for code,rows in adjustments.items()}
    if any(not records for records in adjustments.values()):
        raise ValueError("公司行动记录没有覆盖收盘日")
    return panel,raw,adjustments


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init",action="store_true")
    group.add_argument("--step",action="store_true")
    group.add_argument("--status",action="store_true")
    parser.add_argument("--db",default="data/reports/etf-forward-20261012/forward.db")
    parser.add_argument("--capital",type=float,default=100_000)
    args=parser.parse_args()
    try:
        if args.status:
            value=read_status(args.db)
            result=value["latest"]
        else:
            now=datetime.now(SHANGHAI)
            if args.step:
                value=read_status(args.db)
                if value["latest"].get("status")=="completed":
                    print(json.dumps({"status":"completed","promotion_allowed":False}))
                    return
            panel,raw,actions=collect(now)
            # 信号的可用时间是全部数据到达后，不能使用网络请求开始前的时钟。
            now=datetime.now(SHANGHAI)
            result=(initialize(args.db,panel,raw,actions,now,args.capital) if args.init
                    else advance(args.db,panel,raw,actions,now))
        summary={k:v for k,v in result.items() if k!="accounts"}
        if "accounts" in result:
            summary["accounts"]={mode:{"metrics":book["metrics"],"last_day":book["curve"][-1],
                                       "blocked_counts":book["blocked_counts"]} for mode,book in result["accounts"].items()}
        print(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False))
    except Exception as exc:
        print(json.dumps({"status":"blocked","reason":str(exc),"promotion_allowed":False},ensure_ascii=False))
        raise SystemExit(1)


if __name__=="__main__":main()
