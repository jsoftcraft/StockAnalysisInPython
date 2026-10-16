import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Qt5Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
import pymysql
from datetime import datetime

plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

def load_market_prices(connection, market_name):
    end_timestamp = pd.Timestamp.today().normalize()
    start_timestamp = end_timestamp - pd.DateOffset(months=3)
    start_date = start_timestamp.strftime('%Y-%m-%d')
    end_date = end_timestamp.strftime('%Y-%m-%d')
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT code, company FROM selected_stock "
            "WHERE market = %s ORDER BY selection_rank",
            (market_name,),
        )
        selected = cursor.fetchall()
        if not selected:
            raise ValueError(
                f'{market_name} selected_stock 테이블에 선정 종목이 없습니다.'
            )

        codes = [row[0] for row in selected]
        company_names = {row[0]: row[1] or row[0] for row in selected}
        placeholders = ', '.join(['%s'] * len(codes))

        cursor.execute(
            f"SELECT code, date, close FROM daily_price "
            f"WHERE code IN ({placeholders}) AND date BETWEEN %s AND %s "
            "ORDER BY date, code",
            (*codes, start_date, end_date),
        )
        rows = cursor.fetchall()
    if not rows:
        raise ValueError(
            f'{market_name} 선정 종목의 {start_date} 이후 일봉 데이터가 없습니다.'
        )

    price_rows = pd.DataFrame(rows, columns=['code', 'date', 'close'])
    price_rows['date'] = pd.to_datetime(price_rows['date'])
    prices = price_rows.pivot(index='date', columns='code', values='close')
    available_codes = [code for code in codes if code in prices.columns]
    missing_codes = [code for code in codes if code not in prices.columns]
    if missing_codes:
        print(
            f'{market_name}: 일봉 데이터가 없는 선정 종목 '
            f'{len(missing_codes)}개 제외',
            flush=True,
        )
    prices = prices.reindex(columns=available_codes).dropna()
    stocks = {
        f'{company_names[code]} ({code})': code
        for code in available_codes
    }
    if len(prices) < 2:
        raise ValueError(
            f'{market_name} 포트폴리오 분석에는 공통 거래일이 2일 이상 필요합니다.'
        )
    if len(stocks) < 10:
        raise ValueError(
            f'{market_name} 최적화에는 최소 10종목이 필요하지만 '
            f'{len(stocks)}종목의 데이터만 있습니다.'
        )
    actual_start = prices.index.min().strftime('%Y-%m-%d')
    actual_end = prices.index.max().strftime('%Y-%m-%d')
    print(
        f'{market_name}: 선정 {len(codes)}종목 중 {len(stocks)}종목 사용, '
        f'공통 데이터 {actual_start}~{actual_end}',
        flush=True,
    )
    return stocks, prices, actual_start, actual_end


def ensure_portfolio_result_table(connection):
    sql = """
    CREATE TABLE IF NOT EXISTS portfolio_result (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        run_at DATETIME NOT NULL,
        market VARCHAR(10) NOT NULL,
        portfolio_type VARCHAR(20) NOT NULL,
        position_rank SMALLINT NOT NULL,
        code VARCHAR(20) NOT NULL,
        company VARCHAR(100) NOT NULL,
        weight DECIMAL(12, 10) NOT NULL,
        expected_return DECIMAL(16, 10) NOT NULL,
        risk DECIMAL(16, 10) NOT NULL,
        sharpe DECIMAL(16, 10) NOT NULL,
        data_start DATE NOT NULL,
        data_end DATE NOT NULL,
        PRIMARY KEY (id),
        KEY portfolio_result_lookup (market, portfolio_type, run_at)
    )
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
    connection.commit()


def save_portfolios(connection, market_name, portfolios, stocks, start_date, end_date):
    run_at = datetime.now()
    rows = []
    for portfolio_type, portfolio in portfolios:
        result = portfolio.iloc[0]
        weights = result[list(stocks)]
        weights = weights[weights > 0].sort_values(ascending=False)
        for position_rank, (name, weight) in enumerate(weights.items(), start=1):
            rows.append((
                run_at,
                market_name,
                portfolio_type,
                position_rank,
                stocks[name],
                name.rsplit(' (', 1)[0],
                float(weight),
                float(result['Returns']),
                float(result['Risk']),
                float(result['Sharpe']),
                start_date,
                end_date,
            ))

    sql = """INSERT INTO portfolio_result
             (run_at, market, portfolio_type, position_rank, code, company,
              weight, expected_return, risk, sharpe, data_start, data_end)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
    try:
        with connection.cursor() as cursor:
            cursor.executemany(sql, rows)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    print(
        f'{market_name}: {len(rows)}개 포트폴리오 구성 행을 '
        'portfolio_result 테이블에 저장했습니다.',
        flush=True,
    )


