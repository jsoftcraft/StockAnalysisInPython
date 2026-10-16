import math

import matplotlib.pyplot as plt
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
                "SELECT date, close FROM daily_price "
                "WHERE code = %s AND date <= %s "
                "ORDER BY date DESC LIMIT 139",
                (code, data_end),
            )
            rows = cursor.fetchall()
            if not rows:
                print(f'{market_name} {company} ({code}): 일봉 데이터 없음')
                continue

            prices = pd.DataFrame(rows, columns=['date', 'close'])
            prices['date'] = pd.to_datetime(prices['date'])
            prices = prices.set_index('date').sort_index()
            prices['middle'] = prices['close'].rolling(20).mean()
            stddev = prices['close'].rolling(20).std()
            prices['upper'] = prices['middle'] + 2 * stddev
            prices['lower'] = prices['middle'] - 2 * stddev
            prices['bandwidth'] = (
                (prices['upper'] - prices['lower'])
                / prices['middle'].replace(0, float('nan'))
                * 100
            )
            prices = prices.dropna().tail(120)
            if prices.empty:
                print(f'{market_name} {company} ({code}): 밴드 계산 데이터 부족')
                continue
            charts.append((company, float(weight), prices))

    if not charts:
        raise ValueError(f'{market_name} 포트폴리오의 볼린저 밴드 데이터가 없습니다.')
    return latest_run[0], charts


def plot_market_page(market_name, run_at, charts):
    columns = 2
    rows = math.ceil(len(charts) / columns)
    figure = plt.figure(figsize=(16, max(10, rows * 4.2)))
    page_grid = figure.add_gridspec(rows, columns, hspace=0.38, wspace=0.2)

    for chart_index, (company, weight, prices) in enumerate(charts):
        outer_row, outer_column = divmod(chart_index, columns)
        stock_grid = page_grid[outer_row, outer_column].subgridspec(
            2, 1, height_ratios=(3, 1), hspace=0.06
        )
        price_axis = figure.add_subplot(stock_grid[0])
        bandwidth_axis = figure.add_subplot(stock_grid[1], sharex=price_axis)

        price_axis.fill_between(
            prices.index,
            prices['lower'],
            prices['upper'],
            color='#b9d9e8',
            alpha=0.35,
        )
        price_axis.plot(
            prices.index,
            prices['close'],
            color='#202a44',
            label='종가',
            linewidth=1,
        )
        price_axis.plot(
            prices.index,
            prices['middle'],
            color='#168aad',
            label='20일선',
            linewidth=1,
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
        price_axis.set_title(f'{company} ({weight:.1%})', fontsize=10)
        price_axis.grid(True, alpha=0.25)
        price_axis.tick_params(axis='x', labelbottom=False)
        price_axis.legend(loc='upper left', fontsize=7, ncol=2)

        bandwidth_axis.plot(
            prices.index,
            prices['bandwidth'],
            color='#c4458a',
            label='BandWidth (%)',
            linewidth=1,
        )
        bandwidth_axis.grid(True, alpha=0.25)
        bandwidth_axis.tick_params(axis='x', labelrotation=30, labelsize=7)
        bandwidth_axis.tick_params(axis='y', labelsize=7)
        bandwidth_axis.legend(loc='upper left', fontsize=7)

    figure.suptitle(
        f'{market_name} Max-Sharpe Bollinger BandWidth '
        f'({pd.Timestamp(run_at):%Y-%m-%d %H:%M})'
    )
    figure.tight_layout(rect=(0, 0, 1, 0.97))


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