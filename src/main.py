"""Entry point: wires all components, runs the main loop, handles shutdown."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

from src.config_loader import load_config
from src.connection import IBConnection
from src.market_data import MarketData
from src.models import OrderStatus
from src.notifier import Notifier
from src.order_manager import OrderManager
from src.portfolio import Portfolio
from src.risk_manager import RiskManager
from src.scheduler import Scheduler
from src.spread_builder import SpreadBuilder
from src.strategy import Strategy


def setup_logging(level: str):
    """Configure logging to stdout and file."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]

    log_dir = Path("/app/logs")
    if log_dir.exists():
        handlers.append(logging.FileHandler(log_dir / "bot.log"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        handlers=handlers,
    )


logger = logging.getLogger(__name__)


class TradingBot:
    """Main trading bot orchestrator."""

    def __init__(self):
        self.config = load_config()
        setup_logging(self.config.logging.level)
        self._shutdown = asyncio.Event()

    async def run(self):
        """Main entry point: connect, then run scan/exit loops."""
        logger.info("Starting trading bot (mode=%s)", self.config.ib.trading_mode)

        # Set up signal handlers for graceful shutdown
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_shutdown)

        # Connect to IB Gateway
        conn = IBConnection(self.config.ib)
        ib = await conn.connect()

        # Wire components
        market_data = MarketData(ib, self.config.strategy)
        spread_builder = SpreadBuilder(ib, market_data, self.config.strategy)
        strategy = Strategy(ib, self.config, market_data, spread_builder)
        risk_manager = RiskManager(self.config.risk)
        order_manager = OrderManager(ib)
        portfolio = Portfolio(self.config)
        scheduler = Scheduler(self.config.schedule)
        notifier = Notifier()

        logger.info("All components initialized. Entering main loop.")

        try:
            await self._main_loop(
                ib, strategy, risk_manager, order_manager,
                portfolio, scheduler, notifier, market_data,
                spread_builder,
            )
        finally:
            logger.info("Shutting down...")
            await conn.disconnect()
            logger.info("Bot stopped.")

    async def _main_loop(
        self, ib, strategy, risk_manager, order_manager,
        portfolio, scheduler, notifier, market_data, spread_builder,
    ):
        """Run scan and exit check loops until shutdown."""
        scan_interval = self.config.strategy.scan_interval_minutes * 60
        exit_interval = self.config.strategy.exit_check_interval_minutes * 60

        last_scan = 0.0
        last_exit_check = 0.0

        while not self._shutdown.is_set():
            now = asyncio.get_event_loop().time()

            # Exit check (runs during market hours, every exit_interval)
            if scheduler.is_market_open() and (now - last_exit_check) >= exit_interval:
                await self._check_exits(
                    portfolio, risk_manager, order_manager,
                    spread_builder, notifier, market_data,
                )
                last_exit_check = now

            # Scan for new entries (runs during entry window, every scan_interval)
            if scheduler.can_enter_trades() and (now - last_scan) >= scan_interval:
                await self._scan_and_trade(
                    strategy, risk_manager, order_manager,
                    portfolio, notifier, market_data, spread_builder,
                )
                last_scan = now

            # Sleep until next check, but wake on shutdown
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=min(exit_interval, 30),
                )
                break  # Shutdown signaled
            except asyncio.TimeoutError:
                pass  # Normal timeout, continue loop

    async def _scan_and_trade(
        self, strategy, risk_manager, order_manager,
        portfolio, notifier, market_data, spread_builder,
    ):
        """Scan watchlist and place trades for valid candidates."""
        logger.info("Starting watchlist scan...")
        candidates = await strategy.scan_all()

        account_value = await market_data.get_account_value()
        snapshot = portfolio.get_snapshot(account_value)

        for candidate in candidates:
            # Risk gate check
            allowed, reason = risk_manager.can_open_position(candidate, snapshot)
            if not allowed:
                notifier.risk_gate_blocked(candidate.symbol, reason)
                continue

            # Position sizing
            contracts = risk_manager.calculate_position_size(
                candidate, account_value
            )
            if contracts == 0:
                logger.info(
                    "Position size is 0 for %s, skipping", candidate.symbol
                )
                continue

            # Build qualified BAG contract
            bag = await spread_builder.build_qualified_bag(candidate)
            if not bag:
                continue

            # Place order
            trade = await order_manager.place_spread_order(
                bag, candidate, contracts
            )
            if trade and trade.status != OrderStatus.ERROR:
                portfolio.add_trade(trade)
                notifier.trade_opened(trade)
                notifier.signal_detected(candidate.symbol, candidate)

                # Update snapshot for subsequent candidates
                snapshot = portfolio.get_snapshot(account_value)

        logger.info("Scan complete. Open positions: %d", snapshot.open_positions)

    async def _check_exits(
        self, portfolio, risk_manager, order_manager,
        spread_builder, notifier, market_data,
    ):
        """Check all open positions for exit conditions."""
        for trade in portfolio.open_trades:
            try:
                # Get current spread value (would need market data for the combo)
                # For now, use a simplified approach
                current_value = trade.entry_price  # Placeholder

                should_exit, reason = risk_manager.check_exit_conditions(
                    trade, current_value
                )
                if should_exit:
                    logger.info(
                        "Exit triggered for %s (%s): %s",
                        trade.symbol, trade.trade_id, reason,
                    )
                    # Build close contract
                    from src.models import SpreadCandidate
                    bag = await spread_builder.build_qualified_bag(
                        SpreadCandidate(
                            symbol=trade.symbol,
                            spread_type=trade.spread_type,
                            long_leg=trade.long_leg,
                            short_leg=trade.short_leg,
                            max_profit=trade.max_profit,
                            max_loss=trade.max_loss,
                            net_debit=trade.entry_price,
                            dte=0,
                            signal=trade.spread_type,
                        )
                    )
                    if bag:
                        await order_manager.close_position(trade, bag)
                        portfolio.close_trade(
                            trade.trade_id, current_value, reason
                        )
                        notifier.trade_closed(trade)
            except Exception as e:
                logger.error(
                    "Error checking exit for %s: %s",
                    trade.trade_id, e, exc_info=True,
                )

    def _handle_shutdown(self):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        logger.info("Shutdown signal received")
        self._shutdown.set()


def main():
    asyncio.run(TradingBot().run())


if __name__ == "__main__":
    main()
