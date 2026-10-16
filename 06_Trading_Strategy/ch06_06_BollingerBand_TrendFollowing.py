import math

import matplotlib.pyplot as plt
from matplotlib.markers import MarkerStyle
import numpy as np
import pandas as pd
import pymysql


plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False


def load_latest_portfolio(connection, market_name):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT run_at FROM portfolio_result "
            "WHERE market = %s AND portfolio_type = 'max_sharpe' "
            "ORDER BY run_at DESC LIMIT 1",
            (market_name,),
        )
        latest_run = cursor.fetchone()
        if latest_run is None:
            raise ValueError(
                f'{market_name} max_sharpe 포트폴리오 기록이 없습니다.'
            )

        cursor.execute(
            "SELECT code, company, weight, data_end FROM portfolio_result "
            "WHERE market = %s AND portfolio_type = 'max_sharpe' "
            "AND run_at = %s ORDER BY position_rank",
            (market_name, latest_run[0]),
        )
        portfolio = cursor.fetchall()

    charts = []
    with connection.cursor() as cursor:
        for code, company, weight, data_end in portfolio:
            cursor.execute(
                "SELECT date, high, low, close, volume FROM daily_price "
                "WHERE code = %s AND date <= %s "
                "ORDER BY date DESC LIMIT 139",
                (code, data_end),
            )
            rows = cursor.fetchall()
            if not rows:
                print(f'{market_name} {company} ({code}): daily data missing')
                continue

            prices = pd.DataFrame(
                rows, columns=['date', 'high', 'low', 'close', 'volume']
            )
            prices['date'] = pd.to_datetime(prices['date'])
            prices = prices.set_index('date').sort_index()
            prices['middle'] = prices['close'].rolling(20).mean()
            stddev = prices['close'].rolling(20).std()
            prices['upper'] = prices['middle'] + 2 * stddev
            prices['lower'] = prices['middle'] - 2 * stddev
            band_width = (prices['upper'] - prices['lower']).replace(
                0, float('nan')
            )
            prices['percent_b'] = (prices['close'] - prices['lower']) / band_width

            typical_price = (prices['high'] + prices['low'] + prices['close']) / 3
            raw_money_flow = typical_price * prices['volume']
            price_change = typical_price.diff()
            positive_flow = raw_money_flow.where(price_change > 0, 0)
            negative_flow = raw_money_flow.where(price_change < 0, 0)
            positive_sum = positive_flow.rolling(10).sum()
            negative_sum = negative_flow.rolling(10).sum()
            money_flow_ratio = positive_sum / negative_sum.replace(0, float('nan'))
            prices['mfi10'] = 100 - 100 / (1 + money_flow_ratio)
            prices.loc[(negative_sum == 0) & (positive_sum > 0), 'mfi10'] = 100

            prices = prices.dropna().tail(120)
            if prices.empty:
                print(f'{market_name} {company} ({code}): indicator data insufficient')
                continue
            charts.append((company, code, float(weight), prices))

    if not charts:
        raise ValueError(f'{market_name} portfolio has no trend-indicator data.')
    return latest_run[0], charts


def calculate_first_trade_return(prices, buy_signal, sell_signal):
    buy_positions = np.flatnonzero(buy_signal.to_numpy())
    if buy_positions.size == 0:
        return None

    buy_position = int(buy_positions[0])
    buy_price = float(prices['close'].iloc[buy_position])
    exit_position = None
    stop_loss_fill_price = None
    exit_reason = 'final_mark'
    status = None
    loss_sell_signal_seen = False

    for position in range(buy_position + 1, len(prices)):
        current_price = float(prices['close'].iloc[position])
        current_return = current_price / buy_price - 1
        if current_return <= -0.10:
            exit_position = position
            stop_loss_fill_price = buy_price * 0.90
            exit_reason = 'stop_loss'
            status = '손절 매도 (-10% 고정)'
            break

        if sell_signal.iloc[position]:
            if current_price >= buy_price:
                exit_position = position
                exit_reason = 'sell_signal'
                status = '본전 이상 매도 완료'
                break
            loss_sell_signal_seen = True

    if exit_position is None:
        exit_position = len(prices) - 1
        if loss_sell_signal_seen:
            status = '손실 매도 신호 보류, 최종 종가 평가'
        else:
            status = '매도 신호 없음, 최종 종가 평가'

    exit_price = (
        stop_loss_fill_price
        if stop_loss_fill_price is not None
        else float(prices['close'].iloc[exit_position])
    )
    return_pct = (exit_price / buy_price - 1) * 100
    return {
        'buy_date': prices.index[buy_position],
        'exit_date': prices.index[exit_position],
        'exit_price': exit_price,
        'return_pct': return_pct,
        'status': status,
        'exit_reason': exit_reason,
    }


