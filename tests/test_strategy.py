"""Tests for strategy signal logic (unit tests, no IB connection)."""

import pytest

from src.indicators import _determine_signal
from src.models import Signal


class TestDetermineSignal:
    """Test the signal determination logic directly."""

    def test_bullish_momentum(self, indicators_config):
        signal = _determine_signal(
            price=100.0,
            rsi=25.0,           # Below oversold (30)
            sma_fast=99.0,      # price > sma_fast
            sma_slow=98.0,      # sma_fast > sma_slow
            bb_upper=110.0,
            bb_lower=90.0,
            config=indicators_config,
        )
        assert signal == Signal.BULLISH

    def test_bearish_momentum(self, indicators_config):
        signal = _determine_signal(
            price=100.0,
            rsi=75.0,           # Above overbought (70)
            sma_fast=101.0,     # price < sma_fast
            sma_slow=102.0,     # sma_fast < sma_slow
            bb_upper=110.0,
            bb_lower=90.0,
            config=indicators_config,
        )
        assert signal == Signal.BEARISH

    def test_neutral_no_signal(self, indicators_config):
        signal = _determine_signal(
            price=100.0,
            rsi=50.0,           # Mid-range
            sma_fast=99.0,
            sma_slow=98.0,
            bb_upper=110.0,
            bb_lower=90.0,
            config=indicators_config,
        )
        assert signal == Signal.NEUTRAL

    def test_bullish_bollinger_bounce(self, indicators_config):
        signal = _determine_signal(
            price=90.0,          # At lower BB
            rsi=50.0,
            sma_fast=95.0,
            sma_slow=96.0,
            bb_upper=110.0,
            bb_lower=90.0,       # price <= lower * 1.005
            config=indicators_config,
        )
        assert signal == Signal.BULLISH

    def test_bearish_bollinger_rejection(self, indicators_config):
        signal = _determine_signal(
            price=110.0,         # At upper BB
            rsi=50.0,
            sma_fast=105.0,
            sma_slow=104.0,
            bb_upper=110.0,      # price >= upper * 0.995
            bb_lower=90.0,
            config=indicators_config,
        )
        assert signal == Signal.BEARISH

    def test_bullish_takes_priority(self, indicators_config):
        """When both bullish and bearish conditions met, bullish wins (checked first)."""
        signal = _determine_signal(
            price=90.0,          # At lower BB (bullish)
            rsi=75.0,            # Overbought (could be bearish, but no SMA alignment)
            sma_fast=95.0,
            sma_slow=96.0,
            bb_upper=110.0,
            bb_lower=90.0,
            config=indicators_config,
        )
        assert signal == Signal.BULLISH
