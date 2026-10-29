import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from BollingerBand_MFI10_Monitor import decision_snapshot, parse_log_records
from BollingerBand_MFI10_Status import read_status, write_status
from BollingerBand_MFI10_Trader import (
    ALLOCATION_PER_STOCK, BrokerPosition, CreonClient, PositionState, Quote,
    Trader, buy_quantity, creon_code, normalize_code,
)


class CodeRegressionTests(unittest.TestCase):
    def test_numeric_alphanumeric_and_etn_roundtrips(self):
        for plain, api in (
            ('005930', 'A005930'), ('0043B0', 'A0043B0'),
            ('0167A0', 'A0167A0'), ('Q530031', 'Q530031'),
        ):
            with self.subTest(code=plain):
                self.assertEqual(creon_code(plain), api)
                self.assertEqual(creon_code(api), api)
                self.assertEqual(normalize_code(api), plain)
        self.assertEqual(creon_code('AQ530031'), 'Q530031')
        with self.assertRaises(ValueError):
            creon_code('invalid')

    def test_alphanumeric_code_reaches_quotes_and_all_order_apis(self):
        request = Mock()
        request.GetHeaderValue.side_effect = lambda field: {
            0: 'A0167A0', 2: 1, 5: 321, 8: 321,
            11: 16025, 16: 16030, 17: 16020,
        }[field]
        request.GetDataValue.side_effect = lambda field, index: [
            ('A0167A0', 16025, 16030, 16020)
        ][index][field]
        client = CreonClient.__new__(CreonClient)
        client.market_eye = request
        api_patch = patch.object(
            client, 'win32com', SimpleNamespace(Dispatch=lambda _: request),
            create=True,
        )
        api_patch.start()
        self.addCleanup(api_patch.stop)
        client.account = 'mock'
        client.goods_code = 'mock'
        client._block_request = Mock()
        stock = SimpleNamespace(code='0167A0')
        for session in ('REGULAR', 'AFTER_MARKET'):
            request.reset_mock()
            self.assertEqual(
                client.get_quotes([stock], session)['0167A0'].price, 16025
            )
            request.SetInputValue.assert_any_call(
                1 if session == 'REGULAR' else 0,
                ['A0167A0'] if session == 'REGULAR' else 'A0167A0',
            )
        for method, args in (
            (client.send_ioc_best_order, ('buy', stock.code, 1, 16030)),
            (client.send_limit_order, ('sell', stock.code, 1, 16020)),
            (client.send_preopen_close_order, ('sell', stock.code, 1)),
        ):
            request.reset_mock()
            self.assertEqual(method(*args), 321)
            request.SetInputValue.assert_any_call(3, 'A0167A0')


