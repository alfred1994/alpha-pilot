"""风格池候选来源 + LLM 否决层（CANDIDATE_SOURCE=style_pools）回归。

覆盖：风格池读取与过期保护、准入排序、涨停/仓位/一手预检、否决解析（失败一律否决）、
当日复用、订单生成、总仓位上限、持有期满退出，以及 fast_scan 端到端不走旧六维打分。
"""
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scheduler import pipeline
from strategy import llm_veto
from strategy import style_allocator
from strategy import style_candidates as sc
from strategy import style_shadow as ss
from strategy.counterfactual import _denial_layer


def make_style_db(path, pool_date="2026-11-04"):
    with closing(ss.connect(path)) as conn:
        rows = [
            (pool_date, "oversold_rebound", "600001", 1, 0.9, "{}"),
            (pool_date, "low_vol_defensive", "600001", 3, 0.8, "{}"),
            (pool_date, "low_vol_defensive", "600002", 1, 0.7, "{}"),
            (pool_date, "factor_top", "600003", 1, 99.0, "{}"),
            (pool_date, "trend_strength", "600004", 1, 0.9, "{}"),
            (pool_date, "oversold_rebound", "600005", 2, 0.6, "{}"),
        ]
        conn.executemany("INSERT INTO style_candidates VALUES (?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO factor_scores VALUES (?,?,?)", [
            (pool_date, "600001", 80.0), (pool_date, "600002", 40.0), (pool_date, "600003", 95.0),
            (pool_date, "600004", 99.0), (pool_date, "600005", 70.0)])
        conn.executemany("INSERT INTO valuations VALUES (?,?,?,?,?,?,?)", [
            (pool_date, "600001", "甲股份", 10, 1, 1, 1), (pool_date, "600002", "乙股份", 10, 1, 1, 1),
            (pool_date, "600003", "丙股份", 10, 1, 1, 1), (pool_date, "600005", "*ST丁", 10, 1, 1, 1)])
        conn.execute("INSERT INTO market_exposure VALUES (?,?,?,?,?,?)", (pool_date, 0.0, 0, 0, 4000, "test"))
        conn.execute("INSERT INTO runs VALUES (?,?,?)", (pool_date, "2026-11-04T18:30:00", "{}"))
        conn.commit()


class FakeAccount:
    def __init__(self, cash=100000.0, total=100000.0, positions=None):
        self.cash, self._total, self.positions = cash, total, positions or {}

    def total_assets(self, prices=None):
        return self._total


def scored_item(code, factor, price=10.0, prev=10.0, quote=True, styles=("oversold_rebound",)):
    return {"code": code, "name": f"股票{code}", "composite": factor, "factor_score": factor,
            "styles": list(styles), "dimensions": {"factor": {"score": factor, "confidence": 1.0, "detail": ""}},
            "latest_price": price, "signal_prev_close": prev, "signal_price_available": quote,
            "signal_price_at": "2026-11-05T09:35:00"}


def no_refresh(scores, realtime_func=None, now=None):
    return None


def plan_stub():
    return pipeline.TradePlan(date="2026-11-05", scan_id="scan-1")


