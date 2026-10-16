"""Generate KRX day-trading signals and optionally submit guarded CREON buys."""

import argparse
import logging
import math
import subprocess
import sys
import time
from datetime import date, datetime, time as datetime_time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from DayTrading_Indicator_Correlation import (
    DEFAULT_MAX_BUY_AMOUNT_KRW,
    rank_correlations,
)
from DayTrading_DownloadIndicators import (
    DEFAULT_OUTPUT as DEFAULT_INDICATOR_CLOSES,
    DEFAULT_TICKERS as DEFAULT_INDICATOR_TICKERS,
    download_indicator_closes,
)
from DayTrading_Data import (
    collect_missing_korean_closes,
    load_indicator_closes_from_database,
    load_stock_data_from_database,
    save_indicator_closes_to_database,
)
from DayTrading_Results import (
    claim_daytrading_order_attempt,
    connect_database,
    create_top10_chart,
    load_daytrading_buy_signals,
    save_analysis_results,
    update_daytrading_order_attempt,
)
from BollingerBand_MFI10_Trader import (
    DAYTRADING_NET_PROFIT_TARGET,
    DAYTRADING_ROUND_TRIP_COST_ESTIMATE,
    CreonClient,
)


BUY_WINDOW_START = datetime_time(8, 30)
BUY_WINDOW_END = datetime_time(9, 30)
LIVE_ORDER_WINDOW_START = datetime_time(9, 0)
LIVE_ORDER_WINDOW_END = datetime_time(9, 30)
PREOPEN_CLOSE_WINDOW_END = datetime_time(8, 40)
OPENING_AUCTION_WINDOW_END = datetime_time(9, 0)
AFTER_MARKET_START = datetime_time(16, 0)
AFTER_MARKET_END = datetime_time(20, 0)
BUY_COST_BUFFER_RATE = 0.005


def is_buy_window_open(as_of_date, now=None):
    now = now or datetime.now()
    return (
        date.fromisoformat(as_of_date) == now.date()
        and BUY_WINDOW_START <= now.time() < BUY_WINDOW_END
    )


def is_live_order_window_open(as_of_date, now=None):
    now = now or datetime.now()
    return (
        date.fromisoformat(as_of_date) == now.date()
        and now.weekday() < 5
        and BUY_WINDOW_START <= now.time() < LIVE_ORDER_WINDOW_END
    )


def is_after_buy_monitoring_window(as_of_date, now=None):
    now = now or datetime.now()
    return (
        date.fromisoformat(as_of_date) == now.date()
        and now.weekday() < 5
        and LIVE_ORDER_WINDOW_END <= now.time() < AFTER_MARKET_END
    )


def wait_until_after_market(as_of_date, logger, now_provider=None, sleep=None):
    now_provider = now_provider or datetime.now
    sleep = sleep or time.sleep
    deadline = datetime.combine(
        date.fromisoformat(as_of_date), AFTER_MARKET_END
    )
    now = now_provider()
    if now >= deadline:
        logger.info('DayTrading daemon reached after-market end')
        return

    logger.info(
        'DayTrading daemon remains active until %s',
        deadline.isoformat(timespec='minutes'),
    )
    while now < deadline:
        sleep(min(60, max(1, (deadline - now).total_seconds())))
        now = now_provider()
    logger.info('DayTrading after-market monitoring window ended')


