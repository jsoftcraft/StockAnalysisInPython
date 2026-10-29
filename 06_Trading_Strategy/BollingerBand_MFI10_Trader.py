"""KOSPI selected-stock BollingerBand/MFI10 CREON PLUS trader.

The default mode is a one-cycle dry run. Real orders require both --live and
--confirm LIVE. Run with 32-bit Python as administrator while CREON PLUS is
connected.
"""

import argparse
import ctypes
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import pandas as pd
import pymysql
from BollingerBand_MFI10_Data import DailyPriceSynchronizer
from BollingerBand_MFI10_Status import STATUS_PATH, write_status
from Trading_Code import creon_code, normalize_code
from BollingerBand_MFI10_Selection import (
    TOP_STOCK_COUNT,
    TRAILING_STOP_RATE,
    prepare_selection_indicators,
    rank_daily_top_stocks,
    selection_filter_conditions,
)


MARKET = 'KOSPI'
STOCK_COUNT = TOP_STOCK_COUNT
PASSWORD = "root"
ALLOCATION_PER_STOCK = 2_000_000
MA_PERIOD = 20
MFI_PERIOD = 10
MFI_BUY_THRESHOLD = 50
MFI_SELL_THRESHOLD = 80
BUY_COST_BUFFER_RATE = 0.005
DAYTRADING_NET_PROFIT_TARGET = 0.03
DAYTRADING_ROUND_TRIP_COST_ESTIMATE = 0.005
DAYTRADING_FORCE_SELL_TIME = datetime_time(15, 0)
TRADER_STATE_TABLE = 'bollingerband_mfi10_trader_state'
TRADING_START = datetime_time(9, 0)
REGULAR_TRADING_END = datetime_time(15, 30)
AFTER_HOURS_CLOSE_START = datetime_time(15, 40)
AFTER_HOURS_CLOSE_END = datetime_time(16, 0)
AFTER_MARKET_START = datetime_time(16, 0)
AFTER_MARKET_END = datetime_time(20, 0)
ORDER_COOLDOWN_SECONDS = 3
FILL_CHECK_ATTEMPTS = 5
FILL_CHECK_INTERVAL = 0.5
POSITION_PAGE_SIZE = 50
DECISION_LOG_INTERVAL_SECONDS = 300


class TraderAlreadyRunningError(RuntimeError):
    pass


class _TeeStream:
    def __init__(self, stream, logger, level):
        self.stream = stream
        self.logger = logger
        self.level = level
        self.pending = ''

    def write(self, message):
        self.stream.write(message)
        text = self.pending + message
        self.pending = ''
        for line in text.splitlines(keepends=True):
            if line.endswith(('\n', '\r')):
                content = line.rstrip('\r\n')
                if content:
                    self.logger.log(self.level, content)
            else:
                self.pending = line
        return len(message)

    def flush(self):
        self.stream.flush()
        if self.pending.strip():
            self.logger.log(self.level, self.pending)
            self.pending = ''

    def __getattr__(self, name):
        return getattr(self.stream, name)


def configure_trader_logging(process_role='mfi'):
    if process_role == 'daytrading':
        log_directory = Path(__file__).resolve().with_name('results') / 'logs'
    else:
        log_directory = Path(
            os.environ.get('LOCALAPPDATA') or Path.home()
        ) / 'StockAnalysisInPython' / 'logs'
    log_directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(
        f'bollingerband_mfi10_trader.{process_role}'
    )
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(
        log_directory / (
            'trader.log' if process_role == 'mfi'
            else f'trader_{process_role}.log'
        ),
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding='utf-8',
    )
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(message)s'
    ))
    logger.addHandler(handler)

    sys.stdout = _TeeStream(sys.stdout, logger, logging.INFO)
    sys.stderr = _TeeStream(sys.stderr, logger, logging.ERROR)
    logger.info('Trader process started')
    return logger


@dataclass(frozen=True)
class Candidate:
    code: str
    company: str
    ma20: float
    mfi10: float
    mfi_change: float
    return_1m: float
    return_3m: float
    percent_b: float
    average_turnover: float
    selected: bool
    data_date: Optional[date] = None
    required_date: Optional[date] = None
    history_rows: int = 0
    data_issues: tuple[str, ...] = ()
    selection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Quote:
    price: int
    ask: int
    bid: int


@dataclass(frozen=True)
class BrokerPosition:
    quantity: int = 0
    average_price: float = 0.0


@dataclass
class PositionState:
    quantity: int = 0
    average_price: float = 0.0
    peak_price: float = 0.0
    stop_triggered: bool = False
    exit_reason: str = ''
    last_exit_date: Optional[date] = None
    owner: str = ''


def trading_session(now):
    if now.weekday() >= 5:
        return None

    current_time = now.time()
    if TRADING_START <= current_time < REGULAR_TRADING_END:
        return 'REGULAR'
    if AFTER_HOURS_CLOSE_START <= current_time < AFTER_HOURS_CLOSE_END:
        return 'AFTER_HOURS_CLOSE'
    if AFTER_MARKET_START <= current_time < AFTER_MARKET_END:
        return 'AFTER_MARKET'
    return None


def is_monitoring_window_open(now):
    return (
        now.weekday() < 5
        and TRADING_START <= now.time() < AFTER_MARKET_END
    )


def connect_database():
    return pymysql.connect(
        host=os.environ.get('STOCK_DB_HOST', 'localhost'),
        user=os.environ.get('STOCK_DB_USER', 'root'),
        password=PASSWORD,
        database=os.environ.get('STOCK_DB_NAME', 'zunDB'),
        charset='utf8',
        autocommit=False,
    )


def load_company_names(connection, codes):
    codes = {normalize_code(code) for code in codes}
    if not codes:
        return {}

    placeholders = ', '.join(['%s'] * len(codes))
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT code, company FROM company_info '
            f'WHERE code IN ({placeholders})',
            tuple(sorted(codes)),
        )
        return {
            normalize_code(code): company
            for code, company in cursor.fetchall()
            if company and normalize_code(company) != normalize_code(code)
        }


def daily_indicator_issues(indicators, required_date, expected_dates):
    if indicators is None or indicators.empty:
        return ('일봉 없음',)
    data_date = indicators.index[-1].date()
    issues = []
    if data_date != required_date:
        issues.append(f'오래된 일봉 {data_date} (기준 {required_date})')
    if len(indicators) < 64:
        issues.append(f'일봉 이력 부족 {len(indicators)}/64')
    stored_dates = set(indicators.index.date)
    missing_count = sum(
        day >= indicators.index[0].date() and day not in stored_dates
        for day in expected_dates
    )
    if missing_count:
        issues.append(f'누락 일봉 {missing_count}거래일')
    latest = indicators.iloc[-1]
    if not all(math.isfinite(float(latest[column])) for column in (
        'ma20', 'mfi10', 'mfi_change_3', 'return_1m', 'return_3m',
        'percent_b', 'average_turnover_20', 'band_width_change_5',
    )):
        issues.append('유효하지 않은 일봉 지표')
    return tuple(issues)