class BudgetAndStatusTests(unittest.TestCase):
    def setUp(self):
        self.stock = SimpleNamespace(
            code='036460', company='Gas', selected=True, ma20=30000, mfi10=45,
        )
        self.quote = Quote(price=36600, ask=36700, bid=36500)
        self.state = PositionState(
            quantity=54, average_price=36795, peak_price=36795,
        )
        self.creon = Mock()
        self.creon.get_quotes.return_value = {self.stock.code: self.quote}
        self.creon.get_positions.return_value = {
            self.stock.code: BrokerPosition(54, 36795)
        }
        self.creon.get_available_cash.return_value = 2_000_000
        self.trader = Trader(
            None, self.creon, [self.stock], live=True,
            states={self.stock.code: self.state},
        )

    def run_cycle(self):
        with patch(
            'BollingerBand_MFI10_Trader.trading_session', return_value='REGULAR'
        ), patch('BollingerBand_MFI10_Trader.save_position_state'):
            self.trader.run_cycle()

    def test_two_million_limit_and_cost_buffer_boundaries(self):
        self.assertEqual(ALLOCATION_PER_STOCK, 2_000_000)
        self.assertEqual(buy_quantity(PositionState(), 1_000_000), 1)
        for cash, quantity in ((1004, 0), (1005, 1), (2009, 1), (2010, 2)):
            self.assertEqual(buy_quantity(PositionState(), 1000, cash), quantity)
        for ask in (0, -1, float('nan'), float('inf')):
            self.assertEqual(buy_quantity(PositionState(), ask), 0)
        prior_holding = PositionState(quantity=27, average_price=36553.185185)
        self.assertEqual(buy_quantity(prior_holding, 36700), 27)

    def test_residual_rejected_before_signal_cash_or_order(self):
        output = StringIO()
        with redirect_stdout(output):
            self.run_cycle()
            self.run_cycle()
        self.creon.get_available_cash.assert_not_called()
        self.creon.send_ioc_best_order.assert_not_called()
        self.assertEqual(output.getvalue().count('추가 1주 매수 불가'), 1)
        self.assertIn('매수신호 0건', output.getvalue())
        self.assertEqual(self.trader.cycle_decisions['036460']['status'], 'blocked')

    def test_price_fall_restores_affordability_without_weakening_signal(self):
        self.state.average_price = (ALLOCATION_PER_STOCK - 36800) / 54
        self.state.peak_price = 36000
        self.assertEqual(buy_quantity(self.state, 36700), 0)
        self.creon.get_quotes.return_value = {
            self.stock.code: Quote(price=36500, ask=36600, bid=36400)
        }
        self.trader._wait_for_fill = Mock(return_value=(BrokerPosition(55, 36000), 1))
        with redirect_stdout(StringIO()):
            self.run_cycle()
        self.creon.send_ioc_best_order.assert_called_once_with(
            'buy', self.stock.code, 1, 36600
        )
        row = self.trader.cycle_decisions[self.stock.code]
        self.assertEqual(row['status'], 'filled')
        self.assertEqual(row['last_order']['quantity'], 1)

    def test_unaffordable_buy_does_not_prevent_trailing_stop(self):
        self.creon.get_quotes.return_value = {
            self.stock.code: Quote(price=34000, ask=34100, bid=33900)
        }
        with patch.object(self.trader, '_sell') as sell, redirect_stdout(StringIO()):
            self.run_cycle()
        self.assertEqual(sell.call_args.args[-1], 'trailing_stop')
        self.creon.get_available_cash.assert_not_called()

    def test_cash_empty_unfilled_and_partial_fill_are_distinct(self):
        for cash, filled, expected in (
            (0, 0, 'blocked'), (2_000_000, 0, 'unfilled'),
            (2_000_000, 1, 'filled'),
        ):
            with self.subTest(expected=expected), redirect_stdout(StringIO()):
                self.trader.last_order_at.clear()
                state = PositionState()
                self.creon.get_available_cash.return_value = cash
                self.trader._wait_for_fill = Mock(
                    return_value=(BrokerPosition(filled, 36700), filled)
                )
                result = self.trader._buy(self.stock, self.quote, state, BrokerPosition())
                self.assertEqual(result['status'], expected)
                self.assertEqual(state.quantity, filled)

    def test_missing_quote_warning_throttled_and_recovery_logged(self):
        self.creon.get_quotes.return_value = {}
        output = StringIO()
        with redirect_stdout(output):
            self.run_cycle()
            self.run_cycle()
            self.creon.get_quotes.return_value = {self.stock.code: self.quote}
            self.run_cycle()
        self.assertEqual(output.getvalue().count('현재가 조회 실패'), 1)
        self.assertIn('[시세 복구]', output.getvalue())

    def test_atomic_snapshot_published_on_failure_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            self.trader.status_path = path
            with redirect_stdout(StringIO()):
                self.run_cycle()
                self.assertEqual(read_status(path)['state'], 'fresh')
                self.creon.get_quotes.side_effect = RuntimeError('mock quote failure')
                self.run_cycle()
                self.assertIn('mock quote failure', read_status(path)['cycle_error'])
                self.creon.get_quotes.side_effect = None
                self.run_cycle()
                self.assertEqual(read_status(path)['cycle_error'], '')
                self.creon.get_positions.side_effect = RuntimeError('mock positions failure')
                with self.assertRaisesRegex(RuntimeError, 'mock positions failure'):
                    self.run_cycle()
                self.assertIn('감시 주기 중단', read_status(path)['cycle_error'])
            self.assertFalse(list(Path(directory).glob('*.tmp')))

    def test_order_exception_does_not_publish_success(self):
        self.trader.states[self.stock.code] = PositionState()
        self.creon.get_positions.return_value = {}
        self.creon.send_ioc_best_order.side_effect = RuntimeError('mock order rejected')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            self.trader.status_path = path
            with redirect_stdout(StringIO()), self.assertRaisesRegex(
                RuntimeError, 'mock order rejected'
            ):
                self.run_cycle()
            payload = read_status(path)
            self.assertIn('mock order rejected', payload['cycle_error'])
            self.assertNotEqual(payload['decisions'][0]['status'], 'filled')

    def test_warning_severity_and_repeat_interval(self):
        logger = Mock()
        logger.handlers = [Mock()]
        with patch(
            'BollingerBand_MFI10_Trader.logging.getLogger', return_value=logger
        ), redirect_stdout(StringIO()):
            for tick in (100, 399, 400):
                with patch(
                    'BollingerBand_MFI10_Trader.time.monotonic', return_value=tick
                ):
                    self.trader._log_error(self.stock, '현재가 조회 실패')
        self.assertEqual(logger.warning.call_count, 2)


