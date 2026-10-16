import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Qt5Agg')
import matplotlib.pyplot as plt
import pymysql

stocks = {
    '삼성전자': '005930',
    'SK하이닉스': '000660',
    '현대자동차': '005380',
    'NAVER': '035420',
}
connection = pymysql.connect(
    host='localhost',
    user='root',
    password='root',
    database='zunDB',
    charset='utf8',
)
try:
    codes = list(stocks.values())
    placeholders = ', '.join(['%s'] * len(codes))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT MIN(date), MAX(date) FROM daily_price "
            f"WHERE code IN ({placeholders})",
            codes,
        )
        start_date, end_date = cursor.fetchone()
        if start_date is None:
            raise ValueError('선택한 종목의 수집된 일봉 데이터가 없습니다.')

        start_date = start_date.strftime('%Y-%m-%d')
        end_date = end_date.strftime('%Y-%m-%d')
        df = pd.DataFrame()
        for name, code in stocks.items():
            cursor.execute(
                "SELECT date, close FROM daily_price "
                "WHERE code = %s AND date BETWEEN %s AND %s ORDER BY date",
                (code, start_date, end_date),
            )
            rows = cursor.fetchall()
            if not rows:
                raise ValueError(f'{name}({code})의 수집된 일봉 데이터가 없습니다.')
            df[name] = pd.Series(
                [row[1] for row in rows],
                index=pd.to_datetime([row[0] for row in rows]),
            )
finally:
    connection.close()

df = df.dropna()
if len(df) < 2:
    raise ValueError('포트폴리오 분석에는 공통 거래일 데이터가 2일 이상 필요합니다.')
  
daily_ret = df.pct_change() 
annual_ret = daily_ret.mean() * 252
daily_cov = daily_ret.cov() 
annual_cov = daily_cov * 252

port_ret = [] 
port_risk = [] 
port_weights = [] 

for _ in range(20000): 
    weights = np.random.random(len(stocks)) 
    weights /= np.sum(weights) 

    returns = np.dot(weights, annual_ret) 
    risk = np.sqrt(np.dot(weights.T, np.dot(annual_cov, weights))) 

    port_ret.append(returns) 
    port_risk.append(risk) 
    port_weights.append(weights) 

portfolio = {'Returns': port_ret, 'Risk': port_risk} 
for i, s in enumerate(stocks): 
    portfolio[s] = [weight[i] for weight in port_weights] 
df = pd.DataFrame(portfolio) 
df = df[['Returns', 'Risk'] + [s for s in stocks]] 

df.plot.scatter(x='Risk', y='Returns', figsize=(8, 6), grid=True)
plt.title(f'Efficient Frontier ({start_date} to {end_date})')
plt.xlabel('Risk') 
plt.ylabel('Expected Returns') 
plt.show() 