def load_kospi_candidates(
    connection, extra_codes=None, expected_date=None, expected_dates=(),
):
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT code, company FROM selected_stock '
            'WHERE market = %s ORDER BY selection_rank LIMIT 100',
            (MARKET,),
        )
        portfolio = cursor.fetchall()
        cursor.execute(
            'SELECT COUNT(*) FROM information_schema.TABLES '
            'WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s',
            (TRADER_STATE_TABLE,),
        )
        state_table_exists = cursor.fetchone()[0] > 0
        if state_table_exists:
            cursor.execute(
                f'SELECT DISTINCT code FROM {TRADER_STATE_TABLE} '
                'WHERE managed_quantity > 0'
            )
            managed_codes = {
                normalize_code(row[0]) for row in cursor.fetchall()
            }
        else:
            managed_codes = set()

    if not portfolio:
        raise RuntimeError('selected_stock에 KOSPI 후보 종목이 없습니다.')

    candidate_names = {
        normalize_code(code): company or str(code)
        for code, company in portfolio
    }
    extra_codes = {
        normalize_code(code) for code in (extra_codes or ())
    }.difference(candidate_names)
    pool_codes = list(candidate_names)
    held_codes = managed_codes.difference(candidate_names)
    additional_codes = held_codes.union(extra_codes)
    if additional_codes:
        candidate_names.update({
            code: code for code in additional_codes.difference(candidate_names)
        })
    candidate_codes = pool_codes + sorted(
        additional_codes.difference(pool_codes)
    )
    candidate_names.update(load_company_names(connection, candidate_codes))

    histories = {}
    with connection.cursor() as cursor:
        for code in candidate_codes:
            cursor.execute(
                'SELECT date, high, low, close, volume FROM daily_price '
                'WHERE code = %s AND date < CURDATE() '
                'ORDER BY date DESC LIMIT 180',
                (code,),
            )
            rows = cursor.fetchall()
            if not rows:
                continue
            prices = pd.DataFrame(
                reversed(rows),
                columns=['date', 'high', 'low', 'close', 'volume'],
            )
            prices['date'] = pd.to_datetime(prices['date'])
            prices = prices.set_index('date')
            histories[code] = prepare_selection_indicators(prices)

    pool_histories = {
        code: histories[code] for code in pool_codes if code in histories
    }
    latest_date = expected_date or max(
        (prices.index.max().date() for prices in pool_histories.values()),
        default=None,
    )
    quality_issues = {
        code: daily_indicator_issues(
            histories.get(code), latest_date, expected_dates
        )
        for code in candidate_codes
    }
    membership = rank_daily_top_stocks(
        {
            code: prices for code, prices in pool_histories.items()
            if not quality_issues[code]
        },
        limit=STOCK_COUNT, lag=0,
    )
    candidates = []
    for code in candidate_codes:
        indicators = histories.get(code)
        if indicators is None or indicators.empty:
            candidates.append(Candidate(
                code=code, company=candidate_names[code],
                ma20=float('nan'), mfi10=float('nan'),
                mfi_change=float('nan'), return_1m=float('nan'),
                return_3m=float('nan'), percent_b=float('nan'),
                average_turnover=float('nan'), selected=False,
                required_date=latest_date,
                data_issues=('일봉 없음',),
                selection_reasons=('일봉 없음',),
            ))
            continue
        latest = indicators.iloc[-1]
        data_date = indicators.index[-1].date()
        data_issues = quality_issues[code]
        selection_reasons = list(data_issues) + [
            f'{condition} 불충족'
            for condition, passed in selection_filter_conditions(latest).items()
            if not passed
        ]
        if code not in pool_codes:
            selection_reasons.append('선정 후보군 외 보유/감시 종목')
        selected = (
            not selection_reasons
            and code in membership
            and bool(membership[code].iloc[-1])
        )
        if not selected and not selection_reasons:
            selection_reasons.append('종합 순위/상관관계 제한')
        candidates.append(Candidate(
            code=code,
            company=candidate_names[code],
            ma20=float(latest['ma20']),
            mfi10=float(latest['mfi10']),
            mfi_change=float(latest['mfi_change_3']),
            return_1m=float(latest['return_1m']),
            return_3m=float(latest['return_3m']),
            percent_b=float(latest['percent_b']),
            average_turnover=float(latest['average_turnover_20']),
            selected=selected,
            data_date=data_date,
            required_date=latest_date,
            history_rows=len(indicators),
            data_issues=tuple(data_issues),
            selection_reasons=tuple(selection_reasons),
        ))

    candidates.sort(key=lambda item: (
        not item.selected,
        -item.mfi_change if math.isfinite(item.mfi_change) else float('inf'),
    ))
    if not candidates:
        raise RuntimeError('KOSPI 후보 종목의 일봉 지표를 계산할 수 없습니다.')
    return candidates


class CreonClient:
    def __init__(self, enable_trading):
        if not hasattr(ctypes, 'windll') or not ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError('CREON PLUS 사용을 위해 관리자 권한으로 실행하세요.')
        try:
            import win32com.client
        except ImportError as exc:
            raise RuntimeError(
                'CREON PLUS는 32비트 Python과 pywin32가 필요합니다.'
            ) from exc

        self.win32com = win32com.client
        self.status = self.win32com.Dispatch('CpUtil.CpCybos')
        if self.status.IsConnect == 0:
            raise RuntimeError('CREON PLUS에 로그인한 뒤 실행하세요.')
        self.market_eye = self.win32com.Dispatch('CpSysDib.MarketEye')
        self.code_manager = self.win32com.Dispatch('CpUtil.CpCodeMgr')
        self.stock_section_kinds = {}
        self.last_request_at = 0.0
        self.account = None
        self.goods_code = None

        if enable_trading:
            self.trade_util = self.win32com.Dispatch('CpTrade.CpTdUtil')
            if self.trade_util.TradeInit(0) != 0:
                raise RuntimeError('CREON 주문 초기화에 실패했습니다.')
            accounts = self.trade_util.AccountNumber
            if not accounts:
                raise RuntimeError('CREON 주식 계좌를 찾을 수 없습니다.')
            self.account = accounts[0]
            goods = self.trade_util.GoodsList(self.account, 1)
            if not goods:
                raise RuntimeError('CREON 주식 상품 계좌를 찾을 수 없습니다.')
            self.goods_code = goods[0]

    def _block_request(self, request):
        for attempt in range(4):
            elapsed = time.monotonic() - self.last_request_at
            remain_ms = max(0, self.status.LimitRequestRemainTime)
            wait_seconds = max(0.25 - elapsed, remain_ms / 1000)
            if wait_seconds > 0:
                time.sleep(wait_seconds)

            result = request.BlockRequest()
            self.last_request_at = time.monotonic()
            if result == 4 and attempt < 3:
                time.sleep(max(self.status.LimitRequestRemainTime / 1000, 1))
                continue

            status = request.GetDibStatus()
            if result != 0 or status != 0:
                raise RuntimeError(
                    f'CREON 요청 실패 (ret={result}, status={status}): '
                    f'{request.GetDibMsg1()}'
                )
            return
        raise RuntimeError('CREON 연속 요청 제한을 해소하지 못했습니다.')

    def is_after_market_eligible(self, code):
        code = normalize_code(code)
        if code not in self.stock_section_kinds:
            self.stock_section_kinds[code] = int(
                self.code_manager.GetStockSectionKind(creon_code(code))
            )
        return self.stock_section_kinds[code] != 10

    def get_quotes(self, candidates, session='REGULAR'):
        if session == 'AFTER_MARKET':
            stock = self.win32com.Dispatch('DsCbo1.StockMst')
            quotes = {}
            for candidate in candidates:
                stock.SetInputValue(0, creon_code(candidate.code))
                self._block_request(stock)
                code = normalize_code(stock.GetHeaderValue(0))
                price = int(stock.GetHeaderValue(11))
                if price <= 0:
                    continue
                ask = int(stock.GetHeaderValue(16))
                bid = int(stock.GetHeaderValue(17))
                quotes[code] = Quote(
                    price=price,
                    ask=ask if ask > 0 else price,
                    bid=bid if bid > 0 else price,
                )
            return quotes

        self.market_eye.SetInputValue(0, [0, 4, 8, 9])
        self.market_eye.SetInputValue(
            1, [creon_code(stock.code) for stock in candidates]
        )
        self._block_request(self.market_eye)

        quotes = {}
        for index in range(self.market_eye.GetHeaderValue(2)):
            code = normalize_code(self.market_eye.GetDataValue(0, index))
            price = int(self.market_eye.GetDataValue(1, index))
            ask = int(self.market_eye.GetDataValue(2, index))
            bid = int(self.market_eye.GetDataValue(3, index))
            if price <= 0:
                continue
            quotes[code] = Quote(
                price=price,
                ask=ask if ask > 0 else price,
                bid=bid if bid > 0 else price,
            )
        return quotes

    def get_positions(self):
        request = self.win32com.Dispatch('CpTrade.CpTd6033')
        request.SetInputValue(0, self.account)
        request.SetInputValue(1, self.goods_code)
        request.SetInputValue(2, POSITION_PAGE_SIZE)
        positions = {}

        while True:
            self._block_request(request)
            for index in range(request.GetHeaderValue(7)):
                code = normalize_code(request.GetDataValue(12, index))
                quantity = int(request.GetDataValue(15, index))
                average_price = float(request.GetDataValue(17, index))
                if quantity <= 0:
                    continue
                old = positions.get(code, BrokerPosition())
                total_quantity = old.quantity + quantity
                total_cost = (
                    old.quantity * old.average_price + quantity * average_price
                )
                positions[code] = BrokerPosition(
                    quantity=total_quantity,
                    average_price=total_cost / total_quantity,
                )
            if not request.Continue:
                break
        return positions

    def get_available_cash(self):
        request = self.win32com.Dispatch('CpTrade.CpTdNew5331A')
        request.SetInputValue(0, self.account)
        request.SetInputValue(1, self.goods_code)
        self._block_request(request)
        return int(request.GetHeaderValue(9))

    def send_ioc_best_order(self, side, code, quantity, price):
        request = self.win32com.Dispatch('CpTrade.CpTd0311')
        request.SetInputValue(0, '2' if side == 'buy' else '1')
        request.SetInputValue(1, self.account)
        request.SetInputValue(2, self.goods_code)
        request.SetInputValue(3, creon_code(code))
        request.SetInputValue(4, quantity)
        request.SetInputValue(5, price)
        request.SetInputValue(7, '1')
        request.SetInputValue(8, '12')
        self._block_request(request)
        order_number = int(request.GetHeaderValue(8))
        if order_number <= 0:
            raise RuntimeError('CREON 주문 응답에 유효한 주문번호가 없습니다.')
        return order_number

    def send_limit_order(self, side, code, quantity, price):
        request = self.win32com.Dispatch('CpTrade.CpTd0311')
        request.SetInputValue(0, '2' if side == 'buy' else '1')
        request.SetInputValue(1, self.account)
        request.SetInputValue(2, self.goods_code)
        request.SetInputValue(3, creon_code(code))
        request.SetInputValue(4, quantity)
        request.SetInputValue(5, price)
        request.SetInputValue(7, '0')
        request.SetInputValue(8, '01')
        self._block_request(request)
        order_number = int(request.GetHeaderValue(8))
        if order_number <= 0:
            raise RuntimeError('CREON 주문 응답에 유효한 주문번호가 없습니다.')
        return order_number

    def send_preopen_close_order(self, side, code, quantity):
        request = self.win32com.Dispatch('CpTrade.CpTd0322')
        request.SetInputValue(0, '2' if side == 'buy' else '1')
        request.SetInputValue(1, self.account)
        request.SetInputValue(2, self.goods_code)
        request.SetInputValue(3, creon_code(code))
        request.SetInputValue(4, quantity)
        self._block_request(request)
        order_number = int(request.GetHeaderValue(5))
        if order_number <= 0:
            raise RuntimeError('CREON 시간외 종가 주문번호가 없습니다.')
        return order_number

    def get_unfilled_orders(self):
        request = self.win32com.Dispatch('CpTrade.CpTd5339')
        request.SetInputValue(0, self.account)
        request.SetInputValue(1, self.goods_code)
        request.SetInputValue(4, '1')
        request.SetInputValue(6, '0')
        request.SetInputValue(7, 20)

        orders = {}
        while True:
            self._block_request(request)
            for index in range(int(request.GetHeaderValue(5))):
                order_number = int(request.GetDataValue(1, index))
                orders[order_number] = {
                    'code': normalize_code(request.GetDataValue(3, index)),
                    'quantity': int(request.GetDataValue(6, index)),
                    'filled_quantity': int(request.GetDataValue(8, index)),
                    'remaining_quantity': int(request.GetDataValue(11, index)),
                    'side': (
                        'buy' if str(request.GetDataValue(13, index)) == '2'
                        else 'sell'
                    ),
                }
            if not request.Continue:
                break
        return orders