def run_after_market_monitor(
    as_of_date, logger, run_process=None, now_provider=None
):
    now_provider = now_provider or datetime.now
    deadline = datetime.combine(
        date.fromisoformat(as_of_date), AFTER_MARKET_END
    )
    if now_provider() >= deadline:
        logger.info('After-market monitoring window already ended')
        return 0

    monitor_script = Path(__file__).with_name(
        'BollingerBand_MFI10_Trader.py'
    )
    monitor_bootstrap = '\n'.join((
        'import BollingerBand_MFI10_Trader as trader',
        'import DayTrading_Trader as daytrading',
        'from dataclasses import replace',
        'from datetime import date, datetime',
        'original_load_candidates = trader.load_kospi_candidates',
        'original_filter_candidates = trader.filter_candidates_by_owner',
        'daytrading_signal_codes_cache = None',
        'def daytrading_signal_codes(connection):',
        '    global daytrading_signal_codes_cache',
        '    if daytrading_signal_codes_cache is None:',
        '        try:',
        '            signals = daytrading.load_daytrading_buy_signals(connection, date.today())',
        '            daytrading_signal_codes_cache = {trader.normalize_code(code) for code in signals[\'code\']}',
        '        except Exception as exc:',
        "            print(f'DayTrading aftermarket signals unavailable: {exc}', flush=True)",
        '            daytrading_signal_codes_cache = set()',
        '    return daytrading_signal_codes_cache',
        'def daytrading_load_candidates(connection, extra_codes=None):',
        '    signal_codes = daytrading_signal_codes(connection)',
        '    if signal_codes:',
        '        extra_codes = set(extra_codes or ()) | signal_codes',
        '    candidates = original_load_candidates(connection, extra_codes)',
        '    missing_codes = {',
        '        trader.normalize_code(candidate.code)',
        '        for candidate in candidates',
        '        if not candidate.company or trader.normalize_code(candidate.company) == trader.normalize_code(candidate.code)',
        '    }',
        '    if not missing_codes:',
        '        return candidates',
        "    placeholders = ', '.join(['%s'] * len(missing_codes))",
        '    with connection.cursor() as cursor:',
        "        cursor.execute('SELECT code, company FROM daytrading_stock_universe WHERE code IN (' + placeholders + ')', tuple(sorted(missing_codes)))",
        '        company_names = {',
        '            trader.normalize_code(code): company',
        '            for code, company in cursor.fetchall()',
        '            if company and trader.normalize_code(company) != trader.normalize_code(code)',
        '        }',
        '        unresolved_codes = missing_codes.difference(company_names)',
        '        if unresolved_codes:',
        "            fallback_placeholders = ', '.join(['%s'] * len(unresolved_codes))",
        "            cursor.execute('SELECT code, company FROM company_info WHERE code IN (' + fallback_placeholders + ')', tuple(sorted(unresolved_codes)))",
        '            company_names.update({',
        '                trader.normalize_code(code): company',
        '                for code, company in cursor.fetchall()',
        '                if company and trader.normalize_code(company) != trader.normalize_code(code)',
        '            })',
        '    return [',
        '        replace(candidate, company=company_names.get(',
        '            trader.normalize_code(candidate.code), candidate.company',
        '        ))',
        '        for candidate in candidates',
        '    ]',
        'trader.load_kospi_candidates = daytrading_load_candidates',
        'def daytrading_filter_candidates(candidates, order_quantities, owner, states=None):',
        '    filtered = original_filter_candidates(candidates, order_quantities, owner, states)',
        "    if owner != 'daytrading':",
        '        return filtered',
        '    included = {trader.normalize_code(stock.code) for stock in filtered}',
        '    return filtered + [',
        '        stock for stock in candidates',
        '        if trader.normalize_code(stock.code) in daytrading_signal_codes_cache',
        '        and trader.normalize_code(stock.code) not in included',
        '    ]',
        'trader.filter_candidates_by_owner = daytrading_filter_candidates',
        'def daytrading_net_profit_rate(exit_price, average_price):',
        '    if exit_price <= 0 or average_price <= 0:',
        "        return float('-inf')",
        '    return exit_price / average_price - 1',
        'trader.daytrading_net_profit_rate = daytrading_net_profit_rate',
        'original_sell = trader.Trader._sell',
        'def daytrading_sell(self, stock, quote, state, reason):',
        "    if reason == 'daytrading_take_profit':",
        "        announced = getattr(self, '_daytrading_sell_announced', set())",
        '        if stock.code not in announced:',
        '            announced.add(stock.code)',
        '            self._daytrading_sell_announced = announced',
        '            now = datetime.now()',
        '            session = trader.trading_session(now)',
        "            exit_price = quote.bid if session in ('REGULAR', 'AFTER_MARKET') else quote.price",
        '            rate = trader.daytrading_net_profit_rate(exit_price, state.average_price)',
        '            target = trader.daytrading_net_profit_target(now)',
        "            print(f'[DayTrading 매도 기준 충족] {stock.company} ({stock.code}) 수익률 {rate:.2%}, 기준 {target:.2%}', flush=True)",
        '    return original_sell(self, stock, quote, state, reason)',
        'trader.Trader._sell = daytrading_sell',
        'daytrading_logger = daytrading.configure_logging()',
        'original_run_cycle = trader.Trader.run_cycle',
        'def daytrading_run_cycle(self):',
        '    now = datetime.now()',
        "    if trader.trading_session(now) == 'AFTER_MARKET' and not getattr(self, '_daytrading_aftermarket_orders_checked', False):",
        '        try:',
        '            signals = daytrading.load_daytrading_buy_signals(self.connection, date.today())',
        "            order_args = daytrading.SimpleNamespace(live=True, as_of_date=date.today().isoformat(), order_session='after-market', max_total_buy_amount=1_000_000)",
        '            daytrading.place_live_buy_orders(self.connection, signals, order_args, daytrading_logger, now=now, creon_client=self.creon)',
        '            self._daytrading_aftermarket_orders_checked = True',
        '        except Exception:',
        "            daytrading_logger.exception('Unable to submit DayTrading aftermarket orders')",
        '    return original_run_cycle(self)',
        'trader.Trader.run_cycle = daytrading_run_cycle',
        'args = trader.parse_args()',
        "role = 'daytrading' if args.daytrading_monitor_only else 'mfi'",
        'logger = trader.configure_trader_logging(role)',
        'try:',
        '    trader.main(args)',
        'except KeyboardInterrupt:',
        "    print('\\n사용자 요청으로 Trader를 중지했습니다.')",
        'else:',
        "    logger.info('Trader process finished normally')",
    ))
    command = [
        sys.executable, '-c', monitor_bootstrap,
        '--live', '--confirm', 'LIVE', '--daytrading-monitor-only',
    ]
    logger.info('Starting DayTrading position monitor through 20:00')
    run_process = run_process or subprocess.run
    try:
        result = run_process(
            command, cwd=str(monitor_script.parent), check=False
        )
        return_code = result.returncode
    except Exception:
        logger.exception('Unable to start MFI position monitor')
        return_code = 1

    if return_code != 0:
        logger.error(
            'DayTrading position monitor exited with code %d; '
            'DayTrading will remain active until 20:00', return_code,
        )
        wait_until_after_market(as_of_date, logger)
    elif now_provider() < deadline:
        logger.warning(
            'DayTrading position monitor exited before 20:00; '
            'DayTrading will remain active until 20:00'
        )
        wait_until_after_market(as_of_date, logger)
    return return_code