def print_ranked_weights(title, portfolio_row, stocks):
    result = portfolio_row.iloc[0]
    weights = result[list(stocks)]
    weights = weights[weights > 0].sort_values(ascending=False) * 100
    print(f"\n{title}")
    print(
        f"연환산 수익률: {result['Returns']:.2%}, "
        f"위험도: {result['Risk']:.2%}, Sharpe: {result['Sharpe']:.4f}"
    )
    for rank, (stock, weight) in enumerate(weights.items(), start=1):
        print(f"{rank:02d}. {stock}: {weight:.2f}%")


def plot_selected_stock_candles(
    connection, portfolio_row, stocks, market_name, end_date
):
    result = portfolio_row.iloc[0]
    weights = result[list(stocks)]
    selected = weights[weights > 0].sort_values(ascending=False)
    figure, axes = plt.subplots(5, 2, figsize=(16, 14))

    with connection.cursor() as cursor:
        for axis, (name, weight) in zip(axes.flat, selected.items()):
            cursor.execute(
                "SELECT date, open, high, low, close, volume FROM daily_price "
                "WHERE code = %s AND date <= %s "
                "ORDER BY date DESC LIMIT 120",
                (stocks[name], end_date),
            )
            rows = cursor.fetchall()
            if not rows:
                raise ValueError(f'{market_name} {name}의 캔들 데이터가 없습니다.')

            prices = pd.DataFrame(
                rows,
                columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'],
            )
            prices['Date'] = pd.to_datetime(prices['Date'])
            prices = prices.set_index('Date').sort_index()
            moving_average_20 = prices['Close'].rolling(20).mean()
            moving_average_60 = prices['Close'].rolling(60).mean()
            addplots = [
                mpf.make_addplot(
                    moving_average_20,
                    ax=axis,
                    color='#168aad',
                    width=1.2,
                ),
                mpf.make_addplot(
                    moving_average_60,
                    ax=axis,
                    color='#e76f51',
                    width=1.2,
                ),
            ]
            mpf.plot(
                prices,
                type='candle',
                ax=axis,
                addplot=addplots,
                show_nontrading=False,
                datetime_format='%Y-%m',
                xrotation=45,
            )
            axis.plot([], [], color='#168aad', label='20-day MA')
            axis.plot([], [], color='#e76f51', label='60-day MA')
            axis.legend(loc='upper left', fontsize=7)
            axis.set_title(f'{name} ({weight:.1%})', fontsize=9)
            axis.grid(True, alpha=0.25)

    figure.suptitle(f'{market_name} Max-Sharpe Portfolio Candlesticks')
    figure.tight_layout(rect=(0, 0, 1, 0.97))
    manager = getattr(figure.canvas, 'manager', None)
    window = getattr(manager, 'window', None)
    if window is not None:
        window.showMaximized()


