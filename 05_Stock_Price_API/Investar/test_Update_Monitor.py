import tempfile
import unittest
from pathlib import Path

from Update_Monitor import DEFAULT_LOG_PATH, make_progress, read_log_lines


class UpdateMonitorTests(unittest.TestCase):
    def test_default_log_path_uses_user_desktop(self):
        self.assertEqual(
            DEFAULT_LOG_PATH,
            Path.home() / 'Desktop' / 'StockAnalysis_Update.log',
        )

    def test_reads_carriage_return_progress_as_separate_records(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / 'update.log'
            log_path.write_bytes(
                'Candidate selection started\r'
                '3개월 수익률 조회 12/40\r'
                'Daily price update started\n'
                .encode('cp949')
            )

            records = read_log_lines(log_path)

        self.assertEqual(len(records), 3)
        self.assertEqual(records[1], '3개월 수익률 조회 12/40')
        self.assertIn('Daily price update started', records[-1])

    def test_candidate_query_progress_is_calculated(self):
        runtime = make_progress(
            {'state': 'Running'},
            [
                'Candidate selection started',
                '3개월 수익률 조회 816/2487',
            ],
        )

        self.assertEqual(runtime['status'], 'running')
        self.assertEqual(runtime['phase'], 'candidates')
        self.assertEqual(runtime['current'], 816)
        self.assertEqual(runtime['total'], 2487)
        self.assertEqual(runtime['percent'], 32.8)
        self.assertEqual(runtime['steps'][0]['state'], 'active')

    def test_daily_collection_progress_uses_per_stock_log(self):
        runtime = make_progress(
            {'state': 'Running'},
            [
                'Candidate selection started',
                'Daily price update started',
                '종목 125/4312 삼성전자 (005930) 일봉 조회 중',
            ],
        )

        self.assertEqual(runtime['phase'], 'daily')
        self.assertEqual(runtime['current'], 125)
        self.assertEqual(runtime['total'], 4312)
        self.assertEqual(runtime['steps'][0]['state'], 'done')
        self.assertEqual(runtime['steps'][1]['state'], 'active')

    def test_completed_batch_reports_waiting_next_run(self):
        runtime = make_progress(
            {'state': 'Ready', 'next_run': '2026-10-15 15:40:00'},
            [
                'Candidate selection started',
                'Daily price update started',
                'Candidate and daily price update completed',
            ],
        )

        self.assertEqual(runtime['status'], 'finished')
        self.assertEqual(runtime['steps'][1]['state'], 'done')
        self.assertEqual(runtime['next_run'], '2026-10-15 15:40:00')

    def test_failed_batch_reports_error(self):
        runtime = make_progress(
            {'state': 'Ready'},
            [
                'Candidate selection started',
                'Update failed with error 1',
            ],
        )

        self.assertEqual(runtime['status'], 'error')
        self.assertEqual(runtime['current_step'], 'Update failed with error 1')


if __name__ == '__main__':
    unittest.main()