def resolve_live_order_session(as_of_date, requested='auto', now=None):
    now = now or datetime.now()
    if date.fromisoformat(as_of_date) != now.date() or now.weekday() >= 5:
        return None

    current_time = now.time()
    if AFTER_MARKET_START <= current_time < AFTER_MARKET_END:
        return 'AFTER_MARKET' if requested in ('auto', 'after-market') else None
    if BUY_WINDOW_START <= current_time < PREOPEN_CLOSE_WINDOW_END:
        if requested == 'auction':
            return 'OPENING_AUCTION'
        if requested in ('auto', 'preopen-close'):
            return 'PREOPEN_CLOSE'
        return None
    if PREOPEN_CLOSE_WINDOW_END <= current_time < OPENING_AUCTION_WINDOW_END:
        return 'OPENING_AUCTION' if requested in ('auto', 'auction') else None
    if LIVE_ORDER_WINDOW_START <= current_time < LIVE_ORDER_WINDOW_END:
        return 'REGULAR' if requested == 'auto' else None
    return None


def read_price_file(path):
    frame = pd.read_csv(path)
    if 'date' not in frame.columns:
        raise ValueError(f'{path} must contain a date column')
    frame['date'] = pd.to_datetime(frame['date'])
    return frame.set_index('date').sort_index()


