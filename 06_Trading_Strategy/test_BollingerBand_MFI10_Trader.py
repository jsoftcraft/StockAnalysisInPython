import unittest
from datetime import datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout

from BollingerBand_MFI10_Trader import (
    BrokerPosition,
    CreonClient,
    creon_code,
    is_market_open,
    next_market_open,
    PositionState,
    Quote,
    Trader,
    TraderAlreadyRunningError,
    acquire_trader_lock,
    daytrading_net_profit_rate,
    daytrading_net_profit_target,
    filter_candidates_by_owner,
    load_company_names,
    load_position_state_codes,
    print_candidate_summary,
    reconcile_daytrading_position_state,
    save_position_state,
    run_trader,
    trading_session,
)


class CreonCodeTests(unittest.TestCase):
    def test_numeric_stock_codes_receive_creon_prefix(self):
        self.assertEqual(creon_code('005930'), 'A005930')
        self.assertEqual(creon_code('A005930'), 'A005930')

    def test_alphanumeric_creon_codes_are_preserved(self):
        self.assertEqual(creon_code('Q530031'), 'Q530031')
        self.assertEqual(creon_code('AQ530031'), 'Q530031')


class TradingSessionTests(unittest.TestCase):
    def test_regular_and_after_hours_boundaries(self):
        self.assertEqual(
            trading_session(datetime(2026, 10, 12, 9, 0)), 'REGULAR'
        )
        self.assertIsNone(trading_session(datetime(2026, 10, 12, 15, 30)))
        self.assertIsNone(trading_session(datetime(2026, 10, 12, 15, 39)))
        self.assertEqual(
            trading_session(datetime(2026, 10, 12, 15, 40)),
            'AFTER_HOURS_CLOSE',
        )
        self.assertEqual(
            trading_session(datetime(2026, 10, 12, 16, 0)),
            'AFTER_MARKET',
        )
        self.assertEqual(
            trading_session(datetime(2026, 10, 12, 15, 59, 59)),
            'AFTER_HOURS_CLOSE',
        )
        self.assertEqual(
            trading_session(datetime(2026, 10, 12, 19, 59)),
            'AFTER_MARKET',
        )
        self.assertIsNone(trading_session(datetime(2026, 10, 12, 20, 0)))

    def test_monitor_stays_open_during_session_gap_and_after_hours(self):
        self.assertTrue(is_market_open(datetime(2026, 10, 12, 15, 35)))
        self.assertTrue(is_market_open(datetime(2026, 10, 12, 19, 59)))
        self.assertFalse(is_market_open(datetime(2026, 10, 12, 20, 0)))

    def test_wait_after_regular_close_resumes_at_after_hours_close(self):
        self.assertEqual(
            next_market_open(datetime(2026, 10, 12, 15, 35)),
            datetime(2026, 10, 12, 15, 40),
        )

    def test_after_hours_end_waits_for_next_weekday_open(self):
        self.assertEqual(
            next_market_open(datetime(2026, 10, 12, 20, 1)),
            datetime(2026, 10, 13, 9, 0),
        )


class TraderLaunchTests(unittest.TestCase):
    def test_duplicate_trader_lock_is_reported_as_a_clean_skip(self):
        output = StringIO()
        logger = SimpleNamespace(info=lambda message: None)

        with patch(
            'BollingerBand_MFI10_Trader.main',
            side_effect=TraderAlreadyRunningError(
                '같은 계좌의 Trader가 이미 실행 중입니다.'
            ),
        ), redirect_stdout(output):
            run_trader(SimpleNamespace(), logger)

        self.assertIn('신규 실행을 건너뜁니다.', output.getvalue())

    def test_unrelated_runtime_errors_still_propagate(self):
        logger = SimpleNamespace(warning=lambda message: None)

        with patch(
            'BollingerBand_MFI10_Trader.main',
            side_effect=RuntimeError('database unavailable'),
        ), self.assertRaisesRegex(RuntimeError, 'database unavailable'):
            run_trader(SimpleNamespace(), logger)