class StyleCandidatesTest(unittest.TestCase):
    def test_load_merges_styles_filters_st_and_excludes_trend(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "style.db")
            make_style_db(path)
            items, meta = sc.load_style_candidates("2026-11-05", db_path=path, expected_date="2026-11-04",
                                                   styles=("oversold_rebound", "low_vol_defensive", "factor_top"))
        codes = [item["code"] for item in items]
        self.assertEqual(codes, ["600003", "600001", "600002"])  # 按因子分降序；ST 与趋势池剔除
        self.assertEqual(sorted(items[1]["styles"]), ["low_vol_defensive", "oversold_rebound"])
        self.assertTrue(meta["fresh"])
        self.assertEqual(meta["pool_date"], "2026-11-04")
        self.assertEqual(meta["market_exposure_cap"], 0.0)

    def test_stale_pool_returns_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "style.db")
            make_style_db(path, pool_date="2026-11-02")
            items, meta = sc.load_style_candidates("2026-11-05", db_path=path, expected_date="2026-11-04")
        self.assertEqual(items, [])
        self.assertFalse(meta["fresh"])
        self.assertIn("stale_style_pool", meta["refresh_error"])

    def test_missing_db_returns_nothing(self):
        items, meta = sc.load_style_candidates("2026-11-05", db_path=os.path.join(tempfile.gettempdir(), "nope.db"),
                                               expected_date="2026-11-04")
        self.assertEqual(items, [])
        self.assertEqual(meta["refresh_error"], "style_shadow_db_missing")

    def test_select_orders_by_factor_and_applies_gate(self):
        scored = [scored_item("a", 55), scored_item("b", 90), scored_item("c", 40), scored_item("d", 70)]
        selected, reasons = sc.select_style_candidates(scored, min_factor_score=50, max_reviews=2)
        self.assertEqual([s["code"] for s in selected], ["b", "d"])
        self.assertTrue(reasons["c"].startswith("HOLD_FACTOR_LOW"))
        self.assertTrue(reasons["a"].startswith("HOLD_NOT_TOP2"))

    def test_quota_round_robin_beats_global_factor_order(self):
        # factor_top 的因子分天然最高；全局排序会让其他风格永远轮不到。
        scored = [scored_item("t1", 100, styles=("factor_top",)), scored_item("t2", 99.8, styles=("factor_top",)),
                  scored_item("t3", 99.5, styles=("factor_top",)),
                  scored_item("o1", 80, styles=("oversold_rebound",)), scored_item("o2", 85, styles=("oversold_rebound",)),
                  scored_item("l1", 40, styles=("low_vol_defensive",))]
        for item, rank in zip(scored, (1, 2, 3, 1, 2, 1)):
            item["pool_ranks"] = {item["styles"][0]: rank}
        selected, reasons = sc.select_style_candidates(scored, min_factor_score=0, max_reviews=4)
        self.assertEqual([s["code"] for s in selected], ["t1", "l1", "o1", "t2"])  # 风格内按入池排名，不按因子分
        self.assertEqual(selected[1]["selected_style"], "low_vol_defensive")
        self.assertTrue(reasons["o2"].startswith("HOLD_NOT_TOP4"))
        selected, _ = sc.select_style_candidates(
            scored, min_factor_score=0, max_reviews=4, quotas={"factor_top": 2, "oversold_rebound": 1})
        self.assertEqual([s["code"] for s in selected], ["t1", "o1", "t2", "t3"])  # 名额 0 的风格不参与

    def test_quota_redistributes_when_style_runs_out_and_dedups_codes(self):
        both = scored_item("x", 90, styles=("factor_top", "oversold_rebound"))
        both["pool_ranks"] = {"factor_top": 1, "oversold_rebound": 1}
        other = scored_item("o2", 70, styles=("oversold_rebound",))
        other["pool_ranks"] = {"oversold_rebound": 2}
        selected, _ = sc.select_style_candidates([both, other], min_factor_score=0, max_reviews=5,
                                                 quotas={"factor_top": 3, "oversold_rebound": 1})
        self.assertEqual([s["code"] for s in selected], ["x", "o2"])

    def test_limit_up_detection_by_board(self):
        self.assertTrue(sc.at_limit_up("600001", 11.0, 10.0))
        self.assertFalse(sc.at_limit_up("600001", 10.95, 10.0))
        self.assertFalse(sc.at_limit_up("300001", 11.0, 10.0))
        self.assertTrue(sc.at_limit_up("300001", 12.0, 10.0))
        self.assertFalse(sc.at_limit_up("600001", 11.0, None))

    def test_denial_layers_for_new_reasons(self):
        self.assertEqual(_denial_layer("HOLD", "HOLD_FACTOR_LOW(40<50)", ""), "score_gate")
        self.assertEqual(_denial_layer("HOLD", "HOLD_LLM_VETO(suspension: x)", "HOLD"), "llm")
        self.assertEqual(_denial_layer("HOLD", "HOLD_VETO_UNAVAILABLE(LLM无响应)", "HOLD"), "llm")


