"""Tests for spread builder logic (pure logic, no IB connection)."""

import pytest

from src.models import OptionLeg, Signal, SpreadCandidate, SpreadType


class TestSpreadCandidate:
    def test_risk_reward_ratio(self, sample_candidate):
        ratio = sample_candidate.risk_reward_ratio
        expected = 290.0 / 210.0
        assert ratio == pytest.approx(expected, rel=1e-3)

    def test_risk_reward_zero_loss(self):
        candidate = SpreadCandidate(
            symbol="SPY",
            spread_type=SpreadType.BULL_CALL,
            long_leg=OptionLeg("SPY", "20260401", 450, "C", "BUY"),
            short_leg=OptionLeg("SPY", "20260401", 455, "C", "SELL"),
            max_profit=100.0,
            max_loss=0.0,
            net_debit=0.0,
            dte=30,
            signal=Signal.BULLISH,
        )
        assert candidate.risk_reward_ratio == 0.0


class TestOptionLeg:
    def test_mid_price(self):
        leg = OptionLeg(
            symbol="SPY",
            expiry="20260401",
            strike=450.0,
            right="C",
            action="BUY",
            bid=5.00,
            ask=5.20,
        )
        assert leg.mid == pytest.approx(5.10)

    def test_mid_price_none(self):
        leg = OptionLeg(
            symbol="SPY",
            expiry="20260401",
            strike=450.0,
            right="C",
            action="BUY",
        )
        assert leg.mid is None


class TestSpreadStructure:
    def test_bull_call_long_lower_strike(self, sample_candidate):
        assert sample_candidate.long_leg.strike < sample_candidate.short_leg.strike

    def test_bull_call_uses_calls(self, sample_candidate):
        assert sample_candidate.long_leg.right == "C"
        assert sample_candidate.short_leg.right == "C"

    def test_bear_put_structure(self):
        long_leg = OptionLeg("SPY", "20260401", 455, "P", "BUY", delta=-0.40)
        short_leg = OptionLeg("SPY", "20260401", 450, "P", "SELL", delta=-0.25)
        candidate = SpreadCandidate(
            symbol="SPY",
            spread_type=SpreadType.BEAR_PUT,
            long_leg=long_leg,
            short_leg=short_leg,
            max_profit=290.0,
            max_loss=210.0,
            net_debit=2.10,
            dte=30,
            signal=Signal.BEARISH,
        )
        # Bear put: long higher strike, sell lower strike
        assert candidate.long_leg.strike > candidate.short_leg.strike
        assert candidate.long_leg.right == "P"
