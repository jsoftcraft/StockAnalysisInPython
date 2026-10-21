
import pandas as pd
# from bs4 import BeautifulSoup
import pymysql, time
# import requests
from datetime import datetime
from typing import Optional


class CREON:
    def __init__(self):
        try:
            import win32com.client
        except ImportError as exc:
            raise RuntimeError(
                "CREON 수집에는 pywin32가 설치된 32비트 Python이 필요합니다."
            ) from exc

        self.win32com = win32com.client
        self.objCpCybos = self.win32com.Dispatch("CpUtil.CpCybos")
        if self.objCpCybos.IsConnect == 0:
            raise RuntimeError("CREON PLUS에 로그인한 뒤 다시 실행하세요.")
        self.objStockChart = self.win32com.Dispatch("CpSysDib.StockChart")
        self.objCodeMgr = self.win32com.Dispatch("CpUtil.CpCodeMgr")
        self.objMarketEye = self.win32com.Dispatch("CpSysDib.MarketEye")
        self._last_request_at = 0.0

    def _wait_for_request(self, force=False):
        request_interval = 0.27
        while True:
            remain_time = max(
                0.0, self.objCpCybos.LimitRequestRemainTime / 1000.0
            )
            remain_count = self.objCpCybos.GetLimitRemainCount(1)
            interval_wait = max(
                0.0,
                request_interval - (time.monotonic() - self._last_request_at),
            )
            if force:
                time.sleep(max(remain_time, 1.0))
                force = False
                continue
            if remain_count > 0 and interval_wait == 0:
                return
            time.sleep(max(remain_time, 0.25) if remain_count <= 0 else interval_wait)

    def get_market_cap_codes(self, market, limit: Optional[int] = 100):
        market_codes = self.objCodeMgr.GetStockListByMarket(market)
        market_caps = {}
        fields = [0, 4, 20, 67, 71, 72]

        for start_index in range(0, len(market_codes), 200):
            code_batch = market_codes[start_index:start_index + 200]
            self.objMarketEye.SetInputValue(0, fields)
            self.objMarketEye.SetInputValue(1, code_batch)
            self.objMarketEye.BlockRequest()
            status = self.objMarketEye.GetDibStatus()
            if status != 0:
                raise RuntimeError(
                    "CREON 시가총액 조회 실패: "
                    f"{self.objMarketEye.GetDibMsg1()}"
                )

            count = self.objMarketEye.GetHeaderValue(2)
            for index in range(count):
                code = self.objMarketEye.GetDataValue(0, index)
                price = self.objMarketEye.GetDataValue(1, index)
                listed_shares = self.objMarketEye.GetDataValue(2, index)
                if self.objCodeMgr.IsBigListingStock(code):
                    listed_shares *= 1000
                market_caps[code] = price * listed_shares

        ranked_codes = [
            code
            for code, _ in sorted(
                market_caps.items(), key=lambda item: item[1], reverse=True
            )[:limit]
        ]
        return ranked_codes

    def get_all_market_codes(self, markets=(1, 2)):
        market_codes = []
        for market in markets:
            market_codes.extend(self.objCodeMgr.GetStockListByMarket(market))
        return list(dict.fromkeys(market_codes))

    def get_stock_price_min(self, code, date_yyyymmdd):
        chart = self.objStockChart
        chart.SetInputValue(0, code)
        chart.SetInputValue(1, ord("1"))
        chart.SetInputValue(2, date_yyyymmdd)
        chart.SetInputValue(3, date_yyyymmdd)
        chart.SetInputValue(5, [0, 1, 2, 3, 4, 5, 8])
        chart.SetInputValue(6, ord("m"))
        chart.SetInputValue(7, 1)
        chart.SetInputValue(9, ord("1"))

        for attempt in range(3):
            self._wait_for_request()
            chart.BlockRequest()
            self._last_request_at = time.monotonic()
            status = chart.GetDibStatus()
            if status == 0:
                break
            if status == 4 and attempt < 2:
                self._wait_for_request(force=True)
                continue
            raise RuntimeError(
                f"{code} 분봉 조회 실패 (상태 {status}): "
                f"{chart.GetDibMsg1()}"
            )

        columns = ["date", "time", "open", "high", "low", "close", "volume"]
        count = int(chart.GetHeaderValue(3))
        rows = [
            [chart.GetDataValue(field, index) for field in range(len(columns))]
            for index in range(count)
        ]
        return pd.DataFrame(rows, columns=columns)

    def get_stock_price_min_by_count(self, code, requested_count, max_requests=None):
        if requested_count <= 0:
            raise ValueError("분봉 요청 개수는 1 이상이어야 합니다.")

        chart = self.objStockChart
        chart.SetInputValue(0, code)
        chart.SetInputValue(1, ord("2"))
        chart.SetInputValue(4, requested_count)
        chart.SetInputValue(5, [0, 1, 2, 3, 4, 5, 8])
        chart.SetInputValue(6, ord("m"))
        chart.SetInputValue(7, 1)
        chart.SetInputValue(9, ord("1"))

        columns = ["date", "time", "open", "high", "low", "close", "volume"]
        rows = []
        received_count = 0
        request_count = 0
        has_more = True
        while (
            received_count < requested_count
            and has_more
            and (max_requests is None or request_count < max_requests)
        ):
            page_received = False
            for attempt in range(3):
                if max_requests is not None and request_count >= max_requests:
                    break
                self._wait_for_request()
                chart.BlockRequest()
                request_count += 1
                self._last_request_at = time.monotonic()
                status = chart.GetDibStatus()
                if status == 0:
                    page_received = True
                    break
                if status == 4 and attempt < 2:
                    self._wait_for_request(force=True)
                    continue
                raise RuntimeError(
                    f"{code} 분봉 조회 실패 (상태 {status}): "
                    f"{chart.GetDibMsg1()}"
                )
            if not page_received:
                break

            page_count = int(chart.GetHeaderValue(3))
            if page_count <= 0:
                has_more = False
                break
            rows.extend(
                [chart.GetDataValue(field, index) for field in range(len(columns))]
                for index in range(page_count)
            )
            received_count += page_count
            has_more = bool(chart.Continue)

        complete = received_count >= requested_count or not has_more
        return pd.DataFrame(rows, columns=columns), request_count, complete

    def get_stock_price_day(self, code, start_date, end_date):
        chart = self.objStockChart
        chart.SetInputValue(0, code)
        chart.SetInputValue(1, ord("1"))
        chart.SetInputValue(2, int(end_date.replace("-", "")))
        chart.SetInputValue(3, int(start_date.replace("-", "")))
        chart.SetInputValue(5, [0, 2, 3, 4, 5, 8])
        chart.SetInputValue(6, ord("D"))
        chart.SetInputValue(7, 1)
        chart.SetInputValue(9, ord("1"))

        for attempt in range(3):
            self._wait_for_request()
            result = chart.BlockRequest()
            self._last_request_at = time.monotonic()
            status = chart.GetDibStatus()
            if result == 0 and status == 0:
                break
            if (result == 4 or status == 4) and attempt < 2:
                self._wait_for_request(force=True)
                continue
            raise RuntimeError(
                f"{code} 일봉 조회 실패 (ret={result}, 상태 {status}): "
                f"{chart.GetDibMsg1()}"
            )

        count = int(chart.GetHeaderValue(3))
        columns = ["date", "open", "high", "low", "close", "volume"]
        rows = [
            [chart.GetDataValue(field, index) for field in range(len(columns))]
            for index in range(count)
        ]
        return pd.DataFrame(rows, columns=columns)