def ensure_position_state_table(connection):
    with connection.cursor() as cursor:
        cursor.execute(f"""CREATE TABLE IF NOT EXISTS {TRADER_STATE_TABLE} (
            account_no VARCHAR(32) NOT NULL,
            code VARCHAR(20) NOT NULL,
            owner VARCHAR(16) NOT NULL DEFAULT '',
            managed_quantity BIGINT UNSIGNED NOT NULL DEFAULT 0,
            average_price DECIMAL(16, 4) NOT NULL DEFAULT 0,
            peak_price DECIMAL(16, 4) NOT NULL DEFAULT 0,
            stop_triggered BOOLEAN NOT NULL DEFAULT FALSE,
            exit_reason VARCHAR(24) NOT NULL DEFAULT '',
            last_exit_date DATE NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (account_no, code)
        )""")
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (TRADER_STATE_TABLE,),
        )
        columns = {row[0] for row in cursor.fetchall()}
        if 'owner' not in columns:
            cursor.execute(
                f"ALTER TABLE {TRADER_STATE_TABLE} ADD COLUMN owner "
                "VARCHAR(16) NOT NULL DEFAULT '' AFTER code"
            )
        if 'exit_reason' not in columns:
            cursor.execute(
                f"ALTER TABLE {TRADER_STATE_TABLE} ADD COLUMN exit_reason "
                "VARCHAR(24) NOT NULL DEFAULT '' AFTER stop_triggered"
            )
        if 'last_exit_date' not in columns:
            cursor.execute(
                f"ALTER TABLE {TRADER_STATE_TABLE} ADD COLUMN last_exit_date "
                "DATE NULL AFTER exit_reason"
            )
    connection.commit()


def load_position_states(connection, account_no, candidates):
    codes = [stock.code for stock in candidates]
    if not codes:
        return {}
    placeholders = ', '.join(['%s'] * len(codes))
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT code, managed_quantity, average_price, peak_price, '
            f'stop_triggered, exit_reason, last_exit_date, owner '
            f'FROM {TRADER_STATE_TABLE} '
            f'WHERE account_no = %s AND code IN ({placeholders})',
            (account_no, *codes),
        )
        rows = cursor.fetchall()
        states = {
            normalize_code(code): PositionState(
                quantity=int(quantity),
                average_price=float(average_price),
                peak_price=float(peak_price),
                stop_triggered=bool(stop_triggered),
                exit_reason=exit_reason,
                last_exit_date=last_exit_date,
                owner=owner,
            )
            for (
                code, quantity, average_price, peak_price,
                stop_triggered, exit_reason, last_exit_date, owner,
            ) in rows
        }
    return states


def reconcile_daytrading_position_state(state, position, now=None):
    if (
        state.owner != 'daytrading'
        or position.quantity >= state.quantity
    ):
        return False

    now = now or datetime.now()
    state.quantity = position.quantity
    if position.quantity > 0:
        state.average_price = position.average_price
    else:
        state.average_price = 0.0
        state.peak_price = 0.0
        state.stop_triggered = False
        state.exit_reason = ''
        state.last_exit_date = now.date()
    return True


def load_daytrading_order_codes(connection, account_no):
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT COUNT(*) FROM information_schema.TABLES '
            'WHERE TABLE_SCHEMA = DATABASE() '
            'AND TABLE_NAME = %s',
            ('daytrading_order_attempt',),
        )
        if cursor.fetchone()[0] == 0:
            return {}
        cursor.execute(
            'SELECT code, quantity FROM daytrading_order_attempt '
            'WHERE analysis_date = CURRENT_DATE AND account_no = %s '
            "AND status IN ('SUBMITTING', 'SUBMITTED', 'UNKNOWN')",
            (str(account_no),),
        )
        return {
            normalize_code(code): int(quantity)
            for code, quantity in cursor.fetchall()
        }


def load_position_state_codes(connection, account_no, owners):
    if not owners:
        return set()
    placeholders = ', '.join(['%s'] * len(owners))
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT code FROM {TRADER_STATE_TABLE} '
            f'WHERE account_no = %s AND managed_quantity > 0 '
            f'AND owner IN ({placeholders})',
            (account_no, *owners),
        )
        return {normalize_code(row[0]) for row in cursor.fetchall()}


def filter_candidates_by_owner(
    candidates, daytrading_order_quantities, owner, states=None
):
    daytrading_codes = set(daytrading_order_quantities)
    states = states or {}
    if owner == 'daytrading':
        return [
            stock for stock in candidates
            if (
                stock.code in daytrading_codes
                or states.get(stock.code, PositionState()).owner == 'daytrading'
            )
        ]
    if owner == 'mfi':
        return [
            stock for stock in candidates
            if (
                stock.code not in daytrading_codes
                and states.get(stock.code, PositionState()).owner != 'daytrading'
            )
        ]
    return list(candidates)


