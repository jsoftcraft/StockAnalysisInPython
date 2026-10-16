import math
from datetime import datetime

import matplotlib.pyplot as plt
import pandas as pd
import pymysql


plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

MA_PERIODS = (5, 10, 15, 20, 25, 30)
TEST_CONDITIONS = ((20, 80, 20),)
FALLBACK_COMPANY_NAMES = {'009155': '삼성전기우'}


def load_latest_portfolio(connection, market_name, portfolio_type):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT run_at FROM portfolio_result "
            "WHERE market = %s AND portfolio_type = %s "
            "ORDER BY run_at DESC LIMIT 1",
            (market_name, portfolio_type),
        )
        latest_run = cursor.fetchone()
        if latest_run is None:
            raise ValueError(
                f'{market_name} {portfolio_type} 포트폴리오 기록이 없습니다.'
            )

        cursor.execute(
            "SELECT code, company, weight, data_end FROM portfolio_result "
            "WHERE market = %s AND portfolio_type = %s "
            "AND run_at = %s ORDER BY position_rank",
            (market_name, portfolio_type, latest_run[0]),
        )
        portfolio = cursor.fetchall()

        normalized_codes = {
            str(code)[1:] if str(code).startswith('A') else str(code)
            for code, _, _, _ in portfolio
        }
        company_names = {}
        if normalized_codes:
            placeholders = ', '.join(['%s'] * len(normalized_codes))
            cursor.execute(
                "SELECT code, company FROM company_info "
                f"WHERE code IN ({placeholders})",
                tuple(normalized_codes),
            )
            company_names = {
                str(code): company for code, company in cursor.fetchall()
            }

    charts = []
    with connection.cursor() as cursor:
        for code, company, weight, data_end in portfolio:
            normalized_code = (
                str(code)[1:] if str(code).startswith('A') else str(code)
            )
            company = (
                company_names.get(normalized_code)
                or FALLBACK_COMPANY_NAMES.get(normalized_code)
                or company
                or code
            )
            cursor.execute(
                "SELECT date, high, low, close, volume FROM daily_price "
                "WHERE code = %s AND date <= %s "
                "ORDER BY date DESC LIMIT 140",
                (code, data_end),
            )
            rows = cursor.fetchall()
            if not rows:
                print(f'{market_name} {company} ({code}): 일봉 데이터 없음')
                continue

            prices = pd.DataFrame(
                rows, columns=['date', 'high', 'low', 'close', 'volume']
            )
            prices['date'] = pd.to_datetime(prices['date'])
            prices = prices.set_index('date').sort_index()
            prices['bb_middle'] = prices['close'].rolling(20).mean()
            bb_stddev = prices['close'].rolling(20).std()
            prices['bb_upper'] = prices['bb_middle'] + 2 * bb_stddev
            prices['bb_lower'] = prices['bb_middle'] - 2 * bb_stddev
            for ma_period in MA_PERIODS:
                prices[f'ma{ma_period}'] = prices['close'].rolling(ma_period).mean()
            typical_price = (prices['high'] + prices['low'] + prices['close']) / 3
            money_flow = typical_price * prices['volume']
            price_change = typical_price.diff()
            positive_flow = money_flow.where(price_change > 0, 0).rolling(10).sum()
            negative_flow = money_flow.where(price_change < 0, 0).rolling(10).sum()
            total_flow = positive_flow + negative_flow
            prices['mfi10'] = 100 * positive_flow / total_flow.replace(0, float('nan'))
            prices.loc[total_flow == 0, 'mfi10'] = 50
            ma_columns = [f'ma{ma_period}' for ma_period in MA_PERIODS]
            prices = prices.dropna(
                subset=ma_columns + [
                    'mfi10', 'bb_middle', 'bb_upper', 'bb_lower'
                ]
            ).tail(120)
            if prices.empty:
                print(f'{market_name} {company} ({code}): 지표 계산 데이터 부족')
                continue
            charts.append((company, code, float(weight), prices))

    if not charts:
        raise ValueError(
            f'{market_name} {portfolio_type} 포트폴리오의 '
            'MFI/MA 데이터가 없습니다.'
        )
    return latest_run[0], charts


