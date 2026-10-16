"""Download adjusted daily closes for overseas market indicator ETFs."""

import argparse
import warnings
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests


DEFAULT_TICKERS = (
    'SPY', 'QQQ', 'DIA', 'IWM', 'EFA', 'EEM', 'SOXX', 'XLE',
    'TLT', 'UUP', 'GLD', 'SLV', 'USO', 'VIXY',
)
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent / 'data' / '해외지표종가.csv'
)
MINIMUM_PRICE_ROWS = 61
MINIMUM_VALID_TICKERS = 4
YAHOO_CHART_HOSTS = (
    'https://query1.finance.yahoo.com',
    'https://query2.finance.yahoo.com',
)


def _download_ticker_close(ticker, period, session=requests):
    failures = []
    for host in YAHOO_CHART_HOSTS:
        url = f'{host}/v8/finance/chart/{quote(ticker, safe="^=")}'
        try:
            response = session.get(
                url,
                params={
                    'range': period,
                    'interval': '1d',
                    'events': 'div,splits',
                },
                headers={
                    'User-Agent': 'Mozilla/5.0',
                    'Accept': 'application/json',
                },
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json().get('chart', {})
            results = payload.get('result') or []
            if not results:
                error = payload.get('error') or 'empty chart response'
                raise RuntimeError(str(error))

            result = results[0]
            timestamps = result.get('timestamp') or []
            indicators = result.get('indicators', {})
            quote_data = (indicators.get('quote') or [{}])[0]
            raw_close = quote_data.get('close') or []
            adjusted_data = indicators.get('adjclose') or []
            adjusted_close = (
                adjusted_data[0].get('adjclose', []) if adjusted_data else []
            )
            if not timestamps or not raw_close:
                raise RuntimeError('chart response has no close prices')

            close = pd.Series(raw_close, dtype=float)
            if len(adjusted_close) == len(close):
                adjusted = pd.Series(adjusted_close, dtype=float)
                close = adjusted.combine_first(close)
            dates = pd.to_datetime(timestamps, unit='s', utc=True)
            dates = dates.tz_convert('America/New_York').tz_localize(None)
            close.index = dates.normalize()
            close = close[~close.index.duplicated(keep='last')].dropna()
            close.name = ticker
            return close.sort_index()
        except Exception as exc:
            failures.append(f'{host}: {exc}')

    raise RuntimeError('; '.join(failures))


def download_indicator_closes(
    output_path=DEFAULT_OUTPUT,
    tickers=DEFAULT_TICKERS,
    period='1y',
):
    tickers = tuple(str(ticker).strip().upper() for ticker in tickers)
    if not tickers or any(not ticker for ticker in tickers):
        raise ValueError('하나 이상의 지표 티커가 필요합니다.')

    downloaded = {}
    failures = {}
    for ticker in tickers:
        try:
            downloaded[ticker] = _download_ticker_close(ticker, period)
        except Exception as exc:
            failures[ticker] = str(exc)

    minimum_tickers = min(MINIMUM_VALID_TICKERS, len(tickers))
    if len(downloaded) < minimum_tickers:
        details = '; '.join(
            f'{ticker}: {error}' for ticker, error in failures.items()
        )
        raise RuntimeError(
            f'유효한 지표 다운로드가 {len(downloaded)}개뿐입니다. '
            f'최소 {minimum_tickers}개가 필요합니다. {details}'
        )
    if failures:
        warnings.warn(
            '일부 지표를 받지 못해 성공한 지표만 저장합니다: '
            + ', '.join(sorted(failures)),
            RuntimeWarning,
        )

    closes = pd.concat(downloaded.values(), axis=1)
    closes = closes.reindex(columns=list(downloaded)).dropna(how='all').sort_index()
    if len(closes) < MINIMUM_PRICE_ROWS:
        raise RuntimeError(
            f'상관 분석에는 최소 {MINIMUM_PRICE_ROWS}개 종가 행이 필요합니다. '
            f'현재 {len(closes)}개입니다.'
        )

    dates = pd.DatetimeIndex(pd.to_datetime(closes.index))
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    closes.index = dates.normalize()
    closes = closes[~closes.index.duplicated(keep='last')]
    closes.index.name = 'date'

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(output_path.name + '.tmp')
    closes.to_csv(temporary_path, date_format='%Y-%m-%d')
    temporary_path.replace(output_path)
    return output_path, len(closes), closes.index[-1]


def main():
    parser = argparse.ArgumentParser(
        description='Yahoo Finance에서 해외 ETF 조정 종가 CSV를 생성합니다.'
    )
    parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    parser.add_argument('--tickers', default=','.join(DEFAULT_TICKERS))
    parser.add_argument('--period', default='1y')
    args = parser.parse_args()

    output_path, row_count, latest_date = download_indicator_closes(
        output_path=args.output,
        tickers=args.tickers.split(','),
        period=args.period,
    )
    print(
        f'저장 완료: {output_path.resolve()} '
        f'({row_count}행, 최근 거래일 {latest_date:%Y-%m-%d})'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())