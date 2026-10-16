"""Rank Korean stocks by correlation with completed overseas sessions."""

import pandas as pd
import numpy as np


DEFAULT_MAX_BUY_AMOUNT_KRW = 1_000_000
RESULT_COLUMNS = [
    'rank', 'code', 'market', 'company', 'indicator', 'correlation',
    'indicator_correlations',
    'observations', 'indicator_trade_date', 'indicator_return', 'buy_signal',
    'max_buy_amount_krw', 'reference_close', 'estimated_buy_quantity',
]


def _normalize_code(value):
    code = str(value).strip()
    return code.zfill(6) if code.isdigit() else code


def _normalize_prices(prices, label):
    if not isinstance(prices, pd.DataFrame):
        raise TypeError(f'{label} must be a pandas DataFrame')
    if prices.empty:
        return prices.copy()

    normalized = prices.copy()
    index = pd.DatetimeIndex(pd.to_datetime(normalized.index))
    if index.tz is not None:
        index = index.tz_localize(None)
    normalized.index = index.normalize()
    normalized = normalized[~normalized.index.duplicated(keep='last')]
    normalized = normalized.sort_index()
    normalized.columns = [str(column).strip() for column in normalized.columns]
    return normalized.apply(pd.to_numeric, errors='coerce')


def align_indicator_returns(korean_dates, indicator_returns):
    """Align only the immediately preceding weekday indicator session."""
    returns = _normalize_prices(indicator_returns, 'indicator_returns')
    dates = pd.DatetimeIndex(pd.to_datetime(korean_dates))
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    dates = dates.normalize()
    aligned = pd.DataFrame(index=dates, columns=returns.columns, dtype=float)
    if returns.empty or dates.empty:
        return aligned

    expected_dates = dates - pd.offsets.BDay(1)
    positions = returns.index.get_indexer(expected_dates)
    valid = positions >= 0
    if valid.any():
        aligned.iloc[valid] = returns.iloc[positions[valid]].to_numpy()
    return aligned


def _pairwise_correlations(stock_returns, indicator_returns):
    stock_values = stock_returns.to_numpy(dtype=float)
    indicator_values = indicator_returns.to_numpy(dtype=float)
    stock_valid = np.isfinite(stock_values)
    indicator_valid = np.isfinite(indicator_values)
    stock_values = np.where(stock_valid, stock_values, 0.0)
    indicator_values = np.where(indicator_valid, indicator_values, 0.0)
    stock_mask = stock_valid.astype(np.int32)
    indicator_mask = indicator_valid.astype(np.int32)

    observations = stock_mask.T @ indicator_mask
    stock_indicator_mask = indicator_valid.astype(float)
    indicator_stock_mask = stock_valid.astype(float)
    sum_stock = stock_values.T @ stock_indicator_mask
    sum_indicator = indicator_stock_mask.T @ indicator_values
    sum_stock_squared = (stock_values * stock_values).T @ stock_indicator_mask
    sum_indicator_squared = indicator_stock_mask.T @ (
        indicator_values * indicator_values
    )
    sum_products = stock_values.T @ indicator_values

    count = np.maximum(observations, 1)
    covariance = sum_products - sum_stock * sum_indicator / count
    stock_variance = np.maximum(
        sum_stock_squared - sum_stock * sum_stock / count, 0
    )
    indicator_variance = np.maximum(
        sum_indicator_squared - sum_indicator * sum_indicator / count, 0
    )
    denominator = np.sqrt(stock_variance * indicator_variance)
    correlations = np.divide(
        covariance,
        denominator,
        out=np.full_like(covariance, np.nan),
        where=denominator > 0,
    )
    return np.clip(correlations, -1.0, 1.0), observations


