from bisect import bisect_right

import pandas as pd


TOP_STOCK_COUNT = 10
MIN_AVERAGE_TURNOVER = 1_000_000_000
MAX_RETURN_CORRELATION = 0.85
TRANSACTION_COST_RATE = 0.0015
TRAILING_STOP_RATE = 0.04


def prepare_selection_indicators(prices):
    required_columns = {'high', 'low', 'close', 'volume'}
    missing_columns = required_columns.difference(prices.columns)
    if missing_columns:
        raise ValueError(
            f'종목 선정 지표에 필요한 가격 컬럼이 없습니다: '
            f'{sorted(missing_columns)}'
        )

    indicators = prices.sort_index().copy()
    close = indicators['close'].astype(float)
    high = indicators['high'].astype(float)
    low = indicators['low'].astype(float)
    volume = indicators['volume'].astype(float)

    indicators['ma20'] = close.rolling(20).mean()
    indicators['ma60'] = close.rolling(60).mean()
    indicators['ma20_change_5'] = indicators['ma20'].pct_change(5)
    indicators['return_1m'] = close.pct_change(21)
    indicators['return_3m'] = close.pct_change(63)
    indicators['average_turnover_20'] = (close * volume).rolling(20).mean()

    typical_price = (high + low + close) / 3
    money_flow = typical_price * volume
    typical_change = typical_price.diff()
    positive_flow = money_flow.where(typical_change > 0, 0).rolling(10).sum()
    negative_flow = money_flow.where(typical_change < 0, 0).rolling(10).sum()
    total_flow = positive_flow + negative_flow
    indicators['mfi10'] = (
        100 * positive_flow / total_flow.replace(0, float('nan'))
    )
    indicators.loc[total_flow == 0, 'mfi10'] = 50
    indicators['mfi_change_3'] = indicators['mfi10'].diff(3)

    bb_middle = close.rolling(20).mean()
    bb_stddev = close.rolling(20).std()
    bb_upper = bb_middle + 2 * bb_stddev
    bb_lower = bb_middle - 2 * bb_stddev
    band_width = (bb_upper - bb_lower) / bb_middle
    indicators['percent_b'] = (close - bb_lower) / (bb_upper - bb_lower)
    indicators['band_width_change_5'] = band_width.pct_change(5)
    return indicators


def load_candidate_snapshots(connection, market, start_date, end_date):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT snapshot_date, code, company, updated_at "
            "FROM selected_stock_history "
            "WHERE market = %s AND snapshot_date BETWEEN %s AND %s "
            "ORDER BY snapshot_date, selection_rank",
            (market, start_date, end_date),
        )
        rows = cursor.fetchall()

    snapshots = {}
    company_names = {}
    latest_update = None
    for snapshot_date, code, company, updated_at in rows:
        snapshot_date = pd.Timestamp(snapshot_date).normalize()
        snapshots.setdefault(snapshot_date, []).append(str(code))
        company_names[str(code)] = company or str(code)
        if latest_update is None or updated_at > latest_update:
            latest_update = updated_at
    return snapshots, company_names, latest_update


def selection_filter_conditions(indicators, min_average_turnover=MIN_AVERAGE_TURNOVER):
    return {
        '종가>MA20': indicators['close'] > indicators['ma20'],
        'MA20>MA60': indicators['ma20'] > indicators['ma60'],
        'MA20 5일 변화율>0': indicators['ma20_change_5'] > 0,
        '3개월 수익률>0': indicators['return_3m'] > 0,
        f'20일 평균 거래대금>={min_average_turnover:,.0f}원': (
            indicators['average_turnover_20'] >= min_average_turnover
        ),
        'MFI10>=40': indicators['mfi10'] >= 40,
        'MFI10<=80': indicators['mfi10'] <= 80,
        'MFI 3일 변화량>0': indicators['mfi_change_3'] > 0,
        '%B>=0.5': indicators['percent_b'] >= 0.5,
        '%B<=1.2': indicators['percent_b'] <= 1.2,
    }


