"""BollingerBand_MFI10_Top10_Backtest

실행 절차
1. MySQL의 zunDB에 연결하고, 백테스트 결과 테이블과 키를 준비한다.
2. KOSPI와 KOSDAQ별 selected_stock 상위 100개와 기준일을 읽는다.
3. 구성 종목의 일봉으로 볼린저 밴드, 이동평균, MFI10을 계산하고,
   마지막 거래일은 일봉 매매에서 제외한다.
4. 포트폴리오 종목 안에서 추세, 수익률, MFI10, 볼린저 점수로 매일 TOP 10을
    선정한다. 선정 점수는 전일 데이터까지만 사용한다.
5. TOP 10 및 기존 매수 조건을 만족하면 매수하고, 기존 매도/트레일링 스톱
   조건으로 매매를 시뮬레이션한다. 매도 후에는 TOP 10에 다시 포함되어야
   매수 신호에 따른 재진입이 가능하다.
6. 시장/종목/거래 이벤트 결과를 DB에 저장하고, 실현 매도 수익률을
   플러스/마이너스/본전으로 나누어 출력한다.
7. 마지막 거래일 분봉 데이터를 일봉 이력과 결합해 매매를 시뮬레이션한다.
8. 매수된 종목의 일봉/분봉 가격과 매수/매도 지점을 함께 표시한다.

주의: 과거 일봉 후보는 selected_stock_history의 날짜별 목록을 사용합니다.
이력 시작 전 날짜는 후보 미선정으로 처리합니다. 최신 분봉 대상은 마지막 일봉
시점의 스냅샷과 전략 점수를 기준으로 고릅니다.
"""

import math
import importlib.util
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import pymysql
from BollingerBand_MFI10_Selection import (
    TOP_STOCK_COUNT,
    TRAILING_STOP_RATE,
    load_candidate_snapshots,
    prepare_selection_indicators,
    rank_daily_top_stocks as rank_selection_top_stocks,
    simulate_shared_capital,
)


plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

MA_PERIODS = (5, 10, 15, 20, 25, 30)
TEST_CONDITIONS = ((20, 50, 80),)
FALLBACK_COMPANY_NAMES = {'009155': '삼성전기우'}
BACKTEST_START_DATE = pd.Timestamp('2026-04-01')
INDICATOR_WARMUP_DAYS = 220


def create_creon_client():
    updater_path = (
        Path(__file__).resolve().parents[1]
        / '05_Stock_Price_API'
        / 'Investar'
        / 'DBUpdaterEx.py'
    )
    spec = importlib.util.spec_from_file_location(
        'stock_db_updater_ex', updater_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f'CREON 수집기 파일을 불러올 수 없습니다: {updater_path}')
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    creon = updater.CREON()
    creon.stock_codes_by_db_code = {}
    for market in (1, 2):
        for creon_code in creon.objCodeMgr.GetStockListByMarket(market):
            db_code = (
                creon_code[1:] if creon_code.startswith('A') else creon_code
            )
            creon.stock_codes_by_db_code[db_code] = creon_code
    return creon


