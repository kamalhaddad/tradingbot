"""Order placement, fill callbacks, and position closing."""

import logging
import uuid
from datetime import datetime

from ib_async import IB, Contract, LimitOrder, Trade

from src.models import OrderStatus, SpreadCandidate, TradeRecord

logger = logging.getLogger(__name__)


class OrderManager:
    """Handles order placement and fill tracking."""

    def __init__(self, ib: IB):
        self.ib = ib
        self._pending_orders: dict[int, TradeRecord] = {}

    async def place_spread_order(
        self, contract: Contract, candidate: SpreadCandidate, contracts: int
    ) -> TradeRecord | None:
        """Place a limit order for a vertical spread.

        Uses net debit as the limit price.
        """
        if contracts <= 0:
            logger.warning("Cannot place order with 0 contracts")
            return None

        # Limit price is the net debit (positive for debit spreads)
        limit_price = round(candidate.net_debit, 2)

        order = LimitOrder(
            action="BUY",
            totalQuantity=contracts,
            lmtPrice=limit_price,
        )
        order.smartComboRoutingParams = [{"tag": "NonGuaranteed", "value": "1"}]

        trade_record = TradeRecord(
            trade_id=str(uuid.uuid4())[:8],
            symbol=candidate.symbol,
            spread_type=candidate.spread_type,
            long_leg=candidate.long_leg,
            short_leg=candidate.short_leg,
            contracts=contracts,
            entry_price=candidate.net_debit,
            entry_time=datetime.now(),
            max_profit=candidate.max_profit * contracts,
            max_loss=candidate.max_loss * contracts,
            status=OrderStatus.SUBMITTED,
        )

        try:
            ib_trade = self.ib.placeOrder(contract, order)
            trade_record.order_id = ib_trade.order.orderId
            self._pending_orders[ib_trade.order.orderId] = trade_record

            # Attach fill callback
            ib_trade.filledEvent += lambda t: self._on_fill(t, trade_record)
            ib_trade.cancelledEvent += lambda t: self._on_cancel(t, trade_record)

            logger.info(
                "Order placed: %s %s %dx %s/%s @ %.2f (id=%s)",
                candidate.symbol,
                candidate.spread_type.value,
                contracts,
                candidate.long_leg.strike,
                candidate.short_leg.strike,
                limit_price,
                trade_record.trade_id,
            )
            return trade_record

        except Exception as e:
            logger.error("Order placement failed: %s", e, exc_info=True)
            trade_record.status = OrderStatus.ERROR
            return trade_record

    async def close_position(
        self, trade: TradeRecord, contract: Contract
    ) -> Trade | None:
        """Close an open spread position by selling the combo."""
        order = LimitOrder(
            action="SELL",
            totalQuantity=trade.contracts,
            lmtPrice=0.0,  # Will be updated with market price
        )
        order.smartComboRoutingParams = [{"tag": "NonGuaranteed", "value": "1"}]

        try:
            ib_trade = self.ib.placeOrder(contract, order)
            logger.info(
                "Close order placed for %s (trade %s)",
                trade.symbol,
                trade.trade_id,
            )
            return ib_trade
        except Exception as e:
            logger.error(
                "Failed to close position %s: %s", trade.trade_id, e, exc_info=True
            )
            return None

    def _on_fill(self, ib_trade: Trade, trade_record: TradeRecord):
        """Handle order fill event."""
        trade_record.status = OrderStatus.FILLED
        fill_price = ib_trade.orderStatus.avgFillPrice
        trade_record.entry_price = fill_price
        logger.info(
            "Order FILLED: %s %s @ %.2f",
            trade_record.symbol,
            trade_record.trade_id,
            fill_price,
        )

    def _on_cancel(self, ib_trade: Trade, trade_record: TradeRecord):
        """Handle order cancellation event."""
        trade_record.status = OrderStatus.CANCELLED
        logger.warning(
            "Order CANCELLED: %s %s",
            trade_record.symbol,
            trade_record.trade_id,
        )
