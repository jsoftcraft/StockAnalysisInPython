"""Load Korean stock closes and market metadata from the Investar database."""

import importlib.util
from datetime import datetime
from pathlib import Path

import pandas as pd


INDICATOR_PRICE_TABLE = 'daytrading_indicator_price'
STOCK_UNIVERSE_TABLE = 'daytrading_stock_universe'
CREON_EQUITY_SECTION_KINDS = {1, 2, 6, 10, 13}


def ensure_stock_universe_table(connection):
    with connection.cursor() as cursor:
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS {STOCK_UNIVERSE_TABLE} (
                code VARCHAR(20) NOT NULL,
                market VARCHAR(10) NOT NULL,
                company VARCHAR(100) NOT NULL DEFAULT '',
                security_type VARCHAR(16) NOT NULL,
                creon_code VARCHAR(20) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (code),
                KEY ix_daytrading_universe_market (market, is_active)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        ''')


def _load_creon_stock_universe():
    try:
        import win32com.client
    except ImportError as exc:
        raise RuntimeError(
            '전체 종목 목록 조회에는 32비트 Python과 pywin32가 필요합니다.'
        ) from exc

    status = win32com.client.Dispatch('CpUtil.CpCybos')
    if status.IsConnect == 0:
        raise RuntimeError('CREON PLUS에 로그인한 뒤 실행하세요.')
    code_manager = win32com.client.Dispatch('CpUtil.CpCodeMgr')
    universe = []
    for market_number, market_name in ((1, 'KOSPI'), (2, 'KOSDAQ')):
        for creon_code in code_manager.GetStockListByMarket(market_number):
            creon_code = str(creon_code).strip()
            section_kind = int(
                code_manager.GetStockSectionKind(creon_code)
            )
            if section_kind not in CREON_EQUITY_SECTION_KINDS:
                continue
            code = creon_code[1:] if creon_code.startswith('A') else creon_code
            universe.append({
                'code': code,
                'market': market_name,
                'company': code_manager.CodeToName(creon_code) or code,
                'security_type': 'ETF' if section_kind == 10 else 'equity',
                'creon_code': creon_code,
            })
    if not universe:
        raise RuntimeError('CREON에서 KOSPI/KOSDAQ 종목 목록을 가져오지 못했습니다.')
    return universe


def sync_stock_universe(connection, universe_loader=None):
    universe = (universe_loader or _load_creon_stock_universe)()
    now = datetime.now()
    values = [
        (
            item['code'], item['market'], item['company'],
            item['security_type'], item['creon_code'], now,
        )
        for item in universe
    ]
    ensure_stock_universe_table(connection)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE {STOCK_UNIVERSE_TABLE} SET is_active = FALSE '
                'WHERE is_active = TRUE'
            )
            cursor.executemany(
                f'INSERT INTO {STOCK_UNIVERSE_TABLE} '
                '(code, market, company, security_type, creon_code, '
                'is_active, updated_at) '
                'VALUES (%s, %s, %s, %s, %s, TRUE, %s) '
                'ON DUPLICATE KEY UPDATE market = VALUES(market), '
                'company = VALUES(company), '
                'security_type = VALUES(security_type), '
                'creon_code = VALUES(creon_code), is_active = TRUE, '
                'updated_at = VALUES(updated_at)',
                values,
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return universe


def load_stock_data_from_database(connection, as_of_date, lookback=60):
    ensure_stock_universe_table(connection)
    analysis_date = pd.Timestamp(as_of_date).normalize()
    start_date = (
        analysis_date - pd.Timedelta(days=max(lookback * 3, 90))
    ).date()
    end_date = analysis_date.date()

    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT universe.code, universe.market, '
            'COALESCE(universe.company, ci.company, universe.code) AS company, '
            'dp.date, dp.close '
            f'FROM {STOCK_UNIVERSE_TABLE} AS universe '
            'JOIN daily_price AS dp ON dp.code = universe.code '
            'LEFT JOIN company_info AS ci ON ci.code = universe.code '
            'WHERE universe.is_active = TRUE '
            'AND universe.market IN (%s, %s) '
            'AND dp.date >= %s AND dp.date < %s '
            'ORDER BY dp.date, universe.code',
            ('KOSPI', 'KOSDAQ', start_date, end_date),
        )
        rows = cursor.fetchall()

    if not rows:
        raise RuntimeError(
            '전체 종목 유니버스 또는 가격 데이터가 없습니다. '
            '종목 수집을 생략했다면 먼저 기본 모드로 한 번 실행하세요.'
        )

    prices = pd.DataFrame(
        rows, columns=['code', 'market', 'company', 'date', 'close']
    )
    prices['code'] = prices['code'].astype(str).str.strip().str.zfill(6)
    prices['date'] = pd.to_datetime(prices['date']).dt.normalize()
    prices['close'] = pd.to_numeric(prices['close'], errors='coerce')
    prices = prices.dropna(subset=['close'])
    stock_closes = prices.pivot(
        index='date', columns='code', values='close'
    ).sort_index()
    metadata = prices[['code', 'market', 'company']].drop_duplicates(
        subset=['code'], keep='last'
    ).set_index('code')

    if stock_closes.empty:
        raise RuntimeError('DB 종가 데이터가 비어 있습니다.')
    return stock_closes, metadata


def _load_updater_factory():
    updater_path = (
        Path(__file__).resolve().parents[1]
        / '05_Stock_Price_API' / 'Investar' / 'DBUpdaterEx.py'
    )
    spec = importlib.util.spec_from_file_location(
        'daytrading_db_updater', updater_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f'일봉 수집기를 불러올 수 없습니다: {updater_path}')
    updater_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater_module)
    return updater_module.DBUpdater


def collect_missing_korean_closes(
    connection, as_of_date, lookback=60, updater_factory=None,
    universe_loader=None,
):
    """Sync the full KOSPI/KOSDAQ universe and collect its missing bars."""
    analysis_date = pd.Timestamp(as_of_date).normalize()
    end_date = analysis_date - pd.Timedelta(days=1)
    universe = sync_stock_universe(
        connection, universe_loader=universe_loader
    )

    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT universe.code, universe.company, '
            'universe.creon_code, MAX(dp.date) AS last_date '
            f'FROM {STOCK_UNIVERSE_TABLE} AS universe '
            'LEFT JOIN daily_price AS dp '
            'ON dp.code = universe.code AND dp.date < %s '
            'WHERE universe.is_active = TRUE '
            'GROUP BY universe.code, universe.company, universe.creon_code',
            (analysis_date.date(),),
        )
        coverage = cursor.fetchall()

    if not coverage:
        raise RuntimeError('CREON KOSPI/KOSDAQ 종목 목록이 비어 있습니다.')

    targets = [row for row in coverage if (
        row[3] is None or pd.Timestamp(row[3]).normalize() < end_date
    )]
    if not targets:
        return None

    missing_history = any(row[3] is None for row in targets)
    if missing_history:
        start_date = end_date - pd.Timedelta(
            days=max(lookback * 3, 90)
        )
    else:
        start_date = min(pd.Timestamp(row[3]) for row in targets)
        start_date = start_date + pd.Timedelta(days=1)

    if start_date.normalize() > end_date.normalize():
        return None

    factory = updater_factory or _load_updater_factory()
    updater = factory()
    try:
        updater.update_daily_price(
            start_date.strftime('%Y-%m-%d'),
            end_date.strftime('%Y-%m-%d'),
            stock_codes=[(row[2], row[1]) for row in targets],
        )
    finally:
        close_updater = getattr(updater, 'close', None)
        if callable(close_updater):
            close_updater()
        else:
            updater_connection = getattr(updater, 'conn', None)
            if updater_connection is not None:
                updater_connection.close()
    return start_date.normalize(), end_date.normalize()


def ensure_indicator_price_table(connection):
    with connection.cursor() as cursor:
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS {INDICATOR_PRICE_TABLE} (
                ticker VARCHAR(32) NOT NULL,
                trade_date DATE NOT NULL,
                adjusted_close DECIMAL(20, 8) NOT NULL,
                source VARCHAR(32) NOT NULL DEFAULT 'yahoo',
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, trade_date),
                KEY ix_daytrading_indicator_date (trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        ''')