def refresh_last_day_minute_prices(connection, creon, code, company, trade_date):
    db_code = str(code)
    if db_code.startswith('A'):
        db_code = db_code[1:]
    creon_code = creon.stock_codes_by_db_code.get(db_code)
    if creon_code is None:
        raise ValueError(
            f'{company} ({code}) 종목을 CREON 시장 종목 목록에서 찾을 수 없습니다.'
        )
    date_value = int(trade_date.strftime('%Y%m%d'))
    print(
        f'{company} ({code}) CREON 분봉 요청: {creon_code} {date_value}',
        flush=True,
    )
    try:
        minute_prices = creon.get_stock_price_min(creon_code, date_value)
    except Exception as exc:
        raise RuntimeError(
            f'{company} ({code}) CREON 코드 {creon_code}, '
            f'{date_value} 분봉 조회 실패: {exc}'
        ) from exc
    minute_prices = minute_prices.loc[
        minute_prices['date'].astype(int) == date_value
    ].copy()
    if minute_prices.empty:
        raise ValueError(
            f'{company} ({code}) {trade_date:%Y-%m-%d} CREON 분봉 데이터가 없습니다.'
        )

    minute_values = minute_prices['time'].astype(int).map(
        lambda value: f'{value:04d}'
    )
    minute_prices['trade_time'] = (
        minute_values.str[:2] + ':' + minute_values.str[2:] + ':00'
    )
    values = [
        (
            code,
            trade_date.date(),
            row.trade_time,
            int(row.open),
            int(row.high),
            int(row.low),
            int(row.close),
            int(row.volume),
        )
        for row in minute_prices[
            ['trade_time', 'open', 'high', 'low', 'close', 'volume']
        ].itertuples(index=False)
    ]
    sql = """REPLACE INTO minute_price
        (code, date, trade_time, open, high, low, close, volume)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""
    with connection.cursor() as cursor:
        cursor.executemany(sql, values)
    connection.commit()
    return len(values)


def load_candidate_universe(connection, market_name, creon):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT MAX(prices.date) FROM daily_price AS prices "
            "JOIN (SELECT DISTINCT code FROM selected_stock_history "
            "WHERE market = %s) AS candidates ON candidates.code = prices.code",
            (market_name,),
        )
        latest_price_row = cursor.fetchone()
    if latest_price_row is None or latest_price_row[0] is None:
        raise ValueError('daily_price에 저장된 일봉이 없습니다.')
    data_end = pd.Timestamp(latest_price_row[0]).normalize()
    snapshot_start = (
        BACKTEST_START_DATE - pd.Timedelta(days=INDICATOR_WARMUP_DAYS)
    ).normalize()
    snapshots, company_names, latest_update = load_candidate_snapshots(
        connection, market_name, snapshot_start.date(), data_end.date()
    )
    if not snapshots:
        raise ValueError(
            f'{market_name} 후보 스냅샷이 없습니다. '
            'DBUpdaterEx를 실행해 selected_stock_history를 채우세요.'
        )

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT prices.code, prices.date, prices.open, prices.high, "
            "prices.low, prices.close, prices.volume "
            "FROM daily_price AS prices "
            "JOIN (SELECT DISTINCT code FROM selected_stock_history "
            "WHERE market = %s AND snapshot_date BETWEEN %s AND %s) AS candidates "
            "ON candidates.code = prices.code "
            "WHERE prices.date BETWEEN %s AND %s "
            "ORDER BY prices.code, prices.date",
            (
                market_name, snapshot_start.date(), data_end.date(),
                snapshot_start.date(), data_end.date(),
            ),
        )
        price_rows = cursor.fetchall()
    if not price_rows:
        raise ValueError(f'{market_name} 후보 종목의 일봉 데이터가 없습니다.')

    price_frame = pd.DataFrame(
        price_rows,
        columns=['code', 'date', 'open', 'high', 'low', 'close', 'volume'],
    )
    price_frame['date'] = pd.to_datetime(price_frame['date'])
    charts = []
    last_trade_dates = {}
    for code, rows in price_frame.groupby('code', sort=False):
        code = str(code)
        company = (
            company_names.get(code)
            or FALLBACK_COMPANY_NAMES.get(code)
            or code
        )
        prices = rows.drop(columns='code').set_index('date').sort_index()
        if prices.index.max() < BACKTEST_START_DATE:
            continue
        last_trade_date = prices.index[-1]
        last_trade_dates[code] = last_trade_date
        daily_prices = prices.loc[prices.index < last_trade_date].copy()
        daily_prices['bb_middle'] = daily_prices['close'].rolling(20).mean()
        bb_stddev = daily_prices['close'].rolling(20).std()
        daily_prices['bb_upper'] = daily_prices['bb_middle'] + 2 * bb_stddev
        daily_prices['bb_lower'] = daily_prices['bb_middle'] - 2 * bb_stddev
        for ma_period in MA_PERIODS:
            daily_prices[f'ma{ma_period}'] = (
                daily_prices['close'].rolling(ma_period).mean()
            )
        typical_price = (
            daily_prices['high'] + daily_prices['low'] + daily_prices['close']
        ) / 3
        money_flow = typical_price * daily_prices['volume']
        price_change = typical_price.diff()
        positive_flow = money_flow.where(price_change > 0, 0).rolling(10).sum()
        negative_flow = money_flow.where(price_change < 0, 0).rolling(10).sum()
        total_flow = positive_flow + negative_flow
        daily_prices['mfi10'] = (
            100 * positive_flow / total_flow.replace(0, float('nan'))
        )
        daily_prices.loc[total_flow == 0, 'mfi10'] = 50
        ma_columns = [f'ma{ma_period}' for ma_period in MA_PERIODS]
        daily_prices = daily_prices.dropna(
            subset=ma_columns + ['mfi10', 'bb_middle', 'bb_upper', 'bb_lower']
        )
        if daily_prices.empty:
            continue
        minute_prices = pd.DataFrame(
            columns=['open', 'high', 'low', 'close', 'volume'],
            index=pd.DatetimeIndex([]),
        )
        charts.append((
            company, code, 1 / TOP_STOCK_COUNT, daily_prices, minute_prices
        ))

    if not charts:
        raise ValueError(
            f'{market_name} selected_stock 후보의 MFI/MA 데이터가 없습니다.'
        )
    latest_market_date = max(
        last_trade_dates[chart[1]] for chart in charts
    )
    rankable_charts = [
        chart for chart in charts
        if last_trade_dates.get(chart[1]) == latest_market_date
    ]
    latest_membership = rank_latest_top_stocks(
        rankable_charts, candidate_snapshots=snapshots
    )
    selected_codes = {
        code for code, selected in latest_membership.items() if selected
    }
    for chart_index, (company, code, weight, daily_prices, _) in enumerate(charts):
        if code not in selected_codes:
            continue
        last_trade_date = last_trade_dates[code]
        try:
            refresh_last_day_minute_prices(
                connection, creon, code, company, last_trade_date
            )
        except Exception as exc:
            print(f'{market_name} {company} ({code}) 분봉 조회 실패: {exc}')
            continue
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT date, trade_time, open, high, low, close, volume "
                "FROM minute_price WHERE code = %s AND date = %s "
                "ORDER BY trade_time",
                (code, last_trade_date.date()),
            )
            minute_rows = cursor.fetchall()
        if not minute_rows:
            print(
                f'{market_name} {company} ({code}) '
                f'{last_trade_date:%Y-%m-%d}: 분봉 데이터가 없어 제외합니다.'
            )
            continue
        minute_prices = pd.DataFrame(
            minute_rows,
            columns=[
                'date', 'trade_time', 'open', 'high', 'low', 'close', 'volume'
            ],
        )
        minute_prices.index = (
            pd.to_datetime(minute_prices['date'])
            + pd.to_timedelta(minute_prices['trade_time'].astype(str))
        )
        minute_prices.index.name = 'date'
        minute_prices = minute_prices[
            ['open', 'high', 'low', 'close', 'volume']
        ].sort_index()
        charts[chart_index] = (
            company, code, weight, daily_prices, minute_prices
        )
        print(
            f'{market_name} {company} ({code}) '
            f'{last_trade_date:%Y-%m-%d}: {len(minute_rows)}개 분봉 로드',
            flush=True,
        )
    return latest_update, charts, snapshots


def ensure_trade_result_tables(connection):
    statements = [
        """CREATE TABLE IF NOT EXISTS minute_price (
            code VARCHAR(20),
            date DATE,
            trade_time TIME,
            open BIGINT(20),
            high BIGINT(20),
            low BIGINT(20),
            close BIGINT(20),
            volume BIGINT(20),
            PRIMARY KEY (code, date, trade_time)
        )""",
        """CREATE TABLE IF NOT EXISTS strategy_backtest_summary (
            simulation_at DATETIME(6) NOT NULL,
            market VARCHAR(10) NOT NULL,
            portfolio_type VARCHAR(20) NOT NULL,
            portfolio_run_at DATETIME NOT NULL,
            data_start DATE NOT NULL,
            data_end DATE NOT NULL,
            allocation_weight DECIMAL(12, 10) NOT NULL,
            portfolio_return_pct DECIMAL(14, 8) NOT NULL,
            ma_period TINYINT UNSIGNED NOT NULL,
            mfi_buy_threshold TINYINT UNSIGNED NOT NULL,
            mfi_sell_threshold TINYINT UNSIGNED NOT NULL,
            reinvestment_rule VARCHAR(120) NOT NULL,
            PRIMARY KEY (
                simulation_at, market, portfolio_type,
                ma_period, mfi_buy_threshold, mfi_sell_threshold
            )
        )""",
        """CREATE TABLE IF NOT EXISTS strategy_stock_result (
            simulation_at DATETIME(6) NOT NULL,
            market VARCHAR(10) NOT NULL,
            portfolio_type VARCHAR(20) NOT NULL,
            ma_period TINYINT UNSIGNED NOT NULL,
            mfi_buy_threshold TINYINT UNSIGNED NOT NULL,
            mfi_sell_threshold TINYINT UNSIGNED NOT NULL,
            code VARCHAR(20) NOT NULL,
            company VARCHAR(100) NOT NULL,
            portfolio_weight DECIMAL(12, 10) NOT NULL,
            initial_capital DECIMAL(20, 12) NOT NULL,
            ending_value DECIMAL(20, 12) NOT NULL,
            first_buy_date DATE NULL,
            valuation_date DATE NOT NULL,
            buy_count SMALLINT NOT NULL,
            reinvestment_count SMALLINT NOT NULL,
            return_pct DECIMAL(14, 8) NOT NULL,
            status VARCHAR(100) NOT NULL,
            PRIMARY KEY (
                simulation_at, market, portfolio_type,
                ma_period, mfi_buy_threshold, mfi_sell_threshold, code
            ),
            KEY strategy_stock_lookup (market, code, simulation_at)
        )""",
        """CREATE TABLE IF NOT EXISTS strategy_trade_event (
            simulation_at DATETIME(6) NOT NULL,
            market VARCHAR(10) NOT NULL,
            portfolio_type VARCHAR(20) NOT NULL,
            ma_period TINYINT UNSIGNED NOT NULL,
            mfi_buy_threshold TINYINT UNSIGNED NOT NULL,
            mfi_sell_threshold TINYINT UNSIGNED NOT NULL,
            code VARCHAR(20) NOT NULL,
            event_no SMALLINT NOT NULL,
            event_type VARCHAR(24) NOT NULL,
            event_date DATE NOT NULL,
            price DECIMAL(20, 6) NULL,
            quantity DECIMAL(24, 12) NOT NULL,
            cash_before DECIMAL(24, 12) NOT NULL,
            cash_after DECIMAL(24, 12) NOT NULL,
            shares_after DECIMAL(24, 12) NOT NULL,
            trade_return_pct DECIMAL(14, 8) NULL,
            PRIMARY KEY (
                simulation_at, market, portfolio_type,
                ma_period, mfi_buy_threshold, mfi_sell_threshold, code, event_no
            ),
            KEY strategy_event_lookup (market, code, event_date)
        )""",
    ]
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
        primary_keys = {
            'strategy_backtest_summary': (
                'simulation_at', 'market', 'portfolio_type',
                'ma_period', 'mfi_buy_threshold', 'mfi_sell_threshold'
            ),
            'strategy_stock_result': (
                'simulation_at', 'market', 'portfolio_type',
                'ma_period', 'mfi_buy_threshold', 'mfi_sell_threshold', 'code'
            ),
            'strategy_trade_event': (
                'simulation_at', 'market', 'portfolio_type',
                'ma_period', 'mfi_buy_threshold', 'mfi_sell_threshold',
                'code', 'event_no'
            ),
        }
        for table_name, expected_key in primary_keys.items():
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (table_name,),
            )
            columns = {row[0] for row in cursor.fetchall()}
            if 'portfolio_type' not in columns:
                cursor.execute(
                    f"ALTER TABLE `{table_name}` ADD COLUMN portfolio_type "
                    "VARCHAR(20) NOT NULL DEFAULT 'legacy' AFTER market"
                )
                columns.add('portfolio_type')
            for column_name in (
                'ma_period', 'mfi_buy_threshold', 'mfi_sell_threshold'
            ):
                if column_name not in columns:
                    cursor.execute(
                        f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` "
                        'TINYINT UNSIGNED NOT NULL DEFAULT 0 AFTER portfolio_type'
                    )

            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION",
                (table_name,),
            )
            current_key = tuple(row[0] for row in cursor.fetchall())
            if current_key != expected_key:
                key_columns = ', '.join(f'`{column}`' for column in expected_key)
                cursor.execute(
                    f"ALTER TABLE `{table_name}` DROP PRIMARY KEY, "
                    f"ADD PRIMARY KEY ({key_columns})"
                )
    connection.commit()


def save_market_results(
    connection,
    simulation_at,
    market_name,
    portfolio_type,
    portfolio_run_at,
    ma_period,
    mfi_buy_threshold,
    mfi_sell_threshold,
    stock_results,
    weighted_total_return,
    portfolio_weight,
):
    test_period_indexes = [
        prices.index[prices.index >= BACKTEST_START_DATE]
        for _, _, _, daily_prices, minute_prices, _ in stock_results
        for prices in (daily_prices, minute_prices)
    ]
    test_period_indexes = [index for index in test_period_indexes if not index.empty]
    if not test_period_indexes:
        raise ValueError(
            f'{BACKTEST_START_DATE:%Y-%m-%d} 이후 백테스트 데이터가 없습니다.'
        )
    data_start = min(index.min() for index in test_period_indexes).date()
    data_end = max(
        prices.index.max()
        for _, _, _, daily_prices, minute_prices, _ in stock_results
        for prices in (daily_prices, minute_prices)
    ).date()
    stock_rows = []
    event_rows = []

    for company, code, weight, daily_prices, minute_prices, result in stock_results:
        stock_rows.append((
            simulation_at,
            market_name,
            portfolio_type,
            ma_period,
            mfi_buy_threshold,
            mfi_sell_threshold,
            code,
            company,
            weight,
            result['initial_capital'],
            result['ending_value'],
            result['first_buy_date'].date()
            if result['first_buy_date'] is not None
            else None,
            result['valuation_date'].date(),
            result['buy_count'],
            result['reinvestment_count'],
            result['return_pct'],
            result['status'],
        ))
        for event_no, event in enumerate(result['trade_events'], start=1):
            event_rows.append((
                simulation_at,
                market_name,
                portfolio_type,
                ma_period,
                mfi_buy_threshold,
                mfi_sell_threshold,
                code,
                event_no,
                event['reason'],
                event['date'].date(),
                event['price'],
                event['quantity'],
                event['cash_before'],
                event['cash_after'],
                event['shares_after'],
                event.get('trade_return_pct'),
            ))

    summary_sql = """INSERT INTO strategy_backtest_summary
        (simulation_at, market, portfolio_type, portfolio_run_at, data_start, data_end,
         allocation_weight, portfolio_return_pct, ma_period, mfi_buy_threshold,
         mfi_sell_threshold, reinvestment_rule)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
    stock_sql = """INSERT INTO strategy_stock_result
        (simulation_at, market, portfolio_type, ma_period, mfi_buy_threshold,
         mfi_sell_threshold, code, company, portfolio_weight, initial_capital,
         ending_value, first_buy_date, valuation_date, buy_count,
         reinvestment_count, return_pct, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
    event_sql = """INSERT INTO strategy_trade_event
        (simulation_at, market, portfolio_type, ma_period, mfi_buy_threshold,
         mfi_sell_threshold, code, event_no, event_type, event_date, price,
         quantity, cash_before, cash_after, shares_after, trade_return_pct)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
    summary_row = (
        simulation_at,
        market_name,
        portfolio_type,
        portfolio_run_at,
        data_start,
        data_end,
        portfolio_weight,
        weighted_total_return * 100,
        ma_period,
        mfi_buy_threshold,
        mfi_sell_threshold,
        '회당 0.15% 비용, 공유 현금, 최대 10개 슬롯 균등 배분',
    )

    try:
        with connection.cursor() as cursor:
            cursor.execute(summary_sql, summary_row)
            cursor.executemany(stock_sql, stock_rows)
            cursor.executemany(event_sql, event_rows)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    print(
        f'{market_name} {portfolio_type}: 요약 1건, 종목 {len(stock_rows)}건, '
        f'거래 이벤트 {len(event_rows)}건을 DB에 저장했습니다.',
        flush=True,
    )


