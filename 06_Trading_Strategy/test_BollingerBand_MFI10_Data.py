import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from BollingerBand_MFI10_Data import (
    DAILY_REPAIR_BATCH_SIZE,
    DailyPriceSynchronizer,
    completed_daily_prices,
    load_investar_updater,
)
from BollingerBand_MFI10_Selection import selection_filter_conditions
from BollingerBand_MFI10_Trader import load_kospi_candidates


TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 10)


def daily_frame(days):
    return pd.DataFrame(
        [(day.strftime('%Y%m%d'), 100, 120, 90, 110, 100000)
         for day in days],
        columns=['date', 'open', 'high', 'low', 'close', 'volume'],
    )


class DailyConnection:
    def __init__(self, codes, dates):
        self.codes = codes
        self.rows = {
            code: {day: (day, 100, 120, 90, 110, 100000) for day in dates}
            for code in codes
        }
        self.commits = 0
        self.rollbacks = 0
        self.statements = []
        self.result = []
        self.rowcount = 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        if params is None:
            raise AssertionError('SQL parameters are required')
        self.statements.append((query, params))
        if 'FROM selected_stock ' in query:
            self.result = [(code, code) for code in self.codes]
        elif 'information_schema.TABLES' in query:
            self.result = [(0,)]
        elif 'FROM company_info' in query:
            self.result = [(code, code) for code in params]
        elif 'SELECT date, high, low, close, volume' in query:
            rows = self.rows.get(params[0], {})
            self.result = [
                (day, row[2], row[3], row[4], row[5])
                for day, row in sorted(rows.items(), reverse=True)
                if day < TODAY
            ][:180]
        elif 'SELECT code, date FROM daily_price' in query:
            self.result = [
                (code, day) for code in params[:-2]
                for day in self.rows.get(code, {})
                if params[-2] <= day <= params[-1]
            ]
        elif 'SELECT date FROM daily_price' in query:
            self.result = [
                (day,) for day in self.rows.get(params[0], {})
                if params[1] <= day <= params[2]
            ]
        else:
            raise AssertionError(query)

    def executemany(self, query, values):
        self.statements.append((query, values))
        self.rowcount = 0
        for code, day, opening, high, low, close, diff, volume in values:
            rows = self.rows.setdefault(code, {})
            if day not in rows:
                rows[day] = (day, opening, high, low, close, volume)
                self.rowcount += 1

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0]

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def prepared_indicators(prices):
    result = prices.copy()
    for column, value in {
        'ma20': 100, 'ma60': 90, 'ma20_change_5': 0.01,
        'return_1m': 0.1, 'return_3m': 0.2,
        'average_turnover_20': 2_000_000_000,
        'mfi10': 45, 'mfi_change_3': 5,
        'percent_b': 0.7, 'band_width_change_5': 0.1,
    }.items():
        result[column] = value
    return result


class CompletedDailyPriceTests(unittest.TestCase):
    def test_excludes_today_and_future_bars(self):
        prices = daily_frame([
            date(2026, 10, 20), TODAY, date(2026, 10, 22)
        ])
        result = completed_daily_prices(prices, TODAY)
        self.assertEqual(list(result['date'].dt.date), [date(2026, 10, 20)])

    def test_rejects_invalid_dates_duplicates_and_prices(self):
        valid = daily_frame([date(2026, 10, 20)])
        cases = [pd.concat([valid, valid]), valid.assign(date='20261301')]
        for column, value in [('close', 0), ('high', 99), ('volume', -1),
                              ('volume', float('nan')), ('close', 100.5)]:
            cases.append(valid.assign(**{column: value}))
        for prices in cases:
            with self.subTest(prices=prices.to_dict()):
                with self.assertRaises(ValueError):
                    completed_daily_prices(prices, TODAY)

    def test_storage_preserves_existing_prices_and_leaves_commit_to_caller(self):
        day = date(2026, 10, 20)
        connection = DailyConnection(['005930'], [day])
        original = connection.rows['005930'][day]
        prices = daily_frame([day]).assign(close=115, diff=5)
        prices['date'] = [day]
        inserted = load_investar_updater().insert_missing_daily_prices(
            connection, prices, '005930'
        )
        self.assertEqual(inserted, 0)
        self.assertEqual(connection.rows['005930'][day], original)
        self.assertEqual(connection.commits, 0)
        self.assertIn('ON DUPLICATE KEY UPDATE code', connection.statements[0][0])

    def test_creon_request_return_code_failure_is_not_treated_as_success(self):
        updater = load_investar_updater()
        collector = updater.CREON.__new__(updater.CREON)
        chart = Mock()
        chart.BlockRequest.return_value = 1
        chart.GetDibStatus.return_value = 0
        collector.objStockChart = chart
        collector._wait_for_request = Mock()
        with self.assertRaisesRegex(RuntimeError, 'ret=1'):
            collector.get_stock_price_day('A005930', '2026-10-19', '2026-10-20')
        chart.GetHeaderValue.assert_not_called()


