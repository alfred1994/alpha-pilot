"""获取KT ETF基准，并冻结三个低频方案后检查后段历史。"""
import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.kt_realtime import get_kt_client
from portfolio.factor_backtest import ExecutionConfig, run_portfolio
from strategy.etf_research import (BROAD_ETFS, SECTOR_ETFS, build_etf_targets,
                                   sector_weights_to_etfs, stock_targets_to_etfs, validate_etf_panel,
                                   parse_identity_adjustments)
from scripts.run_sector_research import UNIVERSE, write_json
from scripts.run_strategy_revision import window


def compact(book):
    return {"total":book["metrics"], "2025":window(book,"2025-01-01","2025-12-31"),
            "2026":window(book,"2026-01-01","2026-10-09"),
            "average_exposure":float(np.mean([1-r["cash"]/r["equity"] for r in book["curve"]])),
            "stale_mark_days":sum(bool(r["stale_marks"]) for r in book["curve"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="data/reports/etf-comparison-20261012")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True,exist_ok=True)
    execution = ExecutionConfig(capital=10_000_000, max_positions=17, max_weight=1,
                                stamp_tax=0, slippage=.001, mode="research", price_tick=.001)
    modes = ["sector_equal","sector_trend","sector_rotation"]
    write_json(root/"manifest.json", {"mapping":SECTOR_ETFS,"broad":BROAD_ETFS,
               "data_start":"2024-02-19","data_end":"2026-10-09","signal_start":"2025-05-29",
               "development_end":"2025-12-31","execution":asdict(execution),
               "candidates":modes,"selection_rule":"2025净收益-0.5*最大回撤绝对值最大；不正则现金",
               "adjustment":"KT qfq；仅day字段者须另用新浪hfq事件记录验证为单位调整。供应商记录不是基金公告审计，不重复加入现金分红",
               "limitations":["行业代理并非原股票池精确复制，证券保险ETF含证券重叠", "存续标的选择偏差",
                               "2026已多次观察，结果是探索性历史研究", "ETF不扣股票卖出印花税；管理费用已反映在价格中，不重复扣除"]})
    codes = sorted(set(BROAD_ETFS)|{c for m in SECTOR_ETFS.values() for c in m})
    client = get_kt_client()
    ticks=client.get_realtime_tick(codes)
    names=dict(zip(ticks.code,ticks.name)) if not ticks.empty else {}
    frames,coverage=[],[]
    for code in codes:
        raw=pd.DataFrame()
        for _ in range(2):
            raw=client.get_kline(code,period="d",count=1000)
            if not raw.empty: break
            time.sleep(1)
        if raw.empty:
            raise ValueError(f"{code} 无行情")
        adjustment=raw.attrs.get("adjustment")
        if adjustment!="qfq":
            symbol=client.normalize_security(code)[2]
            response=requests.get(f"https://finance.sina.com.cn/realstock/company/{symbol}/hfq.js",timeout=10)
            response.raise_for_status()
            events=parse_identity_adjustments(response.text)
            write_json(root/(code+"-adjustment-check.json"),events)
            adjustment="unadjusted_sina_identity_checked"
        raw=raw[(raw.data_date>="2024-02-19") & (raw.data_date<="2026-10-09")].sort_values("data_date")
        if raw.empty or raw.data_date.iloc[0]!="2024-02-19" or raw.data_date.iloc[-1]!="2026-10-09":
            raise ValueError(f"{code} 没覆盖共同起止日期")
        path=root/(code+"-kt.json")
        path.write_text(raw.to_json(orient="records",force_ascii=False),encoding="utf-8")
        frame=pd.DataFrame({"date":raw.data_date,"code":code,"open":raw.open,"high":raw.high,
                            "low":raw.low,"close":raw.close,"volume":raw.volume_raw,"source":"kt_tencent_qfq"})
        frames.append(frame)
        coverage.append({"code":code,"name":names.get(code,"unknown"),"rows":len(frame),"adjustment":adjustment,
                         "sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
        print("DATA",code,names.get(code),len(frame),flush=True)
        time.sleep(.1)
    panel=validate_etf_panel(pd.concat(frames).sort_values(["date","code"]))
    if panel.groupby("date").size().min()!=len(codes):
        raise ValueError("ETF面板有缺失日期，需要先核验，不能静默缩短对照区间")
    write_json(root/"coverage.json",coverage)
    from research.audit import audit_panel
    from research.cli import register_existing
    data_audit = audit_panel(panel, metadata={"adjustment": "qfq", "source": "kt_tencent"})
    write_json(root/"data-audit.json", data_audit)
    register_existing({"input_sha256": data_audit["input_sha256"], "data_audit": data_audit,
                       "config": {"mapping": SECTOR_ETFS, "broad": BROAD_ETFS}}, "etf-data")
    panel.to_json(root/"panel.json",orient="records",date_format="iso",force_ascii=False)
    dev=panel[panel.date<="2025-12-31"]
    scores={}
    for mode in modes:
        targets,audit=build_etf_targets(dev,mode)
        book=run_portfolio(dev,targets,execution)
        scores[mode]=book["metrics"]["total_return"]-.5*abs(book["metrics"]["max_drawdown"])
        write_json(root/(mode+"-development.json"),{"book":book,"signals":audit})
        print("DEVELOPMENT",mode,scores[mode],flush=True)
    selected=max(scores,key=lambda x:(scores[x],x))
    if scores[selected]<=0: selected="cash"
    write_json(root/"frozen_selection.json",{"selected":selected,"scores":scores})
    results,books={},{}
    for mode in modes:
        targets,audit=build_etf_targets(panel,mode)
        book=run_portfolio(panel,targets,execution)
        earlier=json.loads((root/(mode+"-development.json")).read_text(encoding="utf-8"))
        assert earlier["signals"]==audit[:len(earlier["signals"])]
        assert earlier["book"]["curve"]==[r for r in book["curve"] if r["date"]<="2025-12-31"]
        books[mode]=book
        results[mode]=compact(book)
        write_json(root/(mode+"-full.json"),{"book":book,"signals":audit})
        print("EVALUATION",mode,json.dumps(results[mode]),flush=True)
    signal=pd.Timestamp("2025-05-29")
    for code,name in BROAD_ETFS.items():
        book=run_portfolio(panel,{signal:{code:1.0}},execution)
        books[code]=book
        results[code]=compact(book)
        write_json(root/(code+"-buyhold.json"),book)
    sector_results={}
    for sector in SECTOR_ETFS:
        book=run_portfolio(panel,{signal:sector_weights_to_etfs({sector:1.0})},execution)
        sector_results[sector]=compact(book)
        write_json(root/("sector-"+str(list(SECTOR_ETFS).index(sector)+1)+".json"),book)
    revision=Path("data/reports/strategy-revision-20261011-final")
    previous=json.loads((revision/"balanced20-full.json").read_text(encoding="utf-8"))
    stock_sectors={c:s for s, members in UNIVERSE.items() for c in members}
    matched=stock_targets_to_etfs(previous["signals"],stock_sectors)
    book=run_portfolio(panel,matched,execution)
    books["allocation_matched_etfs"]=book
    results["allocation_matched_etfs"]=compact(book)
    results["stock_balanced20"]=compact(previous["book"])
    books["stock_balanced20"]=previous["book"]
    write_json(root/"allocation-matched.json",book)
    sensitivity={}
    if selected!="cash":
        targets,_=build_etf_targets(panel,selected)
        for capital,slippage in [(100_000,.001),(10_000_000,.0005),(10_000_000,.002)]:
            label=f"capital{capital}_slippage{slippage}"
            book=run_portfolio(panel,targets,replace(execution,capital=capital,slippage=slippage))
            sensitivity[label]=compact(book)
            write_json(root/(label+".json"),book)
    write_json(root/"summary.json",{"selected":selected,"results":results,"sector_buyhold":sector_results,
                                     "sensitivity":sensitivity,"promotion_allowed":False})
    register_existing({"input_sha256": data_audit["input_sha256"], "data_audit": data_audit,
                       "selected": selected, "results": results, "sector_buyhold": sector_results,
                       "sensitivity": sensitivity, "promotion_allowed": False}, "etf",
                      parameters={"execution": asdict(execution), "mapping": SECTOR_ETFS,
                                  "candidates": modes, "development_end": "2025-12-31"})
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(11,5))
    for key in ["510300","sector_equal","sector_trend","sector_rotation","stock_balanced20"]:
        book=books[key]
        ax.plot(pd.to_datetime([r["date"] for r in book["curve"]]),[r["equity"]/execution.capital for r in book["curve"]],label=key)
    ax.axvline(pd.Timestamp("2026-01-01"),color="gray",linestyle="--")
    ax.axhline(1,color="gray",linestyle=":")
    ax.set_title("ETF benchmarks and frozen research variants / through 2026-10-09")
    ax.set_ylabel("Equity / initial capital")
    ax.grid(alpha=.2); ax.legend(fontsize=8);fig.tight_layout()
    fig.savefig(root/"equity.png",dpi=160);plt.close(fig)
    lines=["# ETF对照与低频策略研究", "",f"仅用2025段选择并冻结的方案：**{selected}**；不自动晋级。",
           "区间：2025-05-30至2026-10-09；预热从2024-02-19开始。2026此前已经查看，仍属受污染历史检查。",
           "默认1000万元、单边佣金0.03%最低5元、单边滑点0.1%；国内股票ETF不扣卖出印花税，保留T+1、整手限制。",
           "KT显式qfq或KT未复权且新浪hfq记录为单位调整；未按基金公告逐笔审计供应商分红/拆分记录，不重复加现金分红。不是精确成交/总回报审计。", "",
           "|方案|全段收益|最大回撤|2025段|2026段|平均仓位|", "|---|---:|---:|---:|---:|---:|"]
    for name,item in results.items():
        lines.append(f"|{name}|{item['total']['total_return']:+.2%}|{item['total']['max_drawdown']:.2%}|{item['2025']['total_return']:+.2%}|{item['2026']['total_return']:+.2%}|{item['average_exposure']:.1%}|")
    lines += ["", "![净值](equity.png)", "", "## 行业ETF买入持有", "", "|人工行业组|ETF代码/组内比例|收益|最大回撤|", "|---|---|---:|---:|"]
    for sector,item in sector_results.items():
        desc=" / ".join(f"{c}({w:.0%})" for c,w in SECTOR_ETFS[sector].items())
        lines.append(f"|{sector}|{desc}|{item['total']['total_return']:+.2%}|{item['total']['max_drawdown']:.2%}|")
    lines += ["", "## 口径", "",
              "- sector_equal：10个人工行业组各10%，组内按manifest固定ETF比例，每20日调仓。",
              "- sector_trend：行业篮子在MA120以上且跳过最近20日的120日动量为正才保留该行业10%份额，剩余现金。",
              "- sector_rotation：趋势合格行业中按中期动量取前5，每组20%，不足留现金，每20日调仓。",
              "- allocation_matched_etfs：沿用此前balanced20股票目标的行业权重、日期、现金比例，只把行业内股票换成ETF，用于区分选股和行业配置效果。",
              "- 510300/510500：相同起止日期买入持有、不按后验表现择优选宽基。",
              "- 半仓/低仓位回撤较小不证明择时有效，须结合仓位和收益判断；本次未做独立前向检验。",
              "- 行业映射近似：证券保险ETF与证券ETF有重叠；芯片+通信不能精确复制电子股票，能源+电力、有色+化工、基建+运输同理。",
              "- 股票池及ETF池是当前存续品种；存在选择偏差。ETF价格与基金净值并非同一序列，市场溢折价已体现在价格中。",
              "- 涨跌停按前复权参考价±10%近似，不模拟排队、流动性容量或历史特殊交易规则；ETF采用0.001价格刻度。",
              "- 开发段用收益减0.5倍最大回撤的固定规则择优，写出frozen_selection后才评估2026；开发段信号/净值与全数据回放逐项相等。",
              "- 全部候选和成本、10万元资金敏感性保留在summary.json。", ""]
    lines += ["## 冻结方案与对照", "",
              f"开发段冻结选择为 {selected}，不按2026结果修改。",
              f"相同行业权重ETF替代组合收益 {results['allocation_matched_etfs']['total']['total_return']:+.2%}，"
              f"股票组合 {results['stock_balanced20']['total']['total_return']:+.2%}；差异包含成分、映射和费用，不能全部解释为纯选股损失。",
              "与两个宽基分别比较收益和回撤，不用低仓位回撤改善替代收益优势证明。后续需要新的前向模拟数据；本次不部署。", ""]
    (root/"report.md").write_text("\n".join(lines),encoding="utf-8")
    print("COMPLETE",root/"report.md",flush=True)


if __name__=="__main__": main()
