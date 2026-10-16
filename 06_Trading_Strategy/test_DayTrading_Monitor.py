import tempfile
import unittest
from datetime import date
from pathlib import Path

from DayTrading_Monitor import make_chart_bars, make_progress, parse_log_records


class DayTradingMonitorLogTests(unittest.TestCase):
    def test_parses_python_and_scheduled_launcher_log_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'daytrading_20261014_083001.log'
            path.write_text(
                '[2026-10-14  8:30:01.08] "DayTrading started in SCHEDULED LIVE mode."\n'
                '2026-10-14 08:30:03 INFO DayTrading run started: analysis_date=2026-10-14\n',
                encoding='utf-8',
            )

            records = parse_log_records(directory, date(2026, 10, 14))

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['timestamp'], '2026-10-14 08:30:01')
        self.assertIn('SCHEDULED LIVE', records[0]['message'])
        self.assertIn('DayTrading run started', records[1]['message'])

    def test_skipped_task_is_distinguished_from_completed_analysis(self):
        records = [{
            'timestamp': '2026-10-14 19:16:48',
            'level': 'INFO',
            'message': 'DayTrading skipped: sell monitoring window ended at 18:00.',
        }]
        analysis = {
            'analysis_date': '2026-10-13',
            'status': 'COMPLETED',
        }

        progress = make_progress(
            {'state': 'Ready', 'last_result': 0},
            records,
            analysis,
            today=date(2026, 10, 14),
        )

        self.assertEqual(progress['status'], 'waiting')
        self.assertIn('18:00', progress['current_step'])
        self.assertEqual(
            [step['state'] for step in progress['steps']],
            ['done', 'done', 'done', 'done', 'pending'],
        )

    def test_running_analysis_reports_latest_stage(self):
        records = [
            {
                'timestamp': '2026-10-14 08:30:00',
                'level': 'INFO',
                'message': 'DayTrading run started: analysis_date=2026-10-14',
            },
            {
                'timestamp': '2026-10-14 08:30:25',
                'level': 'INFO',
                'message': 'Overseas indicator close download started',
            },
        ]

        progress = make_progress(
            {'state': 'Running'}, records, None, today=date(2026, 10, 14)
        )

        self.assertEqual(progress['status'], 'running')
        self.assertEqual(progress['current_step'], '해외 지표 갱신 진행 중')
        self.assertEqual(progress['steps'][1]['state'], 'active')

    def test_scheduler_checking_does_not_block_analysis_summary(self):
        progress = make_progress(
            {'state': 'checking'},
            [],
            {'analysis_date': '2026-10-13', 'status': 'COMPLETED'},
            today=date(2026, 10, 14),
        )

        self.assertEqual(progress['status'], 'checking')
        self.assertEqual(progress['current_step'], '작업 스케줄러 상태 조회 중')
        self.assertEqual(progress['steps'][0]['state'], 'done')


class DayTradingMonitorChartTests(unittest.TestCase):
    def test_calculates_bollinger_bands_and_daily_volume(self):
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