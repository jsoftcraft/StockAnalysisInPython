"""Read-only local dashboard for the KOSPI BollingerBand/MFI10 Trader."""

import argparse
import json
import math
import os
import re
import subprocess
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from BollingerBand_MFI10_Trader import (
    MARKET,
    connect_database,
    load_kospi_candidates,
    normalize_code,
)


TASK_NAME = 'StockAnalysisInPython-03-LiveTrader'
LOG_PATH = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / (
    'StockAnalysisInPython/logs/trader.log'
)
HTML_PATH = Path(__file__).with_name('BollingerBand_MFI10_Monitor.html')
LOG_PATTERN = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?)\s+'
    r'(INFO|WARNING|ERROR|CRITICAL)\s+(.*)$'
)


def parse_log_records(path, limit=240):
    if not path.exists():
        return []

    with path.open('r', encoding='utf-8', errors='replace') as log_file:
        lines = deque(log_file, maxlen=limit)

    records = []
    for line in lines:
        match = LOG_PATTERN.match(line.rstrip('\r\n'))
        if match is None:
            continue
        timestamp, level, message = match.groups()
        records.append({
            'timestamp': timestamp,
            'level': level,
            'message': message,
        })
    return records


def event_records(records, limit=12):
    markers = (
        'Trader process started',
        'Trader process finished normally',
        'KOSPI 후보',
        '[매수/매도 조건 조회]',
        '[매수',
        '[매도',
        '[시세 조회 오류]',
        '[일봉',
        '실패',
        '중단',
        '이미 실행 중',
    )
    return [
        record for record in records
        if record['level'] in ('WARNING', 'ERROR')
        or any(marker in record['message'] for marker in markers)
    ][-limit:]


def progress_snapshot(task, records):
    starts = []
    for index, record in enumerate(records):
        if 'Trader process started' not in record['message']:
            continue
        next_start = next(
            (
                position for position in range(index + 1, len(records))
                if 'Trader process started' in records[position]['message']
            ),
            len(records),
        )
        if any(
            '신규 실행을 건너뜁니다.' in item['message']
            for item in records[index + 1:next_start]
        ):
            continue
        starts.append(record)
    latest_start = starts[-1] if starts else None
    start_time = latest_start['timestamp'] if latest_start else ''
    current_records = [
        record for record in records
        if not start_time or record['timestamp'] >= start_time
    ]
    has_summary = any(
        'KOSPI 후보' in record['message'] for record in current_records
    )
    has_cycle = any(
        '[매수/매도 조건 조회]' in record['message']
        for record in current_records
    )
    has_order = any(
        marker in record['message']
        for record in current_records
        for marker in ('[매수]', '[매도]', '[모의 매수]', '[모의 매도]')
    )
    finished = any(
        'Trader process finished normally' in record['message']
        for record in current_records
    )
    latest_error = next(
        (
            record for record in reversed(current_records)
            if record['level'] in ('ERROR', 'CRITICAL')
        ),
        None,
    )
    latest_progress = next(
        (
            record for record in reversed(current_records)
            if any(
                marker in record['message']
                for marker in (
                    'KOSPI 후보', '[매수/매도 조건 조회]',
                    'Trader process finished normally',
                )
            )
        ),
        None,
    )
    current_error = bool(
        latest_error
        and (
            latest_progress is None
            or latest_error['timestamp'] > latest_progress['timestamp']
        )
    )
    running = str(task.get('state', '')).lower() == 'running'

    if running:
        status = 'running'
        if has_cycle:
            current_step = '실시간 시세 감시 중'
        elif has_summary:
            current_step = '후보 분석 완료, 감시 루프 준비 중'
        else:
            current_step = 'Trader 프로세스 실행 중 · 최근 로그 갱신 대기'
    elif current_error:
        status = 'error'
        current_step = '오류로 실행 중단'
    elif finished:
        status = 'finished'
        current_step = '정상 종료'
    elif str(task.get('state', '')).lower() == 'ready':
        status = 'waiting'
        current_step = '다음 작업 실행 대기'
    else:
        status = 'unknown'
        current_step = '작업 상태 확인 불가'

    steps = [
        {
            'label': 'Trader 시작',
            'state': 'done' if latest_start or running else 'pending',
        },
        {'label': '종목 분석', 'state': 'done' if has_summary else 'pending'},
        {
            'label': '시세 감시',
            'state': 'active' if running and has_summary else (
                'done' if has_cycle else 'pending'
            ),
        },
        {'label': '주문·신호', 'state': 'done' if has_order else 'pending'},
    ]
    return {
        'status': status,
        'current_step': current_step,
        'steps': steps,
        'task_state': task.get('state', 'unknown'),
        'last_run': task.get('last_run'),
        'last_result': task.get('last_result'),
    }