class CandidateSummaryTests(unittest.TestCase):
    def test_summaries_show_sell_rule_for_each_owner(self):
        mfi_output = StringIO()
        daytrading_output = StringIO()

        with redirect_stdout(mfi_output):
            print_candidate_summary([], True, 'mfi')
        with redirect_stdout(daytrading_output):
            print_candidate_summary(
                [], True, 'daytrading', allow_entries=False
            )

        self.assertIn('MFI10>=80', mfi_output.getvalue())
        self.assertIn(
            '순수익률 3.00% 이상 (시간 무관)', daytrading_output.getvalue()
        )
        self.assertIn('15:00부터 전량 청산', daytrading_output.getvalue())
        self.assertNotIn('MFI10>=', daytrading_output.getvalue())


class CandidateNameTests(unittest.TestCase):
    def test_loads_real_company_name_and_ignores_code_as_name(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query, params):
                self.query = query
                self.params = params

            def fetchall(self):
                return [('005930', '삼성전자'), ('000660', '000660')]

        class Connection:
            def __init__(self):
                self.current_cursor = Cursor()

            def cursor(self):
                return self.current_cursor

        connection = Connection()

        self.assertEqual(
            load_company_names(connection, ['005930', '000660']),
            {'005930': '삼성전자'},
        )
        self.assertEqual(connection.current_cursor.params, ('000660', '005930'))


class AfterHoursSellTests(unittest.TestCase):
    def setUp(self):
        self.stock = SimpleNamespace(code='005930', company='Samsung')
        self.quote = Quote(price=95, ask=96, bid=94)
        self.state = PositionState(
            quantity=3,
            average_price=100,
            peak_price=100,
            exit_reason='trailing_stop',
        )
        self.creon = SimpleNamespace(
            get_positions=lambda: {
                '005930': BrokerPosition(quantity=3, average_price=100)
            },
            get_unfilled_orders=lambda: {},
            send_preopen_close_order=lambda *args: 101,
            send_ioc_best_order=lambda *args: 102,
        )
        self.trader = Trader(None, self.creon, [], live=True)

    def test_after_hours_close_uses_close_price_order(self):
        with patch(
            'BollingerBand_MFI10_Trader.trading_session',
            return_value='AFTER_HOURS_CLOSE',
        ):
            with patch.object(self.creon, 'send_preopen_close_order') as order:
                order.return_value = 101
                self.trader._sell(
                    self.stock, self.quote, self.state, 'trailing_stop'
                )

        order.assert_called_once_with('sell', '005930', 3)

    def test_after_market_sell_uses_ioc_order(self):
        with patch(
            'BollingerBand_MFI10_Trader.trading_session',
            return_value='AFTER_MARKET',
        ), patch.object(
            self.creon, 'send_ioc_best_order', return_value=102
        ) as order, patch.object(
            self.trader, '_wait_for_fill',
            return_value=(BrokerPosition(quantity=0), 3),
        ):
            self.trader._sell(
                self.stock, self.quote, self.state, 'trailing_stop'
            )

        order.assert_called_once_with('sell', '005930', 3, 94)


