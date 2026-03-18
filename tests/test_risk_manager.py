"""Tests for risk manager (pure logic, no IB connection)."""

import pytest
from datetime import datetime, timedelta

from src.models import (
    OptionLeg,
    OrderStatus,
    PortfolioSnapshot,
    Signal,
    SpreadCandidate,
    SpreadType,
    TradeRecord,
)
from src.risk_manager import RiskManager


class TestPositionSizing:
    def test_basic_sizing(self, risk_config, sample_candidate):
        rm = RiskManager(risk_config)
        # account=100k, risk=2%, max_loss=210 per contract
        # 100000 * 0.02 / 210 = 9.52 -> floor = 9
        contracts = rm.calculate_position_size(sample_candidate, 100000.0)
        assert contracts == 9

    def test_small_account(self, risk_config, sample_candidate):
        rm = RiskManager(risk_config)
        # account=5k, risk=2%, max_loss=210
        # 5000 * 0.02 / 210 = 0.47 -> floor = 0
        contracts = rm.calculate_position_size(sample_candidate, 5000.0)
        assert contracts == 0

    def test_zero_max_loss(self, risk_config):
        rm = RiskManager(risk_config)
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
        assert rm.calculate_position_size(candidate, 100000.0) == 0


class TestCanOpenPosition:
    def test_allowed(self, risk_config, sample_candidate, sample_snapshot):
        rm = RiskManager(risk_config)
        allowed, reason = rm.can_open_position(sample_candidate, sample_snapshot)
        assert allowed is True
        assert reason == "OK"

    def test_max_concurrent_positions(self, risk_config, sample_candidate):
        rm = RiskManager(risk_config)
        snapshot = PortfolioSnapshot(
            timestamp=datetime.now(),
            account_value=100000.0,
            unrealized_pnl=0.0,
            realized_pnl_today=0.0,
            realized_pnl_month=0.0,
            open_positions=10,  # At limit
            positions_by_symbol={},
        )
        allowed, reason = rm.can_open_position(sample_candidate, snapshot)
        assert allowed is False
        assert "concurrent" in reason.lower()

    def test_max_per_symbol(self, risk_config, sample_candidate):
        rm = RiskManager(risk_config)
        snapshot = PortfolioSnapshot(
            timestamp=datetime.now(),
            account_value=100000.0,
            unrealized_pnl=0.0,
            realized_pnl_today=0.0,
            realized_pnl_month=0.0,
            open_positions=5,
            positions_by_symbol={"SPY": 3},  # At symbol limit
        )
        allowed, reason = rm.can_open_position(sample_candidate, snapshot)
        assert allowed is False
        assert "SPY" in reason

    def test_daily_loss_limit(self, risk_config, sample_candidate):
        rm = RiskManager(risk_config)
        snapshot = PortfolioSnapshot(
            timestamp=datetime.now(),
            account_value=100000.0,
            unrealized_pnl=0.0,
            realized_pnl_today=-3500.0,  # 3.5% loss > 3% limit
            realized_pnl_month=-3500.0,
            open_positions=1,
            positions_by_symbol={},
        )
        allowed, reason = rm.can_open_position(sample_candidate, snapshot)
        assert allowed is False
        assert "daily" in reason.lower()

    def test_monthly_loss_limit(self, risk_config, sample_candidate):
        rm = RiskManager(risk_config)
        snapshot = PortfolioSnapshot(
            timestamp=datetime.now(),
            account_value=100000.0,
            unrealized_pnl=0.0,
            realized_pnl_today=0.0,
            realized_pnl_month=-9000.0,  # 9% loss > 8% limit
            open_positions=1,
            positions_by_symbol={},
        )
        allowed, reason = rm.can_open_position(sample_candidate, snapshot)
        assert allowed is False
        assert "monthly" in reason.lower()


class TestExitConditions:
    def test_profit_target(self, risk_config, sample_trade):
        rm = RiskManager(risk_config)
        # Entry=2.10, max_profit per spread=290
        # 50% profit target = 145 per spread
        # pnl = (entry - current) * 100 = (2.10 - 0.65) * 100 = 145
        should_exit, reason = rm.check_exit_conditions(sample_trade, 0.65)
        assert should_exit is True
        assert "profit" in reason.lower()

    def test_stop_loss(self, risk_config, sample_trade):
        rm = RiskManager(risk_config)
        # max_loss per spread = 210, stop_loss_pct = 1.0 (100%)
        # pnl = (2.10 - 4.20) * 100 = -210
        should_exit, reason = rm.check_exit_conditions(sample_trade, 4.20)
        assert should_exit is True
        assert "stop" in reason.lower()

    def test_hold_position(self, risk_config, sample_trade):
        rm = RiskManager(risk_config)
        # Small loss, not at target
        should_exit, reason = rm.check_exit_conditions(sample_trade, 2.00)
        assert should_exit is False
        assert reason == "hold"

    def test_dte_exit(self, risk_config):
        rm = RiskManager(risk_config)
        # Create trade with expiry within DTE threshold
        near_expiry = (datetime.now() + timedelta(days=20)).strftime("%Y%m%d")
        trade = TradeRecord(
            trade_id="dte_test",
            symbol="SPY",
            spread_type=SpreadType.BULL_CALL,
            long_leg=OptionLeg("SPY", near_expiry, 450, "C", "BUY"),
            short_leg=OptionLeg("SPY", near_expiry, 455, "C", "SELL"),
            contracts=1,
            entry_price=2.10,
            entry_time=datetime.now(),
            max_profit=290.0,
            max_loss=210.0,
            status=OrderStatus.FILLED,
        )
        should_exit, reason = rm.check_exit_conditions(trade, 2.00)
        assert should_exit is True
        assert "dte" in reason.lower()
