"""Read-only dashboard for candidate, daily-price, and minute-price updates."""

import argparse
import json
import os
import re
import subprocess
import threading
import time
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pymysql


TASK_NAME = 'StockAnalysisInPython-01-Update'
DEFAULT_LOG_PATH = Path.home() / 'Desktop' / 'StockAnalysis_Update.log'
LOG_PATH = Path(os.environ.get(
    'STOCK_UPDATE_LOG', str(DEFAULT_LOG_PATH)
))
HTML_PATH = Path(__file__).with_name('Update_Monitor.html')
LOG_LIMIT = 3000


def read_log_lines(path=LOG_PATH, limit=LOG_LIMIT):
    path = Path(path)
    if not path.exists():
        return []
    raw_content = path.read_bytes()
    try:
        content = raw_content.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = raw_content.decode('cp949', errors='replace')
    return content.replace('\r\n', '\n').replace('\r', '\n').splitlines()[-limit:]


def _last_match(lines, pattern):
    compiled = re.compile(pattern, re.IGNORECASE)
    for line in reversed(lines):
        match = compiled.search(line)
        if match:
            return match
    return None


def make_progress(task, log_lines):
    start_index = next(
        (
            index for index in range(len(log_lines) - 1, -1, -1)
            if 'Candidate selection started' in log_lines[index]
        ),
        0,
    )
    current_lines = log_lines[start_index:]
    candidate_start = any(
        'Candidate selection started' in line for line in current_lines
    )
    daily_started = any(
        'Daily price update started' in line for line in current_lines
    )
    completed = any(
        'Candidate and daily price update completed' in line
        for line in current_lines
    )
    failed = any(
        'Update failed with error' in line for line in current_lines
    )
    candidate_match = _last_match(
        current_lines, r'(?:3개월 수익률 조회|수익률 조회)\s*(\d+)\s*/\s*(\d+)'
    )
    daily_match = _last_match(
        current_lines, r'종목\s*(\d+)\s*/\s*(\d+).*?일봉 조회 중'
    )
    state = str(task.get('state') or 'unknown').lower()
    running = state == 'running'

    if daily_started:
        phase = 'daily'
        match = daily_match
        current_step = '전체 종목 일봉 수집 진행 중'
        stage_index = 1
    elif candidate_start:
        phase = 'candidates'
        match = candidate_match
        current_step = '시가총액·3개월 수익률 분석 진행 중'
        stage_index = 0
    else:
        phase = 'unknown'
        match = None
        current_step = '실행 로그에서 현재 단계를 확인할 수 없습니다.'
        stage_index = -1

    completed_stages = 0
    if daily_started or completed:
        completed_stages = 1
    if completed:
        completed_stages = 2
    if failed:
        current_step = next(
            (line.strip() for line in reversed(current_lines)
             if 'Update failed with error' in line),
            '업데이트 작업이 오류로 종료되었습니다.',
        )
    elif completed:
        current_step = '후보 선정 및 일봉 업데이트 완료'
    elif not running and state == 'ready':
        current_step = '다음 예약 실행 대기'
    elif not running and state in ('unknown', 'not_found'):
        current_step = '작업 스케줄러 상태를 확인할 수 없습니다.'

    current = int(match.group(1)) if match else None
    total = int(match.group(2)) if match else None
    percent = round(current / total * 100, 1) if total else None
    steps = [
        {
            'label': '후보 선정 및 3개월 수익률 분석',
            'state': 'done' if completed_stages >= 1 else (
                'active' if stage_index == 0 and running else 'pending'
            ),
        },
        {
            'label': '전체 종목 일봉 갱신',
            'state': 'done' if completed_stages >= 2 else (
                'active' if stage_index == 1 and running else 'pending'
            ),
        },
        {'label': '분봉 DB 최신 현황 확인', 'state': 'monitor'},
    ]

    if failed:
        status = 'error'
    elif running:
        status = 'running'
    elif completed:
        status = 'finished'
    elif state == 'ready':
        status = 'waiting'
    else:
        status = 'unknown'

    return {
        'status': status,
        'task_state': task.get('state', 'unknown'),
        'current_step': current_step,
        'phase': phase,
        'current': current,
        'total': total,
        'percent': percent,
        'steps': steps,
        'last_run': task.get('last_run'),
        'last_result': task.get('last_result'),
        'next_run': task.get('next_run'),
    }