def calculate_reinvested_return(prices, buy_signal, sell_signal, initial_capital=1.0):
    cash = initial_capital
    shares = 0.0
    entry_price = 0.0
    peak_price = 0.0
    buy_events = []
    exit_events = []
    trade_events = []
    first_buy_date = None

    for position in range(len(prices)):
        current_price = float(prices['close'].iloc[position])

        if shares == 0:
            if not buy_signal.iloc[position]:
                continue
            entry_price = current_price
            cash_before = cash
            shares = cash / entry_price
            cash = 0.0
            peak_price = entry_price
            if first_buy_date is None:
                first_buy_date = prices.index[position]
            buy_event = {
                'date': prices.index[position],
                'price': current_price,
                'quantity': shares,
                'cash_before': cash_before,
                'cash_after': cash,
                'shares_after': shares,
                'reason': 'buy_signal',
            }
            buy_events.append(buy_event)
            trade_events.append(buy_event)
            continue

        current_low = float(prices['low'].iloc[position])
        trailing_stop_price = peak_price * (1 - TRAILING_STOP_RATE)
        exit_price = None
        exit_reason = None
        if current_low <= trailing_stop_price:
            exit_price = trailing_stop_price
            exit_reason = 'trailing_stop'
        elif sell_signal.iloc[position] and current_price >= entry_price:
            exit_price = current_price
            exit_reason = 'sell_signal'

        if exit_price is not None:
            shares_sold = shares
            cash_before = cash
            cash = shares * exit_price
            shares = 0.0
            exit_event = {
                'date': prices.index[position],
                'price': exit_price,
                'reason': exit_reason,
                'quantity': shares_sold,
                'cash_before': cash_before,
                'cash_after': cash,
                'shares_after': shares,
                'trade_return_pct': (exit_price / entry_price - 1) * 100,
            }
            exit_events.append(exit_event)
            trade_events.append(exit_event)
            entry_price = 0.0
            peak_price = 0.0
            continue

        peak_price = max(peak_price, float(prices['high'].iloc[position]))

    is_holding = shares > 0
    final_value = (
        shares * float(prices['close'].iloc[-1])
        if is_holding
        else cash
    )
    return_pct = (final_value / initial_capital - 1) * 100
    if is_holding:
        trade_events.append({
            'date': prices.index[-1],
            'price': float(prices['close'].iloc[-1]),
            'reason': 'final_mark',
            'quantity': shares,
            'cash_before': cash,
            'cash_after': cash,
            'shares_after': shares,
            'trade_return_pct': (float(prices['close'].iloc[-1]) / entry_price - 1) * 100,
        })
    else:
        trade_events.append({
            'date': prices.index[-1],
            'price': None,
            'reason': 'cash_balance',
            'quantity': 0.0,
            'cash_before': cash,
            'cash_after': cash,
            'shares_after': 0.0,
            'trade_return_pct': None,
        })
    buy_count = len(buy_events)
    reinvestment_count = max(buy_count - 1, 0)
    if buy_count == 0:
        status = '매수 신호 없음, 현금 유지'
    elif is_holding:
        status = f'{reinvestment_count}회 재투자, 최종 종가 평가'
    else:
        status = f'{reinvestment_count}회 재투자, 매도 후 현금 대기'

    return {
        'first_buy_date': first_buy_date,
        'valuation_date': prices.index[-1],
        'return_pct': return_pct,
        'status': status,
        'buy_count': buy_count,
        'reinvestment_count': reinvestment_count,
        'exit_events': exit_events,
        'buy_events': buy_events,
        'trade_events': trade_events,
        'is_holding': is_holding,
        'initial_capital': initial_capital,
        'ending_value': final_value,
    }


