import tempfile
import unittest
from pathlib import Path

from BollingerBand_MFI10_Monitor import (
    event_records,
    make_chart_bars,
    parse_log_records,
    progress_snapshot,
)


class MonitorLogTests(unittest.TestCase):
    def test_daily_data_warnings_are_visible_in_recent_events(self):
        record = {
            'timestamp': '2026-10-21 09:00:00', 'level': 'INFO',
            'message': '[일봉 보충 미완료] missing bars; 신규 매수 차단 유지',
        }
        self.assertEqual(event_records([record]), [record])

    def test_parses_recent_trader_log_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trader.log'
            path.write_text(
                '2026-10-14 09:00:00,000 INFO Trader process started\n'
                '2026-10-14 09:00:01,000 ERROR database unavailable\n',
                encoding='utf-8',
            )

            records = parse_log_records(path)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]['level'], 'ERROR')
        self.assertEqual(records[-1]['message'], 'database unavailable')

    def test_progress_uses_current_run_events(self):
        records = [
            {'timestamp': '2026-10-14 09:00:00', 'level': 'INFO', 'message': 'Trader process started'},
            {'timestamp': '2026-10-14 09:00:03', 'level': 'INFO', 'message': 'KOSPI 후보 100종목 중 전략 선정 9종목'},
            {'timestamp': '2026-10-14 09:00:05', 'level': 'INFO', 'message': '[매수/매도 조건 조회] 시세조회 9종목'},
        ]

        progress = progress_snapshot({'state': 'Running'}, records)

        self.assertEqual(progress['status'], 'running')
        self.assertEqual(progress['current_step'], '실시간 시세 감시 중')
        self.assertEqual(progress['steps'][1]['state'], 'done')

    def test_clean_duplicate_launch_does_not_replace_active_run(self):
        records = [
            {'timestamp': '2026-10-14 09:00:00', 'level': 'INFO', 'message': 'Trader process started'},
            {'timestamp': '2026-10-14 09:00:03', 'level': 'INFO', 'message': 'KOSPI 후보 100종목 중 전략 선정 9종목'},
            {'timestamp': '2026-10-14 09:00:05', 'level': 'INFO', 'message': '[매수/매도 조건 조회] 시세조회 9종목'},
            {'timestamp': '2026-10-14 09:01:00', 'level': 'INFO', 'message': 'Trader process started'},
            {'timestamp': '2026-10-14 09:01:01', 'level': 'INFO', 'message': '같은 계좌의 Trader가 이미 실행 중입니다. 신규 실행을 건너뜁니다.'},
        ]

        progress = progress_snapshot({'state': 'Running'}, records)

        self.assertEqual(progress['current_step'], '실시간 시세 감시 중')

    def test_recent_cycle_clears_an_older_error_for_running_task(self):
        records = [
            {'timestamp': '2026-10-14 09:00:00', 'level': 'ERROR', 'message': 'older startup error'},
            {'timestamp': '2026-10-14 09:02:00', 'level': 'INFO', 'message': '[매수/매도 조건 조회] 시세조회 9종목'},
        ]

        progress = progress_snapshot({'state': 'Running'}, records)

        self.assertEqual(progress['status'], 'running')
        self.assertEqual(progress['current_step'], '실시간 시세 감시 중')

    def test_running_task_without_recent_start_log_stays_truthful(self):
        progress = progress_snapshot({'state': 'Running'}, [])

        self.assertEqual(progress['status'], 'running')
        self.assertEqual(
            progress['current_step'],
            'Trader 프로세스 실행 중 · 최근 로그 갱신 대기',
        )
        self.assertEqual(progress['steps'][0]['state'], 'done')


class MonitorChartTests(unittest.TestCase):
    def test_builds_bollinger_bands_and_volume_series(self):
        rows = [
            (f'2026-09-{day:02d}', 101, 99, 100 + day, 1000 + day)
            for day in range(1, 22)
        ]

        bars = make_chart_bars(rows)

        self.assertEqual(len(bars), 21)
        self.assertIsNone(bars[18]['middle'])
        self.assertAlmostEqual(bars[19]['middle'], 110.5)
        self.assertGreater(bars[19]['upper'], bars[19]['middle'])
        self.assertEqual(bars[-1]['volume'], 1021)


if __name__ == '__main__':
    unittest.main()