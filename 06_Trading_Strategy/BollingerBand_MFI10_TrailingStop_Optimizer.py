"""Optimize trailing stops against historical minute bars; never places orders."""

import argparse
import importlib.util
import os
from bisect import bisect_right
from datetime import datetime
from pathlib import Path

import pandas as pd
import pymysql

from BollingerBand_MFI10_Selection import (
    TOP_STOCK_COUNT,
    TRANSACTION_COST_RATE,
    load_candidate_snapshots,
    prepare_selection_indicators,
    rank_daily_top_stocks,
)


WARMUP_DAYS = 220
MA_PERIOD = 20
BUY_MFI = 50
SELL_MFI = 80
DEFAULT_START = '2026-04-01'
PASSWORD = "root"
MINIMUM_OPTIMIZATION_DAYS = 10
MIN_VALID_MINUTE_BARS = 350
MINUTES_PER_TRADING_DAY = 390
MAX_MINUTE_HISTORY_DAYS = 730
SESSION_START_MINUTE = 9 * 60
SESSION_END_MINUTE = 15 * 60 + 30
STOP_CANDIDATES = [
    ('percent', rate) for rate in (0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10)
] + [
    ('atr', multiple) for multiple in (1.5, 2.0, 2.5, 3.0)
] + [
    ('chandelier', multiple) for multiple in (2.0, 2.5, 3.0, 3.5)
]


def connect_database():
    return pymysql.connect(
        host=os.environ.get('STOCK_DB_HOST', 'localhost'),
        user=os.environ.get('STOCK_DB_USER', 'root'),
        password=PASSWORD,
        database=os.environ.get('STOCK_DB_NAME', 'zunDB'),
        charset='utf8',
        autocommit=False,
    )


def ensure_optimizer_tables(connection):
    statements = [
        """CREATE TABLE IF NOT EXISTS minute_price (
            code VARCHAR(20), date DATE, trade_time TIME,
            open BIGINT(20), high BIGINT(20), low BIGINT(20),
            close BIGINT(20), volume BIGINT(20),
            PRIMARY KEY (code, date, trade_time)
        )""",
        """CREATE TABLE IF NOT EXISTS trailing_stop_minute_checkpoint (
            code VARCHAR(20) NOT NULL,
            trade_date DATE NOT NULL,
            bar_count SMALLINT UNSIGNED NOT NULL,
            collected_at DATETIME NOT NULL,
            PRIMARY KEY (code, trade_date)
        )""",
    ]
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    connection.commit()


def count_minute_trading_days(connection, codes, start_date, end_date):
    if not codes:
        return 0
    placeholders = ','.join(['%s'] * len(codes))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(DISTINCT valid_days.date) FROM ("
            "SELECT code, date FROM minute_price "
            f"WHERE code IN ({placeholders}) AND date BETWEEN %s AND %s "
            "AND trade_time BETWEEN '09:00:00' AND '15:30:00' "
            "GROUP BY code, date "
            "HAVING COUNT(*) >= %s "
            "AND COUNT(*) = COUNT(DISTINCT trade_time) "
            "AND MIN(trade_time) <= '09:05:00' "
            "AND MAX(trade_time) >= '15:20:00' "
            "AND SUM(open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 "
            "OR volume < 0 OR high < GREATEST(open, low, close) "
            "OR low > LEAST(open, high, close)) = 0"
            ") AS valid_days",
            (*codes, start_date.date(), end_date.date(), MIN_VALID_MINUTE_BARS),
        )
        row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def validate_minute_day(minute, trade_date, date_column='date', time_column='time'):
    if minute.empty:
        return minute, '분봉 응답이 비어 있습니다.'

    minute = minute.copy()
    expected_date = pd.Timestamp(trade_date).date()
    if date_column in minute.columns:
        date_values = minute[date_column]
        if pd.api.types.is_numeric_dtype(date_values):
            date_mask = pd.to_numeric(date_values, errors='coerce').eq(
                int(expected_date.strftime('%Y%m%d'))
            )
        else:
            date_mask = pd.to_datetime(date_values, errors='coerce').dt.date.eq(expected_date)
        minute = minute.loc[date_mask].copy()
    if minute.empty:
        return minute, '요청 날짜와 일치하는 분봉이 없습니다.'

    if time_column == 'time':
        raw_times = pd.to_numeric(minute[time_column], errors='coerce')
        hours = raw_times // 100
        minutes = raw_times % 100
        minute_of_day = hours * 60 + minutes
        valid_times = raw_times.notna() & minutes.lt(60) & hours.lt(24)
    else:
        durations = pd.to_timedelta(minute[time_column].astype(str), errors='coerce')
        minute_of_day = durations.dt.total_seconds() / 60
        valid_times = durations.notna()

    session_mask = (
        valid_times
        & minute_of_day.ge(SESSION_START_MINUTE)
        & minute_of_day.le(SESSION_END_MINUTE)
    )
    minute = minute.loc[session_mask].copy()
    minute_of_day = minute_of_day.loc[session_mask]
    if minute_of_day.duplicated().any():
        return minute, '정규장 분봉에 중복 시각이 있습니다.'

    prices = minute[['open', 'high', 'low', 'close', 'volume']].apply(
        pd.to_numeric, errors='coerce'
    )
    if prices.isna().any().any() or prices.isin([float('inf'), float('-inf')]).any().any():
        return minute, '가격 또는 거래량에 숫자가 아닌 값이 있습니다.'
    if (
        prices[['open', 'high', 'low', 'close']].le(0).any().any()
        or prices['volume'].lt(0).any()
        or prices['high'].lt(prices[['open', 'low', 'close']].max(axis=1)).any()
        or prices['low'].gt(prices[['open', 'high', 'close']].min(axis=1)).any()
    ):
        return minute, 'OHLC 또는 거래량 범위를 벗어난 분봉이 있습니다.'

    if len(minute) < MIN_VALID_MINUTE_BARS:
        return minute, f'정규장 분봉이 {len(minute)}개로 기준 {MIN_VALID_MINUTE_BARS}개 미만입니다.'
    if minute_of_day.min() > 9 * 60 + 5 or minute_of_day.max() < 15 * 60 + 20:
        return minute, '분봉의 장 초반 또는 장 마감 구간이 누락되었습니다.'
    return minute, None


