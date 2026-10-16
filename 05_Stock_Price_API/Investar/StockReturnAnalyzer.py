"""Rank stocks by market capitalization and three-month return."""

import argparse
import importlib.util
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pymysql


MARKETS = {"KOSPI": 1, "KOSDAQ": 2}


def create_creon_client():
    updater_path = Path(__file__).with_name("DBUpdaterEx.py")
    spec = importlib.util.spec_from_file_location(
        "stock_db_updater_for_return_analysis", updater_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"CREON 수집 모듈을 불러올 수 없습니다: {updater_path}")
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    return updater.CREON()


def save_analysis_results_to_db(result_rows, snapshot_date):
    connection = pymysql.connect(
        host="localhost",
        user="root",
        password="root",
        database="zunDB",
        charset="utf8",
        autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS selected_stock (
                    market VARCHAR(10) NOT NULL,
                    selection_rank SMALLINT NOT NULL,
                    code VARCHAR(20) NOT NULL,
                    company VARCHAR(40),
                    market_cap_rank INT NOT NULL,
                    return_rank INT NOT NULL,
                    return_3m DECIMAL(12, 8) NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (market, selection_rank),
                    UNIQUE KEY (market, code)
                )"""
            )
            cursor.execute("SHOW COLUMNS FROM selected_stock LIKE 'return_1y'")
            old_return_column = cursor.fetchone()
            cursor.execute("SHOW COLUMNS FROM selected_stock LIKE 'return_3m'")
            new_return_column = cursor.fetchone()
            if old_return_column and not new_return_column:
                cursor.execute(
                    "ALTER TABLE selected_stock "
                    "CHANGE COLUMN return_1y return_3m DECIMAL(12, 8) NOT NULL"
                )
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS selected_stock_history (
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
                )"""
            )

        updated_at = datetime.now()
        markets = sorted({row["market"] for row in result_rows})
        for market in markets:
            rows = [row for row in result_rows if row["market"] == market]
            current_values = []
            history_values = []
            for row in rows:
                code = str(row["code"])
                db_code = code[1:] if code.startswith("A") else code
                company = db_code
                values = (
                    market,
                    row["selection_rank"],
                    db_code,
                    company,
                    row["market_cap_rank"],
                    row["return_rank"],
                    row["return_3m"],
                    updated_at,
                )
                current_values.append(values)
                history_values.append((snapshot_date, *values))

            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM selected_stock WHERE market = %s",
                        (market,),
                    )
                    cursor.executemany(
                        """INSERT INTO selected_stock
                        (market, selection_rank, code, company, market_cap_rank,
                         return_rank, return_3m, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        current_values,
                    )
                    cursor.execute(
                        "DELETE FROM selected_stock_history "
                        "WHERE market = %s AND snapshot_date = %s",
                        (market, snapshot_date),
                    )
                    cursor.executemany(
                        """INSERT INTO selected_stock_history
                        (snapshot_date, market, selection_rank, code, company,
                         market_cap_rank, return_rank, return_3m, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        history_values,
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    finally:
        connection.close()


def get_combined_rank_codes(creon, market, end_date, limit=100):
    """Return candidates ranked by capitalization and three-month return.

    ``creon`` supplies market-cap-ranked codes and daily price history. Keeping
    this calculation outside DBUpdaterEx leaves that module responsible for
    collection and persistence.
    """
    market_codes = creon.get_market_cap_codes(market, limit=None)
    if not market_codes:
        return []

    capitalization_ranks = {
        code: rank for rank, code in enumerate(market_codes, start=1)
    }
    cutoff = pd.Timestamp(end_date) - pd.DateOffset(months=3)
    history_start = (cutoff - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    cutoff_value = cutoff.strftime("%Y%m%d")
    end_value = pd.Timestamp(end_date).strftime("%Y%m%d")
    returns = {}
    failed = 0

    for index, code in enumerate(market_codes, start=1):
        print(
            f"\r3개월 수익률 조회 {index}/{len(market_codes)}",
            end="",
            flush=True,
        )
        try:
            prices = creon.get_stock_price_day(code, history_start, end_date)
        except Exception as exc:
            failed += 1
            print(f"\n{code} 수익률 조회 실패: {exc}", flush=True)
            continue

        if prices.empty:
            continue
        prices["date"] = prices["date"].map(
            lambda value: str(value).zfill(8)
        )
        prices.sort_values("date", inplace=True)
        base_prices = prices.loc[prices["date"] <= cutoff_value, "close"]
        latest_prices = prices.loc[prices["date"] <= end_value, "close"]
        if base_prices.empty or latest_prices.empty:
            continue

        base_close = float(base_prices.iloc[-1])
        if base_close <= 0:
            continue
        returns[code] = float(latest_prices.iloc[-1]) / base_close - 1

    print(
        f"\n수익률 조회 완료: {len(returns)}/{len(market_codes)}종목, "
        f"실패 {failed}건",
        flush=True,
    )
    return_ranks = {
        code: rank
        for rank, code in enumerate(
            sorted(returns, key=lambda item: (-returns[item], item)),
            start=1,
        )
    }
    ranked_codes = sorted(
        returns,
        key=lambda code: (
            capitalization_ranks[code] + return_ranks[code],
            capitalization_ranks[code],
            -returns[code],
        ),
    )
    return [
        {
            "code": code,
            "market_cap_rank": capitalization_ranks[code],
            "return_rank": return_ranks[code],
            "return_3m": returns[code],
        }
        for code in ranked_codes[:limit]
    ]


def parse_args():
    parser = argparse.ArgumentParser(
        description="CREON 시가총액 순위와 3개월 수익률을 결합해 종목을 분석합니다."
    )
    parser.add_argument(
        "--market",
        choices=tuple(MARKETS),
        nargs="+",
        default=list(MARKETS),
        help="분석 시장 (기본: KOSPI KOSDAQ)",
    )
    parser.add_argument(
        "--end-date",
        default=date.today().isoformat(),
        help="수익률 기준일 YYYY-MM-DD (기본: 오늘)",
    )
    parser.add_argument(
        "--limit", type=int, default=100, help="시장별 결과 종목 수"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV 저장 경로 (기본: results/stock_return_rank_기준일.csv)",
    )
    parser.add_argument(
        "--no-db-save",
        action="store_true",
        help="순위는 분석하지만 selected_stock과 이력에는 저장하지 않음",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.limit < 1:
        raise ValueError("--limit은 1 이상이어야 합니다.")
    end_date = pd.Timestamp(args.end_date).strftime("%Y-%m-%d")
    creon = create_creon_client()
    result_rows = []

    for market_name in args.market:
        print(f"\n{market_name} 3개월 수익률 분석 시작: {end_date}", flush=True)
        ranked = get_combined_rank_codes(
            creon, MARKETS[market_name], end_date, args.limit
        )
        for selection_rank, row in enumerate(ranked, start=1):
            result_rows.append({
                "market": market_name,
                "selection_rank": selection_rank,
                **row,
            })
    if not result_rows:
        raise RuntimeError("분석 결과가 없습니다. CREON 응답과 기준일을 확인하세요.")

    if not args.no_db_save:
        save_analysis_results_to_db(result_rows, end_date)

    results = pd.DataFrame(result_rows)
    results["return_3m_pct"] = results["return_3m"] * 100
    print("\n분석 결과")
    print(results.to_string(index=False, formatters={
        "return_3m_pct": "{:+.2f}".format,
    }))

    output_path = args.output or (
        Path(__file__).resolve().parent
        / "results"
        / f"stock_return_rank_{pd.Timestamp(end_date):%Y%m%d}.csv"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"\nCSV 저장 완료: {output_path}")


if __name__ == "__main__":
    main()
