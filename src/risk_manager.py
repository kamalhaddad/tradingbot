"""Position sizing, loss limits, and exit condition checks."""

import logging
import math
from datetime import datetime

from src.config_loader import RiskConfig
from src.models import PortfolioSnapshot, SpreadCandidate, TradeRecord

logger = logging.getLogger(__name__)


class RiskManager:
    """Enforces risk rules: sizing, position limits, loss gates, exit triggers."""

    def __init__(self, config: RiskConfig):
        self.config = config

    def calculate_position_size(
        self, candidate: SpreadCandidate, account_value: float
    ) -> int:
        """Calculate number of contracts based on max risk per trade.

        contracts = floor(account_value * max_risk_pct / max_loss_per_contract)
        """
        if candidate.max_loss <= 0 or account_value <= 0:
            return 0
        max_risk = account_value * self.config.max_risk_per_trade_pct
        contracts = math.floor(max_risk / candidate.max_loss)
        return max(contracts, 0)

    def can_open_position(
        self, candidate: SpreadCandidate, snapshot: PortfolioSnapshot
    ) -> tuple[bool, str]:
        """Check if a new position passes all risk gates.

        Returns (allowed, reason).
        """
        # Max concurrent positions
        if snapshot.open_positions >= self.config.max_concurrent_positions:
            return False, (
                f"Max concurrent positions reached "
                f"({self.config.max_concurrent_positions})"
            )

        # Max positions per symbol
        symbol_count = snapshot.positions_by_symbol.get(candidate.symbol, 0)
        if symbol_count >= self.config.max_positions_per_symbol:
            return False, (
                f"Max positions for {candidate.symbol} reached "
                f"({self.config.max_positions_per_symbol})"
            )

        # Daily loss limit
        daily_loss_pct = abs(snapshot.realized_pnl_today) / snapshot.account_value
        if (
            snapshot.realized_pnl_today < 0
            and daily_loss_pct >= self.config.daily_loss_limit_pct
        ):
            return False, (
                f"Daily loss limit reached "
                f"({daily_loss_pct:.1%} >= {self.config.daily_loss_limit_pct:.1%})"
            )

        # Monthly loss limit
        monthly_loss_pct = abs(snapshot.realized_pnl_month) / snapshot.account_value
        if (
            snapshot.realized_pnl_month < 0
            and monthly_loss_pct >= self.config.monthly_loss_limit_pct
        ):
            return False, (
                f"Monthly loss limit reached "
                f"({monthly_loss_pct:.1%} >= {self.config.monthly_loss_limit_pct:.1%})"
            )

        return True, "OK"

    def check_exit_conditions(
        self, trade: TradeRecord, current_spread_value: float
    ) -> tuple[bool, str]:
        """Check if an open position should be closed.

        Returns (should_exit, reason).
        Priority: profit target > DTE exit > stop loss.
        """
        if not trade.is_open:
            return False, "not open"

        # P&L and thresholds per contract (normalize for multi-contract trades)
        pnl_per_spread = (trade.entry_price - current_spread_value) * 100
        max_profit_per = trade.max_profit / max(trade.contracts, 1)
        max_loss_per = trade.max_loss / max(trade.contracts, 1)

        # 1. Profit target: close at X% of max profit
        profit_target = max_profit_per * self.config.profit_target_pct
        if pnl_per_spread >= profit_target:
            return True, (
                f"Profit target reached "
                f"(${pnl_per_spread:.0f} >= ${profit_target:.0f})"
            )

        # 2. DTE exit: close when too close to expiration
        dte = self._calc_dte(trade.long_leg.expiry)
        if dte <= self.config.dte_exit_threshold:
            return True, f"DTE exit ({dte} <= {self.config.dte_exit_threshold})"

        # 3. Stop loss: close at X% of max loss
        max_acceptable_loss = max_loss_per * self.config.stop_loss_pct
        if pnl_per_spread <= -max_acceptable_loss:
            return True, (
                f"Stop loss triggered "
                f"(${pnl_per_spread:.0f} <= -${max_acceptable_loss:.0f})"
            )

        return False, "hold"

    def _calc_dte(self, expiry: str) -> int:
        """Calculate days to expiration from YYYYMMDD string."""
        exp_date = datetime.strptime(expiry, "%Y%m%d").date()
        return (exp_date - datetime.now().date()).days