def create_creon_client():
    updater_path = (
        Path(__file__).resolve().parents[1]
        / '05_Stock_Price_API'
        / 'Investar'
        / 'DBUpdaterEx.py'
    )
    spec = importlib.util.spec_from_file_location(
        'stock_db_updater_for_stop_optimizer', updater_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f'CREON 수집기를 불러올 수 없습니다: {updater_path}')
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    creon = updater.CREON()
    creon.stock_codes_by_db_code = {}
    for market in (1, 2):
        for creon_code in creon.objCodeMgr.GetStockListByMarket(market):
            db_code = creon_code[1:] if creon_code.startswith('A') else creon_code
            creon.stock_codes_by_db_code[db_code] = creon_code
    return creon


def _snapshot_codes_for_date(snapshots, snapshot_dates, date):
    snapshot_index = bisect_right(snapshot_dates, date) - 1
    if snapshot_index < 0:
        return []
    return snapshots[snapshot_dates[snapshot_index]]


def load_daily_inputs(
    connection, market, start_date, end_date,
    candidate_source='snapshot', buy_mfi=BUY_MFI,
):
    warmup_start = (start_date - pd.Timedelta(days=WARMUP_DAYS)).date()
    snapshots, companies, latest_snapshot_update = load_candidate_snapshots(
        connection, market, warmup_start, end_date.date()
    )
    if candidate_source == 'snapshot' and not snapshots:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT MIN(snapshot_date), MAX(snapshot_date), COUNT(*) "
                "FROM selected_stock_history WHERE market = %s",
                (market,),
            )
            history_start, history_end, history_count = cursor.fetchone()
        if history_count and history_end > end_date.date():
            raise ValueError(
                f'{market} 일봉 데이터는 {end_date:%Y-%m-%d}까지지만 '
                f'저장된 후보 스냅샷은 {history_start:%Y-%m-%d}~'
                f'{history_end:%Y-%m-%d}입니다. 스냅샷 이후 거래일의 '
                '일봉을 DBUpdaterEx로 갱신한 뒤 다시 실행하세요. '
                '현재 DB에는 스냅샷 이후 일봉이 없어 유효한 최적화 구간이 없습니다.'
            )
        if history_count:
            raise ValueError(
                f'{market} 후보 스냅샷 이력은 {history_start:%Y-%m-%d}~'
                f'{history_end:%Y-%m-%d}이며, 분석 구간 '
                f'{warmup_start:%Y-%m-%d}~{end_date:%Y-%m-%d}와 겹치지 않습니다.'
            )
        raise ValueError(
            f'{market} selected_stock_history 스냅샷이 없습니다. '
            'DBUpdaterEx를 실행해 selected_stock_history를 저장하세요.'
        )

    if candidate_source == 'snapshot':
        snapshot_dates = sorted(snapshots)
        snapshot_start = snapshot_dates[0]
        effective_start = max(start_date.normalize(), snapshot_start)
        if effective_start > start_date.normalize():
            print(
                f'주의: 스냅샷 이력이 {snapshot_start:%Y-%m-%d}부터라 '
                f'시작일을 {effective_start:%Y-%m-%d}로 조정합니다.'
            )
        codes = sorted({code for values in snapshots.values() for code in values})
    else:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT code FROM selected_stock_history "
                "WHERE market = %s ORDER BY code",
                (market,),
            )
            codes = [str(row[0]) for row in cursor.fetchall()]
        if not codes:
            raise ValueError(
                f'{market} 일봉 후보 종목이 없습니다. '
                'selected_stock_history 또는 daily_price를 먼저 채우세요.'
            )
        snapshots = {}
        snapshot_dates = []
        companies = {}
        latest_snapshot_update = None
        effective_start = start_date.normalize()

    placeholders = ','.join(['%s'] * len(codes))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT code, date, open, high, low, close, volume "
            f"FROM daily_price WHERE code IN ({placeholders}) "
            "AND date BETWEEN %s AND %s ORDER BY code, date",
            (*codes, warmup_start, end_date.date()),
        )
        rows = cursor.fetchall()
    if not rows:
        raise ValueError('후보 종목의 daily_price 데이터가 없습니다.')

    frame = pd.DataFrame(
        rows, columns=['code', 'date', 'open', 'high', 'low', 'close', 'volume']
    )
    frame['code'] = frame['code'].astype(str)
    frame['date'] = pd.to_datetime(frame['date']).dt.normalize()
    histories = {}
    for code, code_rows in frame.groupby('code', sort=False):
        prices = code_rows.drop(columns='code').set_index('date').sort_index()
        histories[code] = prepare_selection_indicators(prices)

    memberships = rank_daily_top_stocks(
        histories,
        candidate_snapshots=(snapshots if candidate_source == 'snapshot' else None),
    )
    daily_signals = {}
    daily_features = {}
    for code, prices in histories.items():
        ma = prices['close'].rolling(MA_PERIOD).mean()
        buy = (
            prices['mfi10'].le(buy_mfi)
            & prices['close'].gt(ma)
            & memberships[code]
            & prices.index.to_series(index=prices.index).ge(effective_start)
        )
        sell = prices['mfi10'].ge(SELL_MFI) & prices['close'].lt(ma)

        true_range = pd.concat(
            [
                prices['high'] - prices['low'],
                (prices['high'] - prices['close'].shift()).abs(),
                (prices['low'] - prices['close'].shift()).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = true_range.rolling(14, min_periods=14).mean()
        chandelier_high = prices['high'].rolling(22, min_periods=22).max()
        daily_signals[code] = {'buy': buy, 'sell': sell}
        daily_features[code] = {
            'atr': atr,
            'chandelier_high': chandelier_high,
        }

    minute_codes = codes
    if candidate_source == 'daily':
        candidate_pool = set()
        for code, membership in memberships.items():
            in_period = membership.loc[
                (membership.index >= effective_start)
                & (membership.index <= end_date)
            ]
            if in_period.any():
                candidate_pool.add(code)
        minute_codes = sorted(candidate_pool)
        if not minute_codes:
            raise ValueError(
                f'{start_date:%Y-%m-%d}~{end_date:%Y-%m-%d} 구간에서 '
                '일봉 기술지표 후보가 없습니다. 일봉 범위와 종목군을 확인하세요.'
            )
        collection_snapshot_date = effective_start - pd.Timedelta(days=1)
        snapshots = {collection_snapshot_date: minute_codes}
        snapshot_dates = [collection_snapshot_date]
        print(
            f'일봉 기술지표 후보 모드: 저장된 {market} 종목군 {len(codes)}개 중 '
            f'기간 후보 {len(minute_codes)}개',
            flush=True,
        )

    return {
        'histories': histories,
        'daily_signals': daily_signals,
        'daily_features': daily_features,
        'snapshots': snapshots,
        'snapshot_dates': snapshot_dates,
        'companies': companies,
        'latest_snapshot_update': latest_snapshot_update,
        'start_date': effective_start,
        'codes': codes,
        'minute_codes': minute_codes,
    }


def collect_minute_data(connection, creon, daily_inputs, start_date, end_date, max_requests):
    dates = sorted({
        date
        for prices in daily_inputs['histories'].values()
        for date in prices.index
        if start_date <= date <= end_date
    })
    snapshots = daily_inputs['snapshots']
    snapshot_dates = daily_inputs['snapshot_dates']
    requests = set()
    for date in dates:
        prior_date = date - pd.Timedelta(days=1)
        active_codes = _snapshot_codes_for_date(
            snapshots, snapshot_dates, prior_date
        )
        requests.update((str(code), date.date()) for code in active_codes)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT code, date, COUNT(*), COUNT(DISTINCT trade_time), "
            "MIN(trade_time) <= '09:05:00', MAX(trade_time) >= '15:20:00', "
            "SUM(open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 OR volume < 0 "
            "OR high < GREATEST(open, low, close) "
            "OR low > LEAST(open, high, close)) "
            "FROM minute_price WHERE date BETWEEN %s AND %s "
            "AND trade_time BETWEEN '09:00:00' AND '15:30:00' "
            "GROUP BY code, date",
            (start_date.date(), end_date.date()),
        )
        completed = {
            (str(code), pd.Timestamp(date).date())
            for code, date, count, distinct_count, starts_early, ends_late, invalid_count
            in cursor.fetchall()
            if count >= MIN_VALID_MINUTE_BARS
            and count == distinct_count
            and starts_early
            and ends_late
            and not invalid_count
        }
    pending = sorted(requests - completed, key=lambda item: (item[1], item[0]))
    if max_requests <= 0:
        raise ValueError('--max-requests는 1 이상이어야 합니다.')
    pending_by_code = {}
    for code, date in pending:
        pending_by_code.setdefault(code, set()).add(date)

    print(
        f'분봉 수집: 대상 {len(requests)}건, 완료 {len(completed & requests)}건, '
        f'이번 요청 {len(pending)}종목-날짜 ({len(pending_by_code)}종목)',
        flush=True,
    )
    if pending_by_code and creon is None:
        creon = create_creon_client()

    def save_checkpoint(code, date, bar_count):
        with connection.cursor() as cursor:
            cursor.execute(
                "REPLACE INTO trailing_stop_minute_checkpoint "
                "(code, trade_date, bar_count, collected_at) "
                "VALUES (%s, %s, %s, %s)",
                (code, date, bar_count, datetime.now()),
            )
        connection.commit()

    requests_made = 0
    today = datetime.now().date()
    for number, (code, target_dates) in enumerate(
        sorted(pending_by_code.items()), start=1
    ):
        creon_code = creon.stock_codes_by_db_code.get(code)
        if creon_code is None:
            print(f'[{number}/{len(pending_by_code)}] {code} CREON 코드 없음, 건너뜀')
            continue
        if requests_made >= max_requests:
            print(f'분봉 수집 요청 상한 {max_requests}회에 도달했습니다.')
            break

        oldest_date = min(target_dates)
        history_days = min(
            max(1, (today - oldest_date).days + 1),
            MAX_MINUTE_HISTORY_DAYS,
        )
        requested_count = history_days * MINUTES_PER_TRADING_DAY
        try:
            minute, page_requests, history_complete = (
                creon.get_stock_price_min_by_count(
                    creon_code,
                    requested_count,
                    max_requests - requests_made,
                )
            )
            requests_made += page_requests
        except Exception as exc:
            connection.rollback()
            print(f'[{number}/{len(pending_by_code)}] {code} 수집 실패: {exc}')
            continue

        if not minute.empty:
            returned_dates = pd.to_numeric(minute['date'], errors='coerce')
            for date in sorted(target_dates):
                day_minute = minute.loc[
                    returned_dates.eq(int(date.strftime('%Y%m%d')))
                ].copy()
                day_minute, invalid_reason = validate_minute_day(day_minute, date)
                if invalid_reason:
                    if not day_minute.empty:
                        print(f'{code} {date} 유효하지 않은 분봉: {invalid_reason}')
                    continue

                times = day_minute['time'].astype(int).map(lambda value: f'{value:04d}')
                trade_times = times.str[:2] + ':' + times.str[2:] + ':00'
                values = [
                    (
                        code, date, trade_time, int(row.open), int(row.high),
                        int(row.low), int(row.close), int(row.volume),
                    )
                    for trade_time, row in zip(
                        trade_times,
                        day_minute[['open', 'high', 'low', 'close', 'volume']]
                        .itertuples(index=False),
                    )
                ]
                try:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            "REPLACE INTO minute_price "
                            "(code, date, trade_time, open, high, low, close, volume) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            values,
                        )
                        cursor.execute(
                            "REPLACE INTO trailing_stop_minute_checkpoint "
                            "(code, trade_date, bar_count, collected_at) "
                            "VALUES (%s, %s, %s, %s)",
                            (code, date, len(values), datetime.now()),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

        if not history_complete:
            print(
                f'{code}: 요청 예산이 소진되어 분봉 이력을 일부만 받았습니다. '
                '다시 실행하면 미완료 날짜를 이어서 수집합니다.'
            )
        if number % 25 == 0 or number == len(pending_by_code):
            print(
                f'분봉 수집 진행: {number}/{len(pending_by_code)}종목, '
                f'API 요청 {requests_made}/{max_requests}',
                flush=True,
            )
        if requests_made >= max_requests:
            print(f'분봉 수집 요청 상한 {max_requests}회에 도달했습니다.')
            break


def load_minute_histories(connection, daily_inputs, start_date, end_date):
    codes = daily_inputs.get('minute_codes', daily_inputs['codes'])
    placeholders = ','.join(['%s'] * len(codes))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT code, date, trade_time, open, high, low, close, volume "
            f"FROM minute_price WHERE code IN ({placeholders}) "
            "AND date BETWEEN %s AND %s ORDER BY code, date, trade_time",
            (*codes, start_date.date(), end_date.date()),
        )
        rows = cursor.fetchall()
    if not rows:
        raise ValueError(
            '해당 기간의 minute_price가 없습니다. --collect-minute으로 수집하세요.'
        )
    frame = pd.DataFrame(
        rows,
        columns=['code', 'date', 'trade_time', 'open', 'high', 'low', 'close', 'volume'],
    )
    frame['code'] = frame['code'].astype(str)
    frame['timestamp'] = (
        pd.to_datetime(frame['date'])
        + pd.to_timedelta(frame['trade_time'].astype(str))
    )
    minute_histories = {}
    minute_signals = {}
    for code, code_rows in frame.groupby('code', sort=False):
        valid_days = []
        for trade_date, day_rows in code_rows.groupby('date', sort=True):
            valid_day, invalid_reason = validate_minute_day(
                day_rows, pd.Timestamp(str(trade_date)),
                date_column='date', time_column='trade_time',
            )
            if invalid_reason is None:
                valid_days.append(valid_day)
        if not valid_days:
            continue
        code_rows = pd.concat(valid_days)
        prices = code_rows.set_index('timestamp')[
            ['open', 'high', 'low', 'close', 'volume']
        ].sort_index()
        prices = prices.loc[
            (prices.index.normalize() >= start_date)
            & (prices.index.normalize() <= end_date)
        ].copy()
        if prices.empty:
            continue

        daily = daily_inputs['histories'][code]
        features = daily_inputs['daily_features'][code]
        dates = prices.index.normalize()
        previous_positions = daily.index.searchsorted(dates, side='left') - 1
        valid_previous = previous_positions >= 0
        prices['atr'] = float('nan')
        prices['chandelier_high'] = float('nan')
        if valid_previous.any():
            previous_dates = daily.index.take(previous_positions[valid_previous])
            prices.loc[valid_previous, 'atr'] = (
                features['atr'].reindex(previous_dates).to_numpy()
            )
            prices.loc[valid_previous, 'chandelier_high'] = (
                features['chandelier_high'].reindex(previous_dates).to_numpy()
            )
        buy = pd.Series(False, index=prices.index)
        sell = pd.Series(False, index=prices.index)
        signals = daily_inputs['daily_signals'][code]
        for _, day_rows in prices.groupby(prices.index.normalize(), sort=True):
            signal_date = day_rows.index[0].normalize()
            if signal_date in signals['buy'].index:
                last_timestamp = day_rows.index[-1]
                buy.loc[last_timestamp] = bool(signals['buy'].loc[signal_date])
                sell.loc[last_timestamp] = bool(signals['sell'].loc[signal_date])
        minute_histories[code] = prices
        minute_signals[code] = {'buy': buy, 'sell': sell}
    if not minute_histories:
        raise ValueError('일봉 후보와 겹치는 분봉 데이터가 없습니다.')
    return minute_histories, minute_signals