class VetoParseTest(unittest.TestCase):
    def test_pass(self):
        r = llm_veto.parse_response('{"veto": false, "category": "none", "evidence": "无利空", "confidence": 0.7}')
        self.assertFalse(r["failed"])
        self.assertFalse(r["veto"])

    def test_valid_veto(self):
        r = llm_veto.parse_response('{"veto": true, "category": "suspension", "evidence": "公告明日停牌", "confidence": 0.9}')
        self.assertFalse(r["failed"])
        self.assertTrue(r["veto"])
        self.assertEqual(r["category"], "suspension")

    def test_failures_are_vetoes(self):
        for raw in (None, "", "not json", '{"veto": "no"}',
                    '{"veto": true, "category": "bear_market", "evidence": "熊市", "confidence": 0.8}',
                    '{"veto": true, "category": "suspension", "evidence": "", "confidence": 0.8}',
                    '{"veto": false, "category": "suspension", "evidence": "", "confidence": 0.8}',
                    '{"veto": false, "category": "none", "evidence": ""}'):
            r = llm_veto.parse_response(raw)
            self.assertTrue(r["failed"], raw)
            self.assertTrue(r["veto"], raw)
        self.assertEqual(llm_veto.parse_response(None)["reason"], llm_veto.FAIL_REASON_NO_RESPONSE)

    def test_prompt_contains_facts_and_no_free_text_policy(self):
        prompt = llm_veto.build_prompt(scored_item("600001", 80), news=[{"title": "某公司签订大单", "time": "2026-11-04"}],
                                       quote={"price": 10.2, "prev_close": 10.0, "at": "t", "target_amount": 4000},
                                       fees=llm_veto.default_fees())
        self.assertIn("因子分 80", prompt)
        self.assertIn("涨停价 11.0", prompt)
        self.assertIn("某公司签订大单", prompt)
        self.assertIn("[不可信外部数据开始]", prompt)
        self.assertIn("缺少这些数据不是否决理由", prompt)
        self.assertNotIn("防守观察", prompt)


class VetoReviewTest(unittest.TestCase):
    def setUp(self):
        llm_veto.reset_cache()
        self.saved = []
        self.memory = SimpleNamespace(save_decision=lambda **kw: self.saved.append(kw) or len(self.saved))

    def run_review(self, raw):
        calls = []

        def call(prompt, timeout):
            calls.append(prompt)
            return raw
        result = llm_veto.review(scored_item("600001", 80), date="2026-11-05", quote={"price": 10, "prev_close": 10},
                                 fees=llm_veto.default_fees(), timeout=10, memory=self.memory,
                                 call_model=call, news_fetcher=lambda code: [])
        return result, calls

    def test_pass_is_saved_as_buy_and_reused_same_day(self):
        result, calls = self.run_review('{"veto": false, "category": "none", "evidence": "无", "confidence": 0.6}')
        self.assertFalse(result["veto"])
        self.assertEqual(self.saved[-1]["action"], "BUY")
        again, calls2 = self.run_review(None)
        self.assertTrue(again["reused"])
        self.assertFalse(again["veto"])
        self.assertEqual(calls2, [])

    def test_failure_is_veto_and_not_cached(self):
        result, _ = self.run_review(None)
        self.assertTrue(result["failed"] and result["veto"])
        self.assertEqual(self.saved[-1]["action"], "HOLD")
        self.assertEqual(self.saved[-1]["reasoning"], llm_veto.FAIL_REASON_NO_RESPONSE)
        _, calls = self.run_review('{"veto": false, "category": "none", "evidence": "", "confidence": 0.5}')
        self.assertEqual(len(calls), 1)

    def test_model_exception_is_veto(self):
        def boom(prompt, timeout):
            raise TimeoutError("slow")
        result = llm_veto.review(scored_item("600009", 80), date="2026-11-05", quote={}, fees=llm_veto.default_fees(),
                                 timeout=10, memory=self.memory, call_model=boom, news_fetcher=lambda c: [])
        self.assertTrue(result["failed"] and result["veto"])


