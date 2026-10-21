"""Repair completed daily bars using the existing Investar CREON collector."""

import importlib.util
import math
import time
from collections import deque
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol

import pandas as pd
import pymysql


DAILY_REFRESH_SECONDS = 300
DAILY_REPAIR_BATCH_SIZE = 3
DAILY_HISTORY_BARS = 180
REFERENCE_CODE = '005930'


class DailyPriceSource(Protocol):
    def get_stock_price_day(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        ...


@lru_cache(maxsize=1)
def load_investar_updater():
    path = (
        Path(__file__).resolve().parents[1]
        / '05_Stock_Price_API' / 'Investar' / 'DBUpdaterEx.py'
    )
    spec = importlib.util.spec_from_file_location('mfi_daily_updater', path)
    if spec is None or spec.loader is None:
        raise ImportError(f'일봉 수집기를 불러올 수 없습니다: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CreonDailyPriceSource:
    def __init__(self):
        import pywintypes

        self.com_error = pywintypes.com_error
        try:
            self.collector = load_investar_updater().CREON()
        except self.com_error as exc:
            raise RuntimeError(f'CREON 일봉 수집 초기화 실패: {exc}') from exc

    def get_stock_price_day(self, code, start_date, end_date):
        try:
            return self.collector.get_stock_price_day(code, start_date, end_date)
        except self.com_error as exc:
            raise RuntimeError(f'{code} CREON 일봉 조회 실패: {exc}') from exc


def completed_daily_prices(prices, today):
    required = ['date', 'open', 'high', 'low', 'close', 'volume']
    if not set(required).issubset(prices.columns):
        raise ValueError('CREON 일봉 응답에 필수 컬럼이 없습니다.')
    frame = prices[required].copy()
    frame['date'] = pd.to_datetime(
        frame['date'].astype(str).str.replace('-', '', regex=False),
        format='%Y%m%d',
        errors='raise',
    )
    frame = frame[frame['date'].dt.date < today].sort_values('date')
    if frame['date'].duplicated().any():
        raise ValueError('CREON 일봉 응답에 중복 날짜가 있습니다.')
    for column in required[1:]:
        frame[column] = pd.to_numeric(frame[column], errors='raise')
        if not frame[column].map(
            lambda value: math.isfinite(value) and value >= 0 and value == int(value)
        ).all():
            raise ValueError(f'CREON 일봉 {column}에 유효하지 않은 값이 있습니다.')
    if (
        (frame['close'] <= 0)
        | (frame['high'] < frame[['open', 'low', 'close']].max(axis=1))
        | (frame['low'] > frame[['open', 'high', 'close']].min(axis=1))
    ).any():
        raise ValueError('CREON 일봉 OHLC 가격 범위가 유효하지 않습니다.')
    return frame


class DailyPriceSynchronizer:
    def __init__(
        self, connection,
        source_factory: Callable[[], DailyPriceSource] = CreonDailyPriceSource,
    ):
        self.connection = connection
        self.source_factory = source_factory
        self.source = None
        self.required_date = None
        self.required_dates = ()
        self.verified_for = None
        self.block_reason = '완료 일봉 기준 거래일 확인 전'
        self.next_refresh = 0.0
        self.pending = deque()

    def _scan(self, candidates, now):
        if self.source is None:
            self.source = self.source_factory()
        reference = completed_daily_prices(
            self.source.get_stock_price_day(
                'A' + REFERENCE_CODE,
                (now.date() - timedelta(days=400)).isoformat(),
                (now.date() - timedelta(days=1)).isoformat(),
            ),
            now.date(),
        ).tail(DAILY_HISTORY_BARS)
        if reference.empty:
            raise RuntimeError('CREON 기준 종목의 완료 일봉 응답이 없습니다.')
        self.required_dates = tuple(reference['date'].dt.date)
        self.required_date = self.required_dates[-1]
        codes = {stock.code: stock.company for stock in candidates}
        if not codes:
            raise RuntimeError('일봉 최신성을 확인할 후보 종목이 없습니다.')
        placeholders = ', '.join(['%s'] * len(codes))
        with self.connection.cursor() as cursor:
            cursor.execute(
                'SELECT code, date FROM daily_price '
                f'WHERE code IN ({placeholders}) AND date BETWEEN %s AND %s',
                (*codes, self.required_dates[0], self.required_date),
            )
            coverage = {}
            for code, day in cursor.fetchall():
                coverage.setdefault(code, set()).add(day)
        self.pending.clear()
        for code, company in codes.items():
            stored = coverage.get(code, set())
            first_date = (
                min(stored) if len(stored) >= 64
                else self.required_dates[0]
            )
            missing = [
                day for day in self.required_dates
                if day >= first_date and day not in stored
            ]
            if missing:
                self.pending.append((code, company, tuple(missing)))
                print(
                    f'[일봉 보충 대기] {company} ({code}) '
                    f'최근 {max(stored) if stored else "없음"}, '
                    f'기준 {self.required_date}, 누락 {len(missing)}거래일; '
                    '보충 확인 전 신규 매수 차단',
                    flush=True,
                )
        self.verified_for = now.date()
        self.block_reason = ''
        print(
            f'[일봉 최신성 확인] 기준 {self.required_date} '
            f'(CREON {REFERENCE_CODE}, 오늘 일봉 제외), '
            f'대상 {len(codes)}종목, 보충 대기 {len(self.pending)}종목',
            flush=True,
        )

    def step(self, candidates, now):
        changed = False
        if time.monotonic() >= self.next_refresh or self.verified_for != now.date():
            # End the previous read snapshot so externally collected bars are visible.
            self.next_refresh = time.monotonic() + DAILY_REFRESH_SECONDS
            try:
                self.connection.commit()
                self._scan(candidates, now)
            except (ImportError, RuntimeError, ValueError, pymysql.MySQLError) as exc:
                self.block_reason = f'기준 거래일/DB 최신성 확인 실패: {exc}'
                self.pending.clear()
                print(
                    f'[일봉 갱신 오류] {self.block_reason}; 신규 매수 차단, '
                    f'{DAILY_REFRESH_SECONDS}초 후 재시도, 보유 종목 매도 감시 유지',
                    flush=True,
                )
                self.verified_for = now.date()
                return False
            changed = True
        if self.block_reason:
            return changed
        source = self.source
        required_date = self.required_date
        if source is None or required_date is None:
            raise RuntimeError('일봉 최신성 확인 없이 보충을 실행할 수 없습니다.')
        for _ in range(min(DAILY_REPAIR_BATCH_SIZE, len(self.pending))):
            code, company, missing_dates = self.pending.popleft()
            first_missing = missing_dates[0]
            try:
                prices = completed_daily_prices(
                    source.get_stock_price_day(
                        code if code.startswith('Q') else 'A' + code,
                        (first_missing - timedelta(days=10)).isoformat(),
                        required_date.isoformat(),
                    ),
                    now.date(),
                )
                prices = prices[prices['date'].dt.date <= required_date].copy()
                if prices.empty:
                    raise RuntimeError('보충할 완료 일봉 응답이 없습니다.')
                prices['diff'] = prices['close'].diff().abs().fillna(0)
                prices = prices[prices['date'].dt.date >= first_missing].copy()
                if prices.empty:
                    raise RuntimeError('누락 구간의 일봉 응답이 없습니다.')
                prices['date'] = prices['date'].dt.date
                inserted = load_investar_updater().insert_missing_daily_prices(
                    self.connection, prices, code
                )
                self.connection.commit()
                changed = True
                with self.connection.cursor() as cursor:
                    cursor.execute(
                        'SELECT date FROM daily_price '
                        'WHERE code = %s AND date BETWEEN %s AND %s',
                        (code, first_missing, required_date),
                    )
                    stored_dates = {row[0] for row in cursor.fetchall()}
                remaining = set(missing_dates).difference(stored_dates)
                if remaining:
                    print(
                        f'[일봉 보충 미완료] {company} ({code}) '
                        f'신규 {inserted}행, 누락 {len(remaining)}거래일 잔여; '
                        '신규 매수 차단 유지, 다음 최신성 확인에서 재시도',
                        flush=True,
                    )
                    continue
                print(
                    f'[일봉 보충 완료] {company} ({code}) '
                    f'{first_missing}~{required_date}, 신규 {inserted}행; '
                    'DB 재조회 후 최신성·선정 조건 재평가',
                    flush=True,
                )
            except (RuntimeError, ValueError, pymysql.MySQLError) as exc:
                self.connection.rollback()
                print(
                    f'[일봉 보충 실패] {company} ({code}): {exc}; '
                    '신규 매수 차단 유지, 다음 최신성 확인에서 재시도',
                    flush=True,
                )
        return changed
