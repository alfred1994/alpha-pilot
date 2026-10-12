import os
import sys
import unittest
from dataclasses import replace

import numpy as np
import pandas as pd

sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategy.etf_research import SECTOR_ETFS,build_etf_targets,sector_weights_to_etfs,stock_targets_to_etfs,validate_etf_panel
from portfolio.factor_backtest import ExecutionConfig,run_portfolio,_cent
from strategy.etf_research import parse_identity_adjustments


def fixture():
    rows=[]
    codes=sorted({c for m in SECTOR_ETFS.values() for c in m})
    for i,code in enumerate(codes):
        for j,date in enumerate(pd.bdate_range("2024-01-01",periods=240)):
            price=1+(i+1)*j*.0001
            rows.append(dict(date=date,code=code,open=price,close=price,high=price*1.01,low=price*.99,volume=1e6))
    return pd.DataFrame(rows).sort_values(["date","code"]).reset_index(drop=True)


class ETFTests(unittest.TestCase):
    def test_unadjusted_requires_explicit_identity_records(self):
        good='var x={"total":1,"data":[{"d":"1900-01-01","f":"1","s":"1","u":"0"}]} /*comment*/'
        self.assertEqual(parse_identity_adjustments(good)["total"],1)
        for bad in ('var x={"total":0,"data":[]}',good.replace('"u":"0"','"u":"0.1"'),good.replace('"s":"1"','"s":"2"')):
            with self.assertRaises(ValueError):parse_identity_adjustments(bad)

    def test_mapping_preserves_sector_exposure_and_cash(self):
        weights=sector_weights_to_etfs({"电子通信":.2,"银行":.1})
        self.assertEqual(weights,{"512760":.1,"515050":.1,"512800":.1})
        converted=stock_targets_to_etfs([{"date":"2025-01-01","weights":{"600001":.2,"600002":.1}}],{"600001":"电子通信","600002":"银行"})
        self.assertEqual(next(iter(converted.values())),weights)
        with self.assertRaises(ValueError): sector_weights_to_etfs({"银行":2})

    def test_future_change_does_not_change_previous_etf_targets(self):
        panel=fixture(); dates=sorted(panel.date.unique()); start=dates[140]
        before,_=build_etf_targets(panel,"sector_rotation",start)
        changed=panel.copy()
        changed.loc[changed.date>dates[181],["open","close","high","low"]]*=.8
        after,_=build_etf_targets(changed,"sector_rotation",start)
        self.assertEqual({d:w for d,w in before.items() if d<=dates[181]}, {d:w for d,w in after.items() if d<=dates[181]})
        self.assertTrue(all(sum(w.values())<=1.00001 for w in before.values()))
        self.assertTrue(all(max(w.values())<=.2 for w in before.values()))

    def test_missing_fund_fails_not_reweighted(self):
        panel=fixture(); panel=panel[panel.code!="512800"]
        with self.assertRaises(ValueError): build_etf_targets(panel,"sector_equal","2024-08-01")

    def test_etf_tax_zero_and_tick_not_stock_tick(self):
        panel=fixture(); panel=panel[panel.code=="512800"].iloc[:3].copy()
        panel[["open","close"]]=1.0
        panel["high"]=1.11;panel["low"]=.89
        dates=list(panel.date)
        config=ExecutionConfig(capital=10000,max_positions=1,max_weight=1,stamp_tax=0,slippage=0,mode="research",price_tick=.001)
        book=run_portfolio(panel,{dates[0]:{"512800":1},dates[1]:{}},config)
        self.assertEqual(book["metrics"]["stamp_tax"],0)
        self.assertAlmostEqual(book["metrics"]["ending_equity"],9990)
        panel.loc[panel.index[1],"open"]=1.096
        book=run_portfolio(panel,{dates[0]:{"512800":1}},config)
        self.assertTrue(book["trades"],"距离涨停0.004不能按股票0.005容差误阻断")
        self.assertEqual(_cent(1.2345,.001),1.235)
        with self.assertRaises(ValueError): replace(config,price_tick=0).validate()

    def test_bad_ohlc_rejected(self):
        panel=fixture();panel.loc[0,"high"]=0.1
        with self.assertRaises(ValueError):validate_etf_panel(panel)


if __name__=="__main__":unittest.main()
