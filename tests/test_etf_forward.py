import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock

import pandas as pd

sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scheduler.etf_forward import initialize,advance,read_status,SHANGHAI,check_previous_close
from strategy.etf_research import BROAD_ETFS,SECTOR_ETFS


def fixture():
    rows=[]
    codes=sorted(set(BROAD_ETFS)|{c for m in SECTOR_ETFS.values() for c in m})
    for i,code in enumerate(codes):
        for j,date in enumerate(pd.bdate_range("2026-01-01",periods=180)):
            price=1+(i+1)*j*.0001
            rows.append(dict(date=date,code=code,open=price,close=price,high=price*1.01,low=price*.99,volume=1e6))
    return pd.DataFrame(rows).sort_values(["date","code"]).reset_index(drop=True)


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"forward.db"
        self.panel=fixture(); self.dates=sorted(self.panel.date.unique())
        self.start=self.dates[140]
        self.actions={"check":"identity"}

    def tearDown(self):self.temp.cleanup()

    def data(self,i):
        date=self.dates[i]
        return self.panel[self.panel.date<=date].copy(),self.panel[self.panel.date==date].copy()

    def clock(self,i,hour=16):return pd.Timestamp(self.dates[i]).to_pydatetime().replace(hour=hour,tzinfo=SHANGHAI)

    def init(self):
        panel,raw=self.data(140)
        return initialize(self.path,panel,raw,self.actions,self.clock(140))

    def test_initialize_no_history_profit_no_fills(self):
        result=self.init()
        self.assertEqual(result["trade_count"],0)
        self.assertEqual(result["observed_days"],0)
        self.assertEqual(len(result["signals"]),5)
        with self.assertRaises(FileExistsError):self.init()

    def test_new_day_and_idempotency(self):
        self.init(); panel,raw=self.data(141)
        result=advance(self.path,panel,raw,self.actions,self.clock(141))
        self.assertEqual(result["observed_days"],1)
        self.assertGreater(result["accounts"]["sector_equal"]["metrics"]["trade_count"],0)
        self.assertEqual(advance(self.path,panel,raw,self.actions,self.clock(141))["status"],"unchanged")
        self.assertEqual(read_status(self.path)["latest"],result)

    def test_late_signal_cannot_get_historical_open(self):
        panel,raw=self.data(140)
        initialize(self.path,panel,raw,self.actions,self.clock(141,10))
        panel,raw=self.data(141)
        with self.assertRaisesRegex(ValueError,"开盘后"):
            advance(self.path,panel,raw,self.actions,self.clock(141))
        self.assertEqual(read_status(self.path)["latest"]["observed_days"],0)

    def test_gap_actions_and_code_change_block(self):
        self.init(); panel,raw=self.data(142)
        with self.assertRaisesRegex(ValueError,"漏跑"):advance(self.path,panel,raw,self.actions,self.clock(142))
        panel,raw=self.data(141)
        with self.assertRaisesRegex(ValueError,"分红"):advance(self.path,panel,raw,{"new":"dividend"},self.clock(141))
        with mock.patch("scheduler.etf_forward.strategy_hash",return_value="changed"):
            with self.assertRaisesRegex(ValueError,"指纹"):advance(self.path,panel,raw,self.actions,self.clock(141))

    def test_incomplete_day_and_revised_prices_fail(self):
        self.init(); panel,raw=self.data(141)
        with self.assertRaises(ValueError):advance(self.path,panel,raw,self.actions,self.clock(141,14))
        panel.loc[panel.date==self.start,"close"]*=1.0001
        with self.assertRaisesRegex(ValueError,"修订"):advance(self.path,panel,raw,self.actions,self.clock(141))

    def test_twenty_days_rebalance_and_previous_equity_immutable(self):
        self.init()
        for i in range(141,162):
            panel,raw=self.data(i)
            result=advance(self.path,panel,raw,self.actions,self.clock(i))
            self.assertEqual(result["observed_days"],i-140)
            self.assertEqual(bool(result["new_signals"]),i==160)
            for book in result["accounts"].values():
                self.assertGreaterEqual(book["curve"][-1]["cash"],0)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM signals").fetchone()[0],8)

    def test_terminal_day_does_not_create_new_signals_or_extend_account(self):
        self.init()
        # 缩短仅测试临时库的观察周期，避免做60次相同重放来重复验证。
        with closing(sqlite3.connect(self.path)) as connection:
            payload=json.loads(connection.execute("SELECT payload FROM manifest").fetchone()[0])
            payload["evaluation_days"]=2
            connection.execute("UPDATE manifest SET payload=?",(json.dumps(payload),))
            connection.commit()
        for i in (141,142):
            panel,raw=self.data(i)
            result=advance(self.path,panel,raw,self.actions,self.clock(i))
        self.assertEqual(result["status"],"completed")
        self.assertEqual(result["new_signals"],{})
        panel,raw=self.data(143)
        self.assertEqual(advance(self.path,panel,raw,self.actions,self.clock(143))["status"],"completed")
        self.assertEqual(read_status(self.path)["latest"]["observed_days"],2)

    def test_precision_restore_requires_exact_stored_raw_and_rounding(self):
        date = pd.Timestamp("2026-10-12")
        def frame(value, stamp=date):
            return pd.DataFrame([{"date":stamp,"code":"512010","close":value}])
        for old, new in ((.38,.379),(.86,.863),(7.13,7.125)):
            audit=check_previous_close(frame(old),frame(new),frame(new),date)
            self.assertEqual(len(audit),1)
            self.assertEqual(audit[0]["saved_raw_close"],new)
            self.assertTrue(audit[0]["historical_input_preserved"])
        # 不能把任意小额变化、复权比例差、非ETF刻度、反向降精度视作安全。
        for old,new,raw in ((.38,.379,.378),(.38,.374,.374),(.379,.38,.38),
                            (.38,.3795,.3795),(.379,.378,.378),(7.12,7.125,7.125)):
            with self.subTest(old=old,new=new,raw=raw):
                with self.assertRaisesRegex(ValueError,"修订"):
                    check_previous_close(frame(old),frame(new),frame(raw),date)
        with self.assertRaisesRegex(ValueError,"快照"):
            check_previous_close(frame(.38),frame(.379),frame(.379,date-pd.Timedelta(days=1)),date)
        with self.assertRaises(ValueError):
            check_previous_close(frame(.38),frame(.379),pd.concat([frame(.379)]*2),date)

    def test_precision_audit_preserves_original_rows_signals_and_accounts(self):
        date=self.dates[140]
        code="512010"
        initial,raw=self.data(140)
        # 只有特征日线为两位，原始快照为三位。
        initial.loc[(initial.date==date)&(initial.code==code),["open","close","high","low"]]=[.38,.38,.39,.37]
        raw.loc[raw.code==code,["open","close","high","low"]]=[.379,.379,.39,.37]
        initialize(self.path,initial,raw,self.actions,self.clock(140))
        with closing(sqlite3.connect(self.path)) as connection:
            old_rows=connection.execute("SELECT * FROM observations ORDER BY date").fetchall()
            signals=connection.execute("SELECT * FROM signals ORDER BY date,mode").fetchall()
        panel,new_raw=self.data(141)
        panel.loc[(panel.date==date)&(panel.code==code),["open","close","high","low"]]=[.379,.379,.39,.37]
        result=advance(self.path,panel,new_raw,self.actions,self.clock(141))
        self.assertEqual(result["observed_days"],1)
        self.assertEqual([r["code"] for r in result["data_precision_audit"]],[code])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM observations ORDER BY date LIMIT 1").fetchall(),old_rows)
            self.assertEqual(connection.execute("SELECT * FROM signals ORDER BY date,mode").fetchall(),signals)
            stored=json.loads(connection.execute("SELECT panel FROM observations ORDER BY date DESC LIMIT 1").fetchone()[0])
            self.assertEqual(next(r["close"] for r in stored if r["date"]==str(pd.Timestamp(date).date()) and r["code"]==code),.38)
        self.assertEqual(advance(self.path,panel,new_raw,self.actions,self.clock(141))["status"],"unchanged")
        panel,new_raw=self.data(142)
        next_result=advance(self.path,panel,new_raw,self.actions,self.clock(142))
        for mode,book in next_result["accounts"].items():
            self.assertEqual(book["curve"][:-1],result["accounts"][mode]["curve"])

    def test_unsupported_revision_rolls_back_without_audit_or_new_observation(self):
        self.init()
        panel,raw=self.data(141)
        panel.loc[(panel.date==self.start)&(panel.code=="512010"),"close"]*=1.00001
        with closing(sqlite3.connect(self.path)) as connection:
            before=connection.execute("SELECT * FROM observations").fetchall()
        with self.assertRaisesRegex(ValueError,"修订"):
            advance(self.path,panel,raw,self.actions,self.clock(141))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM observations").fetchall(),before)


if __name__=="__main__":unittest.main()