def rank_daily_top_stocks(charts, candidate_snapshots=None):
    histories = {
        code: prepare_selection_indicators(daily_prices)
        for _, code, _, daily_prices, _ in charts
    }
    return rank_selection_top_stocks(
        histories,
        limit=TOP_STOCK_COUNT,
        lag=1,
        candidate_snapshots=candidate_snapshots,
    )


def rank_latest_top_stocks(charts, candidate_snapshots=None):
    histories = {
        code: prepare_selection_indicators(daily_prices)
        for _, code, _, daily_prices, _ in charts
    }
    membership = rank_selection_top_stocks(
        histories,
        limit=TOP_STOCK_COUNT,
        lag=0,
        candidate_snapshots=candidate_snapshots,
    )
    return pd.Series({
        code: bool(selected.iloc[-1])
        for code, selected in membership.items()
    })


def print_exit_return_summary(market_name, portfolio_type, stock_results):
    exit_returns = [
        (company, code, event)
        for company, code, _, _, _, trade in stock_results
        for event in trade['exit_events']
    ]
    return_groups = (
        ('플러스 수익 매도', lambda return_pct: return_pct > 0),
        ('마이너스 수익 매도', lambda return_pct: return_pct < 0),
        ('본전 매도', lambda return_pct: return_pct == 0),
    )

    print(f'\n{market_name} {portfolio_type} 매도 완료 수익률')
    for label, matches in return_groups:
        matching_returns = [
            (company, code, event)
            for company, code, event in exit_returns
            if matches(event['trade_return_pct'])
        ]
        print(f'{label} ({len(matching_returns)}건)')
        if not matching_returns:
            print('  없음')
            continue
        for company, code, event in matching_returns:
            print(
                f'  {event["date"]:%Y-%m-%d} {company} ({code}): '
                f'{event["trade_return_pct"]:+.2f}%'
            )


