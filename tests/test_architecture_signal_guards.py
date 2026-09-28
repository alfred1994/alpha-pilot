#!/usr/bin/env python3
"""架构审计 C 域的离线回归：无网络、无真实账号和运行时文件。"""
import os
import sys
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals import SignalResult, Direction
from signals import sentiment
from strategy import decision, shadow_traders, shadow_eval
from strategy.stock_picker import Candidate, _merge_candidates
from strategy.memory import TradeMemory
from portfolio import fast_backtest as fb
from data.database import Database


class SignalGuards(unittest.TestCase):
    def test_sentiment_malformed_payload_is_unavailable(self):
        for raw in ('broken', '[]', '{}', '{"score":NaN,"direction":"买入"}',
                    '{"score":80,"direction":"unknown"}'):
            with self.subTest(raw=raw), patch.object(sentiment, '_call_llm', return_value=raw):
                result = sentiment.sentiment_signal('600000', [], [{'title': '测试新闻'}])
                self.assertEqual(result.confidence, 0)
                self.assertEqual(result.direction, Direction.HOLD)
        with patch.object(sentiment, '_call_llm', return_value='{"score":50,"direction":"持有"}'):
            self.assertEqual(sentiment.sentiment_signal('600000', [], [{'title': '测试'}]).confidence, .7)

    def test_missing_technical_signals_do_not_dilute_available_score(self):
        signals = [SignalResult('available', 80, Direction.BUY, .8),
                   SignalResult('missing', 50, Direction.HOLD, 0, '数据不足')]
        with patch('signals.technical.all_technical_signals', return_value=signals):
            dim = decision._technical_dimension(pd.DataFrame({'close': range(40)}))
        self.assertEqual((dim.score, dim.confidence), (80, .8))
        self.assertIn('不可用', dim.detail)

    def test_cached_capital_keeps_tuple_contract(self):
        with patch.object(decision, '_get_adaptive_params', return_value={}), \
             patch.object(decision, '_compute_capital_score', return_value=(81, .73, '真实资金证据')), \
             patch.object(decision, '_compute_fundamental_score', return_value=50), \
             patch.object(decision, '_compute_ml_dimension', return_value=decision.DimensionScore('ml', 50, 0)):
            result = decision.make_decision_with_cache('600000')
        self.assertEqual(result.dimensions['capital'].score, 81)
        self.assertEqual(result.dimensions['capital'].confidence, .73)

    def test_nested_candidate_merge_counts_reward_once_and_preserves_inputs(self):
        a = Candidate('600000', '测试', ['a'], 20)
        b = Candidate('600000', '测试', ['b'], 20)
        c = Candidate('600000', '测试', ['c'], 20)
        inner = _merge_candidates({'600000': a}, {'600000': b}, {})[0]
        outer = _merge_candidates({'600000': inner}, {'600000': c}, {})[0]
        self.assertEqual(outer.score, 80)
        self.assertEqual((a.score, a.source, inner.score), (20, ['a'], 50))
        self.assertEqual(_merge_candidates({'600000': a}, {'600000': a}, {})[0].score, 20)
        saturated = _merge_candidates({'600000': Candidate('600000', '测试', ['a'], 60)},
                                      {'600000': Candidate('600000', '测试', ['b'], 50)}, {})[0]
        remerged = _merge_candidates({'600000': saturated}, {'600000': c}, {})[0]
        self.assertEqual(remerged.source_base_score, 130)

    def test_shadow_ignores_unavailable_dimensions(self):
        entry = {'dimensions': {'technical': {'score': 80, 'confidence': .8},
                                'ml': {'score': 0, 'confidence': 0}}}
        self.assertEqual(shadow_traders._adjust_score(entry, {'ml_weight_factor': 2}), 80)

    def test_shadow_leaderboard_does_not_write_empty_database(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('PRAGMA query_only=ON')
        self.assertEqual(shadow_eval.evaluate_variants(SimpleNamespace(conn=conn)), [])
        self.assertEqual(conn.execute('SELECT name FROM sqlite_master').fetchall(), [])
        conn.close()

    def test_shadow_outcomes_loaded_once(self):
        with tempfile.TemporaryDirectory() as folder, Database(db_path=os.path.join(folder, 'db.sqlite')) as db:
            shadow_traders.ensure_tables(db)
            with patch.object(shadow_eval, '_load_outcome_map', return_value={}) as load:
                self.assertEqual(len(shadow_eval.evaluate_variants(db)), len(shadow_traders.SHADOW_VARIANT_IDS))
                load.assert_called_once_with(db)

    def test_memory_does_not_pair_sale_before_buy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'db.sqlite')
            with Database(db_path=path) as db:
                decision_id = db.insert_llm_decision({'code': '600000', 'date': '2026-01-05',
                    'action': 'BUY', 'created_at': '2026-01-05T09:50:00'})
                db.insert_trade({'code': '600000', 'action': 'SELL', 'price': 20, 'shares': 100,
                                 'created_at': '2026-01-04T10:00:00'})
                db.insert_trade({'code': '600000', 'action': 'BUY', 'price': 10, 'shares': 100,
                                 'created_at': '2026-01-05T10:00:00'})
            with TradeMemory(db_path=path) as memory:
                memory.update_pending_decisions()
                self.assertIsNone(memory._get_db().conn.execute('SELECT outcome FROM llm_decisions WHERE id=?', (decision_id,)).fetchone()[0])
                memory._get_db().insert_trade({'code': '600000', 'action': 'SELL', 'price': 11, 'shares': 100,
                                               'created_at': '2026-01-06T10:00:00'})
                memory.update_pending_decisions()
                self.assertEqual(memory._get_db().conn.execute('SELECT outcome FROM llm_decisions WHERE id=?', (decision_id,)).fetchone()[0], 'win')

    def test_small_sample_memory_remains_tentative_and_expires(self):
        with tempfile.TemporaryDirectory() as folder, TradeMemory(db_path=os.path.join(folder, 'db.sqlite')) as memory:
            db = memory._get_db()
            for day in (2, 3, 4):
                db.insert_llm_decision({'code': '600000', 'date': f'2026-01-0{day}', 'action': 'BUY',
                    'outcome': 'win', 'outcome_pct': 1, 'created_at': f'2026-01-0{day}T10:00:00'})
            medium, long = memory._consolidate_decision_patterns('2026-01-01', '2026-01-05')
            self.assertEqual((medium, long), (1, 0))
            row = db.conn.execute("SELECT score, expires_at, content FROM memory_items WHERE source='decision_pattern' AND active=1").fetchone()
            self.assertLessEqual(row['score'], 50)
            self.assertTrue(row['expires_at'])
            self.assertIn('待验证', row['content'])

    def test_walk_forward_each_fold_only_trains_on_its_past(self):
        folds = fb.build_walk_forward_folds(pd.bdate_range('2024-01-01', periods=430), 250, 60)
        for train_start, train_end, valid_start, valid_end in folds:
            self.assertLess(train_end, valid_start)
        self.assertLessEqual(folds[1][0], folds[0][2])  # 前折验证已成为过去，可进入后折训练。

    def test_drawdown_sort_uses_magnitude_with_either_sign(self):
        for sign in (-1, 1):
            mild = SimpleNamespace(max_drawdown=sign * .05, total_return=.1)
            severe = SimpleNamespace(max_drawdown=sign * .4, total_return=.2)
            self.assertGreater(fb._selection_key(mild, 'max_drawdown'), fb._selection_key(severe, 'max_drawdown'))

    def test_latest_ml_prediction_row_is_absent_from_refit(self):
        import strategy.qlib_signal as qlib
        frame = pd.DataFrame({'date': pd.bdate_range('2024-01-01', periods=200).strftime('%Y-%m-%d'),
                              'close': 10 + np.sin(np.arange(200))})
        features = pd.DataFrame({name: np.arange(200, dtype=float) for name in qlib.FEATURE_COLS})
        class Model:
            def __init__(self, **kwargs): self.fits = []
            def fit(self, X, y): self.fits.append(X.copy())
            def predict(self, X): return (X[:, 0] % 2).astype(int)
            def predict_proba(self, X):
                self.latest = X.copy()
                return np.array([[.4, .6]])
        qlib._model_cache.clear()
        with patch.dict(sys.modules, {'lightgbm': SimpleNamespace(LGBMClassifier=Model)}), \
             patch.object(qlib, 'build_features', return_value=features), \
             patch('data.history.get_daily', return_value=frame):
            predictor = qlib.QlibPredictor(train_days=60)
            predictor.predict('600000', date=frame.date.iloc[-1])
        self.assertEqual(predictor._model.latest[0, 0], 199)
        self.assertNotIn(199, predictor._model.fits[-1][:, 0])
        qlib._model_cache.clear()

    def test_sell_prompt_carries_live_price_and_unrealized_pnl(self):
        """卖出提示词必须带现价与浮动盈亏。

        曾经只标注成本价，LLM 于是反复以"无当前价/无法确认止损位"为由默认
        HOLD——2026-09-28 的 20 次决策里多次出现，止盈止损判断形同盲判。
        """
        from strategy.llm_trader import _build_decision_prompt
        from strategy.decision import DimensionScore

        dims = {name: DimensionScore(name=name, score=50.0, confidence=0.8)
                for name in ("technical", "sentiment", "capital", "ml", "fundamental", "emotion")}
        positions = {"600519": {"code": "600519", "name": "测试", "shares": 1000,
                                "buy_price": 10.0, "current_price": 9.2,
                                "unrealized_pnl_pct": -0.08,
                                "buy_date": "2026-09-20"}}
        prompt = _build_decision_prompt("600519", "测试", dims, regime="sideways",
                                        current_positions=positions,
                                        total_assets=100000.0, cash=50000.0)
        self.assertIn("现价: 9.20", prompt, "卖出分析必须给出现价")
        self.assertIn("浮动盈亏: -8.00%", prompt, "卖出分析必须给出浮动盈亏")
        self.assertIn("现价9.20", prompt, "持仓清单必须给出现价")

        # 没有现价时不得伪造数字，只保留成本并如实说明。
        bare = {"600519": {"code": "600519", "name": "测试", "shares": 1000,
                           "buy_price": 10.0, "buy_date": "2026-09-20"}}
        bare_prompt = _build_decision_prompt("600519", "测试", dims, regime="sideways",
                                             current_positions=bare,
                                             total_assets=100000.0, cash=50000.0)
        self.assertIn("成本: 10.00", bare_prompt)
        self.assertNotIn("浮动盈亏:", bare_prompt.split("持仓卖出分析")[-1],
                         "无现价时不得编造浮动盈亏")

    def test_strategy_backtest_uses_prior_signal_and_costs(self):
        import strategy.backtest as backtest
        frame = pd.DataFrame({'date': pd.bdate_range('2024-01-01', periods=35).strftime('%Y-%m-%d'),
                              'close': 10., 'open': 10.})
        observed = []
        def signal(history, code, name):
            observed.append(history.date.iloc[-1])
            return SimpleNamespace(score=80, reasons=['测试'])
        with patch.dict(sys.modules, {'baostock': SimpleNamespace(login=lambda: None, logout=lambda: None)}), \
             patch.object(backtest, 'fetch_stock_data', return_value=frame):
            result = backtest.backtest_strategy({'600000': '测试'}, signal, 'test',
                start_date=frame.date.iloc[31], end_date=frame.date.iloc[-1], hold_days=1)
        self.assertTrue(result.trades)
        self.assertLess(observed[0], result.trades[0].entry_date)
        self.assertTrue(all(t.pnl_pct < 0 and t.exit_date > t.entry_date for t in result.trades))
        self.assertIn('平均每笔净收益', result.summary())


if __name__ == '__main__':
    unittest.main()