class MonitorOnlyTests(unittest.TestCase):
    def test_after_market_monitor_keeps_pending_daytrading_order_in_quotes(self):
        stock = SimpleNamespace(
            code='005930', company='Samsung', selected=False,
            ma20=100, mfi10=50,
        )
        requested = []
        creon = SimpleNamespace(
            get_quotes=lambda candidates, session: requested.extend(
                candidate.code for candidate in candidates
            ) or {'005930': Quote(price=101, ask=102, bid=100)},
            get_positions=lambda: {},
            is_after_market_eligible=lambda code: True,
        )
        trader = Trader(
            None, creon, [stock], live=True,
            daytrading_order_quantities={'005930': 5},
            allow_entries=False,
            daytrading_monitor_only=True,
        )

        with patch(
            'BollingerBand_MFI10_Trader.trading_session',
            return_value='AFTER_MARKET',
        ):
            trader.run_cycle()

        self.assertEqual(requested, ['005930'])

    def test_after_market_cycle_allows_selected_buy_signals(self):
        stock = SimpleNamespace(
            code='005930', company='Samsung', selected=True,
            ma20=100, mfi10=20,
        )
        creon = SimpleNamespace(
            get_quotes=lambda candidates, session: {
                '005930': Quote(price=101, ask=102, bid=100)
            },
        )
        trader = Trader(None, creon, [stock], live=False)
        output = StringIO()

        with patch(
            'BollingerBand_MFI10_Trader.trading_session',
            return_value='AFTER_MARKET',
        ), redirect_stdout(output):
            trader.run_cycle()

        self.assertIn('[모의 매수]', output.getvalue())

    def test_quote_error_skips_cycle_and_reports_retry(self):
        stock = SimpleNamespace(
            code='005930', company='Samsung', selected=False,
            ma20=100, mfi10=50,
        )

        def get_quotes(candidates, session):
            raise RuntimeError('invalid CREON ProgID')

        creon = SimpleNamespace(get_quotes=get_quotes)
        trader = Trader(
            None, creon, [stock], live=False,
            states={'005930': PositionState(quantity=1)},
        )
        output = StringIO()

        with patch(
            'BollingerBand_MFI10_Trader.trading_session',
            return_value='AFTER_MARKET',
        ), redirect_stdout(output):
            trader.run_cycle()

        self.assertIn('시세 조회 오류', output.getvalue())
        self.assertIn('다음 주기에 재시도합니다', output.getvalue())

    def test_regular_cycle_prints_buy_condition_query_to_console(self):
        stock = SimpleNamespace(
            code='005930', company='Samsung', selected=True,
            ma20=100, mfi10=20,
        )
        creon = SimpleNamespace(
            get_quotes=lambda candidates, session: {
                '005930': Quote(price=101, ask=102, bid=100)
            },
        )
        trader = Trader(None, creon, [stock], live=False)
        output = StringIO()

        with patch(
            'BollingerBand_MFI10_Trader.trading_session',
            return_value='REGULAR',
        ), redirect_stdout(output):
            trader.run_cycle()

        self.assertIn('[매수/매도 조건 조회]', output.getvalue())
        self.assertIn('신규매수 허용', output.getvalue())
        self.assertIn(
            '[매수조건 확인] 평가 1종목, 매수신호 1건',
            output.getvalue(),
        )

    def test_monitor_only_does_not_enter_new_positions(self):
        stock = SimpleNamespace(
            code='005930', company='Samsung', selected=True,
            ma20=100, mfi10=20,
        )
        creon = SimpleNamespace(
            get_quotes=lambda candidates, session: {
                '005930': Quote(price=110, ask=111, bid=109)
            },
            get_positions=lambda: {},
        )
        trader = Trader(
            None, creon, [stock], live=True,
            states={'005930': PositionState()}, allow_entries=False,
        )
        with patch(
            'BollingerBand_MFI10_Trader.trading_session',
            return_value='REGULAR',
        ), patch.object(trader, '_buy') as buy:
            trader.run_cycle()

        buy.assert_not_called()

    def test_monitor_only_still_dispatches_sell_for_held_position(self):
        stock = SimpleNamespace(
            code='005930', company='Samsung', selected=False,
            ma20=100, mfi10=50,
        )
        creon = SimpleNamespace(
            account='account',
            get_quotes=lambda candidates, session: {
                '005930': Quote(price=90, ask=91, bid=89)
            },
            get_positions=lambda: {
                '005930': BrokerPosition(quantity=3, average_price=100)
            },
        )
        state = PositionState(
            quantity=3, average_price=100, peak_price=100
        )
        trader = Trader(
            None, creon, [stock], live=True,
            states={'005930': state}, allow_entries=False,
        )
        with patch(
            'BollingerBand_MFI10_Trader.trading_session',
            return_value='REGULAR',
        ), patch.object(trader, '_sell') as sell, patch(
            'BollingerBand_MFI10_Trader.save_position_state'
        ):
            trader.run_cycle()

        sell.assert_called_once_with(stock, Quote(90, 91, 89), state, 'trailing_stop')


