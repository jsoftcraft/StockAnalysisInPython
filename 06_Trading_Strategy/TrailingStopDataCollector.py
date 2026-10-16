"""Collect candidate snapshots, daily bars, and minute bars for stop research."""

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INVESTAR_DIR = REPOSITORY_ROOT / '05_Stock_Price_API' / 'Investar'
DEFAULT_START = '2026-04-01'


def run_step(label, command):
    print(f'\n[{label}] 시작', flush=True)
    result = subprocess.run(command, cwd=REPOSITORY_ROOT, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    print(f'[{label}] 완료', flush=True)


def parse_args():
    parser = argparse.ArgumentParser(
        description='후보 스냅샷, 일봉, 분봉을 수집합니다. 최적화 결과는 계산하지 않습니다.'
    )
    parser.add_argument('--market', choices=('KOSPI', 'KOSDAQ'), default='KOSPI')
    parser.add_argument('--start', default=DEFAULT_START, help='분봉 수집 시작일 YYYY-MM-DD')
    parser.add_argument('--max-requests', type=int, default=5000)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.max_requests < 1:
        raise ValueError('--max-requests는 1 이상이어야 합니다.')
    if date.today().weekday() >= 5:
        raise ValueError('주말에는 후보 스냅샷을 기록하지 않습니다. 거래일에 실행하세요.')

    analyzer = INVESTAR_DIR / 'StockReturnAnalyzer.py'
    updater = INVESTAR_DIR / 'DBUpdaterEx.py'
    optimizer = Path(__file__).with_name(
        'BollingerBand_MFI10_TrailingStop_Optimizer.py'
    )

    analyzer_command = [
        sys.executable, str(analyzer), '--market', args.market,
    ]
    optimizer_command = [
        sys.executable, str(optimizer),
        '--market', args.market,
        '--start', args.start,
        '--collect-only',
        '--max-requests', str(args.max_requests),
    ]

    run_step('후보 스냅샷 저장', analyzer_command)
    run_step('후보 종목 일봉 갱신', [sys.executable, str(updater)])
    run_step('분봉 누락 수집', optimizer_command)
    print('\n요청한 데이터 수집이 완료됐습니다. 최적화 결과 계산은 수행하지 않았습니다.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())