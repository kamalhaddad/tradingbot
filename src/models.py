"""Core data models for the trading bot."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Signal(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SpreadType(Enum):
    BULL_CALL = "bull_call"
    BEAR_PUT = "bear_put"
    IRON_CONDOR = "iron_condor"
    BUTTERFLY = "butterfly"
    BROKEN_WING_BUTTERFLY = "broken_wing_butterfly"
    CALENDAR = "calendar"


class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class OptionLeg:
    symbol: str
    expiry: str              # YYYYMMDD
    strike: float
    right: str               # "C" or "P"
    action: str              # "BUY" or "SELL"
    ratio: int = 1           # contract ratio (e.g. 2 for butterfly body)
    delta: Optional[float] = None
    open_interest: Optional[int] = None
    bid: Optional[float] = None
    ask: Optional[float] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return None


@dataclass
class SpreadCandidate:
    symbol: str
    spread_type: SpreadType
    long_leg: OptionLeg
    short_leg: OptionLeg
    max_profit: float        # credit or debit spread max profit
    max_loss: float          # max loss per contract (always positive)
    net_debit: float         # net cost to enter (positive = debit, negative = credit)
    dte: int                 # days to expiration
    signal: Signal
    extra_legs: list["OptionLeg"] = field(default_factory=list)  # 3rd/4th legs for multi-leg strategies

    @property
    def all_legs(self) -> list["OptionLeg"]:
        return [self.long_leg, self.short_leg] + self.extra_legs

    @property
    def risk_reward_ratio(self) -> float:
        if self.max_loss == 0:
            return 0.0
        return self.max_profit / self.max_loss


@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    spread_type: SpreadType
    long_leg: OptionLeg
    short_leg: OptionLeg
    contracts: int
    entry_price: float       # net debit/credit per spread
    entry_time: datetime
    max_profit: float
    max_loss: float
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    order_id: Optional[int] = None
    extra_legs: list["OptionLeg"] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.exit_time is None and self.status in (
            OrderStatus.FILLED, OrderStatus.SUBMITTED
        )


@dataclass
class PortfolioSnapshot:
    timestamp: datetime
    account_value: float
    unrealized_pnl: float
    realized_pnl_today: float
    realized_pnl_month: float
    open_positions: int
    positions_by_symbol: dict[str, int] = field(default_factory=dict)


@dataclass
class IndicatorResult:
    symbol: str
    price: float
    rsi: Optional[float] = None
    sma_fast: Optional[float] = None
    sma_slow: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    signal: Signal = Signal.NEUTRAL