def save_position_state(connection, account_no, code, state):
    with connection.cursor() as cursor:
        cursor.execute(
            f'INSERT INTO {TRADER_STATE_TABLE} '
            '(account_no, code, owner, managed_quantity, average_price, peak_price, '
            'stop_triggered, exit_reason, last_exit_date, updated_at) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
            'ON DUPLICATE KEY UPDATE owner = VALUES(owner), '
            'managed_quantity = VALUES(managed_quantity), '
            'average_price = VALUES(average_price), peak_price = VALUES(peak_price), '
            'stop_triggered = VALUES(stop_triggered), '
            'exit_reason = VALUES(exit_reason), '
            'last_exit_date = VALUES(last_exit_date), '
            'updated_at = VALUES(updated_at)',
            (
                account_no,
                code,
                state.owner,
                state.quantity,
                state.average_price,
                state.peak_price,
                state.stop_triggered,
                state.exit_reason,
                state.last_exit_date,
                datetime.now(),
            ),
        )
    connection.commit()


def acquire_trader_lock(connection, account_no, role='account'):
    legacy_name = f'BollingerBand_MFI10_Trader_{account_no}'[:64]
    role_names = {
        'mfi': f'BBMFI_MFI_{account_no}'[:64],
        'daytrading': f'BBMFI_DAYTRADING_{account_no}'[:64],
    }
    if role == 'account':
        lock_name = legacy_name
    elif role in role_names:
        lock_name = role_names[role]
    else:
        raise ValueError(f'Unknown Trader lock role: {role}')

    with connection.cursor() as cursor:
        cursor.execute('SELECT GET_LOCK(%s, 0)', (lock_name,))
        acquired = cursor.fetchone()[0] == 1
    if not acquired:
        raise TraderAlreadyRunningError(
            '같은 계좌의 Trader가 이미 실행 중입니다.'
        )

    if role != 'account':
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT GET_LOCK(%s, 1)', (legacy_name,))
                acquired_legacy = cursor.fetchone()[0] == 1
            if acquired_legacy:
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SELECT RELEASE_LOCK(%s)', (legacy_name,)
                    )
            else:
                raise RuntimeError(
                    '구버전 Trader가 계좌 잠금을 보유 중입니다.'
                )
        except Exception:
            release_trader_lock(connection, lock_name)
            raise
    return lock_name


def release_trader_lock(connection, lock_name):
    with connection.cursor() as cursor:
        cursor.execute('SELECT RELEASE_LOCK(%s)', (lock_name,))


def entry_signal_reasons(stock, price):
    reasons = []
    if not math.isfinite(stock.mfi10):
        reasons.append('유효하지 않은 MFI10')
    elif stock.mfi10 > MFI_BUY_THRESHOLD:
        reasons.append(f'MFI10 {stock.mfi10:.2f}>{MFI_BUY_THRESHOLD}')
    if not math.isfinite(stock.ma20):
        reasons.append('유효하지 않은 MA20')
    elif not math.isfinite(price) or price <= 0 or price <= stock.ma20:
        reasons.append(f'현재가 {price:,}<=MA20 {stock.ma20:,.2f} 또는 유효하지 않은 가격')
    return reasons


def buy_quantity(state, ask, available_cash=None):
    budget_left = max(0, ALLOCATION_PER_STOCK - state.quantity * state.average_price)
    spend_limit = (
        budget_left if available_cash is None else min(budget_left, available_cash)
    )
    if not math.isfinite(ask) or ask <= 0 or spend_limit <= 0:
        return 0
    return math.floor(spend_limit / (ask * (1 + BUY_COST_BUFFER_RATE)))


def buy_budget_reasons(state, ask):
    if buy_quantity(state, ask) > 0:
        return []
    budget_left = max(0, ALLOCATION_PER_STOCK - state.quantity * state.average_price)
    return [
        f'추가 1주 매수 불가: 종목 잔여 한도 {budget_left:,.0f}원, '
        f'매도호가 {ask:,}원, 비용 여유율 {BUY_COST_BUFFER_RATE:.2%}'
    ]


def _should_enter_position(stock, price, allow_entry):
    return (
        allow_entry
        and not getattr(stock, 'data_issues', ())
        and not entry_signal_reasons(stock, price)
    )


def decide_action(stock, price, state, allow_entry=True):
    if state.quantity == 0:
        return 'buy_signal' if _should_enter_position(stock, price, allow_entry) else 'hold'

    peak_price = max(state.peak_price, state.average_price)
    stop_price = peak_price * (1 - TRAILING_STOP_RATE)
    if state.stop_triggered or price <= stop_price:
        return 'trailing_stop'

    if (
        stock.mfi10 >= MFI_SELL_THRESHOLD
        and price < stock.ma20
        and price >= state.average_price
    ):
        return 'sell_signal'

    invested = state.quantity * state.average_price
    if (
        invested < ALLOCATION_PER_STOCK
        and _should_enter_position(stock, price, allow_entry)
    ):
        return 'buy_signal'

    return 'hold'


def daytrading_net_profit_rate(exit_price, average_price):
    if exit_price <= 0 or average_price <= 0:
        return float('-inf')
    net_exit_value = exit_price * (1 - DAYTRADING_ROUND_TRIP_COST_ESTIMATE)
    return net_exit_value / average_price - 1


def daytrading_net_profit_target(now):
    return DAYTRADING_NET_PROFIT_TARGET


def is_market_open(now):
    return is_monitoring_window_open(now)


def next_market_open(now):
    opening_today = datetime.combine(now.date(), TRADING_START)
    if now.weekday() < 5 and now < opening_today:
        return opening_today

    after_hours_open_today = datetime.combine(now.date(), AFTER_HOURS_CLOSE_START)
    if (
        now.weekday() < 5
        and REGULAR_TRADING_END <= now.time() < AFTER_HOURS_CLOSE_START
    ):
        return after_hours_open_today

    next_date = now.date() + timedelta(days=1)
    while next_date.weekday() >= 5:
        next_date += timedelta(days=1)
    return datetime.combine(next_date, TRADING_START)


def wait_until_market_open():
    while True:
        now = datetime.now()
        if is_market_open(now):
            print('거래 감시 시간입니다. Trader를 시작합니다.', flush=True)
            return

        opening = next_market_open(now)
        print(
            f'거래 감시 시작({opening:%Y-%m-%d %H:%M})까지 대기합니다. '
            '중단하려면 Ctrl+C를 누르세요.',
            flush=True,
        )
        time.sleep(min(60, max(1, (opening - now).total_seconds())))