class DailyPriceSyncTests(unittest.TestCase):
    def make_sync(self, codes=('005930',), stored=None, reference=None):
        reference = reference or [date(2026, 10, 19), date(2026, 10, 20)]
        connection = DailyConnection(codes, stored or [])
        source = Mock()
        source.get_stock_price_day.return_value = daily_frame(reference)
        sync = DailyPriceSynchronizer(connection, source_factory=lambda: source)
        candidates = [SimpleNamespace(code=code, company=code) for code in codes]
        return sync, connection, source, candidates

    def test_repairs_missing_bars_and_rechecks_stored_dates(self):
        sync, connection, source, candidates = self.make_sync(
            stored=[date(2026, 10, 19)]
        )
        output = StringIO()
        with redirect_stdout(output):
            changed = sync.step(candidates, NOW)
        self.assertTrue(changed)
        self.assertEqual(sync.required_date, date(2026, 10, 20))
        self.assertFalse(sync.block_reason)
        self.assertIn(date(2026, 10, 20), connection.rows['005930'])
        self.assertIn('[일봉 보충 완료]', output.getvalue())
        self.assertGreaterEqual(connection.commits, 2)
        self.assertEqual(source.get_stock_price_day.call_count, 2)

    def test_detects_an_internal_gap_even_when_latest_date_matches(self):
        days = [date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20)]
        sync, connection, source, candidates = self.make_sync(
            stored=[days[0], days[2]], reference=days
        )
        with redirect_stdout(StringIO()):
            sync.step(candidates, NOW)
        self.assertIn(days[1], connection.rows['005930'])
        self.assertEqual(source.get_stock_price_day.call_count, 2)

    def test_incomplete_repair_is_not_reported_as_complete(self):
        sync, connection, source, candidates = self.make_sync()
        source.get_stock_price_day.side_effect = [
            daily_frame([date(2026, 10, 19), date(2026, 10, 20)]),
            daily_frame([date(2026, 10, 19)]),
        ]
        output = StringIO()
        with redirect_stdout(output):
            sync.step(candidates, NOW)
        self.assertIn('[일봉 보충 미완료]', output.getvalue())
        self.assertNotIn('[일봉 보충 완료]', output.getvalue())
        self.assertNotIn(date(2026, 10, 20), connection.rows['005930'])

    def test_collection_failure_is_logged_and_retried_after_five_minutes(self):
        sync, connection, source, candidates = self.make_sync()
        source.get_stock_price_day.side_effect = [
            daily_frame([date(2026, 10, 20)]),
            RuntimeError('CREON disconnected'),
            daily_frame([date(2026, 10, 20)]),
            daily_frame([date(2026, 10, 20)]),
        ]
        output = StringIO()
        with patch('BollingerBand_MFI10_Data.time.monotonic', return_value=100):
            with redirect_stdout(output):
                sync.step(candidates, NOW)
                self.assertFalse(sync.step(candidates, NOW))
        self.assertIn('[일봉 보충 실패]', output.getvalue())
        self.assertEqual(connection.rollbacks, 1)
        with patch('BollingerBand_MFI10_Data.time.monotonic', return_value=400):
            with redirect_stdout(StringIO()):
                self.assertTrue(sync.step(candidates, NOW))
        self.assertIn(date(2026, 10, 20), connection.rows['005930'])

    def test_reference_failure_blocks_entries_but_step_returns_for_monitoring(self):
        sync, connection, source, candidates = self.make_sync()
        source.get_stock_price_day.side_effect = RuntimeError('reference failed')
        output = StringIO()
        with redirect_stdout(output):
            self.assertFalse(sync.step(candidates, NOW))
        self.assertIn('reference failed', sync.block_reason)
        self.assertIn('보유 종목 매도 감시 유지', output.getvalue())

    def test_uses_creon_calendar_instead_of_guessing_weekdays(self):
        friday = date(2026, 10, 16)
        sync, connection, source, candidates = self.make_sync(
            stored=[friday], reference=[friday]
        )
        with redirect_stdout(StringIO()):
            sync.step(candidates, datetime(2026, 10, 20, 9))
        self.assertEqual(sync.required_date, friday)
        self.assertEqual(source.get_stock_price_day.call_count, 1)

    def test_limits_repairs_per_cycle_and_does_not_rescan_each_tick(self):
        codes = tuple(f'{index:06d}' for index in range(5))
        sync, connection, source, candidates = self.make_sync(codes=codes)
        with redirect_stdout(StringIO()):
            sync.step(candidates, NOW)
            self.assertEqual(len(sync.pending), len(codes) - DAILY_REPAIR_BATCH_SIZE)
            self.assertEqual(
                source.get_stock_price_day.call_count, 1 + DAILY_REPAIR_BATCH_SIZE
            )
            sync.step(candidates, NOW)
        self.assertEqual(len(sync.pending), 0)
        self.assertEqual(source.get_stock_price_day.call_count, 1 + len(codes))

    def test_never_stores_today_even_if_creon_ignores_end_date(self):
        sync, connection, source, candidates = self.make_sync()
        source.get_stock_price_day.return_value = daily_frame(
            [date(2026, 10, 20), TODAY]
        )
        with redirect_stdout(StringIO()):
            sync.step(candidates, NOW)
        self.assertEqual(sync.required_date, date(2026, 10, 20))
        self.assertNotIn(TODAY, connection.rows['005930'])


class CandidateFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.days = list(pd.bdate_range(end='2026-10-20', periods=80).date)
        self.connection = DailyConnection(['005930', '000660'], self.days)
        self.indicators = patch(
            'BollingerBand_MFI10_Trader.prepare_selection_indicators',
            side_effect=prepared_indicators,
        )
        self.indicators.start()
        self.addCleanup(self.indicators.stop)

    def load(self):
        return {
            stock.code: stock for stock in load_kospi_candidates(
                self.connection, expected_date=self.days[-1],
                expected_dates=self.days,
            )
        }

    def test_blocks_uniformly_stale_data_against_verified_market_date(self):
        for code in self.connection.codes:
            del self.connection.rows[code][self.days[-1]]
        stocks = self.load()
        for stock in stocks.values():
            self.assertFalse(stock.selected)
            self.assertIn('오래된 일봉', ';'.join(stock.data_issues))
            self.assertEqual(stock.required_date, self.days[-1])

    def test_blocks_internal_gap_and_keeps_missing_history_candidate_visible(self):
        del self.connection.rows['005930'][self.days[-5]]
        self.connection.rows['000660'].clear()
        stocks = self.load()
        self.assertFalse(stocks['005930'].selected)
        self.assertIn('누락 일봉 1거래일', stocks['005930'].data_issues)
        self.assertFalse(stocks['000660'].selected)
        self.assertIn('일봉 없음', stocks['000660'].data_issues)

    def test_stale_candidate_becomes_selected_only_after_repair_and_reload(self):
        del self.connection.rows['005930'][self.days[-1]]
        self.assertFalse(self.load()['005930'].selected)
        source = Mock()
        source.get_stock_price_day.return_value = daily_frame(self.days)
        sync = DailyPriceSynchronizer(
            self.connection, source_factory=lambda: source
        )
        with redirect_stdout(StringIO()):
            sync.step(list(self.load().values()), NOW)
        stock = self.load()['005930']
        self.assertTrue(stock.selected)
        self.assertEqual(stock.data_issues, ())
        self.assertEqual(stock.data_date, self.days[-1])

    def test_selection_diagnostics_use_the_actual_filter_conditions(self):
        with patch(
            'BollingerBand_MFI10_Trader.prepare_selection_indicators',
            side_effect=lambda prices: prepared_indicators(prices).assign(
                ma20_change_5=-0.01, average_turnover_20=900_000_000
            ),
        ):
            stocks = self.load()
        reasons = stocks['005930'].selection_reasons
        self.assertIn('MA20 5일 변화율>0 불충족', reasons)
        self.assertIn('20일 평균 거래대금>=1,000,000,000원 불충족', reasons)
        self.assertFalse(stocks['005930'].selected)

    def test_bad_data_does_not_consume_ranking_slots(self):
        del self.connection.rows['005930'][self.days[-5]]
        with patch(
            'BollingerBand_MFI10_Trader.rank_daily_top_stocks',
            return_value={},
        ) as rank:
            self.load()
        self.assertEqual(set(rank.call_args.args[0]), {'000660'})

    def test_shared_diagnostic_filters_preserve_the_original_eligibility_rule(self):
        frame = prepared_indicators(
            pd.DataFrame({'close': [110] * 12})
        )
        for index, (column, value) in enumerate([
            ('close', 99), ('ma60', 101), ('ma20_change_5', 0),
            ('return_3m', 0), ('average_turnover_20', 999_999_999),
            ('mfi10', 39), ('mfi10', 81), ('mfi_change_3', 0),
            ('percent_b', 0.49), ('percent_b', 1.21),
        ]):
            frame.loc[index, column] = value
        original = (
            (frame['close'] > frame['ma20'])
            & (frame['ma20'] > frame['ma60'])
            & (frame['ma20_change_5'] > 0)
            & (frame['return_3m'] > 0)
            & (frame['average_turnover_20'] >= 1_000_000_000)
            & frame['mfi10'].ge(40) & frame['mfi10'].le(80)
            & (frame['mfi_change_3'] > 0)
            & frame['percent_b'].ge(0.5) & frame['percent_b'].le(1.2)
        )
        filters = list(selection_filter_conditions(frame).values())
        shared = filters[0]
        for condition in filters[1:]:
            shared = shared & condition
        pd.testing.assert_series_equal(original, shared)


if __name__ == '__main__':
    unittest.main()
