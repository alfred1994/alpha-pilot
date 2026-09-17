"""Offline HTTP tests for snapshot-based asset/benchmark comparison."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from data.database import Database
from web.routers import returns


class ReturnsDashboardTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'returns.db')
        patcher = patch('data.database.DB_PATH', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(returns.router, prefix='/api')
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def seed(self, assets=(100, 120, 90), prices=(200, 200, 220), capital=(100, 100, 100)):
        with Database(db_path=self.path) as db:
            for i, amount in enumerate(assets):
                day = f'2020-01-0{i+1}'
                db.save_review_snapshot(day, {'total_assets': amount, 'initial_capital': capital[i],
                                             'benchmark_pnl_pct': 999, 'private': 'PRIVATE_SECRET'})
                if prices[i] is not None:
                    # k_daily stores the canonical system code (pure digits);
                    # _save_to_cache normalizes "000300.SH" -> "000300".
                    db.conn.execute('INSERT INTO k_daily(code,date,close) VALUES (?,?,?)',
                                    ('000300', day, prices[i]))
            db.conn.commit()

    def get(self, start='2020-01-01', end='2020-01-03'):
        return self.client.get(f'/api/research/returns?start_date={start}&end_date={end}').json()

    def test_rebased_comparison_drawdown_and_read_only(self):
        self.seed()
        before = Path(self.path).read_bytes()
        result = self.get()
        self.assertTrue(result['success'])
        summary = result['summary']
        self.assertAlmostEqual(summary['asset_return'], -0.1)
        self.assertAlmostEqual(summary['benchmark_return'], 0.1)
        self.assertAlmostEqual(summary['relative_asset_change'], -0.2)
        self.assertAlmostEqual(summary['max_asset_drawdown'], -0.25)
        self.assertEqual(summary['asset_change'], -10)
        self.assertIsNone(result['points'][0]['change_since_previous'])
        self.assertEqual(result['points'][1]['change_since_previous'], 20)
        self.assertFalse(result['cash_flow_adjusted'])
        self.assertNotIn('PRIVATE_SECRET', json.dumps(result))
        self.assertNotIn('benchmark_pnl_pct', result)
        self.assertTrue(all(row['benchmark_return'] is None or abs(row['benchmark_return']) < 1
                            for row in result['points']))
        shorter = self.get('2020-01-02')
        self.assertAlmostEqual(shorter['summary']['asset_return'], -0.25)
        self.assertEqual(shorter['points'][0]['asset_return'], 0)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_zero_benchmark_return_is_not_missing(self):
        self.seed(prices=(200, None, 200))
        result = self.get()
        self.assertEqual(result['summary']['benchmark_return'], 0)
        self.assertIsNone(result['points'][1]['benchmark_return'])
        self.assertEqual(result['benchmark_points'], 2)

    def test_missing_endpoint_benchmark_not_carried_forward(self):
        self.seed(prices=(200, 210, None))
        result = self.get()
        self.assertIsNone(result['summary']['benchmark_return'])
        self.assertIsNone(result['summary']['relative_asset_change'])

    def test_benchmark_uses_system_code_not_suffixed(self):
        # k_daily stores the canonical system code "000300"; a legacy
        # "000300.SH" row must be ignored, not blended into the benchmark.
        self.seed(assets=(100, 120, 90), prices=(200, 200, 220))  # canonical -> +10%
        with Database(db_path=self.path) as db:
            for day, close in (('2020-01-01', 500), ('2020-01-03', 500)):  # suffixed -> 0%
                db.conn.execute('INSERT INTO k_daily(code,date,close) VALUES (?,?,?)',
                                ('000300.SH', day, close))
            db.conn.commit()
        result = self.get()
        # +10% proves the canonical code was used; 0% would prove the suffixed one.
        self.assertAlmostEqual(result['summary']['benchmark_return'], 0.1)

    def test_invalid_middle_snapshot_breaks_daily_change_and_drawdown(self):
        self.seed(assets=(100, None, 90))
        result = self.get()
        self.assertEqual(result['invalid_snapshots'], 1)
        self.assertIsNone(result['points'][2]['change_since_previous'])
        self.assertIsNone(result['summary']['max_asset_drawdown'])

    def test_zero_assets_and_single_snapshot(self):
        self.seed(assets=(100, 120, 0))
        result = self.get()
        self.assertEqual(result['summary']['asset_return'], -1)
        self.assertEqual(result['summary']['max_asset_drawdown'], -1)
        single = self.get('2020-01-02', '2020-01-02')
        self.assertIsNone(single['summary']['asset_return'])
        self.assertIsNone(single['summary']['benchmark_return'])

    def test_capital_reset_suppresses_comparison(self):
        self.seed(capital=(100, 100, 200))
        result = self.get()
        self.assertTrue(result['reset_suspected'])
        self.assertIsNone(result['summary']['asset_change'])
        self.assertIsNone(result['summary']['asset_return'])
        self.assertIsNone(result['summary']['relative_asset_change'])

    def test_absent_storage_and_validation(self):
        self.assertFalse(self.get()['available'])
        self.assertFalse(Path(self.path).exists())
        for query in ('start_date=bad', 'start_date=2020-02-01&end_date=2020-01-01',
                      'start_date=2020-01-01&end_date=2022-01-01', 'end_date=2999-01-01'):
            self.assertEqual(self.client.get('/api/research/returns?' + query).status_code, 422)

    def test_error_is_redacted(self):
        with patch.object(returns, '_open_db', side_effect=RuntimeError('PRIVATE_SECRET /home/private')):
            result = self.get()
            self.assertFalse(result['success'])
            self.assertNotIn('PRIVATE', str(result))


if __name__ == '__main__':
    unittest.main()