class TraderOwnershipTests(unittest.TestCase):
    def test_daytrading_state_reconciles_down_to_broker_quantity(self):
        state = PositionState(
            quantity=440, average_price=100, peak_price=120,
            owner='daytrading',
        )
        position = BrokerPosition(quantity=328, average_price=105)

        reconciled = reconcile_daytrading_position_state(
            state, position, datetime(2026, 10, 16, 9, 0)
        )

        self.assertTrue(reconciled)
        self.assertEqual(state.quantity, 328)
        self.assertEqual(state.average_price, 105)
        self.assertEqual(state.peak_price, 120)

    def test_position_reconciliation_does_not_expand_or_change_mfi_state(self):
        daytrading_state = PositionState(quantity=328, owner='daytrading')
        mfi_state = PositionState(quantity=440, owner='mfi')

        self.assertFalse(reconcile_daytrading_position_state(
            daytrading_state, BrokerPosition(quantity=440, average_price=100)
        ))
        self.assertFalse(reconcile_daytrading_position_state(
            mfi_state, BrokerPosition(quantity=328, average_price=100)
        ))
        self.assertEqual(daytrading_state.quantity, 328)
        self.assertEqual(mfi_state.quantity, 440)

    def test_position_state_persists_owner(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query, params):
                self.query = query
                self.params = params

        class Connection:
            def __init__(self):
                self.current_cursor = Cursor()
                self.committed = False

            def cursor(self):
                return self.current_cursor

            def commit(self):
                self.committed = True

        connection = Connection()
        state = PositionState(quantity=2, owner='daytrading')

        save_position_state(connection, 'account', '005930', state)

        self.assertIn('(account_no, code, owner,', connection.current_cursor.query)
        self.assertEqual(connection.current_cursor.params[2], 'daytrading')
        self.assertTrue(connection.committed)

    def test_loads_open_position_codes_for_requested_owner(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query, params):
                self.query = query
                self.params = params

            def fetchall(self):
                return [('A005930',)]

        class Connection:
            def __init__(self):
                self.current_cursor = Cursor()

            def cursor(self):
                return self.current_cursor

        connection = Connection()

        self.assertEqual(
            load_position_state_codes(
                connection, 'account', ('daytrading',)
            ),
            {'005930'},
        )
        self.assertEqual(connection.current_cursor.params,
                          ('account', 'daytrading'))

    def test_persisted_owner_routes_positions_without_daily_order_records(self):
        daytrading_stock = SimpleNamespace(code='005930')
        mfi_stock = SimpleNamespace(code='000660')
        states = {
            '005930': PositionState(quantity=2, owner='daytrading'),
            '000660': PositionState(quantity=3, owner='mfi'),
        }

        self.assertEqual(
            filter_candidates_by_owner(
                [daytrading_stock, mfi_stock], {}, 'daytrading', states
            ),
            [daytrading_stock],
        )
        self.assertEqual(
            filter_candidates_by_owner(
                [daytrading_stock, mfi_stock], {}, 'mfi', states
            ),
            [mfi_stock],
        )

    def test_daytrading_monitor_sells_owned_code_without_buying(self):
        daytrading_stock = SimpleNamespace(
            code='005930', company='Samsung', selected=True,
            ma20=10000, mfi10=20,
        )
        mfi_stock = SimpleNamespace(
            code='000660', company='SK Hynix', selected=True,
            ma20=10000, mfi10=20,
        )
        state = PositionState(
            quantity=2, average_price=10000, peak_price=10000
        )
        creon = SimpleNamespace(
            account='account',
            get_quotes=lambda candidates, session: {
                stock.code: Quote(10360, 10360, 10352)
                for stock in candidates
            },
            get_positions=lambda: {
                '005930': BrokerPosition(2, 10000),
            },
        )
        trader = Trader(
            object(), creon, [daytrading_stock, mfi_stock], True,
            states={'005930': state, '000660': PositionState()},
            allow_entries=False,
            daytrading_monitor_only=True,
        )

        with patch(
            'BollingerBand_MFI10_Trader.load_daytrading_order_codes',
            return_value={'005930': 2},
        ), patch(
            'BollingerBand_MFI10_Trader.datetime'
        ) as mocked_datetime, patch(
            'BollingerBand_MFI10_Trader.trading_session',
            return_value='REGULAR',
        ), patch.object(trader, '_sell') as sell, patch.object(
            trader, '_buy'
        ) as buy, patch(
            'BollingerBand_MFI10_Trader.save_position_state'
        ):
            mocked_datetime.now.return_value = datetime(2026, 10, 12, 10, 0)
            trader.run_cycle()

        sell.assert_called_once_with(
            daytrading_stock, Quote(10360, 10360, 10352),
            state, 'daytrading_take_profit',
        )
        buy.assert_not_called()

    def test_mfi_and_daytrading_processes_manage_disjoint_codes(self):
        daytrading_stock = SimpleNamespace(
            code='005930', company='Samsung', selected=False,
            ma20=100, mfi10=50,
        )
        mfi_stock = SimpleNamespace(
            code='000660', company='SK Hynix', selected=False,
            ma20=100, mfi10=50,
        )

        for owner, expected_code in (
            ('mfi', '000660'),
            ('daytrading', '005930'),
        ):
            queried_codes = []
            creon = SimpleNamespace(
                account='account',
                get_quotes=lambda candidates, session: (
                    queried_codes.extend(stock.code for stock in candidates)
                    or {
                        stock.code: Quote(100, 101, 99)
                        for stock in candidates
                    }
                ),
                get_positions=lambda: {},
            )
            trader = Trader(
                object(), creon, [daytrading_stock, mfi_stock], True,
                allow_entries=False,
                daytrading_monitor_only=owner == 'daytrading',
                exclude_daytrading_positions=owner == 'mfi',
            )

            with patch(
                'BollingerBand_MFI10_Trader.load_daytrading_order_codes',
                return_value={'005930': 2},
            ), patch(
                'BollingerBand_MFI10_Trader.trading_session',
                return_value='REGULAR',
            ):
                trader.run_cycle()

            self.assertEqual(queried_codes, [expected_code])

    def test_process_roles_have_independent_account_locks(self):
        lock_server = {}

        class LockCursor:
            def __init__(self, connection):
                self.connection = connection
                self.result = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query, parameters):
                name = parameters[0]
                if 'GET_LOCK' in query:
                    holder = lock_server.get(name)
                    acquired = holder is None or holder is self.connection
                    if acquired:
                        lock_server[name] = self.connection
                    self.result = (1 if acquired else 0,)
                else:
                    released = lock_server.get(name) is self.connection
                    if released:
                        del lock_server[name]
                    self.result = (1 if released else 0,)

            def fetchone(self):
                return self.result

        class LockConnection:
            def cursor(self):
                return LockCursor(self)

        mfi_connection = LockConnection()
        daytrading_connection = LockConnection()
        mfi_lock = acquire_trader_lock(
            mfi_connection, 'account', role='mfi'
        )
        daytrading_lock = acquire_trader_lock(
            daytrading_connection, 'account', role='daytrading'
        )

        self.assertNotEqual(mfi_lock, daytrading_lock)
        with self.assertRaises(RuntimeError):
            acquire_trader_lock(LockConnection(), 'account', role='mfi')

    def test_role_lock_rejects_legacy_account_wide_trader(self):
        lock_server = {}

        class LockCursor:
            def __init__(self, connection):
                self.connection = connection
                self.result = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query, parameters):
                name = parameters[0]
                if 'GET_LOCK' in query:
                    holder = lock_server.get(name)
                    acquired = holder is None or holder is self.connection
                    if acquired:
                        lock_server[name] = self.connection
                    self.result = (1 if acquired else 0,)
                else:
                    released = lock_server.get(name) is self.connection
                    if released:
                        del lock_server[name]
                    self.result = (1 if released else 0,)

            def fetchone(self):
                return self.result

        class LockConnection:
            def cursor(self):
                return LockCursor(self)

        legacy_connection = LockConnection()
        acquire_trader_lock(legacy_connection, 'account')

        with self.assertRaises(RuntimeError):
            acquire_trader_lock(LockConnection(), 'account', role='mfi')