def print_last_day_minute_trade_stocks(
    market_name, portfolio_type, stock_results
):
    minute_events = []
    for company, code, _, _, minute_prices, trade in stock_results:
        for action, events in (
            ('매수', trade['buy_events']),
            ('매도', trade['exit_events']),
        ):
            minute_events.extend(
                (event['date'], company, code, action, event)
                for event in events
                if event['date'] in minute_prices.index
            )

    minute_events.sort(key=lambda item: item[0])
    stock_codes = {code for _, _, code, _, _ in minute_events}
    print(
        f'\n{market_name} {portfolio_type} 마지막 날 분봉 매매 종목 '
        f'({len(stock_codes)}종목, {len(minute_events)}건)'
    )
    if not minute_events:
        print('  없음')
        return

    reason_labels = {
        'buy_signal': '매수 신호',
        'sell_signal': '매도 신호',
        'trailing_stop': '트레일링 스탑',
    }
    for event_date, company, code, action, event in minute_events:
        reason = reason_labels.get(event['reason'], event['reason'])
        print(
            f'  {event_date:%Y-%m-%d %H:%M} {company} ({code}) '
            f'{action} [{reason}] {event["price"]:,.0f}원'
        )


def plot_market_page(
    connection, simulation_at, market_name, portfolio_type, portfolio_run_at,
    charts, candidate_snapshots,
):
    portfolio_weight = 1.0
    condition_rows = []
    top10_membership = rank_daily_top_stocks(charts, candidate_snapshots)
    latest_top10_membership = rank_latest_top_stocks(
        charts, candidate_snapshots
    )

    for ma_period, buy_threshold, sell_threshold in TEST_CONDITIONS:
        ma_column = f'ma{ma_period}'
        histories = {}
        buy_signals = {}
        sell_signals = {}
        for company, code, weight, daily_prices, minute_prices in charts:
            test_period = daily_prices.index >= BACKTEST_START_DATE
            daily_buy_signal = (
                (daily_prices['mfi10'] <= buy_threshold)
                & (daily_prices['close'] > daily_prices[ma_column])
                & top10_membership[code]
                & test_period
            )
            daily_sell_signal = (
                (daily_prices['mfi10'] >= sell_threshold)
                & (daily_prices['close'] < daily_prices[ma_column])
                & test_period
            )
            last_daily_mfi = daily_prices['mfi10'].iloc[-1]
            last_daily_ma = daily_prices[ma_column].iloc[-1]
            minute_buy_signal = pd.Series(
                (last_daily_mfi <= buy_threshold)
                & minute_prices['close'].gt(last_daily_ma)
                & latest_top10_membership[code],
                index=minute_prices.index,
            )
            minute_sell_signal = pd.Series(
                (last_daily_mfi >= sell_threshold)
                & minute_prices['close'].lt(last_daily_ma),
                index=minute_prices.index,
            )
            execution_prices = pd.concat(
                [daily_prices, minute_prices], sort=False
            ).sort_index()
            buy_signals[code] = pd.concat(
                [daily_buy_signal, minute_buy_signal]
            ).reindex(execution_prices.index).fillna(False)
            sell_signals[code] = pd.concat(
                [daily_sell_signal, minute_sell_signal]
            ).reindex(execution_prices.index).fillna(False)
            histories[code] = execution_prices[
                ['open', 'high', 'low', 'close', 'volume']
            ]

        simulation = simulate_shared_capital(
            histories, buy_signals, sell_signals,
            max_positions=TOP_STOCK_COUNT,
        )
        weighted_total_return = simulation['return_pct'] / 100
        stock_results = [
            (
                company,
                code,
                1 / TOP_STOCK_COUNT,
                daily_prices,
                minute_prices,
                simulation['stock_results'][code],
            )
            for company, code, _, daily_prices, minute_prices in charts
        ]

        save_market_results(
            connection,
            simulation_at,
            market_name,
            portfolio_type,
            portfolio_run_at,
            ma_period,
            buy_threshold,
            sell_threshold,
            stock_results,
            weighted_total_return,
            portfolio_weight,
        )
        condition_rows.append({
            'MA 기간': ma_period,
            'MFI 매수 기준': buy_threshold,
            'MFI 매도 기준': sell_threshold,
            '백테스트 시작일': BACKTEST_START_DATE.strftime('%Y-%m-%d'),
            '최종 수익률': weighted_total_return * 100,
        })

    result_frame = pd.DataFrame(condition_rows)
    print(f'\n{market_name} {portfolio_type} 조건별 최종 수익률 (%)')
    print(result_frame.to_string(index=False, formatters={
        '최종 수익률': '{:+.2f}'.format,
    }))
    print_exit_return_summary(market_name, portfolio_type, stock_results)
    print_last_day_minute_trade_stocks(
        market_name, portfolio_type, stock_results
    )

    purchased_results = [
        stock_result for stock_result in stock_results
        if stock_result[5]['buy_count'] > 0
    ]
    purchased_results.sort(
        key=lambda stock_result: stock_result[5]['initial_capital'],
        reverse=True,
    )
    purchased_results = purchased_results[:20]
    if not purchased_results:
        print(f'{market_name} {portfolio_type}: 매수된 종목이 없어 차트를 생략합니다.')
        return

    backtest_end = max(
        prices.index.max()
        for _, _, _, daily_prices, minute_prices, _ in stock_results
        for prices in (
            daily_prices.loc[daily_prices.index >= BACKTEST_START_DATE],
            minute_prices,
        )
        if not prices.empty
    )
    condition = condition_rows[0]
    columns = 2
    rows = math.ceil(len(purchased_results) / columns)
    figure, axes = plt.subplots(
        rows * 2,
        columns,
        figsize=(16, max(9, rows * 5.2)),
        squeeze=False,
    )
    for chart_index, (
        company, code, weight, daily_prices, minute_prices, trade
    ) in enumerate(purchased_results):
        daily_chart_prices = daily_prices.loc[
            daily_prices.index >= BACKTEST_START_DATE
        ]
        row_index = (chart_index // columns) * 2
        column_index = chart_index % columns
        daily_axis = axes[row_index, column_index]
        minute_axis = axes[row_index + 1, column_index]
        daily_axis.fill_between(
            daily_chart_prices.index,
            daily_chart_prices['bb_lower'],
            daily_chart_prices['bb_upper'],
            color='#b9d9e8',
            alpha=0.35,
            label='볼린저 밴드',
        )
        daily_axis.plot(
            daily_chart_prices.index,
            daily_chart_prices['close'],
            color='#202a44',
            label='일봉 종가',
            linewidth=1,
        )
        daily_axis.plot(
            daily_chart_prices.index,
            daily_chart_prices['bb_middle'],
            color='#168aad',
            label='중심선 (20일)',
            linewidth=0.9,
        )
        daily_axis.plot(
            daily_chart_prices.index,
            daily_chart_prices['bb_upper'],
            color='#e76f51',
            linestyle='--',
            label='상단 밴드',
            linewidth=0.8,
        )
        daily_axis.plot(
            daily_chart_prices.index,
            daily_chart_prices['bb_lower'],
            color='#e76f51',
            linestyle='--',
            label='하단 밴드',
            linewidth=0.8,
        )
        daily_buys = [
            event for event in trade['buy_events']
            if event['date'] in daily_chart_prices.index
        ]
        if daily_buys:
            daily_axis.scatter(
                [event['date'] for event in daily_buys],
                [event['price'] for event in daily_buys],
                color='#d1495b',
                marker='^',
                s=40,
                label='매수',
                zorder=3,
            )
        sell_events = [
            event for event in trade['exit_events']
            if event['reason'] == 'sell_signal'
            and event['date'] in daily_chart_prices.index
        ]
        if sell_events:
            daily_axis.scatter(
                [event['date'] for event in sell_events],
                [event['price'] for event in sell_events],
                color='#168aad',
                marker='v',
                s=40,
                label='매도',
                zorder=3,
            )
        stop_events = [
            event for event in trade['exit_events']
            if event['reason'] == 'trailing_stop'
            and event['date'] in daily_chart_prices.index
        ]
        if stop_events:
            daily_axis.scatter(
                [event['date'] for event in stop_events],
                [event['price'] for event in stop_events],
                color='#f4a261',
                marker='X',
                s=46,
                label='트레일링 스탑',
                zorder=4,
            )
        daily_axis.set_title(
            f'{company} ({code}, 비중 {weight:.1%}) '
            f'| 수익률 {trade["return_pct"]:+.2f}%'
        )
        daily_axis.set_ylabel('일봉')
        daily_axis.grid(True, alpha=0.25)
        daily_axis.tick_params(axis='x', labelrotation=25, labelsize=8)
        daily_axis.legend(loc='upper left', fontsize=7, ncol=3)

        minute_positions = list(range(len(minute_prices)))
        for minute_position, (_, minute) in zip(
            minute_positions, minute_prices.iterrows()
        ):
            candle_color = (
                '#d1495b' if minute['close'] >= minute['open'] else '#168aad'
            )
            minute_axis.vlines(
                minute_position, minute['low'], minute['high'],
                color=candle_color, linewidth=0.8,
            )
            minute_axis.vlines(
                minute_position, minute['open'], minute['close'],
                color=candle_color, linewidth=2.4,
            )
        minute_axis.plot(
            minute_positions,
            minute_prices['close'],
            color='#202a44',
            linewidth=0.7,
            label='분봉 종가',
        )
        minute_buys = [
            event for event in trade['buy_events']
            if event['date'] in minute_prices.index
        ]
        if minute_buys:
            minute_axis.scatter(
                [minute_prices.index.get_loc(event['date']) for event in minute_buys],
                [event['price'] for event in minute_buys],
                color='#d1495b', marker='^', s=38, label='매수', zorder=3,
            )
        for reason, marker, color, label in (
            ('sell_signal', 'v', '#168aad', '매도'),
            ('trailing_stop', 'X', '#f4a261', '트레일링 스탑'),
        ):
            minute_events = [
                event for event in trade['exit_events']
                if event['reason'] == reason
                and event['date'] in minute_prices.index
            ]
            if minute_events:
                minute_axis.scatter(
                    [
                        minute_prices.index.get_loc(event['date'])
                        for event in minute_events
                    ],
                    [event['price'] for event in minute_events],
                    color=color, marker=marker, s=40, label=label, zorder=3,
                )
        tick_step = max(1, len(minute_prices) // 6)
        tick_positions = list(range(0, len(minute_prices), tick_step))
        minute_axis.set_xticks(tick_positions)
        minute_axis.set_xticklabels([
            minute_prices.index[position].strftime('%H:%M')
            for position in tick_positions
        ])
        minute_axis.set_ylabel('분봉')
        minute_axis.grid(True, alpha=0.25)
        minute_axis.tick_params(axis='x', labelrotation=25, labelsize=8)
        minute_axis.legend(loc='upper left', fontsize=7, ncol=3)

    for chart_index in range(len(purchased_results), rows * columns):
        row_index = (chart_index // columns) * 2
        column_index = chart_index % columns
        axes[row_index, column_index].set_visible(False)
        axes[row_index + 1, column_index].set_visible(False)

    figure.suptitle(
        f'{market_name} {portfolio_type} 볼린저 밴드 매매 결과 '
        f'({BACKTEST_START_DATE:%Y-%m-%d}~{backtest_end:%Y-%m-%d}, '
        f'MA {condition["MA 기간"]}일, '
        f'최종 수익률 {condition["최종 수익률"]:+.2f}%)'
    )
    figure.tight_layout(rect=(0, 0, 1, 0.96))


def main():
    connection = pymysql.connect(
        host='localhost',
        user='root',
        password='root',
        database='zunDB',
        charset='utf8',
    )
    try:
        ensure_trade_result_tables(connection)
        creon = create_creon_client()
        simulation_at = datetime.now()
        for market_name in ('KOSPI', 'KOSDAQ'):
            portfolio_type = 'MFI10_Top10'
            portfolio_run_at, market_charts, candidate_snapshots = (
                load_candidate_universe(connection, market_name, creon)
            )
            plot_market_page(
                connection,
                simulation_at,
                market_name,
                portfolio_type,
                portfolio_run_at,
                market_charts,
                candidate_snapshots,
            )
    finally:
        connection.close()

    plt.show()


if __name__ == '__main__':
    main()
