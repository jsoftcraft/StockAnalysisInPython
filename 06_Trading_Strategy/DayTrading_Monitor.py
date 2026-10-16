"""Read-only local dashboard for scheduled KRX DayTrading analysis."""

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
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from DayTrading_Data import STOCK_UNIVERSE_TABLE
from DayTrading_Results import (
    ORDER_ATTEMPT_TABLE,
    RESULT_TABLE,
    RUN_TABLE,
    TRADE_SIGNAL_TABLE,
    connect_database,
)


TASK_NAME = 'StockAnalysisInPython-02-DayTrading'
LOG_DIRECTORY = Path(__file__).resolve().with_name('results') / 'logs'
HTML_PATH = Path(__file__).with_name('DayTrading_Monitor.html')
LOG_PATTERN = re.compile(
    r'^\[?(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2}:\d{2})'
    r'(?:[.,]\d+)?\]?\s+'
    r'(?:(INFO|WARNING|ERROR|CRITICAL)\s+)?(.*)$'
)


def _read_log_tail(path, line_limit=500):
    with path.open('r', encoding='utf-8', errors='replace') as log_file:
        return deque(log_file, maxlen=line_limit)


def parse_log_records(log_directory=LOG_DIRECTORY, today=None):
    today = today or date.today()
    directory = Path(log_directory)
    if not directory.exists():
        return []

    daily_log = directory / f'daytrading_{today:%Y%m%d}.log'
    dated_logs = sorted(
        directory.glob(f'daytrading_{today:%Y%m%d}_*.log'),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:5]
    paths = list(dict.fromkeys(
        ([daily_log] if daily_log.exists() else []) + dated_logs
    ))
    records = []
    for path in paths:
        for line in _read_log_tail(path):
            match = LOG_PATTERN.match(line.strip())
            if match is None:
                continue
            log_date, log_time, level, message = match.groups()
            hour, minute_second = log_time.split(':', 1)
            normalized_time = f'{int(hour):02d}:{minute_second}'
            records.append({
                'timestamp': f'{log_date} {normalized_time}',
                'level': level or 'INFO',
                'message': message.strip().strip('"'),
            })

    records.sort(key=lambda record: record['timestamp'])
    deduplicated = []
    seen = set()
    for record in records:
        key = (record['timestamp'], record['level'], record['message'])
        if key not in seen:
            seen.add(key)
            deduplicated.append(record)
    return deduplicated[-300:]


def event_records(records, limit=10):
    markers = (
        'DayTrading run started',
        'started in SCHEDULED LIVE mode',
        'DayTrading skipped',
        'daily close collection',
        'daily closes',
        'indicator close download',
        'indicator closes saved',
        'Correlation analysis completed',
        'Analysis results saved',
        'buy signals',
        'buy orders submitted',
        'CREON buy order submitted',
        'Starting MFI position monitor',
        'DayTrading run failed',
        'ERROR',
    )
    selected = [
        record for record in records
        if record['level'] in ('WARNING', 'ERROR', 'CRITICAL')
        or any(marker.lower() in record['message'].lower() for marker in markers)
    ]
    return selected[-limit:]