def simulate_stop_strategy(
    histories,
    signals,
    stop_kind,
    stop_parameter,
    max_positions=TOP_STOCK_COUNT,
    transaction_cost=TRANSACTION_COST_RATE,
    slippage=0.001,
):
    timestamps = sorted(set().union(*(set(history.index) for history in histories.values())))
    cash = 1.0
    positions = {}
    exited_today = set()
    equity = []
    trade_returns = []
    buy_flags = {
        code: signals[code]['buy'].reindex(history.index, fill_value=False).shift(1).fillna(False)
        for code, history in histories.items()
    }
    sell_flags = {
        code: signals[code]['sell'].reindex(history.index, fill_value=False).shift(1).fillna(False)
        for code, history in histories.items()
    }
    current_day = None

    for timestamp in timestamps:
        day = timestamp.normalize()
        if day != current_day:
            current_day = day
            exited_today.clear()

        for code in list(positions):
            prices = histories[code]
            if timestamp not in prices.index:
                continue
            row = prices.loc[timestamp]
            position = positions[code]
            atr = float(row['atr']) if pd.notna(row['atr']) else None
            stop_price = position['stop_price']
            if stop_kind == 'percent':
                candidate_stop = position['peak_price'] * (1 - stop_parameter)
            elif stop_kind == 'atr':
                candidate_stop = (
                    position['peak_price'] - stop_parameter * atr
                    if atr is not None else None
                )
            else:
                chandelier_high = (
                    float(row['chandelier_high'])
                    if pd.notna(row['chandelier_high']) else None
                )
                candidate_stop = (
                    min(position['peak_price'], chandelier_high) - stop_parameter * atr
                    if atr is not None and chandelier_high is not None else None
                )
            if candidate_stop is not None:
                stop_price = max(stop_price or 0, candidate_stop)
                position['stop_price'] = stop_price

            exit_price = None
            reason = None
            if stop_price is not None and float(row['open']) <= stop_price:
                exit_price = float(row['open'])
                reason = 'trailing_stop_gap'
            elif stop_price is not None and float(row['low']) <= stop_price:
                exit_price = stop_price
                reason = 'trailing_stop'
            elif bool(sell_flags[code].get(timestamp, False)):
                exit_price = float(row['open'])
                reason = 'sell_signal'

            if exit_price is not None:
                execution_price = exit_price * (1 - slippage)
                proceeds = position['shares'] * execution_price * (1 - transaction_cost)
                trade_returns.append(proceeds / position['cost_basis'] - 1)
                cash += proceeds
                del positions[code]
                exited_today.add(code)
            else:
                position['peak_price'] = max(position['peak_price'], float(row['high']))
                position['last_price'] = float(row['close'])

        eligible = [
            code for code in histories
            if code not in positions
            and code not in exited_today
            and timestamp in histories[code].index
            and bool(buy_flags[code].get(timestamp, False))
        ]
        eligible.sort()
        slots = max_positions - len(positions)
        nav = cash + sum(
            position['shares'] * position['last_price']
            for position in positions.values()
        )
        budget_per_slot = nav / max_positions
        for code in eligible[:slots]:
            row = histories[code].loc[timestamp]
            open_price = float(row['open'])
            if open_price <= 0 or cash <= 0:
                continue
            budget = min(cash, budget_per_slot)
            execution_price = open_price * (1 + slippage)
            shares = budget / (execution_price * (1 + transaction_cost))
            cash -= budget
            positions[code] = {
                'shares': shares,
                'cost_basis': budget,
                'entry_price': execution_price,
                'peak_price': open_price,
                'last_price': float(row['close']),
                'stop_price': None,
            }

        for code, position in positions.items():
            if timestamp in histories[code].index:
                row = histories[code].loc[timestamp]
                position['peak_price'] = max(position['peak_price'], float(row['high']))
                position['last_price'] = float(row['close'])
        equity.append((timestamp, cash + sum(
            position['shares'] * position['last_price']
            for position in positions.values()
        )))

    equity_frame = pd.DataFrame(equity, columns=['timestamp', 'value']).set_index('timestamp')
    daily_equity = equity_frame['value'].groupby(equity_frame.index.normalize()).last()
    return daily_equity, trade_returns


