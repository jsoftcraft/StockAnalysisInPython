"""Persist day-trading correlation results and render their top-10 chart."""

import os
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use('Agg')

from matplotlib import font_manager
import matplotlib.pyplot as plt
import pandas as pd
import pymysql


for _font_name in ('Malgun Gothic', 'NanumGothic'):
    try:
        font_manager.findfont(_font_name, fallback_to_default=False)
    except ValueError:
        continue
    plt.rcParams['font.family'] = _font_name
    break
plt.rcParams['axes.unicode_minus'] = False


RUN_TABLE = 'daytrading_analysis_run'
RESULT_TABLE = 'daytrading_correlation_history'
TRADE_SIGNAL_TABLE = 'daytrading_trade_signal'
ORDER_ATTEMPT_TABLE = 'daytrading_order_attempt'


def connect_database():
    return pymysql.connect(
        host=os.environ.get('STOCK_DB_HOST', 'localhost'),
        user=os.environ.get('STOCK_DB_USER', 'root'),
        password=os.environ.get('STOCK_DB_PASSWORD', 'root'),
        database=os.environ.get('STOCK_DB_NAME', 'zunDB'),
        charset='utf8mb4',
        connect_timeout=5,
        autocommit=False,
    )


def ensure_result_tables(connection):
    with connection.cursor() as cursor:
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS {RUN_TABLE} (
                analysis_date DATE NOT NULL,
                generated_at DATETIME NOT NULL,
                indicator_trade_date DATE NOT NULL,
                status VARCHAR(20) NOT NULL,
                result_count SMALLINT UNSIGNED NOT NULL,
                PRIMARY KEY (analysis_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        ''')
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS {RESULT_TABLE} (
                analysis_date DATE NOT NULL,
                rank_no SMALLINT UNSIGNED NOT NULL,
                code VARCHAR(20) NOT NULL,
                market VARCHAR(10) NOT NULL,
                company VARCHAR(100) NOT NULL,
                indicator VARCHAR(32) NOT NULL,
                correlation DECIMAL(12, 9) NOT NULL,
                observations SMALLINT UNSIGNED NOT NULL,
                indicator_trade_date DATE NOT NULL,
                indicator_return DECIMAL(16, 10) NOT NULL,
                nxt_eligible BOOLEAN NULL DEFAULT NULL,
                buy_signal BOOLEAN NOT NULL,
                max_buy_amount_krw BIGINT UNSIGNED NOT NULL DEFAULT 1000000,
                reference_close BIGINT UNSIGNED NOT NULL DEFAULT 0,
                estimated_buy_quantity BIGINT UNSIGNED NOT NULL DEFAULT 0,
                PRIMARY KEY (analysis_date, code),
                KEY ix_daytrading_rank (analysis_date, rank_no)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        ''')
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS {TRADE_SIGNAL_TABLE} (
                signal_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                analysis_date DATE NOT NULL,
                code VARCHAR(20) NOT NULL,
                market VARCHAR(10) NOT NULL,
                company VARCHAR(100) NOT NULL,
                signal_type VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL,
                indicator VARCHAR(32) NOT NULL,
                correlation DECIMAL(12, 9) NOT NULL,
                reference_close BIGINT UNSIGNED NOT NULL DEFAULT 0,
                estimated_buy_quantity BIGINT UNSIGNED NOT NULL DEFAULT 0,
                max_buy_amount_krw BIGINT UNSIGNED NOT NULL DEFAULT 1000000,
                generated_at DATETIME NOT NULL,
                PRIMARY KEY (signal_id),
                KEY ix_daytrading_signal_date_code (analysis_date, code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        ''')
        cursor.execute(
            'SELECT COLUMN_NAME FROM information_schema.COLUMNS '
            'WHERE TABLE_SCHEMA = DATABASE() '
            'AND TABLE_NAME = %s',
            (RESULT_TABLE,),
        )
        columns = {row[0] for row in cursor.fetchall()}
        migrations = {
            'max_buy_amount_krw': (
                'BIGINT UNSIGNED NOT NULL DEFAULT 1000000'
            ),
            'reference_close': 'BIGINT UNSIGNED NOT NULL DEFAULT 0',
            'estimated_buy_quantity': (
                'BIGINT UNSIGNED NOT NULL DEFAULT 0'
            ),
        }
        for column, definition in migrations.items():
            if column not in columns:
                cursor.execute(
                    f'ALTER TABLE {RESULT_TABLE} ADD COLUMN '
                    f'{column} {definition}'
                )
        if 'nxt_eligible' in columns:
            cursor.execute(
                f'ALTER TABLE {RESULT_TABLE} MODIFY COLUMN '
                'nxt_eligible BOOLEAN NULL DEFAULT NULL'
            )


def save_analysis_results(
    connection, analysis_date, results, indicator_trade_date=None
):
    analysis_date = pd.Timestamp(analysis_date).date()
    if indicator_trade_date is None:
        indicator_trade_date = (
            pd.Timestamp(analysis_date) - pd.offsets.BDay(1)
        ).date()
    else:
        indicator_trade_date = pd.Timestamp(indicator_trade_date).date()

    ensure_result_tables(connection)
    generated_at = datetime.now()
    status = 'COMPLETED' if not results.empty else 'NO_SIGNAL'
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO {RUN_TABLE} '
                '(analysis_date, generated_at, indicator_trade_date, status, '
                'result_count) VALUES (%s, %s, %s, %s, %s) '
                'ON DUPLICATE KEY UPDATE generated_at = VALUES(generated_at), '
                'indicator_trade_date = VALUES(indicator_trade_date), '
                'status = VALUES(status), result_count = VALUES(result_count)',
                (
                    analysis_date, generated_at, indicator_trade_date,
                    status, len(results),
                ),
            )
            cursor.execute(
                f'DELETE FROM {RESULT_TABLE} WHERE analysis_date = %s',
                (analysis_date,),
            )
            if not results.empty:
                cursor.executemany(
                    f'INSERT INTO {RESULT_TABLE} '
                    '(analysis_date, rank_no, code, market, company, indicator, '
                    'correlation, observations, indicator_trade_date, '
                    'indicator_return, buy_signal, max_buy_amount_krw, '
                    'reference_close, '
                    'estimated_buy_quantity) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '
                    '%s, %s, %s, %s)',
                    [
                        (
                            analysis_date,
                            int(row.rank),
                            str(row.code),
                            str(row.market or ''),
                            str(row.company or ''),
                            str(row.indicator),
                            float(row.correlation),
                            int(row.observations),
                            pd.Timestamp(row.indicator_trade_date).date(),
                            float(row.indicator_return),
                            bool(row.buy_signal),
                            int(row.max_buy_amount_krw),
                            int(row.reference_close),
                            int(row.estimated_buy_quantity),
                        )
                        for row in results.itertuples(index=False)
                    ],
                )
                buy_signals = results.loc[
                    results['buy_signal'].fillna(False)
                ]
                if not buy_signals.empty:
                    cursor.executemany(
                        f'INSERT INTO {TRADE_SIGNAL_TABLE} '
                        '(analysis_date, code, market, company, signal_type, '
                        'status, indicator, correlation, reference_close, '
                        'estimated_buy_quantity, max_buy_amount_krw, '
                        'generated_at) '
                        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '
                        '%s, %s)',
                        [
                            (
                                analysis_date,
                                str(row.code),
                                str(row.market or ''),
                                str(row.company or ''),
                                'BUY',
                                'SIGNAL_ONLY',
                                str(row.indicator),
                                float(row.correlation),
                                int(row.reference_close),
                                int(row.estimated_buy_quantity),
                                int(row.max_buy_amount_krw),
                                generated_at,
                            )
                            for row in buy_signals.itertuples(index=False)
                        ],
                    )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def load_daytrading_buy_signals(connection, analysis_date):
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT code, market, company, indicator, correlation, '
            'observations, indicator_trade_date, indicator_return, '
            'max_buy_amount_krw, reference_close, estimated_buy_quantity '
            f'FROM {RESULT_TABLE} '
            'WHERE analysis_date = %s AND buy_signal = TRUE '
            'ORDER BY rank_no',
            (pd.Timestamp(analysis_date).date(),),
        )
        rows = cursor.fetchall()
    columns = [
        'code', 'market', 'company', 'indicator', 'correlation',
        'observations', 'indicator_trade_date', 'indicator_return',
        'max_buy_amount_krw', 'reference_close', 'estimated_buy_quantity',
    ]
    return pd.DataFrame(rows, columns=columns).assign(buy_signal=True)


def claim_daytrading_order_attempt(
    connection, analysis_date, account_no, code, quantity, limit_price
):
    with connection.cursor() as cursor:
        cursor.execute(f'''CREATE TABLE IF NOT EXISTS {ORDER_ATTEMPT_TABLE} (
            analysis_date DATE NOT NULL,
            account_no VARCHAR(32) NOT NULL,
            code VARCHAR(20) NOT NULL,
            status VARCHAR(20) NOT NULL,
            order_no BIGINT UNSIGNED NULL,
            quantity BIGINT UNSIGNED NOT NULL,
            limit_price BIGINT UNSIGNED NOT NULL,
            message VARCHAR(255) NOT NULL DEFAULT '',
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (analysis_date, account_no, code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
        cursor.execute(
            f'INSERT IGNORE INTO {ORDER_ATTEMPT_TABLE} '
            '(analysis_date, account_no, code, status, quantity, '
            'limit_price, updated_at) '
            "VALUES (%s, %s, %s, 'SUBMITTING', %s, %s, %s)",
            (
                pd.Timestamp(analysis_date).date(), str(account_no),
                str(code), int(quantity), int(limit_price), datetime.now(),
            ),
        )
        claimed = cursor.rowcount == 1
    connection.commit()
    return claimed


def update_daytrading_order_attempt(
    connection, analysis_date, account_no, code, status, order_no=None,
    message='',
):
    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE {ORDER_ATTEMPT_TABLE} SET status = %s, order_no = %s, '
            'message = %s, updated_at = %s '
            'WHERE analysis_date = %s AND account_no = %s AND code = %s',
            (
                status, order_no, str(message)[:255], datetime.now(),
                pd.Timestamp(analysis_date).date(), str(account_no), str(code),
            ),
        )
    connection.commit()


def create_top10_chart(results, output_path, analysis_date):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    figure, axis = plt.subplots(figsize=(11, max(4, 0.55 * len(results) + 1.8)))
    if results.empty:
        axis.text(
            0.5, 0.5, 'No connected positive-correlation signals',
            ha='center', va='center', transform=axis.transAxes,
        )
        axis.set_axis_off()
    else:
        chart = results.sort_values('correlation', ascending=True).copy()
        labels = [
            f"{row.company or row.code} ({row.code})  |  {row.indicator}"
            for row in chart.itertuples(index=False)
        ]
        bars = axis.barh(labels, chart['correlation'], color='#167D72')
        axis.set_xlim(0, min(1.05, max(0.1, chart['correlation'].max() * 1.18)))
        axis.set_xlabel('Pearson correlation')
        axis.grid(axis='x', color='#D9E2E0', linewidth=0.8)
        axis.set_axisbelow(True)
        for bar, value in zip(bars, chart['correlation']):
            axis.text(
                value + 0.008, bar.get_y() + bar.get_height() / 2,
                f'{value:.3f}', va='center', fontsize=9,
            )

    figure.suptitle(
        f'DayTrading TOP 10 Correlation  |  {pd.Timestamp(analysis_date):%Y-%m-%d}',
        fontsize=14,
    )
    figure.tight_layout()
    figure.savefig(str(output_path), dpi=160, bbox_inches='tight')
    plt.close(figure)
    return output_path