class StyleOrdersTest(unittest.TestCase):
    def build(self, scored, *, account=None, reviewer=None, directive=None, allocation=None):
        plan = plan_stub()
        calls = []

        def default_reviewer(candidate, **kw):
            calls.append(candidate["code"])
            return {"veto": False, "failed": False, "category": "none", "evidence": "", "confidence": 0.6,
                    "reasoning": "未发现否决事实", "decision_id": 7}
        with patch.object(pipeline, "_refresh_signal_prices", no_refresh):
            pipeline._style_pool_orders(plan, scored, directive=directive or {"valid_for_date": True},
                                        regime="bear", remaining=lambda: 300, account=account or FakeAccount(),
                                        reviewer=reviewer or default_reviewer,
                                        allocation=allocation or style_allocator.default_allocation(
                                            ("oversold_rebound", "low_vol_defensive", "factor_top"), 12, 10))
        return plan, calls

    def test_passed_candidates_become_fixed_weight_buys(self):
        plan, calls = self.build([scored_item("600001", 80), scored_item("600002", 90), scored_item("600003", 30)])
        self.assertEqual([o.code for o in plan.orders], ["600002", "600001", "600003"])  # 默认不设因子门槛
        self.assertTrue(all(o.action == "BUY" and o.target_weight == 0.05 for o in plan.orders))  # 不随熊市指令缩仓
        self.assertEqual(plan.orders[0].ref_price, 10.0)
        self.assertEqual(plan.orders[0].decision_id, 7)
        self.assertIn("via=oversold_rebound", plan.orders[0].signal_detail)
        self.assertEqual(sorted(calls), ["600001", "600002", "600003"])

    def test_optional_factor_gate(self):
        with patch("config.STYLE_MIN_FACTOR_SCORE", 50):
            plan, calls = self.build([scored_item("600001", 80), scored_item("600003", 30)])
        self.assertEqual([o.code for o in plan.orders], ["600001"])
        self.assertTrue(plan.hold_reasons["600003"].startswith("HOLD_FACTOR_LOW"))
        self.assertEqual(calls, ["600001"])

    def test_veto_failure_and_timeout_block_buys(self):
        def reviewer(candidate, **kw):
            code = candidate["code"]
            if code == "600001":
                return {"veto": True, "failed": False, "category": "suspension", "evidence": "停牌公告",
                        "confidence": 0.9, "reasoning": "否决"}
            if code == "600002":
                return {"veto": True, "failed": True, "category": "unavailable", "reasoning": "LLM无响应"}
            raise TimeoutError("timeout")
        plan, _ = self.build([scored_item("600001", 90), scored_item("600002", 85), scored_item("600003", 80)],
                             reviewer=reviewer)
        self.assertEqual(plan.orders, [])
        self.assertTrue(plan.hold_reasons["600001"].startswith("HOLD_LLM_VETO(suspension"))
        self.assertTrue(plan.hold_reasons["600002"].startswith("HOLD_VETO_UNAVAILABLE"))
        self.assertTrue(plan.hold_reasons["600003"].startswith("HOLD_VETO_UNAVAILABLE"))

    def test_prechecks_skip_llm(self):
        account = FakeAccount(positions={"600001": {"shares": 100}})
        plan, calls = self.build([
            scored_item("600001", 90), scored_item("600002", 85, price=11.0, prev=10.0),
            scored_item("600003", 80, quote=False), scored_item("600004", 75, price=500.0, prev=500.0)],
            account=account)
        self.assertEqual(calls, [])
        self.assertEqual(plan.orders, [])
        self.assertTrue(plan.hold_reasons["600001"].startswith("HOLD_ALREADY_HELD"))
        self.assertTrue(plan.hold_reasons["600002"].startswith("HOLD_LIMIT_UP"))
        self.assertTrue(plan.hold_reasons["600003"].startswith("HOLD_SIGNAL_QUOTE"))
        self.assertTrue(plan.hold_reasons["600004"].startswith("HOLD_MIN_LOT"))

    def test_exposure_cap_blocks_without_llm_calls(self):
        plan, calls = self.build([scored_item("600002", 85)], account=FakeAccount(cash=41000.0))  # 59% 已用
        self.assertEqual(calls, [])
        self.assertTrue(plan.hold_reasons["600002"].startswith("HOLD_EXPOSURE_CAP"))

    def test_buy_budget_respects_exposure_slots_and_daily_cap(self):
        scored = [scored_item(f"60000{i}", 90 - i) for i in range(1, 6)]
        plan, _ = self.build(scored)  # 空仓：每日新开仓上限 3
        self.assertEqual(len(plan.orders), 3)
        self.assertTrue(any(r.startswith("HOLD_BUY_BUDGET") for r in plan.hold_reasons.values()))
        plan, _ = self.build(scored, account=FakeAccount(cash=50000.0))  # 50% 已用 → 还剩 2 个 5% 名额
        self.assertEqual(len(plan.orders), 2)
        today = {f"60009{i}": {"shares": 100, "buy_date": "2026-11-05"} for i in range(2)}
        plan, _ = self.build(scored, account=FakeAccount(cash=90000.0, positions=today))
        self.assertEqual(len(plan.orders), 1)
        today["600099"] = {"shares": 100, "buy_date": "2026-11-05 09:40:00"}
        plan, calls = self.build(scored, account=FakeAccount(cash=85000.0, positions=today))
        self.assertEqual((plan.orders, calls), ([], []))
        self.assertTrue(plan.hold_reasons["600001"].startswith("HOLD_BUY_BUDGET(今日已开3/上限3)"))

    def test_stale_directive_blocks_before_llm(self):
        plan, calls = self.build([scored_item("600001", 80)], directive={"valid_for_date": False})
        self.assertEqual(calls, [])
        self.assertTrue(plan.hold_reasons["600001"].startswith("HOLD_STALE_DIRECTIVE"))