def ensure_trade_result_tables(connection):
    statements = [
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
    data_start = min(
        prices.index.min() for _, _, _, prices, _ in stock_results
    ).date()
    data_end = max(
        prices.index.max() for _, _, _, prices, _ in stock_results
    ).date()
    stock_rows = []
    event_rows = []

    for company, code, weight, prices, result in stock_results:
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
        '매도대금 전액을 동일 종목의 다음 매수 신호에 재투자',
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
        trailing_stop_price = peak_price * 0.95
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


def plot_market_page(
    connection, simulation_at, market_name, portfolio_type, portfolio_run_at, charts
):
    portfolio_weight = sum(weight for _, _, weight, _ in charts)
    condition_rows = []

    for ma_period, buy_threshold, sell_threshold in TEST_CONDITIONS:
        ma_column = f'ma{ma_period}'
        weighted_total_return = 0.0
        stock_results = []
        for company, code, weight, prices in charts:
            buy_signal = (
                (prices['mfi10'] <= buy_threshold)
                & (prices['close'] > prices[ma_column])
            )
            sell_signal = (
                (prices['mfi10'] >= sell_threshold)
                & (prices['close'] < prices[ma_column])
            )
            trade = calculate_reinvested_return(
                prices, buy_signal, sell_signal, initial_capital=weight
            )
            weighted_total_return += weight * trade['return_pct'] / 100
            stock_results.append((company, code, weight, prices, trade))

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
            '최종 수익률': weighted_total_return * 100,
        })

    result_frame = pd.DataFrame(condition_rows)
    print(f'\n{market_name} {portfolio_type} 조건별 최종 수익률 (%)')
    print(result_frame.to_string(index=False, formatters={
        '최종 수익률': '{:+.2f}'.format,
    }))

    condition = condition_rows[0]
    columns = 2
    rows = math.ceil(len(stock_results) / columns)
    figure, axes = plt.subplots(
        rows,
        columns,
        figsize=(16, max(9, rows * 3.6)),
        squeeze=False,
    )
    for chart_index, (company, code, weight, prices, trade) in enumerate(
        stock_results
    ):
        axis = axes.flat[chart_index]
        axis.fill_between(
            prices.index,
            prices['bb_lower'],
            prices['bb_upper'],
            color='#b9d9e8',
            alpha=0.35,
            label='볼린저 밴드',
        )
        axis.plot(
            prices.index, prices['close'], color='#202a44', label='종가', linewidth=1
        )
        axis.plot(
            prices.index,
            prices['bb_middle'],
            color='#168aad',
            label='중심선 (20일)',
            linewidth=0.9,
        )
        axis.plot(
            prices.index,
            prices['bb_upper'],
            color='#e76f51',
            linestyle='--',
            label='상단 밴드',
            linewidth=0.8,
        )
        axis.plot(
            prices.index,
            prices['bb_lower'],
            color='#e76f51',
            linestyle='--',
            label='하단 밴드',
            linewidth=0.8,
        )
        if trade['buy_events']:
            axis.scatter(
                [event['date'] for event in trade['buy_events']],
                [event['price'] for event in trade['buy_events']],
                color='#d1495b',
                marker='^',
                s=40,
                label='매수',
                zorder=3,
            )
        sell_events = [
            event for event in trade['exit_events']
            if event['reason'] == 'sell_signal'
        ]
        if sell_events:
            axis.scatter(
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
        ]
        if stop_events:
            axis.scatter(
                [event['date'] for event in stop_events],
                [event['price'] for event in stop_events],
                color='#f4a261',
                marker='X',
                s=46,
                label='트레일링 스탑',
                zorder=4,
            )
        axis.set_title(
            f'{company} ({code}, 비중 {weight:.1%}) '
            f'| 수익률 {trade["return_pct"]:+.2f}%'
        )
        axis.grid(True, alpha=0.25)
        axis.tick_params(axis='x', labelrotation=25, labelsize=8)
        axis.legend(loc='upper left', fontsize=7, ncol=3)

    for unused_axis in axes.flat[len(stock_results):]:
        unused_axis.set_visible(False)

    figure.suptitle(
        f'{market_name} {portfolio_type} 볼린저 밴드 매매 결과 '
        f'(MA {condition["MA 기간"]}일, '
        f'최종 수익률 {condition["최종 수익률"]:+.2f}%)'
    )
    figure.tight_layout(rect=(0, 0, 1, 0.96))


connection = pymysql.connect(
    host='localhost',
    user='root',
    password='root',
    database='zunDB',
    charset='utf8',
)
try:
    ensure_trade_result_tables(connection)
    simulation_at = datetime.now()
    for market_name in ('KOSPI', 'KOSDAQ'):
        for portfolio_type in ('max_sharpe',):
            portfolio_run_at, market_charts = load_latest_portfolio(
                connection, market_name, portfolio_type
            )
            plot_market_page(
                connection,
                simulation_at,
                market_name,
                portfolio_type,
                portfolio_run_at,
                market_charts,
            )
finally:
    connection.close()

plt.show()