def save_indicator_closes_to_database(connection, indicator_closes):
    if indicator_closes.empty:
        raise ValueError('저장할 해외 지표 종가가 없습니다.')
    ensure_indicator_price_table(connection)

    closes = indicator_closes.copy()
    dates = pd.DatetimeIndex(pd.to_datetime(closes.index))
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    closes.index = dates.normalize()
    closes = closes.sort_index()
    closes.columns = [str(ticker).strip().upper() for ticker in closes.columns]
    values = [
        (ticker, trade_date.date(), float(value))
        for trade_date, row in closes.iterrows()
        for ticker, value in row.items()
        if pd.notna(value)
    ]
    if not values:
        raise ValueError('저장할 유효한 해외 지표 종가가 없습니다.')

    try:
        with connection.cursor() as cursor:
            cursor.executemany(
                f'INSERT INTO {INDICATOR_PRICE_TABLE} '
                '(ticker, trade_date, adjusted_close, source) '
                'VALUES (%s, %s, %s, %s) '
                'ON DUPLICATE KEY UPDATE '
                'adjusted_close = VALUES(adjusted_close), source = VALUES(source)',
                [(*row, 'yahoo') for row in values],
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(values)


def load_indicator_closes_from_database(
    connection, as_of_date, lookback=60
):
    analysis_date = pd.Timestamp(as_of_date).normalize()
    start_date = (
        analysis_date - pd.Timedelta(days=max(lookback * 3, 90))
    ).date()
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT trade_date, ticker, adjusted_close '
            f'FROM {INDICATOR_PRICE_TABLE} '
            'WHERE trade_date >= %s AND trade_date < %s '
            'ORDER BY trade_date, ticker',
            (start_date, analysis_date.date()),
        )
        rows = cursor.fetchall()
    if not rows:
        raise RuntimeError(
            f'{start_date} 이후 DB에 해외 지표 종가가 없습니다. '
            '먼저 지표 종가를 수집하세요.'
        )

    prices = pd.DataFrame(
        rows, columns=['date', 'ticker', 'adjusted_close']
    )
    prices['date'] = pd.to_datetime(prices['date']).dt.normalize()
    prices['ticker'] = prices['ticker'].astype(str).str.strip().str.upper()
    prices['adjusted_close'] = pd.to_numeric(
        prices['adjusted_close'], errors='coerce'
    )
    return prices.pivot(
        index='date', columns='ticker', values='adjusted_close'
    ).sort_index()