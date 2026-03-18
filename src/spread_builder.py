"""Option chain filtering, strike selection, and BAG contract construction."""

import logging
from datetime import datetime, timedelta

from ib_async import ComboLeg, Contract, IB

from src.config_loader import StrategyConfig
from src.market_data import MarketData
from src.models import OptionLeg, Signal, SpreadCandidate, SpreadType

logger = logging.getLogger(__name__)


class SpreadBuilder:
    """Builds vertical spread candidates from option chain data."""

    def __init__(self, ib: IB, market_data: MarketData, config: StrategyConfig):
        self.ib = ib
        self.market_data = market_data
        self.config = config

    async def find_spread(
        self, symbol: str, signal: Signal, price: float
    ) -> SpreadCandidate | None:
        """Find the best vertical spread for the given signal.

        Returns None if no suitable spread is found.
        """
        if signal == Signal.NEUTRAL:
            return None

        spread_type = (
            SpreadType.BULL_CALL if signal == Signal.BULLISH else SpreadType.BEAR_PUT
        )
        right = "C" if spread_type == SpreadType.BULL_CALL else "P"

        # Get option chains
        chains = await self.market_data.get_option_chains(symbol)
        if not chains:
            logger.warning("No option chains found for %s", symbol)
            return None

        # Find valid expiries within DTE range
        expiries = self._filter_expiries(chains)
        if not expiries:
            logger.warning("No expiries in DTE range for %s", symbol)
            return None

        # Try each expiry (closest to 30 DTE first)
        target_dte = 30
        expiries.sort(key=lambda e: abs(self._calc_dte(e) - target_dte))

        for expiry in expiries:
            candidate = await self._build_spread_for_expiry(
                symbol, expiry, right, spread_type, signal, price
            )
            if candidate:
                return candidate

        logger.info("No valid spread found for %s (%s)", symbol, signal.value)
        return None

    def _filter_expiries(self, chains: list) -> list[str]:
        """Filter expiries to those within configured DTE range."""
        today = datetime.now().date()
        min_date = today + timedelta(days=self.config.dte_min)
        max_date = today + timedelta(days=self.config.dte_max)

        valid = set()
        for chain in chains:
            for exp in chain.expirations:
                exp_date = datetime.strptime(exp, "%Y%m%d").date()
                if min_date <= exp_date <= max_date:
                    valid.add(exp)
        return sorted(valid)

    def _calc_dte(self, expiry: str) -> int:
        """Calculate days to expiration."""
        exp_date = datetime.strptime(expiry, "%Y%m%d").date()
        return (exp_date - datetime.now().date()).days

    async def _build_spread_for_expiry(
        self,
        symbol: str,
        expiry: str,
        right: str,
        spread_type: SpreadType,
        signal: Signal,
        price: float,
    ) -> SpreadCandidate | None:
        """Try to build a spread for a specific expiry."""
        # Determine strike range around current price
        width = self.config.spread_width
        strike_range = self._get_strike_range(price, width)

        # Get option contracts
        contracts = await self.market_data.get_option_chain_data(
            symbol, expiry, right, strike_range
        )
        if len(contracts) < 2:
            return None

        # Get greeks and market data
        greeks = await self.market_data.get_option_greeks(contracts)
        if not greeks:
            return None

        # Filter by delta ranges
        long_candidates = []
        short_candidates = []
        for strike, data in greeks.items():
            if not self._passes_liquidity_filter(data):
                continue

            delta = data.get("delta")
            if delta is None:
                continue

            abs_delta = abs(delta)

            if self.config.delta_long_min <= abs_delta <= self.config.delta_long_max:
                long_candidates.append((strike, data))
            if self.config.delta_short_min <= abs_delta <= self.config.delta_short_max:
                short_candidates.append((strike, data))

        if not long_candidates or not short_candidates:
            return None

        # Select best pair
        return self._select_best_pair(
            symbol, expiry, right, spread_type, signal,
            long_candidates, short_candidates
        )

    def _get_strike_range(self, price: float, width: float) -> list[float]:
        """Generate strike prices around current price."""
        # Round to nearest dollar for strike generation
        base = round(price)
        strikes = []
        for i in range(-10, 11):
            strike = base + i * width
            if strike > 0:
                strikes.append(strike)
        # Also add $1 increments near ATM
        for i in range(-5, 6):
            strike = base + i
            if strike > 0 and strike not in strikes:
                strikes.append(strike)
        return sorted(strikes)

    def _passes_liquidity_filter(self, data: dict) -> bool:
        """Check if option passes liquidity filters."""
        oi = data.get("open_interest")
        if oi is not None and oi < self.config.min_open_interest:
            return False

        bid = data.get("bid")
        ask = data.get("ask")
        if bid and ask and bid > 0:
            spread_pct = (ask - bid) / ((bid + ask) / 2)
            if spread_pct > self.config.max_bid_ask_spread_pct:
                return False

        return True

    def _select_best_pair(
        self,
        symbol: str,
        expiry: str,
        right: str,
        spread_type: SpreadType,
        signal: Signal,
        long_candidates: list[tuple[float, dict]],
        short_candidates: list[tuple[float, dict]],
    ) -> SpreadCandidate | None:
        """Select the best long/short leg pair for the spread."""
        best = None

        for long_strike, long_data in long_candidates:
            for short_strike, short_data in short_candidates:
                # Validate spread structure
                if spread_type == SpreadType.BULL_CALL:
                    # Bull call: buy lower strike, sell higher strike
                    if long_strike >= short_strike:
                        continue
                else:
                    # Bear put: buy higher strike, sell lower strike
                    if long_strike <= short_strike:
                        continue

                long_mid = self._mid_price(long_data)
                short_mid = self._mid_price(short_data)
                if long_mid is None or short_mid is None:
                    continue

                # Calculate spread economics
                if spread_type == SpreadType.BULL_CALL:
                    net_debit = long_mid - short_mid
                    spread_width = short_strike - long_strike
                    max_profit = (spread_width - net_debit) * 100
                    max_loss = net_debit * 100
                else:
                    net_debit = long_mid - short_mid
                    spread_width = long_strike - short_strike
                    max_profit = (spread_width - net_debit) * 100
                    max_loss = net_debit * 100

                if max_loss <= 0 or max_profit <= 0:
                    continue

                long_leg = OptionLeg(
                    symbol=symbol,
                    expiry=expiry,
                    strike=long_strike,
                    right=right,
                    action="BUY",
                    delta=long_data.get("delta"),
                    open_interest=long_data.get("open_interest"),
                    bid=long_data.get("bid"),
                    ask=long_data.get("ask"),
                )
                short_leg = OptionLeg(
                    symbol=symbol,
                    expiry=expiry,
                    strike=short_strike,
                    right=right,
                    action="SELL",
                    delta=short_data.get("delta"),
                    open_interest=short_data.get("open_interest"),
                    bid=short_data.get("bid"),
                    ask=short_data.get("ask"),
                )

                candidate = SpreadCandidate(
                    symbol=symbol,
                    spread_type=spread_type,
                    long_leg=long_leg,
                    short_leg=short_leg,
                    max_profit=max_profit,
                    max_loss=max_loss,
                    net_debit=net_debit,
                    dte=self._calc_dte(expiry),
                    signal=signal,
                )

                # Prefer best risk/reward ratio
                if best is None or candidate.risk_reward_ratio > best.risk_reward_ratio:
                    best = candidate

        return best

    def _mid_price(self, data: dict) -> float | None:
        """Calculate mid price from bid/ask."""
        bid = data.get("bid")
        ask = data.get("ask")
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2
        return None

    def build_bag_contract(self, candidate: SpreadCandidate) -> Contract:
        """Build an IB BAG (combo) contract for the spread."""
        contract = Contract()
        contract.symbol = candidate.symbol
        contract.secType = "BAG"
        contract.currency = "USD"
        contract.exchange = "SMART"

        leg1 = ComboLeg()
        leg1.conId = 0  # Will be set after qualification
        leg1.ratio = 1
        leg1.action = "BUY"
        leg1.exchange = "SMART"

        leg2 = ComboLeg()
        leg2.conId = 0
        leg2.ratio = 1
        leg2.action = "SELL"
        leg2.exchange = "SMART"

        contract.comboLegs = [leg1, leg2]
        return contract

    async def build_qualified_bag(self, candidate: SpreadCandidate) -> Contract | None:
        """Build and qualify a BAG contract with proper conIds."""
        from ib_async import Option as IBOption

        long_opt = IBOption(
            candidate.symbol,
            candidate.long_leg.expiry,
            candidate.long_leg.strike,
            candidate.long_leg.right,
            "SMART",
        )
        short_opt = IBOption(
            candidate.symbol,
            candidate.short_leg.expiry,
            candidate.short_leg.strike,
            candidate.short_leg.right,
            "SMART",
        )

        qualified = await self.ib.qualifyContractsAsync(long_opt, short_opt)
        if len(qualified) < 2 or any(c.conId == 0 for c in qualified):
            logger.warning("Could not qualify option contracts for spread")
            return None

        contract = Contract()
        contract.symbol = candidate.symbol
        contract.secType = "BAG"
        contract.currency = "USD"
        contract.exchange = "SMART"

        leg1 = ComboLeg()
        leg1.conId = qualified[0].conId
        leg1.ratio = 1
        leg1.action = "BUY"
        leg1.exchange = "SMART"

        leg2 = ComboLeg()
        leg2.conId = qualified[1].conId
        leg2.ratio = 1
        leg2.action = "SELL"
        leg2.exchange = "SMART"

        contract.comboLegs = [leg1, leg2]
        return contract
