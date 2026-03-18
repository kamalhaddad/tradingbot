"""Technical indicators: RSI, SMA, Bollinger Bands. Pure pandas, no IB dependency."""

import pandas as pd

from src.config_loader import IndicatorsConfig
from src.models import IndicatorResult, Signal


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Compute Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_sma(series: pd.Series, period: int) -> pd.Series:
    """Compute Simple Moving Average."""
    return series.rolling(window=period).mean()


def compute_bollinger_bands(
    series: pd.Series, period: int = 20, std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Compute Bollinger Bands. Returns (upper, middle, lower)."""
    middle = series.rolling(window=period).mean()
    rolling_std = series.rolling(window=period).std()
    upper = middle + std * rolling_std
    lower = middle - std * rolling_std
    return upper, middle, lower


def generate_signal(
    df: pd.DataFrame, config: IndicatorsConfig
) -> IndicatorResult:
    """Generate trading signal from OHLCV DataFrame.

    Expects df with 'close' column indexed by date.
    """
    if len(df) < config.sma_slow:
        return IndicatorResult(
            symbol="",
            price=float(df["close"].iloc[-1]) if len(df) > 0 else 0.0,
        )

    close = df["close"]
    price = float(close.iloc[-1])

    rsi = compute_rsi(close, config.rsi_period)
    sma_fast = compute_sma(close, config.sma_fast)
    sma_slow = compute_sma(close, config.sma_slow)
    bb_upper, bb_middle, bb_lower = compute_bollinger_bands(
        close, config.bb_period, config.bb_std
    )

    current_rsi = float(rsi.iloc[-1])
    current_sma_fast = float(sma_fast.iloc[-1])
    current_sma_slow = float(sma_slow.iloc[-1])
    current_bb_upper = float(bb_upper.iloc[-1])
    current_bb_middle = float(bb_middle.iloc[-1])
    current_bb_lower = float(bb_lower.iloc[-1])

    signal = _determine_signal(
        price=price,
        rsi=current_rsi,
        sma_fast=current_sma_fast,
        sma_slow=current_sma_slow,
        bb_upper=current_bb_upper,
        bb_lower=current_bb_lower,
        config=config,
    )

    return IndicatorResult(
        symbol="",  # Caller sets this
        price=price,
        rsi=current_rsi,
        sma_fast=current_sma_fast,
        sma_slow=current_sma_slow,
        bb_upper=current_bb_upper,
        bb_middle=current_bb_middle,
        bb_lower=current_bb_lower,
        signal=signal,
    )


def _determine_signal(
    price: float,
    rsi: float,
    sma_fast: float,
    sma_slow: float,
    bb_upper: float,
    bb_lower: float,
    config: IndicatorsConfig,
) -> Signal:
    """Determine bullish/bearish/neutral signal from indicator values."""
    # Bullish: RSI oversold + price above rising SMAs, or bounce off lower BB
    bullish_momentum = (
        rsi < config.rsi_oversold
        and price > sma_fast > sma_slow
    )
    bullish_bb = price <= bb_lower * 1.005  # Within 0.5% of lower band

    # Bearish: RSI overbought + price below falling SMAs, or rejection at upper BB
    bearish_momentum = (
        rsi > config.rsi_overbought
        and price < sma_fast < sma_slow
    )
    bearish_bb = price >= bb_upper * 0.995  # Within 0.5% of upper band

    if bullish_momentum or bullish_bb:
        return Signal.BULLISH
    elif bearish_momentum or bearish_bb:
        return Signal.BEARISH
    else:
        return Signal.NEUTRAL