class MonitorStatusTests(unittest.TestCase):
    def test_missing_stale_restart_inactive_corrupt_and_cycle_error(self):
        now = datetime(2026, 10, 29, 10, 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trader_status.json'
            self.assertEqual(read_status(path, now)['state'], 'unavailable')
            payload = {
                'version': 1, 'generated_at': now.isoformat(),
                'cycle_error': '', 'decisions': [],
            }
            write_status(path, payload)
            task = {'state': 'Running', 'last_run': '2026-10-29 08:50:00'}
            self.assertEqual(decision_snapshot(path, task, [], now)['state'], 'fresh')
            self.assertEqual(read_status(path, now + timedelta(seconds=181))['state'], 'stale')
            self.assertEqual(read_status(path, now - timedelta(seconds=1))['state'], 'stale')
            restarted = {**task, 'last_run': '2026-10-29 10:00:01'}
            self.assertEqual(decision_snapshot(path, restarted, [], now)['state'], 'stale')
            self.assertEqual(decision_snapshot(path, {}, [], now)['state'], 'inactive')
            write_status(path, {**payload, 'cycle_error': '계좌 조회 실패'})
            self.assertEqual(decision_snapshot(path, task, [], now)['state'], 'error')
            write_status(path, {**payload, 'decisions': [{}]})
            self.assertEqual(read_status(path, now)['state'], 'unavailable')
            path.write_text('{broken', encoding='utf-8')
            self.assertEqual(read_status(path, now)['state'], 'unavailable')

    def test_failed_write_preserves_previous_snapshot_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trader_status.json'
            payload = {
                'version': 1, 'generated_at': datetime.now().isoformat(),
                'cycle_error': '', 'decisions': [],
            }
            write_status(path, payload)
            with self.assertRaises(ValueError):
                write_status(path, {**payload, 'invalid': float('nan')})
            self.assertEqual(read_status(path)['state'], 'fresh')
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_legacy_info_quote_failure_is_visible_as_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trader.log'
            path.write_text(
                '2026-10-29 10:00:00 INFO Stock (0167A0): 현재가 조회 실패\n',
                encoding='utf-8',
            )
            self.assertEqual(parse_log_records(path)[0]['level'], 'WARNING')


if __name__ == '__main__':
    unittest.main()