def plot_market_page(market_name, run_at, charts):
    columns = 2
    rows = math.ceil(len(charts) / columns)
    weighted_total_return = 0.0
    entered_weight = 0.0
    trade_rows = []
    figure = plt.figure(figsize=(16, max(10, rows * 4.5)))
    page_grid = figure.add_gridspec(rows, columns, hspace=0.4, wspace=0.2)

    for chart_index, (company, code, weight, prices) in enumerate(charts):
        outer_row, outer_column = divmod(chart_index, columns)
        stock_grid = page_grid[outer_row, outer_column].subgridspec(
            2, 1, height_ratios=(3, 1), hspace=0.06
        )
        price_axis = figure.add_subplot(stock_grid[0])
        signal_axis = figure.add_subplot(stock_grid[1], sharex=price_axis)

        upper_signal = (prices['percent_b'] > 0.8) & (prices['mfi10'] > 80)
        lower_signal = (prices['percent_b'] < 0.2) & (prices['mfi10'] < 20)
        trade = calculate_first_trade_return(prices, upper_signal, lower_signal)
        if trade is None:
            trade_label = '첫 매수 신호 없음 | 수익률 N/A'
            trade_rows.append({
                '종목명': company,
                '종목코드': code,
                '비중': f'{weight:.2%}',
                '매수일': '-',
                '매도/평가일': '-',
                '수익률': 'N/A',
                '상태': '매수 신호 없음',
            })
        else:
            weighted_total_return += weight * trade['return_pct'] / 100
            entered_weight += weight
            buy_date = trade['buy_date'].strftime('%Y-%m-%d')
            exit_date = trade['exit_date'].strftime('%Y-%m-%d')
            trade_label = (
                f"수익률 {trade['return_pct']:+.2f}% | "
                f"매수 {buy_date} → 매도/최종평가 {exit_date}"
            )
            trade_rows.append({
                '종목명': company,
                '종목코드': code,
                '비중': f'{weight:.2%}',
                '매수일': buy_date,
                '매도/평가일': exit_date,
                '수익률': f"{trade['return_pct']:+.2f}%",
                '상태': trade['status'],
            })

        price_axis.fill_between(
            prices.index,
            prices['lower'],
            prices['upper'],
            color='#b9d9e8',
            alpha=0.35,
        )
        price_axis.plot(
            prices.index, prices['close'], color='#202a44', label='종가', linewidth=1
        )
        price_axis.plot(
            prices.index, prices['middle'], color='#168aad', label='20일선', linewidth=1
        )
        price_axis.plot(
            prices.index,
            prices['upper'],
            color='#e76f51',
            linestyle='--',
            label='상단 밴드',
            linewidth=0.9,
        )
        price_axis.plot(
            prices.index,
            prices['lower'],
            color='#e76f51',
            linestyle='--',
            label='하단 밴드',
            linewidth=0.9,
        )
        price_axis.scatter(
            prices.index[upper_signal],
            prices.loc[upper_signal, 'close'],
            color='#d1495b',
            marker=MarkerStyle('^'),
            s=34,
            label='상단 신호',
            zorder=3,
        )
        price_axis.scatter(
            prices.index[lower_signal],
            prices.loc[lower_signal, 'close'],
            color='#168aad',
            marker=MarkerStyle('v'),
            s=34,
            label='하단 신호',
            zorder=3,
        )
        if trade is not None and trade['exit_reason'] == 'stop_loss':
            price_axis.scatter(
                [trade['exit_date']],
                [trade['exit_price']],
                color='#f4a261',
                marker=MarkerStyle('X'),
                s=48,
                label='-10% 손절',
                zorder=4,
            )
        price_axis.set_title(
            f'{company} ({code}, {weight:.1%}) | {trade_label}', fontsize=9
        )
        price_axis.grid(True, alpha=0.25)
        price_axis.tick_params(axis='x', labelbottom=False)
        price_axis.legend(loc='upper left', fontsize=7, ncol=3)

        signal_axis.plot(
            prices.index,
            prices['percent_b'] * 100,
            color='#168aad',
            label='%B x 100',
            linewidth=1,
        )
        signal_axis.plot(
            prices.index,
            prices['mfi10'],
            color='#c4458a',
            linestyle='--',
            label='MFI (10일)',
            linewidth=1,
        )
        signal_axis.scatter(
            prices.index[upper_signal],
            np.zeros(upper_signal.sum()),
            color='#d1495b',
            marker=MarkerStyle('^'),
            s=22,
        )
        signal_axis.scatter(
            prices.index[lower_signal],
            np.zeros(lower_signal.sum()),
            color='#168aad',
            marker=MarkerStyle('v'),
            s=22,
        )
        signal_axis.set_ylim(-20, 120)
        signal_axis.set_yticks([-20, 0, 20, 40, 60, 80, 100, 120])
        signal_axis.grid(True, alpha=0.25)
        signal_axis.tick_params(axis='x', labelrotation=30, labelsize=7)
        signal_axis.tick_params(axis='y', labelsize=7)
        signal_axis.legend(loc='upper left', fontsize=7, ncol=2)

    summary = (
        f'비중가중 전체 수익률 {weighted_total_return:+.2f}% '
        f'(매수 진입 비중 {entered_weight:.1%})'
    )
    print(f'\n{market_name} 종목별 거래 결과')
    print(pd.DataFrame(trade_rows).to_string(index=False))
    summary_rows = [{
        '시장': market_name,
        '전체 수익률': f'{weighted_total_return:+.2f}%',
        '매수 진입 비중': f'{entered_weight:.1%}',
        '기준': '매도 후 재투자 없음, 미진입 비중은 현금',
    }]
    print('\n시장 전체 수익률')
    print(pd.DataFrame(summary_rows).to_string(index=False))
    figure.suptitle(
        f'{market_name} Max-Sharpe Bollinger Trend Following '
        f'({pd.Timestamp(run_at):%Y-%m-%d %H:%M})\n{summary}'
    )
    figure.subplots_adjust(top=0.94)


connection = pymysql.connect(
    host='localhost',
    user='root',
    password='root',
    database='zunDB',
    charset='utf8',
)
try:
    for market_name in ('KOSPI', 'KOSDAQ'):
        run_at, market_charts = load_latest_portfolio(connection, market_name)
        plot_market_page(market_name, run_at, market_charts)
finally:
    connection.close()

plt.show()