class Trader:
    def __init__(
        self, connection, creon, candidates, live, states=None,
        daytrading_order_quantities=None, allow_entries=True,
        daytrading_monitor_only=False, exclude_daytrading_positions=False,
        daily_price_sync=None,
        status_path=None,
    ):
        self.connection = connection
        self.creon = creon
        self.candidates = candidates
        self.live = live
        self.states = states or {}
        self.daytrading_order_quantities = dict(
            daytrading_order_quantities or {}
        )
        self.allow_entries = allow_entries
        self.daytrading_monitor_only = daytrading_monitor_only
        self.exclude_daytrading_positions = exclude_daytrading_positions
        self.last_daytrading_codes_refresh = 0.0
        self.last_order_at = {}
        self.daily_price_sync = daily_price_sync
        self.daily_data_error = ''
        self.last_decision_logs = {}
        self.last_empty_cycle_log = None
        self.status_path = status_path
        self.cycle_decisions = {}
        self.last_orders = {}
        self.cycle_error = ''
        self.last_errors = {}

    def _refresh_daily_data(self, now):
        if self.daily_price_sync is None:
            return
        if not self.daily_price_sync.step(self.candidates, now):
            return
        try:
            self.connection.commit()
            refreshed = load_kospi_candidates(
                self.connection,
                extra_codes=set(self.states).union(
                    stock.code for stock in self.candidates
                ),
                expected_date=self.daily_price_sync.required_date,
                expected_dates=self.daily_price_sync.required_dates,
            )
            if self.live:
                new_candidates = [
                    stock for stock in refreshed if stock.code not in self.states
                ]
                self.states.update(load_position_states(
                    self.connection, self.creon.account, new_candidates
                ))
        except (RuntimeError, ValueError, pymysql.MySQLError) as exc:
            self.daily_data_error = f'보충 후 일봉 지표 재조회 실패: {exc}'
            print(
                f'[일봉 갱신 오류] {self.daily_data_error}; '
                '신규 매수 차단, 보유 종목 매도 감시 유지',
                flush=True,
            )
            return
        self.daily_data_error = ''
        if refreshed != self.candidates:
            print('[일봉 지표 갱신] 최신 완료 일봉으로 선정 조건을 재평가했습니다.')
            self.candidates = refreshed
            print_candidate_summary(
                filter_candidates_by_owner(
                    refreshed, self.daytrading_order_quantities,
                    'mfi' if self.exclude_daytrading_positions else 'all',
                    self.states,
                ),
                self.live, allow_entries=self.allow_entries,
            )

    def _log_entry_decision(self, stock, quote, state, session, restrictions):
        if not stock.selected and state.quantity == 0:
            return
        reasons = (
            restrictions + entry_signal_reasons(stock, quote.price)
            + buy_budget_reasons(state, quote.ask)
        )
        outcome = tuple(reasons) or ('매수신호',)
        # Cache reason categories, not changing prices, to avoid per-tick log floods.
        signature = (
            session, tuple(restrictions),
            stock.mfi10 <= MFI_BUY_THRESHOLD, quote.price > stock.ma20,
            outcome == ('매수신호',),
            buy_quantity(state, quote.ask) == 0,
        )
        logged = self.last_decision_logs.get(stock.code)
        tick = time.monotonic()
        if (
            logged is None or logged[0] != signature
            or tick - logged[1] >= DECISION_LOG_INTERVAL_SECONDS
        ):
            print(
                f'[매수 판단 상세] {stock.company} ({stock.code}) '
                f'세션 {session}, 일봉 {getattr(stock, "data_date", None)}, '
                f'기준 {getattr(stock, "required_date", None)}, '
                f'현재가 {quote.price:,}, MA20 {stock.ma20:,.2f}, '
                f'MFI10 {stock.mfi10:.2f}, 보유 {state.quantity}주 | '
                f'{"; ".join(outcome)}',
                flush=True,
            )
            self.last_decision_logs[stock.code] = (signature, tick)

    def _log_order(self, action, stock, quantity, price, reason='', order_no=0):
        self.last_orders[stock.code] = {
            'side': action, 'quantity': quantity, 'price': price,
            'at': datetime.now().isoformat(timespec='seconds'),
            'status': 'filled' if self.live else 'dry_run',
        }
        prefix = f'{"[모의 " if not self.live else "["}{"매수" if action == "buy" else "매도"}'
        msg = (
            f'{prefix}] {stock.company} ({stock.code}) {quantity}주 '
            f'@ {price:,}원'
        )
        if reason:
            msg += f', {reason}'
        if order_no:
            msg += f' (주문번호 {order_no})'
        print(msg)

    def _log_error(self, stock, message):
        tick = time.monotonic()
        previous = self.last_errors.get(stock.code)
        if (
            previous is not None and previous[0] == message
            and tick - previous[1] < DECISION_LOG_INTERVAL_SECONDS
        ):
            return
        self.last_errors[stock.code] = (message, tick)
        logger = (
            sys.stdout.logger if isinstance(sys.stdout, _TeeStream)
            else logging.getLogger('bollingerband_mfi10_trader.mfi')
        )
        text = f'[거래 경고] {stock.company} ({stock.code}): {message}'
        if logger.handlers:
            logger.warning(text)
            output = sys.stdout.stream if isinstance(sys.stdout, _TeeStream) else sys.stdout
            print(text, file=output, flush=True)
        else:
            print(text, flush=True)

    def _log_signal(self, stock, quote, state, reason):
        stop_price = state.peak_price * (1 - TRAILING_STOP_RATE)
        print(
            f'[모의 매도 신호] {stock.company} ({stock.code}) '
            f'{reason}, 현재가 {quote.price:,}원, '
            f'고점 기준 스탑 {stop_price:,.0f}원'
        )

    def run_cycle(self):
        self.cycle_decisions = {}
        self.cycle_error = ''
        try:
            self._run_cycle()
        except Exception as exc:
            self.cycle_error = f'감시 주기 중단: {exc}'
            raise
        finally:
            if self.status_path is not None:
                try:
                    write_status(self.status_path, {
                        'version': 1,
                        'generated_at': datetime.now().isoformat(timespec='seconds'),
                        'cycle_error': self.cycle_error,
                        'decisions': list(self.cycle_decisions.values()),
                    })
                except (OSError, ValueError) as exc:
                    print(f'[상태 기록 오류] 모니터 판단 정보를 저장하지 못했습니다: {exc}', flush=True)

    def _record_decision(self, stock, quote, state, restrictions):
        reasons = list(restrictions)
        if quote is not None:
            reasons += entry_signal_reasons(stock, quote.price)
            reasons += buy_budget_reasons(state, quote.ask)
        row = {
            'code': stock.code, 'company': stock.company,
            'selected': stock.selected,
            'price': quote.price if quote else None,
            'ask': quote.ask if quote else None,
            'ma20': stock.ma20 if math.isfinite(stock.ma20) else None,
            'mfi10': stock.mfi10 if math.isfinite(stock.mfi10) else None,
            'quantity': state.quantity,
            'budget_left': max(0, ALLOCATION_PER_STOCK - state.quantity * state.average_price),
            'order_quantity': buy_quantity(state, quote.ask) if quote and not reasons else 0,
            'status': 'blocked' if reasons else 'cash_unchecked',
            'reasons': reasons,
            'last_order': self.last_orders.get(stock.code),
        }
        if stock.selected or state.quantity > 0 or quote is None or row['last_order']:
            self.cycle_decisions[stock.code] = row
        return row

    def _run_cycle(self):
        now = datetime.now()
        self._refresh_daily_data(now)
        now = datetime.now()
        session = trading_session(now)
        if (
            self.live and self.connection is not None
            and time.monotonic() - self.last_daytrading_codes_refresh >= 2
        ):
            self.daytrading_order_quantities = load_daytrading_order_codes(
                self.connection, self.creon.account
            )
            self.last_daytrading_codes_refresh = time.monotonic()

        owner = (
            'daytrading' if self.daytrading_monitor_only
            else 'mfi' if self.exclude_daytrading_positions
            else 'all'
        )
        candidates = filter_candidates_by_owner(
            self.candidates, self.daytrading_order_quantities, owner,
            self.states,
        )
        for stock in self.candidates:
            state = self.states.get(stock.code, PositionState())
            if stock.selected or state.quantity > 0:
                self._record_decision(
                    stock, None, state,
                    ['시세/거래시간/소유권 필터로 미평가'],
                )
        if session == 'AFTER_MARKET' and self.allow_entries:
            candidates = [
                stock for stock in candidates
                if stock.selected
                or self.states.get(
                    stock.code, PositionState()
                ).quantity > 0
            ]
        elif session == 'AFTER_MARKET' and self.daytrading_monitor_only:
            candidates = [
                stock for stock in self.candidates
                if (
                    self.states.get(
                        stock.code, PositionState()
                    ).quantity > 0
                    or stock.code in self.daytrading_order_quantities
                )
            ]
        elif session != 'REGULAR':
            candidates = [
                stock for stock in self.candidates
                if self.states.get(stock.code, PositionState()).quantity > 0
            ]
        if session == 'AFTER_MARKET' and self.live:
            eligible_candidates = []
            for stock in candidates:
                try:
                    eligible = self.creon.is_after_market_eligible(
                        stock.code
                    )
                except Exception as exc:
                    self.cycle_error = f'시세 조회 오류: {exc}'
                    self._log_error(
                        stock,
                        f'애프터마켓 대상 확인 실패, 주문 차단: {exc}',
                    )
                    continue
                if eligible:
                    eligible_candidates.append(stock)
                else:
                    self._log_error(
                        stock, 'ETF는 KRX 애프터마켓 거래 대상이 아닙니다.'
                    )
            candidates = eligible_candidates
        if not candidates:
            tick = time.monotonic()
            if (
                self.last_empty_cycle_log is None
                or self.last_empty_cycle_log[0] != session
                or tick - self.last_empty_cycle_log[1] >= DECISION_LOG_INTERVAL_SECONDS
            ):
                print(
                    f'[매수/매도 조건 조회] {now:%H:%M:%S} 세션 {session}, '
                    '조회 대상 0종목 (선정/보유/소유권/거래시간 필터 적용)',
                    flush=True,
                )
                self.last_empty_cycle_log = (session, tick)
            return
        quote_session = session or 'REGULAR'
        if session in ('REGULAR', 'AFTER_MARKET'):
            entry_status = '허용' if self.allow_entries else '차단(감시 전용)'
            data_block_reason = self.daily_data_error or (
                self.daily_price_sync.block_reason
                if self.daily_price_sync is not None else ''
            )
            if data_block_reason:
                entry_status = f'차단({data_block_reason})'
            print(
                f'[매수/매도 조건 조회] {now:%H:%M:%S} 세션 {session}, 시세조회 '
                f'{len(candidates)}종목, 신규매수 '
                f'{entry_status}',
                flush=True,
            )
        try:
            quotes = self.creon.get_quotes(candidates, session=quote_session)
        except Exception as exc:
            self.cycle_error = f'시세 조회 오류: {exc}'
            print(
                f'[시세 조회 오류] {quote_session} 시세를 가져오지 못했습니다: '
                f'{exc}; 다음 주기에 재시도합니다.',
                flush=True,
            )
            return
        positions = self.creon.get_positions() if self.live else {}
        states_to_save = {}
        buy_condition_checks = 0
        buy_signal_count = 0

        for stock in candidates:
            quote = quotes.get(stock.code)
            if quote is None:
                self._log_error(stock, '현재가 조회 실패')
                self._record_decision(
                    stock, None, self.states.get(stock.code, PositionState()),
                    ['현재가 조회 실패'],
                )
                continue
            previous_error = self.last_errors.get(stock.code)
            if previous_error and previous_error[0] == '현재가 조회 실패':
                print(f'[시세 복구] {stock.company} ({stock.code}) 시세 정상 수신', flush=True)
                self.last_errors.pop(stock.code)

            state = self.states.setdefault(stock.code, PositionState())
            broker_position = positions.get(stock.code, BrokerPosition())
            if self.live and broker_position.quantity != state.quantity:
                daytrading_quantity = self.daytrading_order_quantities.get(
                    stock.code, 0
                )
                filled_daytrading_quantity = (
                    broker_position.quantity - state.quantity
                )
                if (
                    0 < filled_daytrading_quantity <= daytrading_quantity
                ):
                    state.quantity = broker_position.quantity
                    state.average_price = broker_position.average_price
                    state.peak_price = max(
                        state.peak_price, broker_position.average_price,
                        quote.price,
                    )
                elif (
                    state.exit_reason
                    and 0 <= broker_position.quantity < state.quantity
                ):
                    state.quantity = broker_position.quantity
                    if state.quantity == 0:
                        state.average_price = 0.0
                        state.peak_price = 0.0
                        state.stop_triggered = False
                        state.exit_reason = ''
                        state.last_exit_date = now.date()
                    else:
                        state.average_price = broker_position.average_price
                else:
                    raise RuntimeError(
                        f'{stock.company} ({stock.code}) 계좌 잔고 '
                        f'{broker_position.quantity}주와 Trader 관리 수량 '
                        f'{state.quantity}주가 다릅니다. 신규 주문을 중단합니다.'
                    )

            is_daytrading_position = (
                self.daytrading_order_quantities.get(stock.code, 0) > 0
                or state.owner == 'daytrading'
            )
            if (
                is_daytrading_position and broker_position.quantity > 0
            ):
                state.owner = 'daytrading'
            elif state.quantity > 0 and not state.owner:
                state.owner = 'mfi'
            if is_daytrading_position and state.quantity > 0:
                if (
                    self.daytrading_monitor_only
                    and now.time() >= DAYTRADING_FORCE_SELL_TIME
                ):
                    action = 'daytrading_end_of_day'
                else:
                    exit_price = (
                        quote.bid
                        if session in ('REGULAR', 'AFTER_MARKET')
                        else quote.price
                    )
                    action = (
                        'daytrading_take_profit'
                        if daytrading_net_profit_rate(
                            exit_price, state.average_price
                        ) >= daytrading_net_profit_target(now)
                        else 'hold'
                    )
            else:
                restrictions = []
                if not self.allow_entries:
                    restrictions.append('감시 전용')
                if session not in ('REGULAR', 'AFTER_MARKET'):
                    restrictions.append('신규 매수 거래시간 외')
                if not stock.selected:
                    restrictions.append('전략 미선정')
                if state.last_exit_date == now.date():
                    restrictions.append('당일 매도 후 재진입 금지')
                if state.exit_reason:
                    restrictions.append(f'청산 대기 {state.exit_reason}')
                restrictions.extend(getattr(stock, 'data_issues', ()))
                if self.daily_price_sync is not None:
                    if self.daily_price_sync.block_reason:
                        restrictions.append(self.daily_price_sync.block_reason)
                if self.daily_data_error:
                    restrictions.append(self.daily_data_error)
                entry_allowed = not restrictions
                if entry_allowed and state.quantity == 0:
                    buy_condition_checks += 1
                self._log_entry_decision(
                    stock, quote, state, session, restrictions
                )
                self._record_decision(stock, quote, state, restrictions)
                action = state.exit_reason or decide_action(
                    stock,
                    quote.price,
                    state,
                    allow_entry=entry_allowed,
                )
                if action == 'buy_signal' and buy_quantity(state, quote.ask) == 0:
                    action = 'hold'
            if state.quantity > 0 and state.peak_price <= 0:
                state.peak_price = max(state.average_price, quote.price)

            if action == 'daytrading_take_profit' and session is None:
                continue

            if action in (
                'trailing_stop', 'sell_signal', 'daytrading_take_profit',
                'daytrading_end_of_day',
            ):
                state.exit_reason = action
                if action == 'trailing_stop':
                    state.stop_triggered = True
                states_to_save[stock.code] = state
                self._sell(stock, quote, state, action)
                row = self._record_decision(stock, quote, state, ['매도 관리 중'])
                row['status'] = 'selling'
                continue

            if action == 'buy_signal':
                buy_signal_count += 1
                result = self._buy(stock, quote, state, broker_position)
                if result is not None:
                    self.cycle_decisions[stock.code].update(result)
                self.cycle_decisions[stock.code]['last_order'] = self.last_orders.get(stock.code)
                self.cycle_decisions[stock.code]['quantity'] = state.quantity
                self.cycle_decisions[stock.code]['budget_left'] = max(
                    0, ALLOCATION_PER_STOCK - state.quantity * state.average_price
                )
                if stock.code in self.states:
                    states_to_save[stock.code] = self.states[stock.code]
                continue

            if state.quantity > 0:
                state.peak_price = max(state.peak_price, quote.price)
                states_to_save[stock.code] = state

        if self.live and states_to_save:
            for code, state in states_to_save.items():
                save_position_state(
                    self.connection, self.creon.account, code, state
                )
        if session in ('REGULAR', 'AFTER_MARKET'):
            print(
                f'[매수조건 확인] 평가 {buy_condition_checks}종목, '
                f'매수신호 {buy_signal_count}건',
                flush=True,
            )

    def _order_cooldown_active(self, code):
        last_order_at = self.last_order_at.get(code, 0.0)
        return time.monotonic() - last_order_at < ORDER_COOLDOWN_SECONDS

    def _buy(self, stock, quote, state, broker_position):
        invested = state.quantity * state.average_price
        budget_left = max(0, ALLOCATION_PER_STOCK - invested)
        order_price = quote.ask
        if buy_quantity(state, order_price) == 0:
            self._log_error(
                stock,
                f'매수 차단: 잔여 종목 한도 {budget_left:,.0f}원, '
                f'매도호가 {order_price:,}원',
            )
            return {'status': 'blocked', 'reasons': buy_budget_reasons(state, order_price), 'order_quantity': 0}

        if self.live:
            if self._order_cooldown_active(stock.code):
                self._log_error(stock, '매수 차단: 주문 재전송 대기 시간')
                return {'status': 'blocked', 'reasons': ['주문 재전송 대기 시간'], 'order_quantity': 0}
            available_cash = self.creon.get_available_cash()
            spend_limit = min(budget_left, available_cash)
        else:
            spend_limit = budget_left

        order_quantity = buy_quantity(state, order_price, spend_limit)
        if order_quantity <= 0:
            self._log_error(
                stock,
                f'매수 신호이나 매수 가능 수량 없음: 종목 잔여 한도 '
                f'{budget_left:,.0f}원, 사용 가능 예산 {spend_limit:,.0f}원, '
                f'매도호가 {order_price:,}원, 비용 여유율 '
                f'{BUY_COST_BUFFER_RATE:.2%}',
            )
            return {'status': 'blocked', 'reasons': [f'계좌 가용 예산 {spend_limit:,.0f}원으로 1주 매수 불가'], 'order_quantity': 0}

        if not self.live:
            expected_amount = order_quantity * order_price
            self._log_order(
                'buy', stock, order_quantity, order_price,
                reason=f'예상 매입액 {expected_amount:,}원 (한도 {ALLOCATION_PER_STOCK:,}원)'
            )
            return {'status': 'dry_run', 'reasons': ['모의 실행: 주문 미전송'], 'order_quantity': order_quantity}

        order_no = self.creon.send_ioc_best_order(
            'buy', stock.code, order_quantity, order_price
        )
        self.last_order_at[stock.code] = time.monotonic()
        updated_position, filled = self._wait_for_fill(
            stock.code, broker_position.quantity, 'buy', order_quantity
        )
        if filled <= 0:
            self._log_error(
                stock, f'최유리 IOC 매수 미체결 (주문번호 {order_no})'
            )
            return {'status': 'unfilled', 'reasons': ['주문 전송 후 체결 미확인'], 'order_quantity': order_quantity}

        old_cost = state.quantity * state.average_price
        state.quantity += filled
        state.average_price = (
            updated_position.average_price
            if updated_position.quantity == state.quantity
            else (old_cost + filled * order_price) / state.quantity
        )
        state.peak_price = max(state.peak_price, state.average_price)
        state.owner = 'mfi'

        cumulative_amount = state.quantity * state.average_price
        self._log_order(
            'buy', stock, filled, order_price,
            reason=f'누적 약정액 {cumulative_amount:,.0f}원',
            order_no=order_no
        )
        if filled < order_quantity:
            print(
                f'{stock.company} ({stock.code}) 매수 일부 체결 '
                f'{filled}/{order_quantity}주; 잔여 예산은 다음 감시 주기에 재주문'
            )
        return {'status': 'filled', 'reasons': [f'매수 {filled}/{order_quantity}주 체결 확인'], 'order_quantity': order_quantity}

    def _sell(self, stock, quote, state, reason):
        if not self.live:
            self._log_signal(stock, quote, state, reason)
            return

        if not state.exit_reason:
            state.exit_reason = reason

        session = trading_session(datetime.now())
        if session is None:
            return

        if self._order_cooldown_active(stock.code):
            return

        position = self.creon.get_positions().get(
            stock.code, BrokerPosition()
        )
        if position.quantity < state.quantity:
            raise RuntimeError(
                f'{stock.company} ({stock.code}) 매도 전 잔고가 Trader 관리 '
                '수량보다 적어 주문을 중단합니다.'
            )

        order_quantity = min(state.quantity, position.quantity)
        if order_quantity <= 0:
            return

        if session == 'AFTER_HOURS_CLOSE':
            pending_orders = self.creon.get_unfilled_orders()
            if any(
                order['code'] == stock.code and order['side'] == 'sell'
                and order['remaining_quantity'] > 0
                for order in pending_orders.values()
            ):
                self._log_error(stock, '시간외 매도 미체결 주문 대기 중')
                self.last_order_at[stock.code] = time.monotonic()
                return

            if session == 'AFTER_HOURS_CLOSE':
                order_price = quote.price
                order_no = self.creon.send_preopen_close_order(
                    'sell', stock.code, order_quantity
                )
            self.last_order_at[stock.code] = time.monotonic()
            self._log_order(
                'sell', stock, order_quantity, order_price,
                reason=f'{reason}, 세션 {session}', order_no=order_no
            )
            return

        order_price = quote.bid
        if order_price <= 0:
            return

        order_no = self.creon.send_ioc_best_order(
            'sell', stock.code, order_quantity, order_price
        )
        self.last_order_at[stock.code] = time.monotonic()
        updated_position, filled = self._wait_for_fill(
            stock.code, position.quantity, 'sell', order_quantity
        )
        if filled <= 0:
            self._log_error(
                stock, f'최유리 IOC 매도 미체결 (주문번호 {order_no})'
            )
            return

        state.quantity = max(0, state.quantity - filled)
        state.last_exit_date = datetime.now().date()
        if state.quantity == 0:
            state.average_price = 0.0
            state.peak_price = 0.0
            state.stop_triggered = False
            state.exit_reason = ''
        elif updated_position.quantity == state.quantity:
            state.average_price = updated_position.average_price

        self._log_order(
            'sell', stock, filled, order_price,
            reason=f'사유 {reason}', order_no=order_no
        )
        if filled < order_quantity:
            print(
                f'{stock.company} ({stock.code}) 매도 일부 체결 '
                f'{filled}/{order_quantity}주; 잔여 수량을 다음 감시 주기에 재주문'
            )

    def _wait_for_fill(self, code, previous_quantity, side, requested_quantity):
        updated_position = BrokerPosition()
        for attempt in range(FILL_CHECK_ATTEMPTS):
            time.sleep(FILL_CHECK_INTERVAL)
            updated_position = self.creon.get_positions().get(
                code, BrokerPosition()
            )
            if side == 'buy':
                filled = updated_position.quantity - previous_quantity
            else:
                filled = previous_quantity - updated_position.quantity
            if filled > 0:
                if filled > requested_quantity:
                    raise RuntimeError(
                        f'{code} 체결 수량 {filled}주가 주문 수량 '
                        f'{requested_quantity}주보다 큽니다.'
                    )
                return updated_position, filled
        return updated_position, 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--live', action='store_true', help='CREON 실계좌 주문을 활성화합니다.'
    )
    parser.add_argument(
        '--confirm', default='', help='실주문 확인 문자열: LIVE'
    )
    parser.add_argument(
        '--once', action='store_true', help='한 번만 신호를 검사하고 종료합니다.'
    )
    parser.add_argument(
        '--monitor-only', action='store_true',
        help='기존 보유 포지션만 관리하고 신규 매수는 하지 않습니다.',
    )
    parser.add_argument(
        '--daytrading-monitor-only', action='store_true',
        help='DayTrading 주문 기록이 있는 종목만 매수 없이 관리합니다.',
    )
    parser.add_argument(
        '--interval', type=float, default=2.0,
        help='실시간 시세 확인 간격(초, 기본값 2)',
    )
    args = parser.parse_args()
    if args.live and args.confirm != 'LIVE':
        parser.error('실주문은 --live --confirm LIVE를 함께 지정해야 합니다.')
    if not args.live and args.confirm:
        parser.error('--confirm은 --live와 함께 사용해야 합니다.')
    if args.monitor_only and not args.live:
        parser.error('--monitor-only는 --live와 함께 사용해야 합니다.')
    if args.daytrading_monitor_only and not args.live:
        parser.error('--daytrading-monitor-only는 --live와 함께 사용해야 합니다.')
    if args.monitor_only and args.daytrading_monitor_only:
        parser.error('감시 모드는 하나만 지정할 수 있습니다.')
    if args.interval < 1:
        parser.error('--interval은 1초 이상이어야 합니다.')
    return args


