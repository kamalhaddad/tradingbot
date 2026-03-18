"""Position tracking, P&L calculation, and trade journal."""

import csv
import logging
from datetime import datetime
from pathlib import Path

from src.config_loader import AppConfig
from src.models import OrderStatus, PortfolioSnapshot, TradeRecord

logger = logging.getLogger(__name__)


class Portfolio:
    """Tracks open positions, computes P&L, writes trade journal."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.trades: list[TradeRecord] = []
        self._journal_path = Path(config.logging.trade_journal)
        self._ensure_journal()

    def _ensure_journal(self):
        """Create trade journal CSV with headers if it doesn't exist."""
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._journal_path.exists():
            with open(self._journal_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "trade_id", "symbol", "spread_type", "contracts",
                    "long_strike", "short_strike", "expiry", "right",
                    "entry_price", "entry_time", "exit_price", "exit_time",
                    "exit_reason", "pnl", "max_profit", "max_loss", "status",
                ])

    def add_trade(self, trade: TradeRecord):
        """Add a new trade to tracking."""
        self.trades.append(trade)
        self._write_journal_row(trade)
        logger.info("Trade added: %s %s", trade.trade_id, trade.symbol)

    def close_trade(
        self, trade_id: str, exit_price: float, reason: str
    ):
        """Mark a trade as closed and update journal."""
        trade = self.get_trade(trade_id)
        if not trade:
            logger.warning("Trade %s not found", trade_id)
            return

        trade.exit_price = exit_price
        trade.exit_time = datetime.now()
        trade.exit_reason = reason
        trade.pnl = (trade.entry_price - exit_price) * 100 * trade.contracts
        trade.status = OrderStatus.FILLED

        self._write_journal_row(trade)
        logger.info(
            "Trade closed: %s %s reason=%s pnl=%.2f",
            trade.trade_id, trade.symbol, reason, trade.pnl,
        )

    def get_trade(self, trade_id: str) -> TradeRecord | None:
        """Find a trade by ID."""
        return next((t for t in self.trades if t.trade_id == trade_id), None)

    @property
    def open_trades(self) -> list[TradeRecord]:
        """Get all currently open trades."""
        return [t for t in self.trades if t.is_open]

    def get_snapshot(self, account_value: float) -> PortfolioSnapshot:
        """Build a portfolio snapshot for risk checks."""
        now = datetime.now()
        today = now.date()
        month_start = today.replace(day=1)

        realized_today = sum(
            t.pnl for t in self.trades
            if t.pnl is not None
            and t.exit_time is not None
            and t.exit_time.date() == today
        )
        realized_month = sum(
            t.pnl for t in self.trades
            if t.pnl is not None
            and t.exit_time is not None
            and t.exit_time.date() >= month_start
        )

        unrealized = sum(
            0.0 for t in self.open_trades  # Placeholder — updated at exit check
        )

        positions_by_symbol: dict[str, int] = {}
        for t in self.open_trades:
            positions_by_symbol[t.symbol] = (
                positions_by_symbol.get(t.symbol, 0) + 1
            )

        return PortfolioSnapshot(
            timestamp=now,
            account_value=account_value,
            unrealized_pnl=unrealized,
            realized_pnl_today=realized_today,
            realized_pnl_month=realized_month,
            open_positions=len(self.open_trades),
            positions_by_symbol=positions_by_symbol,
        )

    def _write_journal_row(self, trade: TradeRecord):
        """Append a trade record to the CSV journal."""
        try:
            with open(self._journal_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    trade.trade_id,
                    trade.symbol,
                    trade.spread_type.value,
                    trade.contracts,
                    trade.long_leg.strike,
                    trade.short_leg.strike,
                    trade.long_leg.expiry,
                    trade.long_leg.right,
                    trade.entry_price,
                    trade.entry_time.isoformat(),
                    trade.exit_price if trade.exit_price else "",
                    trade.exit_time.isoformat() if trade.exit_time else "",
                    trade.exit_reason or "",
                    f"{trade.pnl:.2f}" if trade.pnl is not None else "",
                    trade.max_profit,
                    trade.max_loss,
                    trade.status.value,
                ])
        except Exception as e:
            logger.error("Failed to write trade journal: %s", e)