def make_progress(task, records, analysis, today=None):
    today = today or date.today()
    today_text = today.isoformat()
    today_records = [
        record for record in records
        if record['timestamp'].startswith(today_text)
    ]
    running = str(task.get('state', '')).lower() == 'running'
    skipped = next(
        (
            record for record in reversed(today_records)
            if 'DayTrading skipped:' in record['message']
        ),
        None,
    )
    failed = next(
        (
            record for record in reversed(today_records)
            if record['level'] in ('ERROR', 'CRITICAL')
            or 'DayTrading run failed' in record['message']
        ),
        None,
    )

    stages = [
        ('한국 일봉 수집', ('Korean daily close collection completed', 'Korean daily close collection skipped')),
        ('해외 지표 갱신', ('Overseas indicator download completed', 'Using existing overseas indicator CSV')),
        ('상관 분석 및 TOP 선정', ('Correlation analysis completed',)),
        ('분석 결과 저장 및 주문', ('Analysis results saved to DB', 'DayTrading live orders submitted', 'DayTrading run completed; no live orders were sent')),
        ('장중 포지션 감시', ('Starting MFI position monitor', 'DayTrading daemon remains active')),
    ]
    active_run = next(
        (
            index for index in range(len(today_records) - 1, -1, -1)
            if 'DayTrading run started:' in today_records[index]['message']
        ),
        None,
    )
    active_records = (
        today_records[active_run:] if active_run is not None else today_records
    )
    completed_stages = []
    for index, (label, markers) in enumerate(stages):
        if any(
            any(marker in record['message'] for marker in markers)
            for record in active_records
        ):
            completed_stages.append(index)

    if str(task.get('state', '')).lower() == 'checking':
        status = 'checking'
        current_step = '작업 스케줄러 상태 조회 중'
        active_index = -1
    elif running:
        if active_run is None:
            current_step = 'DayTrading 프로세스 시작 및 로그 확인 중'
            active_index = 0
        else:
            active_index = min(
                max(completed_stages, default=-1) + 1, len(stages) - 1
            )
            latest_message = active_records[-1]['message'] if active_records else ''
            if 'Korean daily close collection started' in latest_message:
                active_index = 0
            elif 'Overseas indicator close download started' in latest_message:
                active_index = 1
            elif 'Correlation analysis' in latest_message:
                active_index = 2
            current_step = f'{stages[active_index][0]} 진행 중'
        status = 'running'
    elif skipped is not None:
        status = 'waiting'
        reason = skipped['message'].replace('DayTrading skipped:', '').strip()
        current_step = (
            '오늘 매도 감시 종료 시각(18:00) 이후라 실행을 건너뜀'
            if 'sell monitoring window ended at 18:00' in reason
            else f'예약 실행 건너뜀: {reason}'
        )
        active_index = -1
    elif failed is not None and int(task.get('last_result') or 0) != 0:
        status = 'error'
        current_step = failed['message']
        active_index = -1
    elif analysis and analysis.get('status') in ('COMPLETED', 'NO_SIGNAL'):
        status = 'waiting'
        current_step = f"최근 분석 {analysis['analysis_date']} 완료 · 다음 실행 대기"
        active_index = len(stages) - 1 if analysis.get('status') == 'COMPLETED' else 2
    else:
        status = 'unknown'
        current_step = '분석 실행 기록이 없습니다.'
        active_index = -1

    if (
        not running and analysis
        and analysis.get('status') in ('COMPLETED', 'NO_SIGNAL')
    ):
        completed_stages = list(range(4))

    progress_steps = []
    for index, (label, _) in enumerate(stages):
        if index in completed_stages:
            step_state = 'done'
        elif running and index == active_index:
            step_state = 'active'
        else:
            step_state = 'pending'
        progress_steps.append({'label': label, 'state': step_state})

    return {
        'status': status,
        'current_step': current_step,
        'steps': progress_steps,
        'task_state': task.get('state', 'unknown'),
        'last_run': task.get('last_run'),
        'last_result': task.get('last_result'),
        'next_run': task.get('next_run'),
    }


def _number(value, digits=3):
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
            'close': _number(row.close, 2),
            'upper': _number(upper.iloc[index], 2),
            'middle': _number(middle.iloc[index], 2),
            'lower': _number(lower.iloc[index], 2),
            'volume': _number(row.volume, 0),
        }
        for index, row in enumerate(prices.itertuples(index=False))
    ]