def rank_daily_top_stocks(
    histories,
    limit=TOP_STOCK_COUNT,
    min_average_turnover=MIN_AVERAGE_TURNOVER,
    max_return_correlation=MAX_RETURN_CORRELATION,
    lag=1,
    candidate_snapshots=None,
):
    if not histories:
        return {}

    columns = (
        'close', 'ma20', 'ma60', 'ma20_change_5', 'return_1m', 'return_3m',
        'average_turnover_20', 'mfi10', 'mfi_change_3', 'percent_b',
        'band_width_change_5',
    )
    matrices = {
        column: pd.concat(
            {code: history[column] for code, history in histories.items()},
            axis=1,
        ).sort_index()
        for column in columns
    }
    prior = {column: values.shift(lag) for column, values in matrices.items()}

    conditions = list(
        selection_filter_conditions(prior, min_average_turnover).values()
    )
    eligible = conditions[0]
    for condition in conditions[1:]:
        eligible = eligible & condition

    close_prices = matrices['close']
    prior_returns = close_prices.pct_change().shift(lag)
    selected = pd.DataFrame(False, index=eligible.index, columns=eligible.columns)
    snapshot_dates = (
        sorted(candidate_snapshots) if candidate_snapshots is not None else []
    )
    factor_weights = {
        'return_3m': 0.40,
        'return_1m': 0.30,
        'mfi_change_3': 0.20,
        'percent_b': 0.05,
        'band_width_change_5': 0.05,
    }
    for row_index, date in enumerate(eligible.index):
        if candidate_snapshots is not None:
            feature_row = row_index - lag
            if feature_row < 0:
                continue
            feature_date = eligible.index[feature_row]
            snapshot_index = bisect_right(snapshot_dates, feature_date) - 1
            if snapshot_index < 0:
                continue
            eligible_codes = candidate_snapshots[snapshot_dates[snapshot_index]]
            eligible_codes = [
                code for code in eligible_codes if code in eligible.columns
            ]
        else:
            eligible_codes = list(eligible.columns)

        if not eligible_codes:
            continue
        factor_ranks = {
            column: prior[column].iloc[row_index]
            .reindex(eligible_codes).rank(pct=True)
            for column in factor_weights
        }
        daily_scores = sum(
            factor_ranks[column] * weight
            for column, weight in factor_weights.items()
        )
        daily_scores = daily_scores.where(
            eligible.iloc[row_index].reindex(eligible_codes, fill_value=False)
        ).dropna().sort_values(ascending=False, kind='mergesort')
        if daily_scores.empty:
            continue

        return_window = prior_returns.loc[:date].tail(60)
        correlations = return_window.corr(min_periods=20)
        chosen = []
        for code in daily_scores.index:
            if any(
                correlations.at[code, selected_code] > max_return_correlation
                for selected_code in chosen
                if code in correlations.index
                and selected_code in correlations.columns
                and pd.notna(correlations.at[code, selected_code])
            ):
                continue
            chosen.append(code)
            if len(chosen) == limit:
                break
        if chosen:
            selected.loc[date, chosen] = True

    return {
        code: selected[code].reindex(history.index, fill_value=False)
        for code, history in histories.items()
    }


