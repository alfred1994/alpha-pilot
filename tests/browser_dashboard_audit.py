#!/usr/bin/env python3
"""本地模拟 API 的看板交互回归；不访问真实交易服务。"""
import json
import os
import sys
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1] / 'web' / 'static'

STATUS = {
    'health': {'ok': True}, 'watchdog': {'ok': True},
    'fetched_at': '2026-09-28T10:00:00+08:00', 'snapshot_at': '2026-09-28T10:00:00+08:00',
    'data_as_of': None, 'capabilities': [], 'risk_warnings': [],
    'recent_logs_total': 12,
    'recent_logs': [{'time': '2026-09-28T09:30', 'status': 'failed', 'action': '自动循环',
                     'error': '超时 <script>window.auditInjected=1</script>'}],
    'account': {'available': True, 'total_assets': 1000000, 'initial_capital': 1000000,
                'cash': 800000, 'total_pnl': 0, 'total_pnl_pct': 0},
    'daily_trader': {'date': '2026-09-28', 'headline': '今日观察', 'state': 'observing',
                     'funnel': {'candidates': 10, 'scored': 7, 'llm_evaluated': 4,
                                'observations': 2, 'buy_signals': None, 'sell_signals': 0,
                                'planned_orders': None, 'filled': 0}},
}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if not path.startswith('/api/'):
            return super().do_GET()
        payload = {
            '/api/public/status': STATUS,
            '/api/positions': {'success': True, 'positions': [{
                'code': '600000', 'name': '测试股票', 'shares': 100,
                'current_price': 10, 'price_fresh': False, 'price_source': 'stored', 'price_as_of': None,
                'market_value': 1000, 'pnl': 0, 'pnl_pct': 0,
            }]},
            '/api/performance': {'success': True, 'performance': [
                {'date': '2026-09-25', 'total_assets': 1000000, 'daily_pnl': 0, 'benchmark_pnl_pct': 0},
                {'date': '2026-09-28', 'total_assets': 1000000, 'daily_pnl': 0, 'benchmark_pnl_pct': 0},
            ]},
            '/api/trades': {'success': True, 'total': 1, 'trades': [
                {'date': '2026-09-28', 'code': '600000', 'name': '测试股票',
                 'action': '<img src=x onerror=window.auditInjected=1>', 'price': 10, 'shares': 100}
            ]},
            '/api/decisions': {'success': True, 'page': 1, 'total': 1, 'has_more': False,
                               'decisions': [{'id': 1, 'code': '600000', 'name': '测试股票',
                                              'action': 'HOLD', 'reasoning': '第一行\n第二行',
                                              'confidence': .7, 'date': '2026-09-28'}]},
            '/api/lessons': {'success': True, 'lessons': []},
            '/api/shadow/leaderboard': {'success': True, 'leaderboard': [], 'promotion_candidates': []},
            '/api/research/market': {'success': True, 'market': {'metrics': []}},
            '/api/research/candidates': {'success': True, 'summary': {}, 'groups': [], 'candidates': [],
                                         'page': 1, 'total': 0, 'has_more': False},
            '/api/research/returns': {'success': True, 'available': False},
        }.get(path, {'success': True})
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FrontendAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def test_dashboard_facts_and_layout(self):
        for width in (1440, 390, 320):
            page = self.browser.new_page(viewport={'width': width, 'height': 1000})
            page.goto(self.base + '/index.html')
            page.locator('#journey-values li').first.wait_for()
            self.assertIn('未知', page.locator('#journey-values').inner_text())
            tooltip = page.evaluate("""() => {
                const option = window.app.tabs.dashboard.funnelChart.getOption();
                const idx = option.series[0].data.findIndex(row => row.name === '模拟成交');
                return option.tooltip[0].formatter({name:'模拟成交',dataIndex:idx});
            }""")
            self.assertIn('模拟成交：0 次', tooltip)
            self.assertIn('存储价格 · 非实时', page.locator('#positions-list').inner_text())
            self.assertEqual(page.evaluate('window.auditInjected'), None)
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width + 2)
            self.assertTrue(page.locator('.topbar-status').is_visible())
            if width == 1440:
                self.assertLess(page.locator('.account-summary').bounding_box()['y'], 600)
                option = page.evaluate('window.app.tabs.dashboard.chart.getOption()')
                self.assertEqual(len(option['series']), 2)  # 全零但有效的基准仍可见
            page.close()

    def test_funnel_drilldown_and_keyboard_dialog(self):
        page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.goto(self.base + '/index.html')
        page.locator('#journey-rejected-candidates').click()
        self.assertEqual(page.locator('#research-layer').input_value(), 'score_gate')
        self.assertEqual(page.locator('#research-start').input_value(), '2026-09-28')
        self.assertEqual(page.locator('#research-end').input_value(), '2026-09-28')
        self.assertIn('#research', page.url)
        page.locator('[data-tab="decisions"]').click()
        button = page.locator('[data-detail-id="1"]')
        button.click()
        self.assertEqual(page.locator('#decision-modal').get_attribute('role'), 'dialog')
        self.assertEqual(page.evaluate('document.activeElement.id'), 'modal-close-btn')
        page.keyboard.press('Escape')
        self.assertEqual(page.evaluate('document.activeElement.dataset.detailId'), '1')
        page.close()

    def test_hung_status_request_recovers_after_timeout(self):
        page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.goto(self.base + '/index.html')
        page.locator('#journey-values li').first.wait_for()
        page.wait_for_function('!window.app.refreshInFlight')
        page.evaluate('window.app.timer && clearInterval(window.app.timer)')
        page.evaluate("""() => {
            window.auditFetch = window.fetch;
            window.fetch = (url, options) => String(url).includes('/api/public/status')
                ? new Promise((resolve, reject) => options.signal.addEventListener('abort',
                    () => reject(new DOMException('Timeout', 'AbortError')), {once:true}))
                : window.auditFetch(url, options);
        }""")
        page.evaluate('void window.app.refresh()')
        page.wait_for_function('window.app.refreshInFlight')
        page.wait_for_function('!window.app.refreshInFlight', timeout=15000)
        self.assertIn('接口暂不可达', page.locator('#data-notice').inner_text())
        page.evaluate('window.fetch = window.auditFetch')
        page.evaluate('void window.app.refresh()')
        page.wait_for_function('!window.app.refreshInFlight')
        self.assertNotIn('接口暂不可达', page.locator('#data-notice').inner_text())
        page.close()

    def test_health_error_category_is_escaped_and_total_is_visible(self):
        page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.goto(self.base + '/index.html')
        page.locator('[data-tab="health"]').click()
        event_list = page.locator('#health-event-list')
        self.assertIn('显示最近 1 条，共 12 条', event_list.inner_text())
        self.assertIn('异常类别：超时 <script>', event_list.inner_text())
        self.assertEqual(event_list.locator('script').count(), 0)
        self.assertIsNone(page.evaluate('window.auditInjected'))
        page.close()


if __name__ == '__main__':
    unittest.main()