def _period_metrics(daily_equity, start, end, initial_value):
    values = daily_equity.loc[(daily_equity.index >= start) & (daily_equity.index <= end)]
    if values.empty:
        return {'return_pct': float('nan'), 'max_drawdown_pct': float('nan'), 'calmar': float('nan')}
    series = pd.concat([pd.Series([initial_value], index=[start - pd.Timedelta(days=1)]), values])
    returns = series.iloc[-1] / series.iloc[0] - 1
    drawdown = series / series.cummax() - 1
    max_drawdown = abs(float(drawdown.min()))
    periods = max(1, len(values))
    annualized = (1 + returns) ** (252 / periods) - 1 if returns > -1 else -1.0
    calmar = annualized / max_drawdown if max_drawdown > 0 else float('inf') if annualized > 0 else 0.0
    return {
        'return_pct': returns * 100,
        'max_drawdown_pct': max_drawdown * 100,
        'calmar': calmar,
    }


def evaluate_candidates(
    histories, signals, output_path, transaction_cost, slippage,
    candidate_source='snapshot', buy_mfi=BUY_MFI,
):
    all_dates = sorted({date.normalize() for prices in histories.values() for date in prices.index})
    if len(all_dates) < MINIMUM_OPTIMIZATION_DAYS:
        first_date = all_dates[0].strftime('%Y-%m-%d') if all_dates else '없음'
        last_date = all_dates[-1].strftime('%Y-%m-%d') if all_dates else '없음'
        raise ValueError(
            f'최적화 데이터가 {len(all_dates)}거래일({first_date}~{last_date})뿐입니다. '
            f'시간순 학습/검증/홀드아웃 비교를 위해 최소 '
            f'{MINIMUM_OPTIMIZATION_DAYS}거래일이 필요합니다. '
            '매 거래일 후보 스냅샷과 일봉을 갱신한 뒤 '
            '이 도구를 --collect-minute 옵션으로 실행해 분봉을 누적하세요. '
            '스냅샷이 시작되기 전 후보 이력은 소급해 만들 수 없습니다.'
        )
    train_end = all_dates[max(0, int(len(all_dates) * 0.60) - 1)]
    validation_end = all_dates[max(0, int(len(all_dates) * 0.80) - 1)]
    start, end = all_dates[0], all_dates[-1]
    rows = []
    for stop_kind, parameter in STOP_CANDIDATES:
        equity, trades = simulate_stop_strategy(
            histories, signals, stop_kind, parameter,
            transaction_cost=transaction_cost,
            slippage=slippage,
        )
        initial = 1.0
        train = _period_metrics(equity, start, train_end, initial)
        prior_to_validation = equity.loc[equity.index < all_dates[int(len(all_dates) * 0.60)]]
        validation_base = float(prior_to_validation.iloc[-1]) if not prior_to_validation.empty else initial
        validation = _period_metrics(
            equity, train_end + pd.Timedelta(days=1), validation_end, validation_base
        )
        prior_to_test = equity.loc[equity.index <= validation_end]
        test_base = float(prior_to_test.iloc[-1]) if not prior_to_test.empty else initial
        holdout = _period_metrics(
            equity, validation_end + pd.Timedelta(days=1), end, test_base
        )
        rows.append({
            'candidate_source': candidate_source,
            'buy_mfi': buy_mfi,
            'stop_type': stop_kind,
            'parameter': parameter,
            'train_return_pct': train['return_pct'],
            'train_max_drawdown_pct': train['max_drawdown_pct'],
            'validation_return_pct': validation['return_pct'],
            'validation_max_drawdown_pct': validation['max_drawdown_pct'],
            'validation_calmar': validation['calmar'],
            'holdout_return_pct': holdout['return_pct'],
            'holdout_max_drawdown_pct': holdout['max_drawdown_pct'],
            'holdout_calmar': holdout['calmar'],
            'completed_trades': len(trades),
            'win_rate_pct': (
                sum(value > 0 for value in trades) / len(trades) * 100
                if trades else 0.0
            ),
            'transaction_cost_rate': transaction_cost,
            'slippage_rate': slippage,
            'train_end': train_end.date(),
            'validation_end': validation_end.date(),
            'holdout_end': end.date(),
        })
    results = pd.DataFrame(rows).sort_values(
        ['validation_calmar', 'validation_return_pct'],
        ascending=[False, False],
        kind='mergesort',
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(output_path, index=False, encoding='utf-8-sig')
    print('\n검증 구간 기준 상위 트레일링 스탑')
    print(results.head(10).to_string(index=False))
    print(f'\n전체 비교 결과 저장: {output_path}')
    print('최종 선택 전에는 holdout 결과와 거래 수를 함께 확인하세요.')
    return results


def parse_args():
    parser = argparse.ArgumentParser(
        description='분봉 트레일링 스탑을 과거 데이터로 비교합니다. 주문 기능은 없습니다.'
    )
    parser.add_argument('--market', choices=('KOSPI', 'KOSDAQ'), default='KOSPI')
    parser.add_argument('--start', default=DEFAULT_START, help='분석 시작일 YYYY-MM-DD')
    parser.add_argument('--end', help='분석 종료일 YYYY-MM-DD (기본: DB 최신 일봉)')
    parser.add_argument(
        '--candidate-source', choices=('snapshot', 'daily'), default='snapshot',
        help=(
            '후보 소스: snapshot은 저장된 시가총액 후보 이력, '
            'daily는 저장 종목군을 과거 일봉 기술지표로 재선정'
        ),
    )
    parser.add_argument(
        '--buy-mfi', type=int, default=BUY_MFI,
        help=f'매수 MFI 상한 (기본: {BUY_MFI}, 허용 범위: 0~100)',
    )
    parser.add_argument(
        '--collect-minute',
        action='store_true',
        help='분봉 데이터가 충분해도 CREON 누락 수집을 확인',
    )
    parser.add_argument(
        '--collect-only',
        action='store_true',
        help='분봉 수집만 수행하고 최적화 결과 계산은 건너뜀',
    )
    parser.add_argument('--max-requests', type=int, default=5000)
    parser.add_argument('--transaction-cost', type=float, default=TRANSACTION_COST_RATE)
    parser.add_argument('--slippage', type=float, default=0.001)
    parser.add_argument('--output', type=Path)
    return parser.parse_args()


def main():
    args = parse_args()
    if not 0 <= args.buy_mfi <= 100:
        raise ValueError('--buy-mfi는 0~100 범위여야 합니다.')
    start_date = pd.Timestamp(args.start).normalize()
    connection = connect_database()
    try:
        ensure_optimizer_tables(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT MAX(prices.date) FROM daily_price AS prices "
                "JOIN (SELECT DISTINCT code FROM selected_stock_history "
                "WHERE market = %s) AS candidates ON candidates.code = prices.code",
                (args.market,),
            )
            latest_row = cursor.fetchone()
            latest = latest_row[0] if latest_row is not None else None
        if latest is None:
            raise ValueError(f'{args.market} 후보 종목의 daily_price가 없습니다.')
        end_date = pd.Timestamp(args.end).normalize() if args.end else pd.Timestamp(latest).normalize()
        if end_date < start_date:
            raise ValueError('--end는 --start보다 빠를 수 없습니다.')

        daily_inputs = load_daily_inputs(
            connection, args.market, start_date, end_date,
            candidate_source=args.candidate_source,
            buy_mfi=args.buy_mfi,
        )
        available_days = count_minute_trading_days(
            connection,
            daily_inputs['minute_codes'],
            daily_inputs['start_date'],
            end_date,
        )
        should_collect_minute = (
            args.collect_minute
            or args.collect_only
            or available_days < MINIMUM_OPTIMIZATION_DAYS
        )
        if should_collect_minute:
            if available_days < MINIMUM_OPTIMIZATION_DAYS:
                print(
                    f'분봉 거래일 {available_days}일 확인: 최소 '
                    f'{MINIMUM_OPTIMIZATION_DAYS}일보다 적어 누락 데이터를 자동 수집합니다.',
                    flush=True,
                )
            collect_minute_data(
                connection, None, daily_inputs,
                daily_inputs['start_date'], end_date, args.max_requests,
            )
        if args.collect_only:
            print('분봉 수집 전용 실행 완료. 최적화 계산은 건너뜁니다.')
            return
        histories, signals = load_minute_histories(
            connection, daily_inputs, daily_inputs['start_date'], end_date
        )
        if not histories:
            raise ValueError('분석 대상 분봉이 없습니다.')

        bars = sum(len(prices) for prices in histories.values())
        print(
            f'분석 데이터: {args.market}, {len(histories)}종목, {bars:,}개 분봉, '
            f'{min(prices.index.min() for prices in histories.values()):%Y-%m-%d} '
            f'~ {max(prices.index.max() for prices in histories.values()):%Y-%m-%d}'
        )
        if daily_inputs['latest_snapshot_update'] is not None:
            print(f'후보 스냅샷 최종 갱신: {daily_inputs["latest_snapshot_update"]}')
        source_suffix = '_daily' if args.candidate_source == 'daily' else ''
        mfi_suffix = f'_mfi{args.buy_mfi}' if args.buy_mfi != 20 else ''
        output_path = args.output or (
            Path(__file__).resolve().parent
            / 'results'
            / f'trailing_stop_{args.market}{source_suffix}{mfi_suffix}_'
            f'{start_date:%Y%m%d}_{end_date:%Y%m%d}.csv'
        )
        evaluate_candidates(
            histories, signals, output_path,
            args.transaction_cost, args.slippage,
            candidate_source=args.candidate_source,
            buy_mfi=args.buy_mfi,
        )
    finally:
        connection.close()


if __name__ == '__main__':
    main()