def configure_logging(log_dir=None):
    log_dir = Path(log_dir) if log_dir else (
        Path(__file__).resolve().with_name('results') / 'logs'
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('daytrading')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    log_path = log_dir / f'daytrading_{date.today():%Y%m%d}.log'
    handler = logging.FileHandler(log_path, encoding='utf-8')
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    logger.addHandler(handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    logger.addHandler(console_handler)
    return logger


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            'KRX 장전 및 장 시작 후 08:30-09:30 매수 신호를 생성합니다. '
            '실주문은 --live --confirm LIVE를 함께 지정해야 합니다.'
        )
    )
    parser.add_argument(
        '--stock-closes',
        help='선택: DB 대신 사용할 한국 종가 CSV: date,005930,000660,...',
    )
    parser.add_argument(
        '--skip-korean-close-collection', action='store_true',
        help='CREON 일봉 수집을 생략하고 현재 DB 종가로 분석',
    )
    parser.add_argument(
        '--indicator-closes', default=str(DEFAULT_INDICATOR_CLOSES),
        help='해외 지표 종가 CSV: date,SPY,QQQ,GLD,USO,...',
    )
    parser.add_argument(
        '--no-indicator-download', action='store_true',
        help='기존 해외 지표 CSV를 사용하고 다운로드하지 않음',
    )
    parser.add_argument(
        '--indicator-tickers', default=','.join(DEFAULT_INDICATOR_TICKERS),
        help='자동으로 갱신할 Yahoo Finance ETF 티커 목록',
    )
    parser.add_argument('--indicator-period', default='1y')
    parser.add_argument(
        '--as-of-date', default=date.today().isoformat(),
        help='신호를 평가할 한국 거래일 (YYYY-MM-DD)',
    )
    parser.add_argument(
        '--metadata', help='선택: code,market,company 컬럼이 있는 종목 CSV',
    )
    parser.add_argument('--lookback', type=int, default=60)
    parser.add_argument('--min-observations', type=int, default=40)
    parser.add_argument('--top-n', type=int, default=10)
    parser.add_argument(
        '--max-buy-amount', type=int, default=DEFAULT_MAX_BUY_AMOUNT_KRW,
        help='종목별 최대 매수 예산 (원, 기본값 1000000)',
    )
    parser.add_argument(
        '--max-total-buy-amount', type=int, default=1_000_000,
        help='이번 실행의 총 매수 한도 (원, 기본값 1000000)',
    )
    parser.add_argument(
        '--order-session', choices=(
            'auto', 'auction', 'preopen-close', 'after-market'
        ),
        default='auto',
        help='실주문 세션: 자동, 장전 동시호가, 장전 시간외 종가',
    )
    parser.add_argument(
        '--live', action='store_true', help='CREON 실계좌 매수 주문 활성화',
    )
    parser.add_argument(
        '--confirm', default='', help='실주문 확인 문자열: LIVE',
    )
    parser.add_argument('--output', help='선택: 결과 CSV 경로')
    parser.add_argument('--chart', help='그래프 PNG 경로; 기본값은 results 폴더')
    parser.add_argument(
        '--no-db', '--no-analysis-db', dest='no_db', action='store_true',
        help='상관 분석 결과 DB 기록만 생략 (가격 DB는 사용)',
    )
    args = parser.parse_args()
    if args.live and args.confirm != 'LIVE':
        parser.error('실주문은 --live --confirm LIVE를 함께 지정해야 합니다.')
    if not args.live and args.confirm:
        parser.error('--confirm은 --live와 함께 사용해야 합니다.')
    if args.live and args.no_db:
        parser.error('실주문은 주문 중복 방지를 위해 DB 기록이 필요합니다.')
    if args.max_total_buy_amount < 1:
        parser.error('--max-total-buy-amount는 1원 이상이어야 합니다.')
    return args


def print_ranked_candidates(result):
    print(
        '\n당일매매 TOP 후보 '
        '(KRX 매수 신호 허용: 08:30-09:30)',
        flush=True,
    )
    print(
        '매도 기준: 예상 왕복 비용 '
        f'{DAYTRADING_ROUND_TRIP_COST_ESTIMATE:.2%} 차감 후 '
        f'순수익률 {DAYTRADING_NET_PROFIT_TARGET:.1%} 이상 (시간 무관), '
        '15:00부터 전량 청산',
        flush=True,
    )
    if result.empty or 'buy_signal' not in result.columns:
        print('  없음', flush=True)
        return

    columns = [
        'rank', 'code', 'company', 'market', 'indicator', 'correlation',
        'buy_signal', 'reference_close', 'estimated_buy_quantity',
    ]
    if 'indicator_correlations' in result.columns:
        columns.append('indicator_correlations')
    display = result[columns].rename(columns={
        'rank': '순위',
        'code': '종목코드',
        'company': '종목명',
        'market': '시장',
        'indicator': '연관지표',
        'correlation': '상관도',
        'indicator_correlations': '지표별 상관계수',
        'buy_signal': '매수신호',
        'reference_close': '기준종가',
        'estimated_buy_quantity': '추정수량',
    })
    display['매수신호'] = display['매수신호'].fillna(False).map(
        lambda value: '가능' if value else '대기'
    )
    print(display.to_string(index=False, formatters={
        '상관도': lambda value: f'{value:.4f}',
        '기준종가': lambda value: f'{value:,.0f}',
        '추정수량': lambda value: f'{value:,.0f}',
        '지표별 상관계수': lambda value: value or '-',
    }), flush=True)


