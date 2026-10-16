import unittest
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from DayTrading_Indicator_Correlation import (
    align_indicator_returns,
    _pairwise_correlations,
    rank_correlations,
)
from DayTrading_DownloadIndicators import (
    DEFAULT_TICKERS,
    download_indicator_closes,
)
from DayTrading_Data import (
    _load_updater_factory,
    collect_missing_korean_closes,
    load_indicator_closes_from_database,
    load_stock_data_from_database,
    save_indicator_closes_to_database,
    sync_stock_universe,
)
from DayTrading_Results import create_top10_chart, save_analysis_results
import DayTrading_Trader as day_trader


class DayTradingIndicatorCorrelationTests(unittest.TestCase):
    def test_ranked_candidate_summary_shows_daytrading_sell_target(self):
        output = StringIO()

        with patch('sys.stdout', output):
            day_trader.print_ranked_candidates(pd.DataFrame())

        self.assertIn(
            '순수익률 3.0% 이상 (시간 무관)',
            output.getvalue(),
        )
        self.assertIn('15:00부터 전량 청산', output.getvalue())

    def test_after_buy_monitoring_window_boundaries(self):
        self.assertTrue(day_trader.is_after_buy_monitoring_window(
            '2026-10-12', datetime(2026, 10, 12, 9, 30)
        ))
        self.assertTrue(day_trader.is_after_buy_monitoring_window(
            '2026-10-12', datetime(2026, 10, 12, 19, 59, 59)
        ))
        self.assertFalse(day_trader.is_after_buy_monitoring_window(
            '2026-10-12', datetime(2026, 10, 12, 20, 0)
        ))

    def test_late_live_run_starts_monitor_without_analysis_or_buys(self):
        logger = MagicMock()
        args = SimpleNamespace(
            as_of_date='2026-10-12', live=True, confirm='LIVE', no_db=False
        )
        with patch.object(
            day_trader, 'parse_args', return_value=args
        ), patch.object(
            day_trader, 'datetime'
        ) as clock, patch.object(
            day_trader, 'is_live_order_window_open', return_value=False
        ), patch.object(
            day_trader, 'is_after_buy_monitoring_window', return_value=True
        ), patch.object(
            day_trader, 'run_after_market_monitor'
        ) as monitor, patch.object(day_trader, 'connect_database') as connect:
            clock.now.return_value = datetime(2026, 10, 12, 9, 44)

            self.assertEqual(day_trader._run_analysis(logger), 0)

        monitor.assert_called_once_with('2026-10-12', logger)
        connect.assert_not_called()

    def test_live_daemon_waits_until_after_market_without_long_sleep(self):
        logger = MagicMock()
        current = [datetime(2026, 10, 12, 19, 59, 30)]
        sleeps = []

        def advance(seconds):
            sleeps.append(seconds)
            current[0] += pd.Timedelta(seconds=seconds).to_pytimedelta()

        day_trader.wait_until_after_market(
            '2026-10-12', logger,
            now_provider=lambda: current[0],
            sleep=advance,
        )

        self.assertEqual(current[0], datetime(2026, 10, 12, 20, 0))
        self.assertEqual(sleeps, [30])
        logger.info.assert_any_call(
            'DayTrading after-market monitoring window ended'
        )

    def test_after_market_wait_returns_immediately_after_deadline(self):
        logger = MagicMock()
        sleep = MagicMock()

        day_trader.wait_until_after_market(
            '2026-10-12', logger,
            now_provider=lambda: datetime(2026, 10, 12, 20, 1),
            sleep=sleep,
        )

        sleep.assert_not_called()

    def test_live_run_starts_daytrading_owned_monitor(self):
        logger = MagicMock()
        run_process = MagicMock(return_value=SimpleNamespace(returncode=0))
        times = iter((
            datetime(2026, 10, 12, 9, 22),
            datetime(2026, 10, 12, 20, 0),
        ))
        result = day_trader.run_after_market_monitor(
            '2026-10-12', logger,
            run_process=run_process,
            now_provider=lambda: next(times),
        )

        command = run_process.call_args.args[0]
        self.assertIn('--daytrading-monitor-only', command)
        self.assertNotIn('--monitor-only', command)
        self.assertIn('--live', command)
        self.assertTrue(any(
            '[DayTrading 매도 기준 충족]' in argument
            for argument in command
        ))
        self.assertTrue(any(
            'company_info' in argument for argument in command
        ))
        self.assertTrue(any(
            'daytrading_stock_universe' in argument
            for argument in command
        ))
        self.assertTrue(any(
            'load_daytrading_buy_signals' in argument
            for argument in command
        ))
        self.assertTrue(any(
            'place_live_buy_orders' in argument
            for argument in command
        ))
        compile(command[2], '<daytrading-monitor-bootstrap>', 'exec')
        self.assertEqual(result, 0)

    def test_configure_logging_records_events_and_exception_traceback(self):
        console_output = StringIO()
        with TemporaryDirectory() as directory, patch(
            'sys.stderr', console_output
        ):
            logger = day_trader.configure_logging(directory)
            try:
                logger.info('indicator download completed')
                try:
                    raise RuntimeError('sample failure')
                except RuntimeError:
                    logger.exception('DayTrading run failed')
                for handler in logger.handlers:
                    handler.flush()
                log_path = next(Path(directory).glob('daytrading_*.log'))
                content = log_path.read_text(encoding='utf-8')
            finally:
                for handler in tuple(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()

        self.assertIn('indicator download completed', content)
        self.assertIn('DayTrading run failed', content)
        self.assertIn('RuntimeError: sample failure', content)
        self.assertIn('indicator download completed', console_output.getvalue())
        self.assertIn('DayTrading run failed', console_output.getvalue())

    def test_main_logs_pipeline_exceptions(self):
        logger = MagicMock()
        with patch.object(
            day_trader, 'configure_logging', return_value=logger
        ), patch.object(
            day_trader, '_run_analysis', side_effect=RuntimeError('failure')
        ):
            with self.assertRaisesRegex(RuntimeError, 'failure'):
                day_trader.main()

        logger.exception.assert_called_once_with('DayTrading run failed')

    def test_print_ranked_candidates_shows_indicator_and_correlation(self):
        results = pd.DataFrame([
            {
                'rank': 1,
                'code': '005930',
                'company': 'Samsung',
                'market': 'KOSPI',
                'indicator': 'SPY',
                'correlation': 0.82,
                'indicator_correlations': 'SPY=+0.8200; QQQ=-0.2100',
                'buy_signal': True,
                'reference_close': 100000,
                'estimated_buy_quantity': 10,
                'max_buy_amount_krw': 1000000,
            },
            {
                'rank': 2,
                'code': '000660',
                'company': 'SK hynix',
                'market': 'KOSPI',
                'indicator': 'QQQ',
                'correlation': 0.75,
                'indicator_correlations': 'SPY=+0.3000; QQQ=+0.7500',
                'buy_signal': False,
                'reference_close': 200000,
                'estimated_buy_quantity': 0,
                'max_buy_amount_krw': 1000000,
            },
        ])

        with patch('builtins.print') as print_mock:
            day_trader.print_ranked_candidates(results)

        output = ' '.join(
            str(argument)
            for call in print_mock.call_args_list
            for argument in call.args
        )
        self.assertIn('005930', output)
        self.assertIn('000660', output)
        self.assertIn('SPY', output)
        self.assertIn('QQQ', output)
        self.assertIn('0.8200', output)
        self.assertIn('0.7500', output)
        self.assertIn('SPY=+0.8200; QQQ=-0.2100', output)
        self.assertIn('대기', output)

    def test_print_ranked_candidates_reports_empty_list(self):
        results = pd.DataFrame(columns=['buy_signal'])

        with patch('builtins.print') as print_mock:
            day_trader.print_ranked_candidates(results)

        self.assertIn('없음', str(print_mock.call_args_list))

    def test_pairwise_correlation_uses_only_shared_finite_observations(self):
        stock_returns = pd.DataFrame({
            '000001': [0.01, 0.02, np.nan, 0.04, 0.05],
        })
        indicator_returns = pd.DataFrame({
            'SPY': [0.02, 0.04, 0.06, np.nan, 0.10],
        })

        correlations, observations = _pairwise_correlations(
            stock_returns, indicator_returns
        )

        self.assertEqual(observations[0, 0], 3)
        self.assertAlmostEqual(correlations[0, 0], 1.0)

    def test_alignment_requires_immediately_preceding_weekday_session(self):
        indicator_returns = pd.DataFrame(
            {'SPY': [0.01, 0.02, 0.03]},
            index=pd.to_datetime(['2026-10-08', '2026-10-09', '2026-10-12']),
        )
        aligned = align_indicator_returns(
            pd.to_datetime([
                '2026-10-09', '2026-10-12', '2026-10-13', '2026-10-14'
            ]),
            indicator_returns,
        )

        self.assertEqual(aligned.loc['2026-10-09', 'SPY'], 0.01)
        self.assertEqual(aligned.loc['2026-10-12', 'SPY'], 0.02)
        self.assertEqual(aligned.loc['2026-10-13', 'SPY'], 0.03)
        self.assertTrue(pd.isna(aligned.loc['2026-10-14', 'SPY']))

    def test_ranks_positive_correlations_for_krx_preopen_signals(self):
        korean_dates = pd.bdate_range('2026-01-05', periods=66)
        indicator_dates = korean_dates - pd.offsets.BDay(1)
        returns = np.sin(np.arange(len(korean_dates)) * 0.7) * 0.004
        returns[0] = 0
        returns[-1] = 0.002
        signal_date = korean_dates[-1] + pd.offsets.BDay(1)
        indicator_sessions = indicator_dates.append(pd.DatetimeIndex([
            korean_dates[-1], signal_date,
        ]))
        indicator_returns = np.concatenate([returns, [0.002, 0.5]])
        indicator_closes = pd.DataFrame(
            {
                'SPY': 100 * np.cumprod(1 + indicator_returns),
                'QQQ': 100 * np.cumprod(1 - indicator_returns),
            },
            index=indicator_sessions,
        )
        stock_closes = pd.DataFrame({
            '000001': 100 * np.cumprod(1 + returns),
            '000002': 100 * np.cumprod(1 - returns),
        }, index=korean_dates)
        metadata = pd.DataFrame({
            'market': ['KOSPI', 'KOSDAQ'],
            'company': ['Positive', 'Negative'],
        }, index=['1', '2'])

        result = rank_correlations(
            stock_closes,
            indicator_closes,
            as_of_date=signal_date,
            stock_metadata=metadata,
            lookback=60,
            min_observations=40,
            top_n=10,
            buy_window_open=True,
        )

        self.assertEqual(result['code'].tolist(), ['000001', '000002'])
        self.assertAlmostEqual(result.iloc[0]['correlation'], 1.0)
        self.assertEqual(
            result.iloc[0]['indicator_correlations'],
            'SPY=+1.0000; QQQ=-1.0000',
        )
        self.assertEqual(result.iloc[1]['indicator'], 'QQQ')
        self.assertEqual(
            result.iloc[1]['indicator_correlations'],
            'SPY=-1.0000; QQQ=+1.0000',
        )
        self.assertEqual(result.iloc[0]['observations'], 60)
        self.assertTrue(result.iloc[0]['buy_signal'])
        self.assertNotIn('nxt_eligible', result.columns)
        self.assertEqual(result.iloc[0]['max_buy_amount_krw'], 1_000_000)
        reference_close = result.iloc[0]['reference_close']
        estimated_quantity = result.iloc[0]['estimated_buy_quantity']
        self.assertLessEqual(estimated_quantity * reference_close, 1_000_000)
        self.assertGreater(
            (estimated_quantity + 1) * reference_close, 1_000_000
        )
        self.assertEqual(
            result.iloc[0]['indicator_trade_date'], korean_dates[-1]
        )

    def test_negative_correlation_uses_absolute_rank_and_falling_buy_signal(self):
        korean_dates = pd.bdate_range('2026-01-05', periods=45)
        indicator_dates = korean_dates - pd.offsets.BDay(1)
        returns = np.sin(np.arange(len(korean_dates)) * 0.7) * 0.004
        returns[0] = 0
        returns[-1] = 0.002
        signal_date = korean_dates[-1] + pd.offsets.BDay(1)
        indicator_sessions = indicator_dates.append(pd.DatetimeIndex([
            korean_dates[-1], signal_date,
        ]))
        indicator_returns = np.concatenate([-returns, [-0.002, 0.5]])
        indicator_closes = pd.DataFrame(
            {'SPY': 100 * np.cumprod(1 + indicator_returns)},
            index=indicator_sessions,
        )
        stock_closes = pd.DataFrame({
            '000001': 100 * np.cumprod(1 + returns),
            '000002': 100 * np.cumprod(1 - returns),
        }, index=korean_dates)

        result = rank_correlations(
            stock_closes,
            indicator_closes,
            as_of_date=signal_date,
            lookback=40,
            min_observations=30,
            top_n=10,
            buy_window_open=True,
        ).set_index('code')

        self.assertAlmostEqual(result.loc['000001', 'correlation'], -1.0)
        self.assertAlmostEqual(result.loc['000002', 'correlation'], 1.0)
        self.assertTrue(result.loc['000001', 'buy_signal'])
        self.assertFalse(result.loc['000002', 'buy_signal'])

    def test_buy_signal_requires_open_window(self):
        korean_dates = pd.bdate_range('2026-01-05', periods=45)
        indicator_dates = korean_dates - pd.offsets.BDay(1)
        returns = np.linspace(-0.001, 0.002, len(korean_dates))
        returns[0] = 0
        signal_date = korean_dates[-1] + pd.offsets.BDay(1)
        stock_closes = pd.DataFrame(
            {'000001': 100 * np.cumprod(1 + returns)}, index=korean_dates
        )
        indicator_closes = pd.DataFrame(
            {'GLD': 100 * np.cumprod(1 + np.append(returns, 0.002))},
            index=indicator_dates.append(pd.DatetimeIndex([korean_dates[-1]])),
        )

        result = rank_correlations(
            stock_closes,
            indicator_closes,
            as_of_date=signal_date,
            lookback=40,
            min_observations=30,
            buy_window_open=True,
        )

        self.assertEqual(len(result), 1)
        self.assertTrue(result.iloc[0]['buy_signal'])

        outside_window = rank_correlations(
            stock_closes,
            indicator_closes,
            as_of_date=signal_date,
            lookback=40,
            min_observations=30,
            buy_window_open=False,
        )
        self.assertFalse(outside_window.iloc[0]['buy_signal'])
        self.assertEqual(outside_window.iloc[0]['estimated_buy_quantity'], 0)

    def test_buy_window_is_today_from_0830_until_before_0930(self):
        analysis_date = '2026-10-12'
        self.assertFalse(day_trader.is_buy_window_open(
            analysis_date, day_trader.datetime(2026, 10, 12, 8, 29, 59)
        ))
        self.assertTrue(day_trader.is_buy_window_open(
            analysis_date, day_trader.datetime(2026, 10, 12, 8, 30)
        ))
        self.assertTrue(day_trader.is_buy_window_open(
            analysis_date, day_trader.datetime(2026, 10, 12, 8, 39, 59)
        ))
        self.assertTrue(day_trader.is_buy_window_open(
            analysis_date, day_trader.datetime(2026, 10, 12, 8, 59, 59)
        ))
        self.assertTrue(day_trader.is_buy_window_open(
            analysis_date, day_trader.datetime(2026, 10, 12, 9, 0)
        ))
        self.assertTrue(day_trader.is_buy_window_open(
            analysis_date, day_trader.datetime(2026, 10, 12, 9, 29, 59)
        ))
        self.assertFalse(day_trader.is_buy_window_open(
            analysis_date, day_trader.datetime(2026, 10, 12, 9, 30)
        ))
        self.assertFalse(day_trader.is_buy_window_open(
            analysis_date, day_trader.datetime(2026, 10, 13, 9, 15)
        ))

    def test_live_order_session_selects_premarket_close_and_auction(self):
        analysis_date = '2026-10-12'
        self.assertEqual(
            day_trader.resolve_live_order_session(
                analysis_date, now=day_trader.datetime(2026, 10, 12, 8, 35)
            ),
            'PREOPEN_CLOSE',
        )
        self.assertEqual(
            day_trader.resolve_live_order_session(
                analysis_date, 'auction',
                day_trader.datetime(2026, 10, 12, 8, 35),
            ),
            'OPENING_AUCTION',
        )
        self.assertEqual(
            day_trader.resolve_live_order_session(
                analysis_date, now=day_trader.datetime(2026, 10, 12, 8, 45)
            ),
            'OPENING_AUCTION',
        )
        self.assertIsNone(day_trader.resolve_live_order_session(
            analysis_date, 'preopen-close',
            day_trader.datetime(2026, 10, 12, 8, 45),
        ))
        self.assertEqual(
            day_trader.resolve_live_order_session(
                analysis_date, now=day_trader.datetime(2026, 10, 12, 9, 5)
            ),
            'REGULAR',
        )
        self.assertEqual(
            day_trader.resolve_live_order_session(
                analysis_date, now=day_trader.datetime(2026, 10, 12, 16, 0)
            ),
            'AFTER_MARKET',
        )
        self.assertEqual(
            day_trader.resolve_live_order_session(
                analysis_date, now=day_trader.datetime(2026, 10, 12, 19, 59, 59)
            ),
            'AFTER_MARKET',
        )
        self.assertIsNone(day_trader.resolve_live_order_session(
            analysis_date, now=day_trader.datetime(2026, 10, 12, 20, 0)
        ))

    def test_live_premarket_close_uses_fixed_previous_close_api(self):
        connection = MagicMock()
        logger = MagicMock()
        client = MagicMock()
        client.account = 'account-1'
        client.get_available_cash.return_value = 1_000_000
        client.send_preopen_close_order.return_value = None
        args = SimpleNamespace(
            live=True,
            as_of_date='2026-10-12',
            order_session='preopen-close',
            max_total_buy_amount=1_000_000,
        )
        result = pd.DataFrame([{
            'code': '005930',
            'company': 'Samsung',
            'buy_signal': True,
            'reference_close': 10_000,
            'max_buy_amount_krw': 1_000_000,
        }])

        with patch.object(
            day_trader, 'CreonClient', return_value=client
        ), patch.object(
            day_trader, 'claim_daytrading_order_attempt', return_value=True
        ) as claim_order, patch.object(
            day_trader, 'update_daytrading_order_attempt'
        ):
            submitted = day_trader.place_live_buy_orders(
                connection, result, args, logger,
                now=day_trader.datetime(2026, 10, 12, 8, 35),
            )

        self.assertEqual(submitted, 1)
        client.get_quotes.assert_not_called()
        client.send_preopen_close_order.assert_called_once_with(
            'buy', '005930', 99
        )
        claim_order.assert_called_once_with(
            connection, '2026-10-12', 'account-1', '005930', 99, 10_000
        )

    def test_live_opening_auction_uses_regular_limit_order(self):
        connection = MagicMock()
        logger = MagicMock()
        client = MagicMock()
        client.account = 'account-1'
        client.get_available_cash.return_value = 1_000_000
        client.get_quotes.return_value = {
            '005930': SimpleNamespace(ask=10_000),
        }
        client.send_limit_order.return_value = 12345
        args = SimpleNamespace(
            live=True,
            as_of_date='2026-10-12',
            order_session='auction',
            max_total_buy_amount=1_000_000,
        )
        result = pd.DataFrame([{
            'code': '005930',
            'company': 'Samsung',
            'buy_signal': True,
            'reference_close': 9_900,
            'max_buy_amount_krw': 1_000_000,
        }])

        with patch.object(
            day_trader, 'CreonClient', return_value=client
        ), patch.object(
            day_trader, 'claim_daytrading_order_attempt', return_value=True
        ), patch.object(
            day_trader, 'update_daytrading_order_attempt'
        ):
            submitted = day_trader.place_live_buy_orders(
                connection, result, args, logger,
                now=day_trader.datetime(2026, 10, 12, 8, 45),
            )

        self.assertEqual(submitted, 1)
        client.send_limit_order.assert_called_once_with(
            'buy', '005930', 99, 10_000
        )

    def test_live_aftermarket_uses_eligible_ioc_and_session_quote(self):
        connection = MagicMock()
        logger = MagicMock()
        client = MagicMock()
        client.account = 'account-1'
        client.get_available_cash.return_value = 1_000_000
        client.is_after_market_eligible.return_value = True
        client.get_quotes.return_value = {
            '005930': SimpleNamespace(ask=10_000),
        }
        client.send_ioc_best_order.return_value = 12345
        args = SimpleNamespace(
            live=True,
            as_of_date='2026-10-12',
            order_session='after-market',
            max_total_buy_amount=1_000_000,
        )
        result = pd.DataFrame([{
            'code': '005930',
            'company': 'Samsung',
            'buy_signal': True,
            'reference_close': 9_900,
            'max_buy_amount_krw': 1_000_000,
        }])

        with patch.object(
            day_trader, 'CreonClient', return_value=client
        ), patch.object(
            day_trader, 'claim_daytrading_order_attempt', return_value=True
        ) as claim_order, patch.object(
            day_trader, 'update_daytrading_order_attempt'
        ):
            submitted = day_trader.place_live_buy_orders(
                connection, result, args, logger,
                now=day_trader.datetime(2026, 10, 12, 16, 0),
            )

        self.assertEqual(submitted, 1)
        client.get_quotes.assert_called_once_with(
            [SimpleNamespace(code='005930')], session='AFTER_MARKET'
        )
        client.send_ioc_best_order.assert_called_once_with(
            'buy', '005930', 99, 10_000
        )
        claim_order.assert_called_once_with(
            connection, '2026-10-12', 'account-1', '005930', 99, 10_000
        )

    def test_missing_immediate_indicator_session_skips_entire_signal(self):
        korean_dates = pd.bdate_range('2026-01-05', periods=45)
        indicator_dates = korean_dates - pd.offsets.BDay(1)
        returns = np.linspace(-0.001, 0.002, len(korean_dates))
        stock_closes = pd.DataFrame(
            {'000001': 100 * np.cumprod(1 + returns)}, index=korean_dates
        )
        indicator_closes = pd.DataFrame(
            {'SPY': 100 * np.cumprod(1 + returns[:-1])},
            index=indicator_dates[:-1],
        )

        result = rank_correlations(
            stock_closes,
            indicator_closes,
            as_of_date=korean_dates[-1] + pd.offsets.BDay(1),
            lookback=40,
            min_observations=30,
        )

        self.assertTrue(result.empty)

    def test_empty_analysis_is_recorded_and_committed(self):
        connection = MagicMock()

        save_analysis_results(
            connection,
            '2026-10-12',
            pd.DataFrame(columns=[
                'rank', 'code', 'market', 'company', 'indicator',
                'correlation', 'observations', 'indicator_trade_date',
                'indicator_return', 'buy_signal', 'max_buy_amount_krw',
                'reference_close',
                'estimated_buy_quantity',
            ]),
        )

        self.assertTrue(connection.cursor.return_value.__enter__.called)
        self.assertTrue(connection.commit.called)
        self.assertFalse(connection.rollback.called)

    def test_top_ten_rows_are_written_to_history_table(self):
        connection = MagicMock()
        results = pd.DataFrame([{
            'rank': 1,
            'code': '005930',
            'market': 'KOSPI',
            'company': 'Samsung',
            'indicator': 'SPY',
            'correlation': 0.82,
            'observations': 60,
            'indicator_trade_date': pd.Timestamp('2026-10-09'),
            'indicator_return': 0.01,
            'buy_signal': True,
            'max_buy_amount_krw': 1_000_000,
            'reference_close': 100_000,
            'estimated_buy_quantity': 10,
        }])
        no_signal = results.iloc[0].copy()
        no_signal['rank'] = 2
        no_signal['code'] = '000660'
        no_signal['buy_signal'] = False
        no_signal['estimated_buy_quantity'] = 0
        results = pd.concat(
            [results, no_signal.to_frame().T], ignore_index=True
        )

        save_analysis_results(connection, '2026-10-12', results)

        self.assertTrue(connection.cursor.return_value.__enter__.return_value
                        .executemany.called)
        cursor = connection.cursor.return_value.__enter__.return_value
        history_rows = cursor.executemany.call_args_list[0].args[1]
        self.assertEqual(len(history_rows), 2)
        self.assertEqual(history_rows[0][2], '005930')
        self.assertTrue(history_rows[0][10])
        self.assertEqual(history_rows[0][11], 1_000_000)
        self.assertEqual(history_rows[0][13], 10)
        signal_query, signal_rows = cursor.executemany.call_args_list[1].args
        self.assertIn('daytrading_trade_signal', signal_query)
        self.assertEqual(len(signal_rows), 1)
        self.assertEqual(signal_rows[0][1], '005930')
        self.assertEqual(signal_rows[0][4], 'BUY')
        self.assertEqual(signal_rows[0][5], 'SIGNAL_ONLY')
        self.assertEqual(signal_rows[0][9], 10)
        self.assertTrue(connection.commit.called)

    def test_chart_is_written_for_top_ten_rows(self):
        results = pd.DataFrame([{
            'code': '005930',
            'market': 'KOSPI',
            'company': 'Samsung',
            'indicator': 'SPY',
            'correlation': 0.82,
        }])

        with TemporaryDirectory() as directory:
            chart_path = Path(directory) / 'top10.png'
            create_top10_chart(results, chart_path, '2026-10-12')

            self.assertTrue(chart_path.is_file())
            self.assertGreater(chart_path.stat().st_size, 0)

    def test_indicator_download_writes_close_csv(self):
        tickers = ['SPY', 'QQQ', 'GLD', 'USO']
        dates = pd.bdate_range('2026-01-01', periods=70)
        market_close_timestamps = (
            (dates + pd.Timedelta(hours=20)).tz_localize('UTC').asi8 // 10 ** 9
        ).tolist()

        def response_for(url, **kwargs):
            ticker = url.rsplit('/', 1)[-1]
            prices = np.arange(70, dtype=float) + 100
            response = MagicMock()
            response.json.return_value = {'chart': {'result': [{
                'timestamp': market_close_timestamps,
                'indicators': {
                    'quote': [{'close': prices.tolist()}],
                    'adjclose': [{'adjclose': (prices * 0.99).tolist()}],
                },
            }]}}
            return response

        with TemporaryDirectory() as directory:
            output_path = Path(directory) / '해외지표종가.csv'
            with patch('requests.get', side_effect=response_for):
                saved_path, row_count, latest_date = download_indicator_closes(
                    output_path=output_path, tickers=tickers
                )

            saved = pd.read_csv(saved_path)
            self.assertEqual(row_count, 70)
            self.assertEqual(latest_date, dates[-1])
            self.assertEqual(
                saved.columns.tolist(), ['date', 'SPY', 'QQQ', 'GLD', 'USO']
            )
            self.assertEqual(len(saved), 70)
            self.assertAlmostEqual(saved.iloc[-1]['SPY'], 167.31)

    def test_failed_indicator_download_preserves_previous_file(self):
        with TemporaryDirectory() as directory:
            output_path = Path(directory) / '해외지표종가.csv'
            output_path.write_text('old data', encoding='utf-8')
            response = MagicMock()
            response.json.return_value = {
                'chart': {'result': None, 'error': {'description': 'unavailable'}}
            }
            with patch('requests.get', return_value=response):
                with self.assertRaises(RuntimeError):
                    download_indicator_closes(output_path=output_path)

            self.assertEqual(
                output_path.read_text(encoding='utf-8'), 'old data'
            )

    def test_default_indicator_set_covers_distinct_asset_groups(self):
        self.assertGreaterEqual(len(DEFAULT_TICKERS), 12)
        self.assertTrue({'SPY', 'QQQ', 'EEM', 'TLT', 'UUP', 'GLD', 'USO'}
                        .issubset(DEFAULT_TICKERS))

    def test_stock_closes_and_market_metadata_load_from_database(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [
            ('005930', 'KOSPI', 'Samsung', '2026-10-06', 100),
            ('005930', 'KOSPI', 'Samsung', '2026-10-07', 101),
            ('000001', 'KOSDAQ', 'Example', '2026-10-06', 200),
            ('000001', 'KOSDAQ', 'Example', '2026-10-07', 202),
        ]

        closes, metadata = load_stock_data_from_database(
            connection, '2026-10-08', lookback=60
        )

        self.assertEqual(closes.shape, (2, 2))
        self.assertEqual(closes.loc['2026-10-07', '005930'], 101)
        self.assertEqual(metadata.loc['005930', 'market'], 'KOSPI')
        self.assertEqual(metadata.loc['000001', 'market'], 'KOSDAQ')
        self.assertIn(
            'FROM daytrading_stock_universe AS universe',
            cursor.execute.call_args.args[0],
        )

    def test_empty_database_stock_history_raises(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.fetchall.return_value = []

        with self.assertRaises(RuntimeError):
            load_stock_data_from_database(connection, '2026-10-08')

    def test_missing_korean_closes_are_collected_incrementally(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [
            ('005930', 'Samsung', 'A005930', '2026-10-08'),
        ]
        updater = MagicMock()
        updater.conn = MagicMock()
        factory = MagicMock(return_value=updater)
        universe = [{
            'code': '005930', 'market': 'KOSPI', 'company': 'Samsung',
            'security_type': 'equity', 'creon_code': 'A005930',
        }]

        date_range = collect_missing_korean_closes(
            connection,
            '2026-10-12',
            updater_factory=factory,
            universe_loader=lambda: universe,
        )

        self.assertEqual(
            date_range,
            (pd.Timestamp('2026-10-09'), pd.Timestamp('2026-10-11')),
        )
        updater.update_daily_price.assert_called_once_with(
            '2026-10-09', '2026-10-11',
            stock_codes=[('A005930', 'Samsung')],
        )
        updater.close.assert_called_once()
        updater.conn.close.assert_not_called()

    def test_db_updater_close_is_idempotent(self):
        updater_type = _load_updater_factory()
        updater = object.__new__(updater_type)
        connection = MagicMock()
        connection.open = True
        updater.conn = connection

        updater.close()
        updater.close()

        connection.close.assert_called_once()

    def test_stock_universe_sync_saves_market_and_security_type(self):
        connection = MagicMock()
        universe = [{
            'code': '069500', 'market': 'KOSPI', 'company': 'ETF Example',
            'security_type': 'ETF', 'creon_code': 'A069500',
        }]

        synced = sync_stock_universe(
            connection, universe_loader=lambda: universe
        )

        cursor = connection.cursor.return_value.__enter__.return_value
        self.assertEqual(synced, universe)
        self.assertTrue(cursor.executemany.called)
        self.assertEqual(
            cursor.executemany.call_args.args[1],
            [('069500', 'KOSPI', 'ETF Example', 'ETF', 'A069500',
              cursor.executemany.call_args.args[1][0][-1])],
        )
        self.assertTrue(connection.commit.called)

    def test_indicator_closes_are_saved_and_loaded_from_database(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [
            ('2026-10-08', 'SPY', 680.0),
            ('2026-10-08', 'GLD', 350.0),
            ('2026-10-09', 'SPY', 682.0),
            ('2026-10-09', 'GLD', 351.0),
        ]
        closes = pd.DataFrame(
            {'SPY': [680.0, 682.0], 'GLD': [350.0, 351.0]},
            index=pd.to_datetime(['2026-10-08', '2026-10-09']),
        )

        row_count = save_indicator_closes_to_database(connection, closes)
        loaded = load_indicator_closes_from_database(
            connection, '2026-10-12'
        )

        self.assertEqual(row_count, 4)
        self.assertTrue(connection.commit.called)
        self.assertEqual(loaded.loc['2026-10-09', 'SPY'], 682.0)
        self.assertEqual(loaded.loc['2026-10-09', 'GLD'], 351.0)

    def test_premarket_main_runs_pipeline_steps_in_order(self):
        events = []
        console_output = []
        connection = MagicMock()
        stocks = pd.DataFrame(
            {'005930': [100.0, 101.0]},
            index=pd.to_datetime(['2026-10-07', '2026-10-08']),
        )
        metadata = pd.DataFrame({'market': ['KOSPI']}, index=['005930'])
        indicators = pd.DataFrame(
            {'SPY': [680.0, 682.0]},
            index=pd.to_datetime(['2026-10-07', '2026-10-08']),
        )
        result = pd.DataFrame()
        args = SimpleNamespace(
            stock_closes=None,
            skip_korean_close_collection=False,
            as_of_date='2026-10-12',
            lookback=60,
            no_indicator_download=False,
            indicator_closes='indicator.csv',
            indicator_tickers='SPY',
            indicator_period='1y',
            metadata=None,
            min_observations=40,
            top_n=10,
            max_buy_amount=1_000_000,
            chart='top10.png',
            output=None,
            no_db=False,
        )

        def record(name, value=None):
            def run(*call_args, **call_kwargs):
                events.append(name)
                return value
            return run

        def capture_print(*values, **kwargs):
            console_output.append(' '.join(str(value) for value in values))

        with patch.object(day_trader, 'parse_args', return_value=args), \
                patch('builtins.print', side_effect=capture_print), \
                patch.object(
                    day_trader, 'configure_logging', return_value=MagicMock()
                ), \
                patch.object(day_trader, 'connect_database', return_value=connection), \
                patch.object(
                    day_trader, 'collect_missing_korean_closes',
                    side_effect=record('collect'),
                ), \
                patch.object(
                    day_trader, 'load_stock_data_from_database',
                    side_effect=record('load_stocks', (stocks, metadata)),
                ), \
                patch.object(
                    day_trader, 'download_indicator_closes',
                    side_effect=record(
                        'download',
                        (Path('indicator.csv'), 70, pd.Timestamp('2026-10-09')),
                    ),
                ), \
                patch.object(
                    day_trader, 'read_price_file',
                    side_effect=record('read_indicator', indicators),
                ), \
                patch.object(
                    day_trader, 'save_indicator_closes_to_database',
                    side_effect=record('save_indicators', 140),
                ), \
                patch.object(
                    day_trader, 'load_indicator_closes_from_database',
                    side_effect=record('load_indicators', indicators),
                ), \
                patch.object(
                    day_trader, 'rank_correlations',
                    side_effect=record('analyze', result),
                ), \
                patch.object(
                    day_trader, 'print_ranked_candidates',
                    side_effect=record('print_candidates'),
                ), \
                patch.object(
                    day_trader, 'create_top10_chart',
                    side_effect=record('chart'),
                ), \
                patch.object(
                    day_trader, 'save_analysis_results',
                    side_effect=record('save_results'),
                ):
            day_trader.main()

        self.assertEqual(events, [
            'collect', 'load_stocks', 'download', 'read_indicator',
            'save_indicators', 'load_indicators', 'analyze',
            'print_candidates', 'chart', 'save_results',
        ])
        self.assertIn(
            '상관 분석 지표종목: SPY', '\n'.join(console_output)
        )
        connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()