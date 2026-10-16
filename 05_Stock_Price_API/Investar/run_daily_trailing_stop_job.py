"""Update daily candidates and run trailing-stop optimization when ready."""

import os
import subprocess
import sys
from pathlib import Path

import pymysql


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INVESTAR_DIR = Path(__file__).resolve().parent
MARKET = 'KOSPI'
MINIMUM_SNAPSHOT_DAYS = 10
MAX_MINUTE_REQUESTS = 5000


def count_snapshot_days():
    connection = pymysql.connect(
        host=os.environ.get('STOCK_DB_HOST', 'localhost'),
        user=os.environ.get('STOCK_DB_USER', 'root'),
        password=os.environ.get('STOCK_DB_PASSWORD', 'root'),
        database=os.environ.get('STOCK_DB_NAME', 'zunDB'),
        charset='utf8',
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(DISTINCT snapshot_date) "
                "FROM selected_stock_history WHERE market = %s",
                (MARKET,),
            )
            row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        connection.close()


def run_required(script_path, *arguments):
    result = subprocess.run(
        [sys.executable, str(script_path), *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    if result.returncode:
        raise SystemExit(result.returncode)


def main():
    run_required(
        INVESTAR_DIR / 'StockReturnAnalyzer.py',
        '--market', MARKET,
    )
    run_required(INVESTAR_DIR / 'DBUpdaterEx.py')

    snapshot_days = count_snapshot_days()
    print(
        f'{MARKET} 후보 스냅샷: {snapshot_days}일 '
        f'(최적화 시작 기준 {MINIMUM_SNAPSHOT_DAYS}일)',
        flush=True,
    )
    if snapshot_days < MINIMUM_SNAPSHOT_DAYS:
        print('스냅샷 이력을 계속 누적합니다. 최적화는 기준 충족 후 실행합니다.')
        return 0

    optimizer = (
        REPOSITORY_ROOT
        / '06_Trading_Strategy'
        / 'BollingerBand_MFI10_TrailingStop_Optimizer.py'
    )
    result = subprocess.run(
        [
            sys.executable,
            str(optimizer),
            '--market', MARKET,
            '--collect-minute',
            '--max-requests', str(MAX_MINUTE_REQUESTS),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        errors='replace',
    )
    if result.stdout:
        print(result.stdout, end='')
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr)
    if result.returncode and '최적화 데이터가' in result.stdout + result.stderr:
        print('유효 분봉 거래일이 아직 부족합니다. 다음 실행에서 이어서 수집합니다.')
        return 0
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())