def rank_correlations(
    stock_closes,
    indicator_closes,
    as_of_date,
    stock_metadata=None,
    lookback=60,
    min_observations=40,
    top_n=10,
    buy_window_open=False,
    max_buy_amount_krw=DEFAULT_MAX_BUY_AMOUNT_KRW,
):
    """Rank absolute correlations and mark directional buy signals.

    Prices must be indexed by exchange-local session dates. ``as_of_date`` is
    the Korean trading date on which the signal would be evaluated before open.
    """
    if lookback < 1:
        raise ValueError('lookback must be at least 1')
    if min_observations < 2 or min_observations > lookback:
        raise ValueError('min_observations must be between 2 and lookback')
    if top_n < 1:
        raise ValueError('top_n must be at least 1')
    if max_buy_amount_krw < 1:
        raise ValueError('max_buy_amount_krw must be at least 1')

    stocks = _normalize_prices(stock_closes, 'stock_closes')
    indicators = _normalize_prices(indicator_closes, 'indicator_closes')
    if stocks.empty or indicators.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    stocks.columns = pd.Index(
        [_normalize_code(code) for code in stocks.columns]
    )
    if stocks.columns.duplicated().any():
        raise ValueError('stock codes are duplicated after normalization')

    signal_date = pd.Timestamp(as_of_date)
    if signal_date.tz is not None:
        signal_date = signal_date.tz_localize(None)
    signal_date = signal_date.normalize()
    expected_indicator_date = signal_date - pd.offsets.BDay(1)
    stock_returns = stocks.div(stocks.shift(1)).sub(1)
    indicator_returns = indicators.div(indicators.shift(1)).sub(1)
    korean_sessions = stock_returns.index[stock_returns.index < signal_date]
    korean_sessions = korean_sessions[-lookback:]
    if korean_sessions.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    if expected_indicator_date not in indicator_returns.index:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    latest_indicator_returns = indicator_returns.loc[expected_indicator_date]
    connected_indicators = latest_indicator_returns.dropna().index
    if len(connected_indicators) == 0:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    indicator_returns = indicator_returns.loc[:, connected_indicators]
    latest_indicator_returns = latest_indicator_returns.loc[connected_indicators]

    aligned_indicators = align_indicator_returns(
        korean_sessions, indicator_returns
    )
    latest_indicator_date = expected_indicator_date
    correlations, observations = _pairwise_correlations(
        stock_returns.loc[korean_sessions], aligned_indicators
    )

    metadata = {}
    if stock_metadata is not None and not stock_metadata.empty:
        metadata_frame = stock_metadata.copy()
        if 'code' in metadata_frame.columns:
            metadata_frame = metadata_frame.set_index('code')
        metadata_frame.index = metadata_frame.index.map(_normalize_code)
        metadata = metadata_frame[~metadata_frame.index.duplicated(
            keep='last'
        )].to_dict(orient='index')

    ranked = []
    indicator_names = list(aligned_indicators.columns)
    for stock_index, code in enumerate(stocks.columns):
        stock_correlations = correlations[stock_index]
        valid_indicators = np.flatnonzero(
            (np.abs(stock_correlations) > 0)
            & (observations[stock_index] >= min_observations)
        )
        if len(valid_indicators) == 0:
            continue

        highest_correlation_magnitude = np.max(
            np.abs(stock_correlations[valid_indicators])
        )
        tied_indicators = valid_indicators[
            np.abs(stock_correlations[valid_indicators])
            == highest_correlation_magnitude
        ]
        indicator_index = min(
            tied_indicators,
            key=lambda index: (
                -stock_correlations[index], indicator_names[index]
            ),
        )
        indicator = indicator_names[indicator_index]
        correlation = float(stock_correlations[indicator_index])
        observation_count = int(observations[stock_index, indicator_index])
        indicator_correlations = '; '.join(
            f'{indicator_names[index]}={stock_correlations[index]:+.4f}'
            for index in range(len(indicator_names))
            if (
                observations[stock_index, index] >= min_observations
                and np.isfinite(stock_correlations[index])
            )
        )
        info = metadata.get(code, {})
        indicator_return = latest_indicator_returns.get(indicator)
        if pd.isna(indicator_return):
            indicator_return = float('nan')
        previous_closes = stocks.loc[stocks.index < signal_date, code].dropna()
        reference_close = (
            int(previous_closes.iloc[-1]) if not previous_closes.empty else 0
        )
        buy_signal = bool(
            buy_window_open
            and correlation * indicator_return > 0
        )
        estimated_buy_quantity = (
            max_buy_amount_krw // reference_close
            if buy_signal and reference_close > 0 else 0
        )
        ranked.append({
            'code': code,
            'market': info.get('market', ''),
            'company': info.get('company', ''),
            'indicator': indicator,
            'correlation': correlation,
            'indicator_correlations': indicator_correlations,
            'observations': observation_count,
            'indicator_trade_date': latest_indicator_date,
            'indicator_return': float(indicator_return),
            'buy_signal': buy_signal,
            'max_buy_amount_krw': max_buy_amount_krw,
            'reference_close': reference_close,
            'estimated_buy_quantity': estimated_buy_quantity,
        })

    ranked.sort(key=lambda row: (-abs(row['correlation']), row['code']))
    ranked = ranked[:top_n]
    for rank, row in enumerate(ranked, start=1):
        row['rank'] = rank
    return pd.DataFrame(ranked, columns=RESULT_COLUMNS)