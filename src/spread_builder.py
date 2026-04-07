"""Option chain filtering, strike selection, and BAG contract construction."""

import logging
from datetime import datetime, timedelta

from ib_async import ComboLeg, Contract, IB
from ib_async import Option as IBOption

from src.config_loader import StrategyConfig
from src.market_data import MarketData
from src.models import OptionLeg, Signal, SpreadCandidate, SpreadType

logger = logging.getLogger(__name__)


class SpreadBuilder:
    """Builds spread candidates from option chain data.

    Supports vertical spreads (bull call / bear put), iron condors,
    butterflies, broken-wing butterflies, and calendar spreads.

    Strategy selection is driven by the signal and IV rank:
      - BULLISH/BEARISH + high IV  → butterfly (benefits from IV crush)
      - BULLISH/BEARISH + normal   → vertical spread (bull call / bear put)
      - NEUTRAL        + high IV  → iron condor (sell premium)
      - NEUTRAL        + low IV   → calendar spread (long vol / theta)
    """

    def __init__(self, ib: IB, market_data: MarketData, config: StrategyConfig):
        self.ib = ib
        self.market_data = market_data
        self.config = config

    # ------------------------------------------------------------------ #
    # Public dispatch                                                       #
    # ------------------------------------------------------------------ #

    async def find_spread(
        self,
        symbol: str,
        signal: Signal,
        price: float,
        iv_rank: float | None = None,
    ) -> SpreadCandidate | None:
        """Find the best spread for the given signal, routing by IV rank."""
        if signal == Signal.NEUTRAL:
            if iv_rank is not None and iv_rank >= self.config.iv_rank_high_threshold:
                return await self.find_iron_condor(symbol, price)
            if iv_rank is not None and iv_rank <= self.config.iv_rank_low_threshold:
                return await self.find_calendar_spread(symbol, price)
            return None

        # Directional signal — butterfly when IV is elevated
        if iv_rank is not None and iv_rank >= self.config.iv_rank_high_threshold:
            candidate = await self.find_butterfly(symbol, signal, price)
            if candidate:
                return candidate

        return await self._find_vertical_spread(symbol, signal, price)

    # ------------------------------------------------------------------ #
    # Vertical spreads (bull call / bear put)                              #
    # ------------------------------------------------------------------ #

    async def _find_vertical_spread(
        self, symbol: str, signal: Signal, price: float
    ) -> SpreadCandidate | None:
        """Find a bull call or bear put vertical spread."""
        spread_type = SpreadType.BULL_CALL if signal == Signal.BULLISH else SpreadType.BEAR_PUT
        right = "C" if spread_type == SpreadType.BULL_CALL else "P"

        chains = await self.market_data.get_option_chains(symbol)
        if not chains:
            logger.warning("No option chains found for %s", symbol)
            return None

        chain_strikes = self._get_chain_strikes(chains)
        expiries = self._filter_expiries(chains)
        if not expiries:
            logger.warning("No expiries in DTE range for %s", symbol)
            return None

        expiries.sort(key=lambda e: abs(self._calc_dte(e) - 30))
        for expiry in expiries:
            candidate = await self._build_vertical_for_expiry(
                symbol, expiry, right, spread_type, signal, price,
                chain_strikes=chain_strikes,
            )
            if candidate:
                return candidate

        logger.info("No valid vertical spread found for %s (%s)", symbol, signal.value)
        return None

    async def _build_vertical_for_expiry(
        self,
        symbol: str,
        expiry: str,
        right: str,
        spread_type: SpreadType,
        signal: Signal,
        price: float,
        chain_strikes: set[float] | None = None,
    ) -> SpreadCandidate | None:
        strike_range = self._get_strike_range(price, self.config.spread_width, chain_strikes)
        contracts = await self.market_data.get_option_chain_data(symbol, expiry, right, strike_range)
        if len(contracts) < 2:
            return None

        greeks = await self.market_data.get_option_greeks(contracts)
        if not greeks:
            return None

        long_candidates, short_candidates = [], []
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

        return self._select_vertical_pair(
            symbol, expiry, right, spread_type, signal,
            long_candidates, short_candidates,
        )

    def _select_vertical_pair(
        self,
        symbol: str,
        expiry: str,
        right: str,
        spread_type: SpreadType,
        signal: Signal,
        long_candidates: list[tuple[float, dict]],
        short_candidates: list[tuple[float, dict]],
    ) -> SpreadCandidate | None:
        best = None
        for long_strike, long_data in long_candidates:
            for short_strike, short_data in short_candidates:
                if spread_type == SpreadType.BULL_CALL:
                    if long_strike >= short_strike:
                        continue
                else:
                    if long_strike <= short_strike:
                        continue

                long_mid = self._mid_price(long_data)
                short_mid = self._mid_price(short_data)
                if long_mid is None or short_mid is None:
                    continue

                if spread_type == SpreadType.BULL_CALL:
                    net_debit = long_mid - short_mid
                    spread_width = short_strike - long_strike
                else:
                    net_debit = long_mid - short_mid
                    spread_width = long_strike - short_strike

                max_profit = (spread_width - net_debit) * 100
                max_loss = net_debit * 100
                if max_loss <= 0 or max_profit <= 0:
                    continue

                long_leg = OptionLeg(
                    symbol=symbol, expiry=expiry, strike=long_strike, right=right,
                    action="BUY", delta=long_data.get("delta"),
                    open_interest=long_data.get("open_interest"),
                    bid=long_data.get("bid"), ask=long_data.get("ask"),
                )
                short_leg = OptionLeg(
                    symbol=symbol, expiry=expiry, strike=short_strike, right=right,
                    action="SELL", delta=short_data.get("delta"),
                    open_interest=short_data.get("open_interest"),
                    bid=short_data.get("bid"), ask=short_data.get("ask"),
                )
                candidate = SpreadCandidate(
                    symbol=symbol, spread_type=spread_type,
                    long_leg=long_leg, short_leg=short_leg,
                    max_profit=max_profit, max_loss=max_loss,
                    net_debit=net_debit, dte=self._calc_dte(expiry), signal=signal,
                )
                if best is None or candidate.risk_reward_ratio > best.risk_reward_ratio:
                    best = candidate
        return best

    # ------------------------------------------------------------------ #
    # Iron Condor                                                          #
    # ------------------------------------------------------------------ #

    async def find_iron_condor(
        self, symbol: str, price: float
    ) -> SpreadCandidate | None:
        """Find an iron condor: sell OTM put spread + sell OTM call spread."""
        chains = await self.market_data.get_option_chains(symbol)
        if not chains:
            return None

        expiries = self._filter_expiries(chains)
        if not expiries:
            return None

        chain_strikes = self._get_chain_strikes(chains)
        expiries.sort(key=lambda e: abs(self._calc_dte(e) - 30))
        for expiry in expiries:
            candidate = await self._build_iron_condor_for_expiry(
                symbol, expiry, price, chain_strikes=chain_strikes,
            )
            if candidate:
                return candidate

        logger.info("No valid iron condor found for %s", symbol)
        return None

    async def _build_iron_condor_for_expiry(
        self, symbol: str, expiry: str, price: float,
        chain_strikes: set[float] | None = None,
    ) -> SpreadCandidate | None:
        strike_range = self._get_strike_range(price, self.config.spread_width, chain_strikes)

        put_contracts = await self.market_data.get_option_chain_data(symbol, expiry, "P", strike_range)
        call_contracts = await self.market_data.get_option_chain_data(symbol, expiry, "C", strike_range)
        if len(put_contracts) < 2 or len(call_contracts) < 2:
            return None

        put_greeks = await self.market_data.get_option_greeks(put_contracts)
        call_greeks = await self.market_data.get_option_greeks(call_contracts)
        if not put_greeks or not call_greeks:
            return None

        # Identify short-leg candidates (delta in [short_min, short_max]) on each side
        short_put_candidates = [
            (s, d) for s, d in put_greeks.items()
            if self._passes_liquidity_filter(d)
            and d.get("delta") is not None
            and self.config.delta_short_min <= abs(d["delta"]) <= self.config.delta_short_max
            and s < price
        ]
        short_call_candidates = [
            (s, d) for s, d in call_greeks.items()
            if self._passes_liquidity_filter(d)
            and d.get("delta") is not None
            and self.config.delta_short_min <= d["delta"] <= self.config.delta_short_max
            and s > price
        ]

        if not short_put_candidates or not short_call_candidates:
            return None

        spread_width = self.config.spread_width
        best: SpreadCandidate | None = None

        for s_put_strike, s_put_data in short_put_candidates:
            l_put_strike = s_put_strike - spread_width
            l_put_data = put_greeks.get(l_put_strike) or self._nearest_below(
                put_greeks, s_put_strike - spread_width * 0.5
            )
            if not l_put_data:
                continue
            l_put_strike = self._find_strike_for_data(put_greeks, l_put_data)

            for s_call_strike, s_call_data in short_call_candidates:
                l_call_strike = s_call_strike + spread_width
                l_call_data = call_greeks.get(l_call_strike) or self._nearest_above(
                    call_greeks, s_call_strike + spread_width * 0.5
                )
                if not l_call_data:
                    continue
                l_call_strike = self._find_strike_for_data(call_greeks, l_call_data)

                mids = [
                    self._mid_price(s_put_data),
                    self._mid_price(l_put_data),
                    self._mid_price(s_call_data),
                    self._mid_price(l_call_data),
                ]
                if any(m is None for m in mids):
                    continue
                s_put_mid, l_put_mid, s_call_mid, l_call_mid = mids

                net_credit = s_put_mid + s_call_mid - l_put_mid - l_call_mid
                if net_credit <= 0:
                    continue

                put_width = s_put_strike - l_put_strike
                call_width = l_call_strike - s_call_strike
                max_loss = (min(put_width, call_width) - net_credit) * 100
                if max_loss <= 0:
                    continue

                long_put = OptionLeg(
                    symbol=symbol, expiry=expiry, strike=l_put_strike, right="P",
                    action="BUY", delta=l_put_data.get("delta"),
                    open_interest=l_put_data.get("open_interest"),
                    bid=l_put_data.get("bid"), ask=l_put_data.get("ask"),
                )
                short_put = OptionLeg(
                    symbol=symbol, expiry=expiry, strike=s_put_strike, right="P",
                    action="SELL", delta=s_put_data.get("delta"),
                    open_interest=s_put_data.get("open_interest"),
                    bid=s_put_data.get("bid"), ask=s_put_data.get("ask"),
                )
                short_call = OptionLeg(
                    symbol=symbol, expiry=expiry, strike=s_call_strike, right="C",
                    action="SELL", delta=s_call_data.get("delta"),
                    open_interest=s_call_data.get("open_interest"),
                    bid=s_call_data.get("bid"), ask=s_call_data.get("ask"),
                )
                long_call = OptionLeg(
                    symbol=symbol, expiry=expiry, strike=l_call_strike, right="C",
                    action="BUY", delta=l_call_data.get("delta"),
                    open_interest=l_call_data.get("open_interest"),
                    bid=l_call_data.get("bid"), ask=l_call_data.get("ask"),
                )

                # long_leg = outermost put (lowest strike)
                # short_leg = outermost call (highest strike)
                # extra_legs = inner short put + inner short call
                candidate = SpreadCandidate(
                    symbol=symbol,
                    spread_type=SpreadType.IRON_CONDOR,
                    long_leg=long_put,
                    short_leg=long_call,
                    extra_legs=[short_put, short_call],
                    max_profit=net_credit * 100,
                    max_loss=max_loss,
                    net_debit=-net_credit,      # negative = credit received
                    dte=self._calc_dte(expiry),
                    signal=Signal.NEUTRAL,
                )
                if best is None or candidate.risk_reward_ratio > best.risk_reward_ratio:
                    best = candidate

        return best

    # ------------------------------------------------------------------ #
    # Butterfly (symmetric and broken-wing)                               #
    # ------------------------------------------------------------------ #

    async def find_butterfly(
        self, symbol: str, signal: Signal, price: float, broken_wing: bool = False
    ) -> SpreadCandidate | None:
        """Find a long butterfly spread (or broken-wing variant)."""
        chains = await self.market_data.get_option_chains(symbol)
        if not chains:
            return None

        expiries = self._filter_expiries(chains)
        if not expiries:
            return None

        right = "C" if signal == Signal.BULLISH else "P"
        chain_strikes = self._get_chain_strikes(chains)
        expiries.sort(key=lambda e: abs(self._calc_dte(e) - 30))
        for expiry in expiries:
            candidate = await self._build_butterfly_for_expiry(
                symbol, expiry, right, signal, price, broken_wing=broken_wing,
                chain_strikes=chain_strikes,
            )
            if candidate:
                return candidate

        label = "broken-wing butterfly" if broken_wing else "butterfly"
        logger.info("No valid %s found for %s", label, symbol)
        return None

    async def find_broken_wing_butterfly(
        self, symbol: str, signal: Signal, price: float
    ) -> SpreadCandidate | None:
        """Find a broken-wing butterfly (wider far wing → reduced debit or small credit)."""
        return await self.find_butterfly(symbol, signal, price, broken_wing=True)

    async def _build_butterfly_for_expiry(
        self,
        symbol: str,
        expiry: str,
        right: str,
        signal: Signal,
        price: float,
        broken_wing: bool = False,
        chain_strikes: set[float] | None = None,
    ) -> SpreadCandidate | None:
        w = self.config.butterfly_wing_width

        # Body (sold 2×) is the ATM strike, rounded to wing_width grid
        atm = self._snap_to_chain(round(price / w) * w, chain_strikes) if chain_strikes else round(price / w) * w

        # Wings: symmetric for normal butterfly, asymmetric for broken-wing
        if broken_wing:
            # Widen the far wing in the direction of the signal to reduce cost
            lower = atm - w
            upper = atm + (w * 1.5 if signal == Signal.BULLISH else w)
            lower = atm - (w * 1.5 if signal == Signal.BEARISH else w)
        else:
            lower = atm - w
            upper = atm + w

        if chain_strikes:
            lower = self._snap_to_chain(lower, chain_strikes)
            upper = self._snap_to_chain(upper, chain_strikes)

        strikes = sorted({lower, atm, upper})
        if len(strikes) < 3:
            return None

        contracts = await self.market_data.get_option_chain_data(symbol, expiry, right, strikes)
        if len(contracts) < 3:
            return None

        greeks = await self.market_data.get_option_greeks(contracts)
        lower_data = greeks.get(lower)
        mid_data = greeks.get(atm)
        upper_data = greeks.get(upper)

        if not lower_data or not mid_data or not upper_data:
            return None
        if not all(self._passes_liquidity_filter(d) for d in [lower_data, mid_data, upper_data]):
            return None

        lower_mid = self._mid_price(lower_data)
        mid_mid = self._mid_price(mid_data)
        upper_mid = self._mid_price(upper_data)
        if any(x is None for x in [lower_mid, mid_mid, upper_mid]):
            return None

        # Net debit: buy lower + upper, sell 2× middle
        net_debit = lower_mid + upper_mid - 2 * mid_mid
        lower_wing = atm - lower
        max_profit = (lower_wing - net_debit) * 100   # profit if price pins at body
        max_loss = abs(net_debit) * 100               # cost of the spread

        if max_profit <= 0:
            return None

        spread_type = SpreadType.BROKEN_WING_BUTTERFLY if broken_wing else SpreadType.BUTTERFLY

        lower_leg = OptionLeg(
            symbol=symbol, expiry=expiry, strike=lower, right=right,
            action="BUY", ratio=1, delta=lower_data.get("delta"),
            open_interest=lower_data.get("open_interest"),
            bid=lower_data.get("bid"), ask=lower_data.get("ask"),
        )
        body_leg = OptionLeg(
            symbol=symbol, expiry=expiry, strike=atm, right=right,
            action="SELL", ratio=2, delta=mid_data.get("delta"),
            open_interest=mid_data.get("open_interest"),
            bid=mid_data.get("bid"), ask=mid_data.get("ask"),
        )
        upper_leg = OptionLeg(
            symbol=symbol, expiry=expiry, strike=upper, right=right,
            action="BUY", ratio=1, delta=upper_data.get("delta"),
            open_interest=upper_data.get("open_interest"),
            bid=upper_data.get("bid"), ask=upper_data.get("ask"),
        )

        # long_leg = lower wing (lowest strike)
        # short_leg = upper wing (highest strike)
        # extra_legs = [body] (the sold 2× middle)
        return SpreadCandidate(
            symbol=symbol,
            spread_type=spread_type,
            long_leg=lower_leg,
            short_leg=upper_leg,
            extra_legs=[body_leg],
            max_profit=max_profit,
            max_loss=max_loss,
            net_debit=net_debit,
            dte=self._calc_dte(expiry),
            signal=signal,
        )

    # ------------------------------------------------------------------ #
    # Calendar spread                                                      #
    # ------------------------------------------------------------------ #

    async def find_calendar_spread(
        self, symbol: str, price: float
    ) -> SpreadCandidate | None:
        """Find a calendar spread: sell near-term ATM, buy far-term ATM (same strike)."""
        chains = await self.market_data.get_option_chains(symbol)
        if not chains:
            return None

        near_expiry = self._find_target_expiry(chains, self.config.calendar_dte_near)
        far_expiry = self._find_target_expiry(chains, self.config.calendar_dte_far)

        if not near_expiry or not far_expiry or near_expiry == far_expiry:
            logger.warning("Could not find suitable near/far expiries for calendar on %s", symbol)
            return None

        # ATM strike rounded to spread_width grid, snapped to real chain strikes
        w = self.config.spread_width
        chain_strikes = self._get_chain_strikes(chains)
        atm = self._snap_to_chain(round(price / w) * w, chain_strikes) if chain_strikes else round(price / w) * w

        near_contracts = await self.market_data.get_option_chain_data(symbol, near_expiry, "C", [atm])
        far_contracts = await self.market_data.get_option_chain_data(symbol, far_expiry, "C", [atm])
        if not near_contracts or not far_contracts:
            return None

        near_greeks = await self.market_data.get_option_greeks(near_contracts)
        far_greeks = await self.market_data.get_option_greeks(far_contracts)

        near_data = near_greeks.get(atm)
        far_data = far_greeks.get(atm)
        if not near_data or not far_data:
            return None

        near_mid = self._mid_price(near_data)
        far_mid = self._mid_price(far_data)
        if near_mid is None or far_mid is None:
            return None

        net_debit = far_mid - near_mid   # always positive (far option costs more)
        if net_debit <= 0:
            return None

        # Conservative estimate: max profit ≈ near premium collected if near expires worthless
        max_profit = near_mid * 100
        max_loss = net_debit * 100

        long_leg = OptionLeg(
            symbol=symbol, expiry=far_expiry, strike=atm, right="C",
            action="BUY", delta=far_data.get("delta"),
            open_interest=far_data.get("open_interest"),
            bid=far_data.get("bid"), ask=far_data.get("ask"),
        )
        short_leg = OptionLeg(
            symbol=symbol, expiry=near_expiry, strike=atm, right="C",
            action="SELL", delta=near_data.get("delta"),
            open_interest=near_data.get("open_interest"),
            bid=near_data.get("bid"), ask=near_data.get("ask"),
        )

        return SpreadCandidate(
            symbol=symbol,
            spread_type=SpreadType.CALENDAR,
            long_leg=long_leg,    # far expiry (BUY)
            short_leg=short_leg,  # near expiry (SELL)
            max_profit=max_profit,
            max_loss=max_loss,
            net_debit=net_debit,
            dte=self._calc_dte(far_expiry),
            signal=Signal.NEUTRAL,
        )

    # ------------------------------------------------------------------ #
    # BAG contract construction                                            #
    # ------------------------------------------------------------------ #

    def build_bag_contract(self, candidate: SpreadCandidate) -> Contract:
        """Build a bare IB BAG contract (legs need qualifying before use)."""
        contract = Contract()
        contract.symbol = candidate.symbol
        contract.secType = "BAG"
        contract.currency = "USD"
        contract.exchange = "SMART"

        combo_legs = []
        for leg in candidate.all_legs:
            cl = ComboLeg()
            cl.conId = 0
            cl.ratio = leg.ratio
            cl.action = leg.action
            cl.exchange = "SMART"
            combo_legs.append(cl)

        contract.comboLegs = combo_legs
        return contract

    async def build_qualified_bag(self, candidate: SpreadCandidate) -> Contract | None:
        """Build and qualify a BAG contract with proper conIds for all legs."""
        all_legs = candidate.all_legs

        ib_options = [
            IBOption(leg.symbol, leg.expiry, leg.strike, leg.right, "SMART")
            for leg in all_legs
        ]
        qualified = await self.ib.qualifyContractsAsync(*ib_options)

        if len(qualified) < len(all_legs) or any(c.conId == 0 for c in qualified):
            logger.warning("Could not qualify all option contracts for %s spread", candidate.spread_type.value)
            return None

        contract = Contract()
        contract.symbol = candidate.symbol
        contract.secType = "BAG"
        contract.currency = "USD"
        contract.exchange = "SMART"

        combo_legs = []
        for leg, qualified_opt in zip(all_legs, qualified):
            cl = ComboLeg()
            cl.conId = qualified_opt.conId
            cl.ratio = leg.ratio
            cl.action = leg.action
            cl.exchange = "SMART"
            combo_legs.append(cl)

        contract.comboLegs = combo_legs
        return contract

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _filter_expiries(self, chains: list) -> list[str]:
        """Return expiries within the configured DTE range."""
        today = datetime.now().date()
        min_date = today + timedelta(days=self.config.dte_min)
        max_date = today + timedelta(days=self.config.dte_max)
        valid: set[str] = set()
        for chain in chains:
            for exp in chain.expirations:
                exp_date = datetime.strptime(exp, "%Y%m%d").date()
                if min_date <= exp_date <= max_date:
                    valid.add(exp)
        return sorted(valid)

    def _find_target_expiry(
        self, chains: list, target_dte: int, tolerance: int = 7
    ) -> str | None:
        """Find the expiry closest to target_dte within ±tolerance days."""
        today = datetime.now().date()
        min_date = today + timedelta(days=target_dte - tolerance)
        max_date = today + timedelta(days=target_dte + tolerance)
        valid: set[str] = set()
        for chain in chains:
            for exp in chain.expirations:
                exp_date = datetime.strptime(exp, "%Y%m%d").date()
                if min_date <= exp_date <= max_date:
                    valid.add(exp)
        if not valid:
            return None
        return min(valid, key=lambda e: abs(self._calc_dte(e) - target_dte))

    def _calc_dte(self, expiry: str) -> int:
        exp_date = datetime.strptime(expiry, "%Y%m%d").date()
        return (exp_date - datetime.now().date()).days

    def _get_chain_strikes(self, chains: list) -> set[float]:
        """Extract all valid strikes from option chain definitions."""
        all_strikes: set[float] = set()
        for chain in chains:
            all_strikes.update(chain.strikes)
        return all_strikes

    def _snap_to_chain(self, target: float, chain_strikes: set[float]) -> float:
        """Snap a target strike to the nearest valid chain strike."""
        if not chain_strikes:
            return target
        return min(chain_strikes, key=lambda s: abs(s - target))

    def _get_strike_range(
        self, price: float, width: float, chain_strikes: set[float] | None = None,
    ) -> list[float]:
        if chain_strikes:
            # Use actual strikes from the chain, filtered to a reasonable range around price
            low = price - 10 * width
            high = price + 10 * width
            return sorted(s for s in chain_strikes if low <= s <= high)

        # Fallback: generate synthetic strikes
        base = round(price)
        strikes: list[float] = []
        for i in range(-10, 11):
            s = base + i * width
            if s > 0:
                strikes.append(s)
        for i in range(-5, 6):
            s = base + i
            if s > 0 and s not in strikes:
                strikes.append(s)
        return sorted(strikes)

    def _passes_liquidity_filter(self, data: dict) -> bool:
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

    def _mid_price(self, data: dict) -> float | None:
        bid = data.get("bid")
        ask = data.get("ask")
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2
        return None

    def _nearest_below(self, greeks: dict[float, dict], threshold: float) -> dict | None:
        """Return data for the highest strike strictly below threshold."""
        candidates = [(s, d) for s, d in greeks.items() if s < threshold]
        if not candidates:
            return None
        return max(candidates, key=lambda x: x[0])[1]

    def _nearest_above(self, greeks: dict[float, dict], threshold: float) -> dict | None:
        """Return data for the lowest strike strictly above threshold."""
        candidates = [(s, d) for s, d in greeks.items() if s > threshold]
        if not candidates:
            return None
        return min(candidates, key=lambda x: x[0])[1]

    def _find_strike_for_data(self, greeks: dict[float, dict], target_data: dict) -> float:
        """Reverse-lookup: find the strike key for a given data dict."""
        for strike, data in greeks.items():
            if data is target_data:
                return strike
        return 0.0
