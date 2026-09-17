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
    def build_orders(self, actions):
        tree = ast.parse(inspect.getsource(pipeline.fast_scan))
        loop = next(node for node in tree.body[0].body if isinstance(node, ast.For)
                    and ast.unparse(node.iter) == 'enumerate(top_candidates)')
        plan = SimpleNamespace(orders=[], hold_reasons={})
        candidates = [dict(code=str(i), name=str(i), composite=70,
                           llm_action=action, latest_price=10)
                      for i, action in enumerate(actions)]
        scope = dict(plan=plan, top_candidates=candidates, top_k=3,
                     max_weight=0.1, regime='sideways',
                     min_lot_affordable=lambda *a, **kw: True,
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

if __name__ == '__main__':
    unittest.main()
