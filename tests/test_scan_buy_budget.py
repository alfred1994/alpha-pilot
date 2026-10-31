"""Exercise the actual fast_scan order-building block without network or trading."""
import ast
import copy
import inspect
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scheduler import pipeline


class BuyBudgetTest(unittest.TestCase):
    def build_orders(self, actions, *, valid_policy=True, quote_available=True):
        tree = ast.parse(inspect.getsource(pipeline.fast_scan))
        loop = next(node for node in tree.body[0].body if isinstance(node, ast.For)
                    and ast.unparse(node.iter) == 'enumerate(top_candidates)')
        plan = SimpleNamespace(orders=[], hold_reasons={})
        candidates = [dict(code=str(i), name=str(i), composite=70,
                           llm_action=action, latest_price=10, signal_price_available=quote_available)
                      for i, action in enumerate(actions)]
        scope = dict(plan=plan, top_candidates=candidates, top_k=3,
                     max_weight=0.1, regime='sideways', directive={"valid_for_date": valid_policy},
                     min_lot_affordable=lambda *a, **kw: True,
                     target_buy_weight=pipeline.target_buy_weight,
                     TradeOrder=pipeline.TradeOrder, logger=pipeline.logger)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(loop)], type_ignores=[])),
                     '<fast_scan order block>', 'exec'), scope)
        return plan

    def test_six_buy_judgments_only_three_orders(self):
        plan = self.build_orders(['BUY'] * 6)
        self.assertEqual(len(plan.orders), 3)
        self.assertEqual(len(plan.hold_reasons), 3)

    def test_sell_not_blocked_by_buy_budget(self):
        plan = self.build_orders(['BUY'] * 4 + ['SELL'])
        self.assertEqual([o.action for o in plan.orders], ['BUY'] * 3 + ['SELL'])

    def test_hold_does_not_consume_buy_budget(self):
        self.assertEqual(len(self.build_orders(['HOLD'] * 3 + ['BUY'] * 3).orders), 3)

    def test_stale_policy_blocks_only_new_buys(self):
        plan = self.build_orders(['BUY', 'SELL'], valid_policy=False)
        self.assertEqual([o.action for o in plan.orders], ['SELL'])
        self.assertIn('HOLD_STALE_DIRECTIVE', plan.hold_reasons['0'])

    def test_missing_signal_quote_blocks_new_buy(self):
        plan = self.build_orders(['BUY'], quote_available=False)
        self.assertFalse(plan.orders)
        self.assertIn('HOLD_SIGNAL_QUOTE', plan.hold_reasons['0'])

if __name__ == '__main__':
    unittest.main()
