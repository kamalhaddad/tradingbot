"""Shared test fixtures."""

import pytest

from src.config_loader import (
    AppConfig,
    IndicatorsConfig,
    RiskConfig,
    StrategyConfig,
)
from src.models import (
    OptionLeg,
    OrderStatus,
    PortfolioSnapshot,
    Signal,
    SpreadCandidate,
    SpreadType,
    TradeRecord,
)
from datetime import datetime, timedelta


@pytest.fixture
def indicators_config():
    return IndicatorsConfig()


@pytest.fixture
def strategy_config():
    return StrategyConfig()


@pytest.fixture
def risk_config():
    return RiskConfig()


@pytest.fixture
def app_config():
    return AppConfig()


@pytest.fixture
def sample_long_leg():
    return OptionLeg(
        symbol="SPY",
        expiry=(datetime.now() + timedelta(days=30)).strftime("%Y%m%d"),
        strike=450.0,
        right="C",
        action="BUY",
        delta=0.40,
        open_interest=5000,
        bid=5.00,
        ask=5.20,
    )


@pytest.fixture
def sample_short_leg():
    return OptionLeg(
        symbol="SPY",
        expiry=(datetime.now() + timedelta(days=30)).strftime("%Y%m%d"),
        strike=455.0,
        right="C",
        action="SELL",
        delta=0.25,
        open_interest=4000,
        bid=3.00,
        ask=3.20,
    )


@pytest.fixture
def sample_candidate(sample_long_leg, sample_short_leg):
    return SpreadCandidate(
        symbol="SPY",
        spread_type=SpreadType.BULL_CALL,
        long_leg=sample_long_leg,
        short_leg=sample_short_leg,
        max_profit=290.0,   # (5.0 - 2.10) * 100
        max_loss=210.0,     # 2.10 * 100
        net_debit=2.10,
        dte=30,
        signal=Signal.BULLISH,
    )


@pytest.fixture
def sample_trade(sample_long_leg, sample_short_leg):
    return TradeRecord(
        trade_id="test001",
        symbol="SPY",
        spread_type=SpreadType.BULL_CALL,
        long_leg=sample_long_leg,
        short_leg=sample_short_leg,
        contracts=2,
        entry_price=2.10,
        entry_time=datetime.now(),
        max_profit=580.0,
        max_loss=420.0,
        status=OrderStatus.FILLED,
    )


@pytest.fixture
def sample_snapshot():
    return PortfolioSnapshot(
        timestamp=datetime.now(),
        account_value=100000.0,
        unrealized_pnl=0.0,
        realized_pnl_today=0.0,
        realized_pnl_month=0.0,
        open_positions=2,
        positions_by_symbol={"SPY": 1, "QQQ": 1},
    )
