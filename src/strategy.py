"""Signal generation and entry/exit orchestration."""

import logging

from ib_async import IB

from src.config_loader import AppConfig
from src.indicators import compute_iv_rank, generate_signal
from src.market_data import MarketData
from src.models import SpreadCandidate
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
        """Scan a single symbol for trading opportunities."""
        try:
            contract = await self.market_data.get_stock_contract(symbol)
            df = await self.market_data.get_historical_bars(contract)

            if df.empty or len(df) < self.config.indicators.sma_slow:
                logger.debug("Insufficient data for %s (%d bars)", symbol, len(df))
                return None

            result = generate_signal(df, self.config.indicators)
            result.symbol = symbol

            # IV rank drives strategy selection (non-critical — falls back to None on error)
            iv_rank = await self._get_iv_rank(symbol, contract)

            logger.info(
                "%s: price=%.2f rsi=%.1f signal=%s iv_rank=%s",
                symbol,
                result.price,
                result.rsi or 0,
                result.signal.value,
                f"{iv_rank:.0%}" if iv_rank is not None else "N/A",
            )

            candidate = await self.spread_builder.find_spread(
                symbol, result.signal, result.price, iv_rank=iv_rank
            )

            if candidate:
                leg_summary = ", ".join(
                    f"{l.right}{l.strike:.0f}{'×'+str(l.ratio) if l.ratio != 1 else ''}"
                    for l in candidate.all_legs
                )
                logger.info(
                    "Spread found for %s: %s [%s] DTE=%d max_profit=%.0f max_loss=%.0f",
                    symbol,
                    candidate.spread_type.value,
                    leg_summary,
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
            if not self.ib.isConnected():
                logger.warning("Lost connection during scan, aborting remaining symbols")
                break
            candidate = await self.scan_symbol(symbol)
            if candidate:
                candidates.append(candidate)
        return candidates

    async def _get_iv_rank(self, symbol: str, contract) -> float | None:
        """Fetch one-year historical IV and return IV rank in [0, 1]."""
        try:
            iv_series = await self.market_data.get_historical_iv(contract)
            if len(iv_series) < 30:
                return None
            iv_rank, _ = compute_iv_rank(iv_series)
            return iv_rank
        except Exception as e:
            logger.debug("Could not fetch IV rank for %s: %s", symbol, e)
            return None