def insert_missing_daily_prices(connection, df, code):
    """Insert missing bars without changing existing prices or committing."""
    values = [
        (
            code, row.date, int(row.open), int(row.high), int(row.low),
            int(row.close), int(row.diff), int(row.volume),
        )
        for row in df.itertuples(index=False)
    ]
    if not values:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(
            'INSERT INTO daily_price '
            '(code, date, open, high, low, close, diff, volume) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) '
            'ON DUPLICATE KEY UPDATE code = VALUES(code)',
            values,
        )
        return cursor.rowcount


class DBUpdater:  
    def __init__(self):
        """생성자: MariaDB 연결 및 종목코드 딕셔너리 생성"""
        self.conn = pymysql.connect(host='localhost', user='root',
            password='root', database='zunDB', charset='utf8')
        self._closed = False
        
        with self.conn.cursor() as curs:
            sql = """
            CREATE TABLE IF NOT EXISTS company_info (
                code VARCHAR(20),
                company VARCHAR(40),
                last_update DATE,
                PRIMARY KEY (code))
            """
            curs.execute(sql)
            sql = """
            CREATE TABLE IF NOT EXISTS daily_price (
                code VARCHAR(20),
                date DATE,
                open BIGINT(20),
                high BIGINT(20),
                low BIGINT(20),
                close BIGINT(20),
                diff BIGINT(20),
                volume BIGINT(20),
                PRIMARY KEY (code, date))
            """
            curs.execute(sql)
            sql = """
            CREATE TABLE IF NOT EXISTS minute_price (
                code VARCHAR(20),
                date DATE,
                trade_time TIME,
                open BIGINT(20),
                high BIGINT(20),
                low BIGINT(20),
                close BIGINT(20),
                volume BIGINT(20),
                PRIMARY KEY (code, date, trade_time))
            """
            curs.execute(sql)
            sql = """
            CREATE TABLE IF NOT EXISTS selected_stock (
                market VARCHAR(10) NOT NULL,
                selection_rank SMALLINT NOT NULL,
                code VARCHAR(20) NOT NULL,
                company VARCHAR(40),
                market_cap_rank INT NOT NULL,
                return_rank INT NOT NULL,
                return_3m DECIMAL(12, 8) NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (market, selection_rank),
                UNIQUE KEY (market, code))
            """
            curs.execute(sql)
            curs.execute("SHOW COLUMNS FROM selected_stock LIKE 'return_1y'")
            old_return_column = curs.fetchone()
            curs.execute("SHOW COLUMNS FROM selected_stock LIKE 'return_3m'")
            new_return_column = curs.fetchone()
            if old_return_column and not new_return_column:
                curs.execute(
                    "ALTER TABLE selected_stock "
                    "CHANGE COLUMN return_1y return_3m DECIMAL(12, 8) NOT NULL"
                )
            sql = """
            CREATE TABLE IF NOT EXISTS selected_stock_history (
                snapshot_date DATE NOT NULL,
                market VARCHAR(10) NOT NULL,
                selection_rank SMALLINT NOT NULL,
                code VARCHAR(20) NOT NULL,
                company VARCHAR(40),
                market_cap_rank INT NOT NULL,
                return_rank INT NOT NULL,
                return_3m DECIMAL(12, 8) NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (market, snapshot_date, selection_rank),
                UNIQUE KEY (market, snapshot_date, code)
            )
            """
            curs.execute(sql)
            curs.execute(
                "INSERT IGNORE INTO selected_stock_history "
                "(snapshot_date, market, selection_rank, code, company, "
                "market_cap_rank, return_rank, return_3m, updated_at) "
                "SELECT DATE(updated_at), market, selection_rank, code, company, "
                "market_cap_rank, return_rank, return_3m, updated_at "
                "FROM selected_stock"
            )
        self.conn.commit()
        self.codes = dict()
        self.creon = None
        self._history_loaded = False

    def get_selected_stocks(self, market=None):
        """저장된 최신 종목 순위를 조회"""
        sql = "SELECT * FROM selected_stock"
        if market is not None:
            return pd.read_sql(
                f"{sql} WHERE market = %s ORDER BY selection_rank",
                self.conn,
                params=(market,),
            )
        return pd.read_sql(
            f"{sql} ORDER BY market, selection_rank", self.conn
        )

    def close(self):
        """Close the MariaDB connection once."""
        if getattr(self, '_closed', False):
            return
        self._closed = True
        if getattr(self.conn, 'open', True):
            self.conn.close()

    def __del__(self):
        """소멸 시 남아 있는 MariaDB 연결을 안전하게 해제"""
        try:
            self.close()
        except Exception:
            pass
     
    def read_krx_code(self):
        """KRX로부터 상장기업 목록 파일을 읽어와서 데이터프레임으로 반환"""
        url = 'http://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13'
        krx = pd.read_html(url, header=0)[0]
        krx = krx[['종목코드', '회사명']]
        krx = krx.rename(columns={'종목코드': 'code', '회사명': 'company'})
        krx['code'] = (
            krx['code']
            .astype(str)
            .str.strip()
            .str.replace(r'\.0$', '', regex=True)
            .str.zfill(6)
        )
        return krx
    
    def update_comp_info(self):
        """종목코드를 company_info 테이블에 업데이트 한 후 딕셔너리에 저장"""
        sql = "SELECT * FROM company_info"
        df = pd.read_sql(sql, self.conn)
        for idx in range(len(df)):
            self.codes[df['code'].values[idx]] = df['company'].values[idx]
                    
        with self.conn.cursor() as curs:
            sql = "SELECT max(last_update) FROM company_info"
            curs.execute(sql)
            rs = curs.fetchone()
            today = datetime.today().strftime('%Y-%m-%d')
            if rs[0] == None or rs[0].strftime('%Y-%m-%d') < today:
                krx = self.read_krx_code()
                for idx in range(len(krx)):
                    code = krx.code.values[idx]
                    company = krx.company.values[idx]                
                    sql = f"REPLACE INTO company_info (code, company, last"\
                        f"_update) VALUES ('{code}', '{company}', '{today}')"
                    curs.execute(sql)
                    self.codes[code] = company
                    tmnow = datetime.now().strftime('%Y-%m-%d %H:%M')
                    print(f"[{tmnow}] #{idx+1:04d} REPLACE INTO company_info "\
                        f"VALUES ({code}, {company}, {today})")
                self.conn.commit()
                print('')              

    # 이전 웹 스크래핑 수집 방식 보존
    # def read_naver(self, code, company, pages_to_fetch):
    #     """네이버에서 주식 시세를 읽어서 데이터프레임으로 반환"""
    #     try:
    #         url = f"http://finance.naver.com/item/sise_day.nhn?code={code}"
    #         html = BeautifulSoup(requests.get(url,
    #             headers={'User-agent': 'Mozilla/5.0'}).text, "lxml")
    #         pgrr = html.find("td", class_="pgRR")
    #         if pgrr is None:
    #             return None
    #         s = str(pgrr.a["href"]).split('=')
    #         lastpage = s[-1]
    #         df = pd.DataFrame()
    #         pages = min(int(lastpage), pages_to_fetch)
    #         for page in range(1, pages + 1):
    #             pg_url = '{}&page={}'.format(url, page)
    #             df = df.append(pd.read_html(requests.get(pg_url,
    #                 headers={'User-agent': 'Mozilla/5.0'}).text)[0])
    #         df = df.rename(columns={'날짜':'date','종가':'close','전일비':'diff'
    #             ,'시가':'open','고가':'high','저가':'low','거래량':'volume'})
    #         df['date'] = df['date'].replace('.', '-')
    #         df['diff'] = df['diff'].str.extract(r'(\d+)')
    #         df = df.dropna()
    #         df[['close', 'diff', 'open', 'high', 'low', 'volume']] = df[['close',
    #             'diff', 'open', 'high', 'low', 'volume']].astype(int)
    #         return df[['date', 'open', 'high', 'low', 'close', 'diff', 'volume']]
    #     except Exception as e:
    #         print('Exception occured :', str(e))
    #         return None

    def insert_missing_daily_prices(self, df, num, code, company):
        """DB에 아직 없는 일봉만 저장하고 기존 가격은 변경하지 않는다."""
        inserted_rows = insert_missing_daily_prices(self.conn, df, code)
        self.conn.commit()
        print(
            f'[{datetime.now():%Y-%m-%d %H:%M}] #{num + 1:04d} '
            f'{company} ({code}): {inserted_rows} missing rows inserted'
        )
        return inserted_rows

    def replace_minute_price(self, df, code, company):
        sql = """REPLACE INTO minute_price
                 (code, date, trade_time, open, high, low, close, volume)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""
        values = [
            (
                code,
                row.date,
                row.trade_time,
                int(row.open),
                int(row.high),
                int(row.low),
                int(row.close),
                int(row.volume),
            )
            for row in df.itertuples(index=False)
        ]
        with self.conn.cursor() as curs:
            curs.executemany(sql, values)
        self.conn.commit()
        print(f"{company} ({code}): {len(df)}분봉 저장 완료")

    # 이전 웹 스크래핑 업데이트 흐름 보존
    # def update_daily_price(self, pages_to_fetch):
    #     for idx, code in enumerate(self.codes):
    #         df = self.read_naver(code, self.codes[code], pages_to_fetch)
    #         if df is not None:
    #             self.replace_into_db(df, idx, code, self.codes[code])

    def update_daily_price(self, start_date, end_date, stock_codes=None):
        is_universe_update = stock_codes is not None
        if self.creon is None:
            self.creon = CREON()

        today = pd.Timestamp.today().normalize()
        end_timestamp = min(pd.Timestamp(end_date).normalize(), today)
        start_timestamp = pd.Timestamp(start_date).normalize()
        if start_timestamp > end_timestamp:
            raise ValueError(
                f"일봉 시작일 {start_date}가 종료일 "
                f"{end_timestamp:%Y-%m-%d}보다 늦습니다."
            )
        end_date = end_timestamp.strftime("%Y-%m-%d")

        if stock_codes is None:
            market_codes = self.creon.get_all_market_codes()
            candidates = pd.DataFrame(
                [
                    {
                        "code": code,
                        "company": self.creon.objCodeMgr.CodeToName(code),
                    }
                    for code in market_codes
                ]
            )
        else:
            candidates = pd.DataFrame(
                stock_codes, columns=["code", "company"]
            )
        if candidates.empty:
            raise RuntimeError("일봉 수집 대상 종목이 없습니다.")

        stock_codes = []
        company_names = {}
        for candidate in candidates.itertuples(index=False):
            db_code = str(candidate.code).strip()
            creon_code = (
                db_code
                if db_code.startswith(("A", "Q"))
                else f"A{db_code.zfill(6)}" if db_code.isdigit()
                else f"A{db_code}"
            )
            if creon_code in stock_codes:
                continue
            stock_codes.append(creon_code)
            normalized_code = (
                creon_code[1:] if creon_code.startswith("A") else creon_code
            )
            company_names[normalized_code] = candidate.company or normalized_code
        if not stock_codes:
            raise RuntimeError("일봉 수집 대상 종목이 없습니다.")
        if is_universe_update:
            reference_prices = self.creon.get_stock_price_day(
                "A005930", start_date, end_date
            )
            if reference_prices.empty:
                print(
                    "요청 기간에 KRX 거래일이 없어 전체 종목 수집을 생략합니다.",
                    flush=True,
                )
                return
        print(
            f"일봉 수집 대상 {len(stock_codes)}종목의 데이터를 조회합니다.",
            flush=True,
        )

        stored_stocks = 0
        stored_rows = 0
        failed_requests = 0
        started_at = time.monotonic()
        print(
            f"일봉 수집 시작: {start_date}~{end_date}, 종목 {len(stock_codes)}개",
            flush=True,
        )

        for stock_index, creon_code in enumerate(stock_codes, start=1):
            db_code = creon_code[1:] if creon_code.startswith("A") else creon_code
            company = company_names.get(db_code, db_code)
            print(
                f"\r종목 {stock_index}/{len(stock_codes)} "
                f"{company} ({db_code}) 일봉 조회 중",
                end="",
                flush=True,
            )
            chunk_start = start_timestamp
            stock_inserted_rows = 0
            while chunk_start <= end_timestamp:
                chunk_end = min(
                    chunk_start + pd.Timedelta(days=179), end_timestamp
                )
                # Include prior bars for diff calculation, but only store this chunk.
                api_start = (
                    chunk_start - pd.Timedelta(days=10)
                ).strftime("%Y-%m-%d")
                try:
                    df = self.creon.get_stock_price_day(
                        creon_code,
                        api_start,
                        chunk_end.strftime("%Y-%m-%d"),
                    )
                except Exception as exc:
                    failed_requests += 1
                    print(
                        f"\n{company} ({db_code}) "
                        f"{chunk_start:%Y-%m-%d}~{chunk_end:%Y-%m-%d} "
                        f"조회 실패: {exc}",
                        flush=True,
                    )
                    chunk_start = chunk_end + pd.Timedelta(days=1)
                    continue

                if df.empty:
                    failed_requests += 1
                    print(
                        f"\n{company} ({db_code}) "
                        f"{chunk_start:%Y-%m-%d}~{chunk_end:%Y-%m-%d} "
                        "일봉 응답이 비어 있습니다.",
                        flush=True,
                    )
                    chunk_start = chunk_end + pd.Timedelta(days=1)
                    continue

                df["date"] = df["date"].map(
                    lambda value: str(value).zfill(8)
                )
                df.sort_values("date", inplace=True)
                df["diff"] = df["close"].diff().abs().fillna(0).astype(int)
                chunk_start_value = chunk_start.strftime("%Y%m%d")
                chunk_end_value = chunk_end.strftime("%Y%m%d")
                df = df[
                    df["date"].between(chunk_start_value, chunk_end_value)
                ].copy()
                if df.empty:
                    failed_requests += 1
                    print(
                        f"\n{company} ({db_code}) "
                        f"{chunk_start:%Y-%m-%d}~{chunk_end:%Y-%m-%d} "
                        "요청 구간에 저장할 일봉이 없습니다.",
                        flush=True,
                    )
                    chunk_start = chunk_end + pd.Timedelta(days=1)
                    continue

                df["date"] = pd.to_datetime(
                    df["date"], format="%Y%m%d"
                ).dt.strftime("%Y-%m-%d")
                df = df[[
                    "date", "open", "high", "low", "close", "diff", "volume"
                ]]
                inserted_rows = self.insert_missing_daily_prices(
                    df, stock_index - 1, db_code, company
                )
                if inserted_rows:
                    stock_inserted_rows += inserted_rows
                    stored_rows += inserted_rows
                chunk_start = chunk_end + pd.Timedelta(days=1)
            if stock_inserted_rows:
                stored_stocks += 1

        elapsed = time.monotonic() - started_at
        print(
            f"\n일봉 수집 완료: {stored_stocks}/{len(stock_codes)}종목, "
            f"{stored_rows}개 신규 데이터, 조회 실패 {failed_requests}건, "
            f"경과 {elapsed:.0f}초",
            flush=True,
        )

    def update_minute_price(self, start_date, end_date):
        if self.creon is None:
            self.creon = CREON()

        print("CREON에서 KOSPI 시가총액 상위 종목을 조회합니다.", flush=True)
        stock_codes = [
            code
            for market in (1, 2)
            for code in self.creon.get_market_cap_codes(market, limit=100)
        ]
        if not stock_codes:
            raise RuntimeError("CREON에서 KOSPI 종목 목록을 가져오지 못했습니다.")

        request_dates = list(pd.date_range(start=start_date, end=end_date, freq="B"))
        total_dates = len(request_dates)
        collection_started = time.monotonic()
        print(
            f"분봉 수집 시작: {total_dates} 거래일, 종목 {len(stock_codes)}개/일",
            flush=True,
        )

        for date_index, request_date in enumerate(request_dates, start=1):
            date_value = int(request_date.strftime("%Y%m%d"))
            stored_stocks = 0
            stored_rows = 0
            failed_stocks = 0
            print(f"[{date_index}/{total_dates}] {date_value} 수집 시작", flush=True)

            for stock_index, creon_code in enumerate(stock_codes, start=1):
                db_code = creon_code[1:] if creon_code.startswith("A") else creon_code
                company = self.codes.get(db_code, creon_code)
                print(
                    f"\r[{date_index}/{total_dates}] {date_value} "
                    f"종목 {stock_index}/{len(stock_codes)} {company} ({db_code}) 조회 중",
                    end="",
                    flush=True,
                )
                try:
                    df = self.creon.get_stock_price_min(creon_code, date_value)
                except Exception as exc:
                    failed_stocks += 1
                    print(
                        f"\n{company} ({db_code}) {date_value} 조회 실패: {exc}",
                        flush=True,
                    )
                    continue

                if df.empty:
                    continue
                df = df[df["date"].astype(int) == date_value].copy()
                if df.empty:
                    continue

                df["date"] = pd.to_datetime(
                    df["date"].astype(str), format="%Y%m%d"
                ).dt.strftime("%Y-%m-%d")
                minute_values = df["time"].astype(int).map(lambda value: f"{value:04d}")
                df["trade_time"] = (
                    minute_values.str[:2] + ":" + minute_values.str[2:] + ":00"
                )
                self.replace_minute_price(
                    df[["date", "trade_time", "open", "high", "low", "close", "volume"]],
                    db_code,
                    company,
                )
                stored_stocks += 1
                stored_rows += len(df)
                time.sleep(0.5)

            elapsed = time.monotonic() - collection_started
            print(
                f"[{date_index}/{total_dates}] {date_value} 완료: "
                f"{stored_stocks}/{len(stock_codes)}종목, {stored_rows}개 분봉, "
                f"조회 실패 {failed_stocks}건, 전체 경과 {elapsed:.0f}초",
                flush=True,
            )

    def execute_daily(self):
        """일봉 데이터를 한 번 수집해 daily_price 테이블을 업데이트"""
        today = datetime.today().strftime('%Y-%m-%d')
        start_date = '2024-01-01'
        end_date = today
        self.update_daily_price(start_date, end_date)

if __name__ == '__main__':
    dbu = DBUpdater()
    dbu.execute_daily()