def get_task_status(task_name=TASK_NAME):
    safe_name = task_name.replace("'", "''")
    command = (
        f"$task = Get-ScheduledTask -TaskName '{safe_name}' "
        "-ErrorAction SilentlyContinue; if ($null -eq $task) { exit 2 }; "
        f"$info = Get-ScheduledTaskInfo -TaskName '{safe_name}'; "
        "[pscustomobject]@{ state=[string]$task.State; "
        "last_run=$info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss'); "
        "last_result=[int]$info.LastTaskResult; "
        "next_run=$info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } "
        '| ConvertTo-Json -Compress'
    )
    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {
                'state': 'not_found', 'last_run': None,
                'last_result': None, 'next_run': None,
            }
        value = json.loads(result.stdout)
        return {
            'state': value.get('state', 'unknown'),
            'last_run': value.get('last_run'),
            'last_result': value.get('last_result'),
            'next_run': value.get('next_run'),
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return {
            'state': 'unknown', 'last_run': None,
            'last_result': None, 'next_run': None,
        }


class DashboardService:
    def __init__(self, log_directory=LOG_DIRECTORY, task_name=TASK_NAME):
        self.log_directory = Path(log_directory)
        self.task_name = task_name
        self._lock = threading.Lock()
        self._data = None
        self._data_at = 0.0
        self._logs = []
        self._logs_at = 0.0
        self._task = {
            'state': 'checking', 'last_run': None,
            'last_result': None, 'next_run': None,
        }
        self._task_at = 0.0
        self._task_loading = False
        self._task_lock = threading.Lock()

    def _load_data(self):
        connection = connect_database()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f'SELECT analysis_date, generated_at, indicator_trade_date, '
                    f'status, result_count FROM {RUN_TABLE} '
                    'ORDER BY analysis_date DESC LIMIT 1'
                )
                run = cursor.fetchone()
                if run is None:
                    return {
                        'analysis': None, 'results': [], 'orders': [],
                        'signal_count': 0,
                    }
                analysis_date, generated_at, indicator_date, status, result_count = run
                analysis = {
                    'analysis_date': analysis_date.strftime('%Y-%m-%d'),
                    'generated_at': generated_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'indicator_trade_date': indicator_date.strftime('%Y-%m-%d'),
                    'status': status,
                    'result_count': int(result_count),
                }
                cursor.execute(
                    f'SELECT rank_no, code, company, market, indicator, '
                    f'correlation, observations, indicator_trade_date, '
                    f'indicator_return, buy_signal, max_buy_amount_krw, '
                    f'reference_close, estimated_buy_quantity '
                    f'FROM {RESULT_TABLE} WHERE analysis_date = %s '
                    'ORDER BY rank_no',
                    (analysis_date,),
                )
                columns = [
                    'rank', 'code', 'company', 'market', 'indicator',
                    'correlation', 'observations', 'indicator_trade_date',
                    'indicator_return', 'buy_signal', 'max_buy_amount_krw',
                    'reference_close', 'estimated_buy_quantity',
                ]
                results = []
                for row in cursor.fetchall():
                    item = dict(zip(columns, row))
                    item['code'] = str(item['code']).strip()
                    item['correlation'] = _number(item['correlation'], 4)
                    item['indicator_return'] = _number(
                        float(item['indicator_return']) * 100, 3
                    )
                    item['observations'] = int(item['observations'])
                    item['buy_signal'] = bool(item['buy_signal'])
                    item['indicator_trade_date'] = (
                        item['indicator_trade_date'].strftime('%Y-%m-%d')
                    )
                    item['max_buy_amount_krw'] = int(item['max_buy_amount_krw'])
                    item['reference_close'] = int(item['reference_close'])
                    item['estimated_buy_quantity'] = int(
                        item['estimated_buy_quantity']
                    )
                    results.append(item)
                cursor.execute(
                    f'SELECT COUNT(*) FROM {TRADE_SIGNAL_TABLE} '
                    'WHERE analysis_date = %s AND signal_type = %s',
                    (analysis_date, 'BUY'),
                )
                signal_count = int(cursor.fetchone()[0])
                cursor.execute(
                    f'SELECT analysis_date, code, status, order_no, quantity, '
                    f'limit_price, message, updated_at FROM {ORDER_ATTEMPT_TABLE} '
                    'ORDER BY updated_at DESC LIMIT 12'
                )
                orders = [
                    {
                        'analysis_date': row[0].strftime('%Y-%m-%d'),
                        'code': str(row[1]).strip(),
                        'status': row[2],
                        'order_no': row[3],
                        'quantity': int(row[4]),
                        'limit_price': int(row[5]),
                        'message': row[6],
                        'updated_at': row[7].strftime('%Y-%m-%d %H:%M:%S'),
                    }
                    for row in cursor.fetchall()
                ]
                cursor.execute(
                    f'SELECT MAX(dp.date) FROM daily_price AS dp '
                    f'JOIN {STOCK_UNIVERSE_TABLE} AS universe '
                    'ON universe.code = dp.code '
                    'WHERE universe.is_active = TRUE AND dp.date < %s',
                    (analysis_date,),
                )
                price_row = cursor.fetchone()
        finally:
            connection.close()

        return {
            'analysis': analysis,
            'results': results,
            'orders': orders,
            'signal_count': signal_count,
            'price_date': (
                price_row[0].strftime('%Y-%m-%d')
                if price_row and price_row[0] else None
            ),
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

    def _get_data(self, force=False):
        with self._lock:
            if (
                force or self._data is None
                or time.monotonic() - self._data_at >= 25
            ):
                self._data = self._load_data()
                self._data_at = time.monotonic()
            return self._data

    def _get_logs(self):
        if time.monotonic() - self._logs_at >= 2:
            self._logs = parse_log_records(self.log_directory)
            self._logs_at = time.monotonic()
        return self._logs

    def _get_task(self):
        with self._task_lock:
            if (
                not self._task_loading
                and time.monotonic() - self._task_at >= 20
            ):
                self._task_loading = True
                threading.Thread(
                    target=self._refresh_task, daemon=True
                ).start()
            return dict(self._task)

    def _refresh_task(self):
        try:
            task = get_task_status(self.task_name)
        except Exception:
            task = {
                'state': 'unknown', 'last_run': None,
                'last_result': None, 'next_run': None,
            }
        with self._task_lock:
            self._task = task
            self._task_at = time.monotonic()
            self._task_loading = False

    def dashboard(self, force=False):
        data = self._get_data(force=force)
        logs = self._get_logs()
        task = self._get_task()
        return {
            **data,
            'runtime': make_progress(task, logs, data['analysis']),
            'events': event_records(logs),
        }

    def chart(self, code, limit=90):
        data = self._get_data()
        result = next(
            (item for item in data['results'] if item['code'] == code), None
        )
        if result is None:
            raise ValueError('최근 분석 TOP 종목만 차트를 조회할 수 있습니다.')
        limit = max(40, min(int(limit), 180))
        connection = connect_database()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT date, high, low, close, volume FROM daily_price '
                    'WHERE code = %s AND date < %s '
                    'ORDER BY date DESC LIMIT %s',
                    (code, data['analysis']['analysis_date'], limit),
                )
                rows = list(reversed(cursor.fetchall()))
        finally:
            connection.close()
        return {'stock': result, 'bars': make_chart_bars(rows)}


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
                        200, 'text/html; charset=utf-8', HTML_PATH.read_bytes()
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
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--log-directory', type=Path, default=LOG_DIRECTORY)
    parser.add_argument('--task-name', default=TASK_NAME)
    args = parser.parse_args()

    service = DashboardService(args.log_directory, args.task_name)
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(service)
    )
    url = f'http://{args.host}:{server.server_port}/'
    print(f'DayTrading Monitor: {url}')
    print('종료하려면 이 창에서 Ctrl+C를 누르세요.')
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print('\nDayTrading 모니터 서버를 종료합니다.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()