def print_candidate_summary(
    candidates, live, process_role='mfi', allow_entries=True
):
    selected_count = sum(stock.selected for stock in candidates)
    if process_role == 'daytrading':
        mode_label = 'DayTrading 감시 전용'
    elif live and not allow_entries:
        mode_label = 'MFI 감시 전용'
    else:
        mode_label = 'LIVE 주문' if live else '모의 실행'
    print(
        f'KOSPI 후보 {len(candidates)}종목 중 전략 선정 '
        f'{selected_count}종목 '
        f'| 모드: {mode_label} '
        f'| 종목별 한도: {ALLOCATION_PER_STOCK:,}원'
    )
    if process_role == 'daytrading':
        print(
            '매수: 신규 매수 없음 | '
            f'매도: 예상 왕복 비용 '
            f'{DAYTRADING_ROUND_TRIP_COST_ESTIMATE:.2%} 차감 후 '
            f'순수익률 {DAYTRADING_NET_PROFIT_TARGET:.2%} 이상 '
            '(시간 무관), 15:00부터 전량 청산'
        )
    else:
        buy_rule = (
            f'MFI{MFI_PERIOD}<={MFI_BUY_THRESHOLD}, '
            f'현재가>MA{MA_PERIOD}'
            if allow_entries else '신규 매수 없음 (감시 전용)'
        )
        print(
            f'매수: {buy_rule} | '
            f'매도: MFI{MFI_PERIOD}>={MFI_SELL_THRESHOLD}, '
            f'현재가<MA{MA_PERIOD} | '
            f'트레일링 스탑: 고점 대비 {TRAILING_STOP_RATE:.0%}'
        )
    print(
        f'{"선정":>4}  {"종목명":<20} {"코드":>6} '
        f'{"3M":>8} {"1M":>8} {"MFI10":>7} '
        f'{"%B":>6} {"MFI Δ3":>10}'
    )
    print('-' * 70)
    for stock in candidates:
        print(
            f'{"Y" if stock.selected else "-":>4}  '
            f'{stock.company:<20.20} {stock.code:>6} '
            f'{stock.return_3m:>+7.1%} {stock.return_1m:>+7.1%} '
            f'{stock.mfi10:>7.2f} {stock.percent_b:>6.2f} '
            f'{stock.mfi_change:>+10.2f}'
        )
    print('-' * 70)
    print(
        '[일봉 지표 기준] 오늘 일봉 제외, 완료 일봉 MFI10/MA20 사용; '
        '현재가만 실시간 갱신'
    )
    for stock in candidates:
        data_date = getattr(stock, 'data_date', None)
        required_date = getattr(stock, 'required_date', None)
        reasons = getattr(stock, 'selection_reasons', ())
        print(
            f'[종목 선정 진단] {stock.company} ({stock.code}) '
            f'일봉 {data_date}, 기준 {required_date}, '
            f'이력 {getattr(stock, "history_rows", 0)}개 | '
            f'{"선정" if stock.selected else "; ".join(reasons) or "전략 미선정"}'
        )