def sample_sparse_portfolios(
    annual_returns,
    annual_covariance,
    stock_names,
    holdings=10,
    iterations=100_000,
    seed=1,
):
    stock_count = len(stock_names)
    if holdings > stock_count:
        raise ValueError('보유 종목 수가 분석 대상보다 많습니다.')

    rng = np.random.default_rng(seed)
    batch_size = 5_000
    weight_batches = []
    return_batches = []
    risk_batches = []
    sharpe_batches = []
    expected_returns = annual_returns.to_numpy()
    covariance = annual_covariance.to_numpy()

    for batch_start in range(0, iterations, batch_size):
        current_batch_size = min(batch_size, iterations - batch_start)
        selected = np.argpartition(
            rng.random((current_batch_size, stock_count)),
            holdings - 1,
            axis=1,
        )[:, :holdings]
        selected_weights = rng.dirichlet(
            np.full(holdings, 0.7), size=current_batch_size
        )
        weights = np.zeros((current_batch_size, stock_count))
        weights[np.arange(current_batch_size)[:, None], selected] = selected_weights

        returns = weights @ expected_returns
        variance = np.einsum(
            'bi,ij,bj->b', weights, covariance, weights, optimize=True
        )
        risks = np.sqrt(np.maximum(variance, 0))
        sharpe = np.divide(
            returns,
            risks,
            out=np.full_like(returns, -np.inf),
            where=risks > 0,
        )

        weight_batches.append(weights)
        return_batches.append(returns)
        risk_batches.append(risks)
        sharpe_batches.append(sharpe)

    all_weights = np.vstack(weight_batches)
    results = pd.DataFrame({
        'Returns': np.concatenate(return_batches),
        'Risk': np.concatenate(risk_batches),
        'Sharpe': np.concatenate(sharpe_batches),
    })
    for index, stock in enumerate(stock_names):
        results[stock] = all_weights[:, index]
    return results


def analyze_market(connection, market_name, seed):
    stocks, prices, start_date, end_date = load_market_prices(
        connection, market_name
    )
    daily_ret = prices.pct_change(fill_method=None)
    annual_ret = daily_ret.mean() * 252
    annual_cov = daily_ret.cov() * 252

    print(
        f'{market_name}: {len(stocks)}종목 중 10종목 조합 '
        '100,000개를 탐색합니다.',
        flush=True,
    )
    results = sample_sparse_portfolios(
        annual_ret,
        annual_cov,
        list(stocks),
        holdings=10,
        iterations=100_000,
        seed=seed,
    )

    max_sharpe = results.loc[[results['Sharpe'].idxmax()]]
    min_risk = results.loc[[results['Risk'].idxmin()]]
    save_portfolios(
        connection,
        market_name,
        [('max_sharpe', max_sharpe), ('min_risk', min_risk)],
        stocks,
        start_date,
        end_date,
    )
    print_ranked_weights(
        f'{market_name} max_sharpe 포트폴리오 비중 순위', max_sharpe, stocks
    )
    plot_selected_stock_candles(
        connection, max_sharpe, stocks, market_name, end_date
    )
    print_ranked_weights(
        f'{market_name} min_risk 포트폴리오 비중 순위', min_risk, stocks
    )

    axes = results.plot.scatter(
        x='Risk', y='Returns', c='Sharpe', cmap='viridis',
        edgecolors='k', figsize=(11, 7), grid=True,
    )
    axes.scatter(max_sharpe['Risk'], max_sharpe['Returns'], c='r', marker='*', s=300)
    axes.scatter(min_risk['Risk'], min_risk['Returns'], c='r', marker='X', s=200)
    axes.set_title(f'{market_name} Portfolio Optimization ({start_date} to {end_date})')
    axes.set_xlabel('Risk')
    axes.set_ylabel('Expected Returns')


connection = pymysql.connect(
    host='localhost',
    user='root',
    password='root',
    database='zunDB',
    charset='utf8',
)
try:
    ensure_portfolio_result_table(connection)
    analyze_market(connection, 'KOSPI', seed=1)
    analyze_market(connection, 'KOSDAQ', seed=2)
finally:
    connection.close()

plt.show()