def place_live_buy_orders(
    connection, result, args, logger, now=None, creon_client=None
):
    if not args.live:
        return 0
    now = now or datetime.now()
    order_session = resolve_live_order_session(
        args.as_of_date, getattr(args, 'order_session', 'auto'), now
    )
    if order_session is None:
        raise RuntimeError(
            '요청한 실주문 세션의 시간이 아닙니다. 장전 시간외 종가는 '
            '08:30-08:40, 장전 동시호가는 08:30-09:00, 장중은 '
            '09:00-09:30, 애프터마켓은 16:00-20:00에만 주문할 수 있습니다.'
        )

    signals = result.loc[result['buy_signal'].fillna(False)]
    if signals.empty:
        logger.info('No live buy signals; no CREON order client initialized')
        return 0

    creon = creon_client or CreonClient(enable_trading=True)
    quote_candidates = [
        SimpleNamespace(code=str(row.code))
        for row in signals.itertuples(index=False)
    ]
    quotes = (
        creon.get_quotes(
            quote_candidates,
            session=(
                'AFTER_MARKET'
                if order_session == 'AFTER_MARKET' else 'REGULAR'
            ),
        )
        if order_session != 'PREOPEN_CLOSE' else {}
    )
    available_cash = creon.get_available_cash()
    remaining_budget = min(
        int(args.max_total_buy_amount), int(available_cash)
    )
    submitted = 0

    for row in signals.itertuples(index=False):
        code = str(row.code).strip()
        if code.startswith('A'):
            code = code[1:]
        code = code.zfill(6) if code.isdigit() else code
        if order_session == 'AFTER_MARKET':
            try:
                eligible = creon.is_after_market_eligible(code)
            except Exception as exc:
                logger.warning(
                    'Skipping aftermarket order for %s: eligibility check '
                    'failed: %s', code, exc,
                )
                continue
            if not eligible:
                logger.info('Skipping %s: not eligible for KRX aftermarket', code)
                continue
        quote = quotes.get(code)
        price = (
            int(row.reference_close)
            if order_session == 'PREOPEN_CLOSE'
            else quote.ask if quote is not None else 0
        )
        if price <= 0 or remaining_budget <= 0:
            logger.warning('Skipping live order for %s: price/budget unavailable', code)
            continue

        order_budget = min(
            int(row.max_buy_amount_krw), remaining_budget
        )
        quantity = math.floor(
            order_budget / (price * (1 + BUY_COST_BUFFER_RATE))
        )
        if quantity <= 0:
            logger.info('Skipping %s: budget cannot buy one share', code)
            continue

        if not claim_daytrading_order_attempt(
            connection, args.as_of_date, creon.account, code, quantity, price
        ):
            logger.warning(
                'Skipping %s: an order attempt already exists for %s',
                code, args.as_of_date,
            )
            continue

        try:
            if order_session == 'PREOPEN_CLOSE':
                order_no = creon.send_preopen_close_order(
                    'buy', code, quantity
                )
            elif order_session == 'OPENING_AUCTION':
                order_no = creon.send_limit_order(
                    'buy', code, quantity, price
                )
            elif order_session == 'AFTER_MARKET':
                order_no = creon.send_ioc_best_order(
                    'buy', code, quantity, price
                )
            else:
                order_no = creon.send_ioc_best_order(
                    'buy', code, quantity, price
                )
        except Exception as exc:
            update_daytrading_order_attempt(
                connection, args.as_of_date, creon.account, code, 'UNKNOWN',
                message=f'Order result uncertain: {exc}',
            )
            logger.exception(
                'CREON order result is uncertain; duplicate retry blocked: %s',
                code,
            )
            raise

        update_daytrading_order_attempt(
            connection, args.as_of_date, creon.account, code, 'SUBMITTED',
            order_no=order_no,
            message='CREON accepted order request; fill not confirmed',
        )
        estimated_cost = math.ceil(
            quantity * price * (1 + BUY_COST_BUFFER_RATE)
        )
        remaining_budget = max(0, remaining_budget - estimated_cost)
        submitted += 1
        print(
            f'CREON 매수 주문 접수: {row.company} ({code}) '
            f'{quantity}주, 기준 가격 {price:,}원, '
            f'세션 {order_session}, '
            f'{f"주문번호 {order_no} " if order_no else ""}(체결 미확인)',
            flush=True,
        )
        logger.info(
            'CREON buy order submitted: code=%s quantity=%d price=%d '
            'session=%s order_no=%s', code, quantity, price,
            order_session, order_no,
        )
    return submitted