class HeldDaysTest(unittest.TestCase):
    def test_counts_trading_days_after_buy(self):
        days = ["20261030", "20261102", "20261103", "20261104", "20261105"]
        with patch("scheduler.market_calendar.get_trading_days", return_value=days):
            self.assertEqual(pipeline.held_trading_days("2026-10-30", "2026-11-05"), 4)
        self.assertIsNone(pipeline.held_trading_days("", "2026-11-05"))
        self.assertIsNone(pipeline.held_trading_days("2026-11-06", "2026-11-05"))


class FastScanStyleModeTest(unittest.TestCase):
    def run_scan(self, account, *, held_days=None):
        items = [{"code": "600001", "name": "甲股份", "styles": ["oversold_rebound"], "pool_ranks": {},
                  "factor_score": 80.0, "detail": {}}]
        meta = {"source": "style_pools", "fresh": True, "pool_date": "2026-11-04", "styles": ["oversold_rebound"],
                "candidate_count": 1, "refresh_error": ""}
        regime = SimpleNamespace(date=pipeline._now_bj().strftime("%Y-%m-%d"), regime="bear", confidence=0.8)
        directive = {"version": "d1", "intent": "防守观察", "valid_for_date": True,
                     "params": {"top_k": 1, "min_score": 62, "max_weight": 0.02}}

        def refresh(scores, realtime_func=None, now=None):
            for s in scores:
                s.update(latest_price=10.0, signal_prev_close=10.0, signal_price_available=True)

        def reviewer(candidate, **kw):
            return {"veto": False, "failed": False, "category": "none", "evidence": "", "confidence": 0.6,
                    "reasoning": "未发现否决事实", "decision_id": None}
        with ExitStack() as stack:
            stack.enter_context(patch.object(pipeline, "CANDIDATE_SOURCE", "style_pools"))
            stack.enter_context(patch.object(style_allocator, "ALLOCATION_PATH",
                                             os.path.join(tempfile.gettempdir(), "no-such-allocation.json")))
            stack.enter_context(patch("strategy.style_candidates.load_style_candidates", return_value=(items, meta)))
            stack.enter_context(patch("data.snapshot.get_market_snapshot_status",
                                      return_value={"snapshot": None, "source": "unavailable"}))
            stack.enter_context(patch("strategy.market_regime.get_regime_history", return_value=[regime]))
            stack.enter_context(patch("strategy.directive.get_effective_trade_policy", return_value=directive))
            stack.enter_context(patch.object(pipeline, "_refresh_signal_prices", refresh))
            stack.enter_context(patch("execution.paper_account.PaperAccount", return_value=account))
            stack.enter_context(patch("strategy.llm_veto.review", side_effect=reviewer))
            stack.enter_context(patch("strategy.shadow_eval.expire_stale_ab_tests"))
            legacy = stack.enter_context(patch.object(pipeline, "_legacy_score_candidates"))
            if held_days is not None:
                stack.enter_context(patch.object(pipeline, "held_trading_days", return_value=held_days))
            plan = pipeline.fast_scan(budget_seconds=120, persist_plan=False)
        legacy.assert_not_called()
        return plan

    def test_style_mode_generates_buy_without_legacy_scoring(self):
        plan = self.run_scan(FakeAccount())
        self.assertEqual([(o.code, o.action) for o in plan.orders], [("600001", "BUY")])
        self.assertEqual(plan.orders[0].target_weight, 0.05)  # 熊市指令 max_weight=0.02 不缩仓
        self.assertEqual(plan.candidate_pool["source"], "style_pools")
        self.assertEqual(plan.raw_scores[0]["llm_action"], "BUY")
        self.assertFalse(any("可转债" in e for e in plan.errors))

    def test_style_mode_exits_after_hold_days(self):
        account = FakeAccount(cash=96000.0, positions={"600009": {
            "name": "持仓股", "shares": 400, "buy_price": 10.0, "buy_date": "2026-10-01"}})
        plan = self.run_scan(account, held_days=10)  # 默认持有期 10
        sells = [o for o in plan.orders if o.action == "SELL"]
        self.assertEqual([o.code for o in sells], ["600009"])
        self.assertIn("持有期满", sells[0].reason)
        plan = self.run_scan(account, held_days=5)
        self.assertFalse([o for o in plan.orders if o.action == "SELL"])
        self.assertTrue(plan.hold_reasons["600009"].startswith("HOLD_STYLE_HOLDING(5/"))


if __name__ == "__main__":
    unittest.main()