def get_task_status(task_name=TASK_NAME):
    safe_task_name = task_name.replace("'", "''")
    command = (
        f"$task = Get-ScheduledTask -TaskName '{safe_task_name}' "
        '-ErrorAction SilentlyContinue; '
        'if ($null -eq $task) { exit 2 }; '
        f"$info = Get-ScheduledTaskInfo -TaskName '{safe_task_name}'; "
        '[pscustomobject]@{ state=[string]$task.State; '
        "last_run=$info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss'); "
        'last_result=[int]$info.LastTaskResult; '
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
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {
            'state': 'unknown', 'last_run': None,
            'last_result': None, 'next_run': None,
        }


def _format_date(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def connect_database():
    return pymysql.connect(
        host=os.environ.get('STOCK_DB_HOST', 'localhost'),
        user=os.environ.get('STOCK_DB_USER', 'root'),
        password=os.environ.get('STOCK_DB_PASSWORD', 'root'),
        database=os.environ.get('STOCK_DB_NAME', 'zunDB'),
        charset='utf8',
        connect_timeout=5,
        read_timeout=15,
        write_timeout=15,
    )


def load_data():
    connection = connect_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT MAX(date) FROM daily_price'
            )
            daily_date = cursor.fetchone()[0]
            daily = {'date': _format_date(daily_date), 'rows': 0, 'stocks': 0}
            if daily_date:
                cursor.execute(
                    'SELECT COUNT(*), COUNT(DISTINCT code) FROM daily_price '
                    'WHERE date = %s', (daily_date,)
                )
                daily['rows'], daily['stocks'] = map(int, cursor.fetchone())

            cursor.execute(
                'SELECT date, COUNT(*), COUNT(DISTINCT code), MAX(trade_time) '
                'FROM minute_price GROUP BY date ORDER BY date DESC LIMIT 8'
            )
            minute_days = [
                {
                    'date': _format_date(row[0]),
                    'rows': int(row[1]),
                    'stocks': int(row[2]),
                    'last_time': str(row[3]) if row[3] else None,
                }
                for row in cursor.fetchall()
            ]

            cursor.execute(
                'SELECT MAX(updated_at), COUNT(*) FROM selected_stock '
                'WHERE market = %s', ('KOSPI',)
            )
            candidate_updated, candidate_count = cursor.fetchone()
            cursor.execute(
                'SELECT MAX(snapshot_date), COUNT(DISTINCT snapshot_date) '
                'FROM selected_stock_history WHERE market = %s', ('KOSPI',)
            )
            snapshot_date, snapshot_count = cursor.fetchone()
    finally:
        connection.close()

    return {
        'daily': daily,
        'minute_days': minute_days,
        'candidates': {
            'updated_at': _format_date(candidate_updated),
            'count': int(candidate_count or 0),
            'snapshot_date': _format_date(snapshot_date),
            'snapshot_days': int(snapshot_count or 0),
        },
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


class DashboardService:
    def __init__(self, log_path=LOG_PATH, task_name=TASK_NAME):
        self.log_path = Path(log_path)
        self.task_name = task_name
        self._data = None
        self._data_at = 0.0
        self._log_lines = []
        self._logs_at = 0.0
        self._task = {
            'state': 'checking', 'last_run': None,
            'last_result': None, 'next_run': None,
        }
        self._task_at = 0.0
        self._task_loading = False
        self._task_lock = threading.Lock()

    def _get_data(self, force=False):
        if force or self._data is None or time.monotonic() - self._data_at >= 15:
            self._data = load_data()
            self._data_at = time.monotonic()
        return self._data

    def _get_logs(self):
        if time.monotonic() - self._logs_at >= 2:
            self._log_lines = read_log_lines(self.log_path)
            self._logs_at = time.monotonic()
        return self._log_lines

    def _get_task(self):
        with self._task_lock:
            if not self._task_loading and time.monotonic() - self._task_at >= 10:
                self._task_loading = True
                threading.Thread(target=self._refresh_task, daemon=True).start()
            return dict(self._task)

    def _refresh_task(self):
        task = get_task_status(self.task_name)
        with self._task_lock:
            self._task = task
            self._task_at = time.monotonic()
            self._task_loading = False

    def dashboard(self, force=False):
        errors = []
        try:
            data = self._get_data(force)
        except Exception as exc:
            data = {
                'daily': {'date': None, 'rows': 0, 'stocks': 0},
                'minute_days': [],
                'candidates': {
                    'updated_at': None, 'count': 0,
                    'snapshot_date': None, 'snapshot_days': 0,
                },
                'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            errors.append(f'DB 조회 실패: {exc}')
        lines = self._get_logs()
        task = self._get_task()
        return {
            **data,
            'runtime': make_progress(task, lines),
            'events': lines[-30:],
            'log_path': str(self.log_path),
            'errors': errors,
        }


def make_handler(service):
    class MonitorHandler(BaseHTTPRequestHandler):
        def _send(self, status, content_type, body):
            encoded = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/api/dashboard':
                try:
                    self._send(
                        200, 'application/json; charset=utf-8',
                        json.dumps(service.dashboard(), ensure_ascii=False),
                    )
                except Exception as exc:
                    self._send(
                        500, 'application/json; charset=utf-8',
                        json.dumps({'error': str(exc)}, ensure_ascii=False),
                    )
                return
            if path in ('/', '/index.html'):
                try:
                    body = HTML_PATH.read_text(encoding='utf-8')
                except OSError as exc:
                    self._send(500, 'text/plain; charset=utf-8', str(exc))
                    return
                self._send(200, 'text/html; charset=utf-8', body)
                return
            self._send(404, 'text/plain; charset=utf-8', 'Not Found')

        def log_message(self, format_string, *args):
            return

    return MonitorHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(DashboardService())
    )
    url = f'http://{args.host}:{server.server_port}/'
    print(f'Update Monitor: {url}', flush=True)
    print('종료하려면 이 창에서 Ctrl+C를 누르세요.', flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()