def main(args=None):
    args = args or parse_args()
    connection = connect_database()
    lock_name = None
    try:
        candidates = load_kospi_candidates(connection)
        if not args.live:
            print_candidate_summary(candidates, args.live)

        if args.live and not is_market_open(datetime.now()):
            wait_until_market_open()
            connection.commit()
            refreshed_candidates = load_kospi_candidates(connection)
            if refreshed_candidates != candidates:
                print('개장 대기 중 후보가 변경되어 최신 후보를 표시합니다.')
                candidates = refreshed_candidates
                process_role = (
                    'daytrading' if args.daytrading_monitor_only else 'mfi'
                )
                print_candidate_summary(
                    candidates, args.live, process_role,
                    allow_entries=not (
                        args.monitor_only or args.daytrading_monitor_only
                    ),
                )

        if args.live:
            ensure_position_state_table(connection)

        creon = CreonClient(enable_trading=args.live)
        states = {}
        daytrading_order_quantities = {}
        if args.live:
            process_role = (
                'daytrading' if args.daytrading_monitor_only else 'mfi'
            )
            lock_name = acquire_trader_lock(
                connection, creon.account, role=process_role
            )
            positions = creon.get_positions()
            daytrading_order_quantities = load_daytrading_order_codes(
                connection, creon.account
            )
            owner = (
                'daytrading' if args.daytrading_monitor_only else 'mfi'
            )
            owned_codes = load_position_state_codes(
                connection,
                creon.account,
                ('daytrading',) if args.daytrading_monitor_only
                else ('mfi', ''),
            )
            candidate_codes = {stock.code for stock in candidates}
            extra_codes = (
                (
                    set(daytrading_order_quantities) | owned_codes
                ).difference(candidate_codes)
                if args.daytrading_monitor_only
                else owned_codes.difference(candidate_codes)
            )
            if extra_codes:
                candidates = load_kospi_candidates(
                    connection, extra_codes=extra_codes
                )
            all_states = load_position_states(
                connection, creon.account, candidates
            )
            candidates = filter_candidates_by_owner(
                candidates, daytrading_order_quantities, owner, all_states
            )
            print_candidate_summary(
                candidates, args.live, process_role,
                allow_entries=not (
                    args.monitor_only or args.daytrading_monitor_only
                ),
            )
            states = {
                stock.code: all_states.get(stock.code, PositionState())
                for stock in candidates
            }
            for stock in candidates:
                position = positions.get(stock.code, BrokerPosition())
                state = states.get(stock.code)
                if (
                    position.quantity > 0
                    and 0 < position.quantity <= (
                        daytrading_order_quantities.get(stock.code, 0)
                    )
                ):
                    if state is None:
                        state = PositionState()
                    if state.quantity == 0:
                        state.quantity = position.quantity
                        state.average_price = position.average_price
                        state.peak_price = position.average_price
                        states[stock.code] = state
                        save_position_state(
                            connection, creon.account, stock.code, state
                        )
                if state is None:
                    if position.quantity > 0:
                        raise RuntimeError(
                            f'{stock.company} ({stock.code}) 계좌에 기존 '
                            f'{position.quantity}주가 있어 안전을 위해 중단합니다. '
                            'Trader가 관리하는 포지션을 먼저 확인하세요.'
                        )
                    state = PositionState()
                    states[stock.code] = state
                    save_position_state(
                        connection, creon.account, stock.code, state
                    )
                elif position.quantity != state.quantity:
                    if reconcile_daytrading_position_state(
                        state, position
                    ):
                        print(
                            f'[DayTrading 잔고 동기화] {stock.company} '
                            f'({stock.code}) 관리수량을 '
                            f'{state.quantity}주로 조정했습니다.'
                        )
                        save_position_state(
                            connection, creon.account, stock.code, state
                        )
                    else:
                        raise RuntimeError(
                            f'{stock.company} ({stock.code}) 계좌 잔고 '
                            f'{position.quantity}주와 저장된 Trader 수량 '
                            f'{state.quantity}주가 다릅니다.'
                        )

        trader = Trader(
            connection, creon, candidates, args.live, states,
            daytrading_order_quantities if args.live else {},
            allow_entries=not (
                args.monitor_only or args.daytrading_monitor_only
            ),
            daytrading_monitor_only=args.daytrading_monitor_only,
            exclude_daytrading_positions=(
                args.live and not args.daytrading_monitor_only
            ),
            daily_price_sync=(
                DailyPriceSynchronizer(connection)
                if args.live and not (
                    args.monitor_only or args.daytrading_monitor_only
                )
                else None
            ),
            status_path=(
                STATUS_PATH if args.live and not args.daytrading_monitor_only else None
            ),
        )
        if not args.live:
            print('DRY RUN: 주문을 전송하지 않습니다.')

        while True:
            if args.live and not is_market_open(datetime.now()):
                print('KRX 애프터마켓 거래가 종료되어 Trader를 종료합니다.')
                break
            trader.run_cycle()
            if not args.live or args.once:
                break
            time.sleep(args.interval)
    finally:
        if lock_name is not None:
            release_trader_lock(connection, lock_name)
        if connection is not None:
            connection.close()


def run_trader(args, logger):
    try:
        main(args)
    except KeyboardInterrupt:
        print('\n사용자 요청으로 Trader를 중지했습니다.')
    except TraderAlreadyRunningError as error:
        message = f'{error} 신규 실행을 건너뜁니다.'
        print(message)
    else:
        logger.info('Trader process finished normally')


if __name__ == '__main__':
    args = parse_args()
    process_role = (
        'daytrading' if args.daytrading_monitor_only else 'mfi'
    )
    logger = configure_trader_logging(process_role)
    run_trader(args, logger)