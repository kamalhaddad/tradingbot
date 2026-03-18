"""Vertical spread signal generation and entry/exit orchestration."""

import logging

from ib_async import IB

from src.config_loader import AppConfig
from src.indicators import generate_signal
from src.market_data import MarketData
from src.models import IndicatorResult, Signal, SpreadCandidate
from src.spread_builder import SpreadBuilder

logger = logging.getLogger(__name__)


class Strategy:
    """Orchestrates signal generation and spread candidate selection."""

    def __init__(
        self,
        ib: IB,
        config: AppConfig,
        market_data: MarketData,
        spread_builder: SpreadBuilder,
    ):
        self.ib = ib
        self.config = config
        self.market_data = market_data
        self.spread_builder = spread_builder

    async def scan_symbol(self, symbol: str) -> SpreadCandidate | None:
        """Scan a single symbol for trading opportunities.

        Returns a SpreadCandidate if a valid signal and spread are found.
        """
        try:
            # Get stock contract and historical data
            contract = await self.market_data.get_stock_contract(symbol)
            df = await self.market_data.get_historical_bars(contract)

            if df.empty or len(df) < self.config.indicators.sma_slow:
                logger.debug(
                    "Insufficient data for %s (%d bars)", symbol, len(df)
                )
                return None

            # Compute indicators and generate signal
            result = generate_signal(df, self.config.indicators)
            result.symbol = symbol

            logger.info(
                "%s: price=%.2f rsi=%.1f sma_fast=%.2f sma_slow=%.2f signal=%s",
                symbol,
                result.price,
                result.rsi or 0,
                result.sma_fast or 0,
                result.sma_slow or 0,
                result.signal.value,
            )

            if result.signal == Signal.NEUTRAL:
                return None

            # Find a spread matching the signal
            candidate = await self.spread_builder.find_spread(
                symbol, result.signal, result.price
            )
            if candidate:
                logger.info(
                    "Spread found for %s: %s %s/%s DTE=%d max_profit=%.0f max_loss=%.0f",
                    symbol,
                    candidate.spread_type.value,
                    candidate.long_leg.strike,
                    candidate.short_leg.strike,
                    candidate.dte,
                    candidate.max_profit,
                    candidate.max_loss,
                )
            return candidate

        except Exception as e:
            logger.error("Error scanning %s: %s", symbol, e, exc_info=True)
            return None

    async def scan_all(self) -> list[SpreadCandidate]:
        """Scan all watchlist symbols for trading opportunities."""
        candidates = []
        for symbol in self.config.strategy.watchlist:
            candidate = await self.scan_symbol(symbol)
            if candidate:
                candidates.append(candidate)
        return candidates