def _finite_number(value, digits=4):
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return round(number, digits) if math.isfinite(number) else None


def make_chart_bars(rows):
    if not rows:
        return []
    prices = pd.DataFrame(
        rows, columns=['date', 'high', 'low', 'close', 'volume']
    ).sort_values('date')
    close = prices['close'].astype(float)
    middle = close.rolling(20).mean()
    deviation = close.rolling(20).std()
    upper = middle + 2 * deviation
    lower = middle - 2 * deviation
    return [
        {
            'date': pd.Timestamp(row.date).strftime('%Y-%m-%d'),
            'close': _finite_number(row.close, 2),
            'upper': _finite_number(upper.iloc[index], 2),
            'middle': _finite_number(middle.iloc[index], 2),
            'lower': _finite_number(lower.iloc[index], 2),
            'volume': _finite_number(row.volume, 0),
        }
        for index, row in enumerate(prices.itertuples(index=False))
    ]


def get_task_status(task_name=TASK_NAME):
    safe_task_name = task_name.replace("'", "''")
    command = (
        f"$task = Get-ScheduledTask -TaskName '{safe_task_name}' "
        "-ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { exit 2 }; "
        f"$info = Get-ScheduledTaskInfo -TaskName '{safe_task_name}'; "
        "[pscustomobject]@{ state=[string]$task.State; "
        "last_run=$info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss'); "
        "last_result=[int]$info.LastTaskResult } | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=4,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {'state': 'not_found', 'last_run': None, 'last_result': None}
        value = json.loads(result.stdout)
        return {
            'state': value.get('state', 'unknown'),
            'last_run': value.get('last_run'),
            'last_result': value.get('last_result'),
        }
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {'state': 'unknown', 'last_run': None, 'last_result': None}


class DashboardService:
    def __init__(self, log_path=LOG_PATH, task_name=TASK_NAME):
        self.log_path = Path(log_path)
        self.task_name = task_name
        self._lock = threading.Lock()
        self._overview = None
        self._overview_at = 0.0
        self._records = []
        self._records_at = 0.0
        self._task = None
        self._task_at = 0.0

    def _load_overview(self):
        connection = connect_database()
        try:
            candidates = load_kospi_candidates(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT MAX(prices.date) FROM daily_price AS prices '
                    'JOIN (SELECT DISTINCT code FROM selected_stock '
                    'WHERE market = %s) AS selection '
                    'ON selection.code = prices.code '
                    'WHERE prices.date < CURDATE()',
                    (MARKET,),
                )
                date_row = cursor.fetchone()
        finally:
            connection.close()

        selected = [candidate for candidate in candidates if candidate.selected]
        return {
            'market': MARKET,
            'analysis_date': (
                date_row[0].strftime('%Y-%m-%d') if date_row and date_row[0]
                else None
            ),
            'candidate_count': len(candidates),
            'selected_count': len(selected),
            'selected': [
                {
                    'company': candidate.company,
                    'code': normalize_code(candidate.code),
                    'mfi10': _finite_number(candidate.mfi10, 2),
                    'percent_b': _finite_number(candidate.percent_b, 3),
                    'return_1m': _finite_number(candidate.return_1m * 100, 2),
                    'return_3m': _finite_number(candidate.return_3m * 100, 2),
                    'ma20': _finite_number(candidate.ma20, 0),
                }
                for candidate in selected
            ],
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

    def _get_overview(self, force=False):
        with self._lock:
            if (
                force or self._overview is None
                or time.monotonic() - self._overview_at >= 25
            ):
                self._overview = self._load_overview()
                self._overview_at = time.monotonic()
            return self._overview

    def _get_records(self):
        if time.monotonic() - self._records_at >= 2:
            self._records = parse_log_records(self.log_path)
            self._records_at = time.monotonic()
        return self._records

    def _get_task(self):
        retry_after = (
            2 if self._task and self._task.get('state') in ('unknown', 'not_found')
            else 8
        )
        if self._task is None or time.monotonic() - self._task_at >= retry_after:
            self._task = get_task_status(self.task_name)
            self._task_at = time.monotonic()
        return self._task

    def dashboard(self, force=False):
        overview = self._get_overview(force=force)
        records = self._get_records()
        task = self._get_task()
        return {
            **overview,
            'runtime': progress_snapshot(task, records),
            'events': event_records(records),
        }

    def chart(self, code, limit=90):
        overview = self._get_overview()
        normalized = normalize_code(code)
        selected = next(
            (stock for stock in overview['selected'] if stock['code'] == normalized),
            None,
        )
        if selected is None:
            raise ValueError('오늘 선정된 종목만 차트를 조회할 수 있습니다.')

        limit = max(40, min(int(limit), 180))
        connection = connect_database()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT date, high, low, close, volume FROM daily_price '
                    'WHERE code = %s AND date < CURDATE() '
                    'ORDER BY date DESC LIMIT %s',
                    (normalized, limit),
                )
                rows = list(reversed(cursor.fetchall()))
        finally:
            connection.close()
        return {'stock': selected, 'bars': make_chart_bars(rows)}


def make_handler(service):
    class MonitorHandler(BaseHTTPRequestHandler):
        def _send(self, status, content_type, body):
            encoded = body if isinstance(body, bytes) else body.encode('utf-8')
            try:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(encoded)
            except (ConnectionError, OSError):
                return

        def _send_json(self, status, payload):
            self._send(
                status,
                'application/json; charset=utf-8',
                json.dumps(payload, ensure_ascii=False, allow_nan=False),
            )

        def do_GET(self):
            request = urlparse(self.path)
            if request.path == '/':
                try:
                    self._send(
                        200,
                        'text/html; charset=utf-8',
                        HTML_PATH.read_bytes(),
                    )
                except OSError as error:
                    self._send_json(500, {'error': str(error)})
                return

            if request.path == '/api/dashboard':
                force = parse_qs(request.query).get('refresh') == ['1']
                try:
                    self._send_json(200, service.dashboard(force=force))
                except Exception as error:
                    self._send_json(503, {'error': str(error)})
                return

            if request.path == '/api/chart':
                query = parse_qs(request.query)
                code = query.get('code', [''])[0]
                if not re.fullmatch(r'[A-Za-z0-9]{1,20}', code):
                    self._send_json(400, {'error': '종목 코드가 올바르지 않습니다.'})
                    return
                try:
                    limit = int(query.get('days', ['90'])[0])
                    self._send_json(200, service.chart(code, limit))
                except ValueError as error:
                    self._send_json(400, {'error': str(error)})
                except Exception as error:
                    self._send_json(503, {'error': str(error)})
                return

            self._send_json(404, {'error': '요청한 주소를 찾을 수 없습니다.'})

        def log_message(self, format_string, *args):
            return

    return MonitorHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--log-file', type=Path, default=LOG_PATH)
    parser.add_argument('--task-name', default=TASK_NAME)
    args = parser.parse_args()

    service = DashboardService(args.log_file, args.task_name)
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(service)
    )
    url = f'http://{args.host}:{server.server_port}/'
    print(f'KOSPI Trader Monitor: {url}')
    print('종료하려면 이 창에서 Ctrl+C를 누르세요.')
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print('\n모니터 서버를 종료합니다.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()