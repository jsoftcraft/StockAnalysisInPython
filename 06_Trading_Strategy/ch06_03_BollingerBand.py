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
    figure, axes = plt.subplots(
        rows,
        columns,
        figsize=(16, max(6, rows * 3.1)),
        squeeze=False,
    )

    for axis, (company, weight, prices) in zip(axes.flat, charts):
        axis.fill_between(
            prices.index,
            prices['lower'],
            prices['upper'],
            color='#b9d9e8',
            alpha=0.35,
        )
        axis.plot(
            prices.index, prices['close'], color='#202a44', label='종가', linewidth=1
        )
        axis.plot(
            prices.index,
            prices['middle'],
            color='#168aad',
            label='20일선',
            linewidth=1,
        )
        axis.plot(
            prices.index,
            prices['upper'],
            color='#e76f51',
            linestyle='--',
            label='상단 밴드',
            linewidth=0.9,
        )
        axis.plot(
            prices.index,
            prices['lower'],
            color='#e76f51',
            linestyle='--',
            label='하단 밴드',
            linewidth=0.9,
        )
        axis.set_title(f'{company} ({weight:.1%})', fontsize=10)
        axis.grid(True, alpha=0.25)
        axis.tick_params(axis='x', labelrotation=30, labelsize=8)
        axis.legend(loc='upper left', fontsize=7, ncol=2)

    for axis in axes.flat[len(charts):]:
        axis.set_visible(False)

    figure.suptitle(
        f'{market_name} Max-Sharpe Bollinger Bands '
        f'({pd.Timestamp(run_at):%Y-%m-%d %H:%M})'
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
    for market_name in ('KOSPI', 'KOSDAQ'):
        run_at, market_charts = load_latest_portfolio(connection, market_name)
        plot_market_page(market_name, run_at, market_charts)
finally:
    connection.close()

plt.show()