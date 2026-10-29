"""Atomic, account-free runtime decisions for the read-only dashboard."""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


STATUS_MAX_AGE_SECONDS = 180
STATUS_PATH = (
    Path(os.environ.get('LOCALAPPDATA') or Path.home())
    / 'StockAnalysisInPython' / 'logs' / 'trader_status.json'
)


def write_status(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent,
            prefix=path.name + '.', suffix='.tmp', delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def read_status(path, now=None):
    now = now or datetime.now()
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        if (
            payload.get('version') != 1
            or not isinstance(payload.get('decisions'), list)
            or not isinstance(payload.get('cycle_error'), str)
        ):
            raise ValueError('지원하지 않는 상태 형식')
        generated = datetime.fromisoformat(payload['generated_at'])
        age = (now - generated).total_seconds()
        for row in payload['decisions']:
            if not isinstance(row, dict) or not all(
                name in row for name in (
                    'code', 'company', 'selected', 'status', 'reasons',
                    'price', 'ask', 'ma20', 'mfi10', 'quantity',
                    'budget_left', 'order_quantity', 'last_order',
                )
            ) or not isinstance(row['reasons'], list):
                raise ValueError('종목 판단 정보가 불완전합니다.')
        fresh = 0 <= age <= STATUS_MAX_AGE_SECONDS
        return {
            **payload, 'state': 'fresh' if fresh else 'stale',
            'message': (
                '실행 중 Trader의 실제 판단입니다. 주문/체결은 별도 상태를 확인하세요.'
                if fresh else '판단 정보가 오래됐거나 시각이 맞지 않습니다. 현재 매수 가능 여부는 미확인입니다.'
            ),
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {
            'state': 'unavailable', 'decisions': [], 'generated_at': None,
            'cycle_error': '',
            'message': f'실행 판단 미확인: {exc}. 수정된 Trader의 다음 실행부터 제공됩니다.',
        }