class DayTradingProfitTargetTests(unittest.TestCase):
    def test_net_profit_includes_round_trip_cost_estimate(self):
        self.assertLess(daytrading_net_profit_rate(10351, 10000), 0.03)
        self.assertGreaterEqual(daytrading_net_profit_rate(10352, 10000), 0.03)

    def test_profit_target_is_three_percent_at_all_times(self):
        for now in (
            datetime(2026, 10, 12, 9, 0),
            datetime(2026, 10, 12, 10, 30),
            datetime(2026, 10, 12, 14, 59),
        ):
            self.assertEqual(daytrading_net_profit_target(now), 0.03)

    def test_daytrading_position_sells_only_at_net_profit_target(self):
        stock = SimpleNamespace(
            code='005930', company='Samsung', selected=False,
            ma20=10000, mfi10=50,
        )
        state = PositionState(quantity=2, average_price=10000, peak_price=10000)
        creon = SimpleNamespace(
            account='account',
            get_quotes=lambda candidates, session: {
                '005930': Quote(price=10360, ask=10360, bid=10352)
            },
            get_positions=lambda: {
                '005930': BrokerPosition(quantity=2, average_price=10000)
            },
        )
        trader = Trader(
            None, creon, [stock], live=True,
            states={'005930': state},
            daytrading_order_quantities={'005930': 2},
            allow_entries=False,
        )

        with patch(
            'BollingerBand_MFI10_Trader.trading_session',
            return_value='REGULAR',
        ), patch.object(trader, '_sell') as sell, patch(
            'BollingerBand_MFI10_Trader.save_position_state'
        ):
            trader.run_cycle()

        sell.assert_called_once_with(
            stock, Quote(price=10360, ask=10360, bid=10352),
            state, 'daytrading_take_profit',
        )

    def test_daytrading_monitor_forces_sale_at_1500(self):
        stock = SimpleNamespace(
            code='005930', company='Samsung', selected=False,
            ma20=10000, mfi10=50,
        )

        for now, expected_reason in (
            (datetime(2026, 10, 12, 14, 59), None),
            (datetime(2026, 10, 12, 15, 0), 'daytrading_end_of_day'),
        ):
            state = PositionState(
                quantity=2, average_price=10000, peak_price=10000,
                owner='daytrading',
            )
            creon = SimpleNamespace(
                account='account',
                get_quotes=lambda candidates, session: {
                    '005930': Quote(price=9000, ask=9010, bid=8990)
                },
                get_positions=lambda: {
                    '005930': BrokerPosition(2, 10000)
                },
            )
            trader = Trader(
                None, creon, [stock], live=True,
                states={'005930': state},
                daytrading_order_quantities={'005930': 2},
                allow_entries=False, daytrading_monitor_only=True,
            )

            with patch(
                'BollingerBand_MFI10_Trader.datetime'
            ) as mocked_datetime, patch(
                'BollingerBand_MFI10_Trader.trading_session',
                return_value='REGULAR',
            ), patch.object(trader, '_sell') as sell, patch(
                'BollingerBand_MFI10_Trader.save_position_state'
            ):
                mocked_datetime.now.return_value = now
                trader.run_cycle()

            if expected_reason is None:
                sell.assert_not_called()
            else:
                sell.assert_called_once_with(
                    stock, Quote(price=9000, ask=9010, bid=8990),
                    state, expected_reason,
                )