def simulate_shared_capital(
    histories,
    buy_signals,
    sell_signals,
    max_positions=TOP_STOCK_COUNT,
    initial_capital=1.0,
    transaction_cost_rate=TRANSACTION_COST_RATE,
    trailing_stop_rate=TRAILING_STOP_RATE,
):
    if not histories:
        raise ValueError('백테스트 가격 이력이 없습니다.')

    dates = sorted(set().union(*(set(history.index) for history in histories.values())))
    buy_on_bar = {
        code: buy_signals[code].reindex(history.index, fill_value=False)
        .shift(1).fillna(False)
        for code, history in histories.items()
    }
    sell_on_bar = {
        code: sell_signals[code].reindex(history.index, fill_value=False)
        .shift(1).fillna(False)
        for code, history in histories.items()
    }
    stock_stats = {
        code: {
            'allocated': 0.0,
            'realized_profit': 0.0,
            'buy_events': [],
            'exit_events': [],
            'trade_events': [],
            'first_buy_date': None,
            'buy_count': 0,
        }
        for code in histories
    }
    positions = {}
    exit_dates = {}
    cash = float(initial_capital)
    daily_values = []

    def row_for(code, date):
        history = histories[code]
        if date not in history.index:
            return None
        return history.loc[date]

    for date in dates:
        exited_today = set()
        for code in list(positions):
            row = row_for(code, date)
            if row is None:
                continue
            position = positions[code]
            open_price = float(row['open'])
            low_price = float(row['low'])
            high_price = float(row['high'])
            stop_price = position['peak_price'] * (1 - trailing_stop_rate)
            exit_price = None
            reason = None
            if open_price <= stop_price:
                exit_price = open_price
                reason = 'trailing_stop'
            elif low_price <= stop_price:
                exit_price = stop_price
                reason = 'trailing_stop'
            elif bool(sell_on_bar[code].get(date, False)):
                exit_price = open_price
                reason = 'sell_signal'

            if exit_price is None:
                position['peak_price'] = max(position['peak_price'], high_price)
                position['last_price'] = float(row['close'])
                continue

            cash_before = cash
            proceeds = (
                position['shares'] * exit_price * (1 - transaction_cost_rate)
            )
            cash += proceeds
            trade_return = (
                proceeds / position['cost_basis'] - 1
            ) * 100
            event = {
                'date': date,
                'price': exit_price,
                'reason': reason,
                'quantity': position['shares'],
                'cash_before': cash_before,
                'cash_after': cash,
                'shares_after': 0.0,
                'trade_return_pct': trade_return,
            }
            stock_stats[code]['realized_profit'] += (
                proceeds - position['cost_basis']
            )
            stock_stats[code]['exit_events'].append(event)
            stock_stats[code]['trade_events'].append(event)
            del positions[code]
            exited_today.add(code)
            exit_dates[code] = date.normalize()

        eligible_buys = [
            code for code in histories
            if code not in positions
            and code not in exited_today
            and exit_dates.get(code) != date.normalize()
            and row_for(code, date) is not None
            and bool(buy_on_bar[code].get(date, False))
        ]
        eligible_buys.sort()
        slots = max_positions - len(positions)
        nav_at_open = cash
        for code, position in positions.items():
            row = row_for(code, date)
            mark_price = (
                position['last_price'] if row is None else float(row['open'])
            )
            nav_at_open += position['shares'] * mark_price
        target_budget = nav_at_open / max_positions
        for code in eligible_buys[:slots]:
            row = row_for(code, date)
            open_price = float(row['open'])
            if open_price <= 0 or cash <= 0:
                continue
            budget = min(cash, target_budget)
            if budget <= 0:
                continue
            entry_price = open_price * (1 + transaction_cost_rate)
            shares = budget / entry_price
            cash_before = cash
            cash -= budget
            position = {
                'shares': shares,
                'cost_basis': budget,
                'entry_price': entry_price,
                'entry_date': date,
                'peak_price': open_price,
                'last_price': float(row['close']),
            }
            positions[code] = position
            stats = stock_stats[code]
            stats['allocated'] += budget
            stats['buy_count'] += 1
            if stats['first_buy_date'] is None:
                stats['first_buy_date'] = date
            event = {
                'date': date,
                'price': open_price,
                'quantity': shares,
                'cash_before': cash_before,
                'cash_after': cash,
                'shares_after': shares,
                'reason': 'buy_signal',
            }
            stats['buy_events'].append(event)
            stats['trade_events'].append(event)

        for code, position in positions.items():
            row = row_for(code, date)
            if row is not None:
                position['peak_price'] = max(
                    position['peak_price'], float(row['high'])
                )
                position['last_price'] = float(row['close'])
        daily_values.append(
            cash + sum(
                position['shares'] * position['last_price']
                for position in positions.values()
            )
        )

    final_value = daily_values[-1] if daily_values else cash
    stock_results = {}
    for code, stats in stock_stats.items():
        position = positions.get(code)
        profit = stats['realized_profit']
        if position is not None:
            unrealized_value = position['shares'] * position['last_price']
            profit += unrealized_value - position['cost_basis']
            stats['trade_events'].append({
                'date': histories[code].index[-1],
                'price': position['last_price'],
                'reason': 'final_mark',
                'quantity': position['shares'],
                'cash_before': cash,
                'cash_after': cash,
                'shares_after': position['shares'],
                'trade_return_pct': (
                    unrealized_value / position['cost_basis'] - 1
                ) * 100,
            })
        if stats['buy_count'] == 0:
            status = '선정/매수 신호 없음'
        elif position is not None:
            status = f'{stats["buy_count"] - 1}회 재진입, 최종 종가 평가'
        else:
            status = f'{stats["buy_count"] - 1}회 재진입, 매도 후 현금 대기'
        allocated = stats['allocated']
        stock_results[code] = {
            'first_buy_date': stats['first_buy_date'],
            'valuation_date': histories[code].index[-1],
            'return_pct': profit / allocated * 100 if allocated else 0.0,
            'status': status,
            'buy_count': stats['buy_count'],
            'reinvestment_count': max(stats['buy_count'] - 1, 0),
            'exit_events': stats['exit_events'],
            'buy_events': stats['buy_events'],
            'trade_events': stats['trade_events'],
            'is_holding': position is not None,
            'initial_capital': allocated,
            'ending_value': allocated + profit,
        }

    return {
        'return_pct': (final_value / initial_capital - 1) * 100,
        'final_value': final_value,
        'daily_values': pd.Series(daily_values, index=dates),
        'stock_results': stock_results,
    }