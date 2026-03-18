"""Log-based alerts, extensible to webhooks later."""

import logging

from src.models import SpreadCandidate, TradeRecord

logger = logging.getLogger(__name__)

# Dedicated logger for trade alerts
alert_logger = logging.getLogger("trading.alerts")


class Notifier:
    """Sends trade notifications via logging. Extend for webhooks/email later."""

    def trade_opened(self, trade: TradeRecord):
        alert_logger.info(
            "TRADE OPENED: %s %s %dx long=%s short=%s entry=%.2f max_profit=%.0f max_loss=%.0f",
            trade.symbol,
            trade.spread_type.value,
            trade.contracts,
            trade.long_leg.strike,
            trade.short_leg.strike,
            trade.entry_price,
            trade.max_profit,
            trade.max_loss,
        )

    def trade_closed(self, trade: TradeRecord):
        alert_logger.info(
            "TRADE CLOSED: %s %s reason=%s pnl=%.2f",
            trade.symbol,
            trade.trade_id,
            trade.exit_reason or "unknown",
            trade.pnl or 0.0,
        )

    def signal_detected(self, symbol: str, candidate: SpreadCandidate):
        alert_logger.info(
            "SIGNAL: %s %s spread=%s/%s DTE=%d",
            symbol,
            candidate.signal.value,
            candidate.long_leg.strike,
            candidate.short_leg.strike,
            candidate.dte,
        )

    def risk_gate_blocked(self, symbol: str, reason: str):
        alert_logger.warning("RISK BLOCKED: %s - %s", symbol, reason)

    def daily_summary(
        self,
        open_positions: int,
        realized_pnl: float,
        account_value: float,
    ):
        alert_logger.info(
            "DAILY SUMMARY: positions=%d realized_pnl=%.2f account=%.2f",
            open_positions,
            realized_pnl,
            account_value,
        )