class CreonAfterHoursApiTests(unittest.TestCase):
    def test_after_market_excludes_etf_section(self):
        client = CreonClient.__new__(CreonClient)
        client.code_manager = SimpleNamespace(
            GetStockSectionKind=lambda code: 10 if code == 'A069500' else 1
        )
        client.stock_section_kinds = {}

        self.assertFalse(client.is_after_market_eligible('069500'))
        self.assertTrue(client.is_after_market_eligible('005930'))

    def test_after_market_quote_uses_stock_mst_fields(self):
        class StockMst:
            def __init__(self):
                self.inputs = {}

            def SetInputValue(self, index, value):
                self.inputs[index] = value

            def GetHeaderValue(self, index):
                return {0: 'A005930', 11: 101, 16: 102, 17: 100}[index]

        stock_api = StockMst()
        client = CreonClient.__new__(CreonClient)
        client.win32com = SimpleNamespace(
            Dispatch=lambda name: stock_api if name == 'DsCbo1.StockMst' else None
        )
        client._block_request = lambda request: None

        quotes = client.get_quotes(
            [SimpleNamespace(code='005930')],
            session='AFTER_MARKET',
        )

        self.assertEqual(stock_api.inputs[0], 'A005930')
        self.assertEqual(quotes['005930'], Quote(price=101, ask=102, bid=100))

    def test_unfilled_order_query_includes_all_sessions(self):
        class OrderRequest:
            Continue = False

            def __init__(self):
                self.inputs = {}

            def SetInputValue(self, index, value):
                self.inputs[index] = value

            def GetHeaderValue(self, index):
                return 1 if index == 5 else 0

            def GetDataValue(self, field, row):
                return {
                    1: 123,
                    3: 'Q530031',
                    6: 10,
                    8: 4,
                    11: 6,
                    13: '1',
                }[field]

        request = OrderRequest()
        client = CreonClient.__new__(CreonClient)
        client.win32com = SimpleNamespace(Dispatch=lambda name: request)
        client.account = 'account'
        client.goods_code = 'goods'
        client._block_request = lambda current: None

        orders = client.get_unfilled_orders()

        self.assertEqual(request.inputs[6], '0')
        self.assertEqual(
            orders[123],
            {
                'code': 'Q530031',
                'quantity': 10,
                'filled_quantity': 4,
                'remaining_quantity': 6,
                'side': 'sell',
            },
        )


if __name__ == '__main__':
    unittest.main()