def _run_analysis(logger):
    args = parse_args()
    current_time = datetime.now()
    live_mode = getattr(args, 'live', False)
    if live_mode:
        if getattr(args, 'confirm', '') != 'LIVE':
            raise RuntimeError('실주문에는 --live --confirm LIVE가 필요합니다.')
        if args.no_db:
            raise RuntimeError('실주문 중복 방지를 위해 DB 기록이 필요합니다.')
        if not is_live_order_window_open(args.as_of_date, current_time):
            if is_after_buy_monitoring_window(args.as_of_date, current_time):
                logger.info(
                    'Buy window ended; starting monitor-only run through 20:00'
                )
                run_after_market_monitor(args.as_of_date, logger)
                return 0
            raise RuntimeError(
                '실주문은 오늘 평일 08:30-09:30에만 실행할 수 있습니다.'
            )
    buy_window_open = is_buy_window_open(args.as_of_date, current_time)
    logger.info('DayTrading run started: analysis_date=%s', args.as_of_date)
    logger.info(
        'KRX buy window: 08:30-09:30; current_time=%s enabled=%s',
        current_time.isoformat(timespec='seconds'), buy_window_open,
    )
    indicator_tickers = tuple(
        ticker.strip().upper()
        for ticker in args.indicator_tickers.split(',')
        if ticker.strip()
    )
    print(
        f'상관 분석 지표종목: {", ".join(indicator_tickers)}',
        flush=True,
    )
    print(
        '매수 신호 허용 시간: 오늘 08:30-09:30 '
        f'(현재 {current_time:%H:%M:%S}, '
        f'{"허용" if buy_window_open else "비허용"})',
        flush=True,
    )
    connection = connect_database()
    try:
        if args.stock_closes:
            stocks = read_price_file(args.stock_closes)
            metadata = pd.read_csv(args.metadata) if args.metadata else None
            print(f'한국 종가 CSV 로드: {stocks.shape[1]}종목')
            logger.info('Loaded Korean closes from CSV: stocks=%d', stocks.shape[1])
        else:
            if not args.skip_korean_close_collection:
                print('1/5 KOSPI/KOSDAQ 누락 일봉 수집 시작', flush=True)
                logger.info('Korean daily close collection started')
                collected_range = collect_missing_korean_closes(
                    connection, args.as_of_date, args.lookback
                )
                connection.commit()
                if collected_range is None:
                    print('한국 종가가 최신 상태입니다.', flush=True)
                    logger.info('Korean daily closes are already current')
                else:
                    print(
                        '한국 일봉 수집 완료: '
                        f'{collected_range[0]:%Y-%m-%d}~'
                        f'{collected_range[1]:%Y-%m-%d}',
                        flush=True,
                    )
                    logger.info(
                        'Korean daily close collection completed: %s to %s',
                        collected_range[0].date(), collected_range[1].date(),
                    )
            else:
                print('1/5 한국 종가 수집 생략', flush=True)
                logger.info('Korean daily close collection skipped')
            stocks, database_metadata = load_stock_data_from_database(
                connection, args.as_of_date, args.lookback
            )
            metadata = database_metadata
            print(
                f'한국 종가 DB 로드: {stocks.shape[1]}종목 '
                f'({stocks.index.min():%Y-%m-%d}~{stocks.index.max():%Y-%m-%d})'
            )
            logger.info('Loaded Korean closes from DB: stocks=%d', stocks.shape[1])

        if not args.no_indicator_download:
            print('2/5 해외 지표 ETF 종가 다운로드', flush=True)
            logger.info('Overseas indicator close download started')
            output_path, row_count, latest_date = download_indicator_closes(
                output_path=args.indicator_closes,
                tickers=indicator_tickers,
                period=args.indicator_period,
            )
            print(
                f'해외 지표 CSV 갱신: {output_path.resolve()} '
                f'({row_count}행, 최근 거래일 {latest_date:%Y-%m-%d})',
                flush=True,
            )
            logger.info(
                'Overseas indicator download completed: rows=%d latest_date=%s',
                row_count, latest_date.date(),
            )
        else:
            print('2/5 기존 해외 지표 CSV 사용', flush=True)
            logger.info('Using existing overseas indicator CSV')

        downloaded_indicators = read_price_file(args.indicator_closes)
        print('3/5 해외 지표 종가 DB 저장', flush=True)
        saved_indicator_rows = save_indicator_closes_to_database(
            connection, downloaded_indicators
        )
        indicators = load_indicator_closes_from_database(
            connection, args.as_of_date, args.lookback
        )
        print(
            f'해외 지표 DB 적재 완료: {saved_indicator_rows}건, '
            f'{indicators.shape[1]}개 지표',
            flush=True,
        )
        logger.info(
            'Overseas indicator closes saved: rows=%d tickers=%d',
            saved_indicator_rows, indicators.shape[1],
        )

        print('4/5 지표별 상관 분석 및 TOP 10 선정', flush=True)
        result = rank_correlations(
            stocks,
            indicators,
            args.as_of_date,
            stock_metadata=metadata,
            lookback=args.lookback,
            min_observations=args.min_observations,
            top_n=args.top_n,
            buy_window_open=buy_window_open,
            max_buy_amount_krw=args.max_buy_amount,
        )
        print_ranked_candidates(result)
        buy_signal_count = (
            int(result['buy_signal'].fillna(False).sum())
            if 'buy_signal' in result.columns else 0
        )
        logger.info(
            'Correlation analysis completed: candidates=%d buy_signals=%d',
            len(result), buy_signal_count,
        )
        if result.empty:
            logger.warning('Analysis produced no ranked candidates')
        elif buy_signal_count == 0:
            logger.info('Analysis produced no buy targets')
        chart_path = args.chart or str(
            Path(__file__).with_name('results')
            / f'daytrading_top10_correlation_{args.as_of_date.replace("-", "")}.png'
        )
        create_top10_chart(result, chart_path, args.as_of_date)
        print(f'5/5 TOP 10 상관 그래프 저장: {Path(chart_path).resolve()}')
        logger.info('Correlation chart saved: %s', Path(chart_path).resolve())

        if args.output:
            output_path = Path(args.output)
            result.to_csv(output_path, index=False, date_format='%Y-%m-%d')
            print(f'신호 결과 저장: {output_path.resolve()}')
        elif result.empty:
            print(
                '조건을 충족하는 상관 후보가 없거나, 직전 평일 지표 세션이 '
                '연결되지 않았습니다.'
            )
        else:
            print(result.to_string(index=False))

        if not args.no_db:
            save_analysis_results(connection, args.as_of_date, result)
            print(
                f'{args.as_of_date} 분석 결과 {len(result)}건을 DB에 저장했습니다.'
            )
            logger.info(
                'Analysis results saved to DB: analysis_date=%s rows=%d',
                args.as_of_date, len(result),
            )
        if live_mode:
            order_count = place_live_buy_orders(
                connection, result, args, logger, now=datetime.now()
            )
            print(f'CREON 매수 주문 접수 건수: {order_count}')
            logger.info('DayTrading live orders submitted: count=%d', order_count)
        else:
            print('실주문은 전송하지 않았습니다.')
            logger.info('DayTrading run completed; no live orders were sent')
    finally:
        connection.close()
    if live_mode:
        run_after_market_monitor(args.as_of_date, logger)
    return 0


def main():
    logger = configure_logging()
    try:
        return _run_analysis(logger)
    except Exception:
        logger.exception('DayTrading run failed')
        raise


if __name__ == '__main__':
    raise SystemExit(main())