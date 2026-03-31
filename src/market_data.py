"""Market data fetching: prices, historical bars, option chains, greeks."""

import asyncio
import logging
from datetime import datetime

import pandas as pd
from ib_async import IB, Contract, Option, Stock, util

from src.config_loader import StrategyConfig

logger = logging.getLogger(__name__)


class MarketData:
    """Fetches market data from IB Gateway."""

    def __init__(self, ib: IB, config: StrategyConfig):
        self.ib = ib
        self.config = config

    async def get_stock_contract(self, symbol: str) -> Stock:
        """Create and qualify a stock contract."""
        contract = Stock(symbol, "SMART", "USD")
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError(f"Could not qualify contract for {symbol}")
        return qualified[0]

    async def get_price(self, contract: Contract) -> float | None:
        """Get current market price for a contract."""
        ticker = self.ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(2)  # Allow time for data
        self.ib.cancelMktData(contract)

        price = ticker.marketPrice()
        if price != price:  # NaN check
            price = ticker.close
        return price if price == price else None

    async def get_historical_bars(
        self,
        contract: Contract,
        duration: str = "90 D",
        bar_size: str = "1 day",
    ) -> pd.DataFrame:
        """Fetch historical OHLCV bars as a DataFrame."""
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if not bars:
            return pd.DataFrame()

        df = util.df(bars)
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
        return df

    async def get_option_chains(self, symbol: str) -> list[dict]:
        """Get available option chain definitions for a symbol."""
        contract = await self.get_stock_contract(symbol)
        chains = await self.ib.reqSecDefOptParamsAsync(
            contract.symbol, "", contract.secType, contract.conId
        )
        # Filter for SMART exchange
        return [c for c in chains if c.exchange == "SMART"]

    async def get_option_chain_data(
        self,
        symbol: str,
        expiry: str,
        right: str,
        strikes: list[float],
    ) -> list[Option]:
        """Get qualified option contracts for specific strikes."""
        contracts = [
            Option(symbol, expiry, strike, right, "SMART")
            for strike in strikes
        ]
        qualified = await self.ib.qualifyContractsAsync(*contracts)
        return [c for c in qualified if c.conId > 0]

    async def get_option_greeks(
        self, contracts: list[Option]
    ) -> dict[float, dict]:
        """Request market data for options and extract greeks.

        Returns dict keyed by strike with delta, gamma, theta, vega, bid, ask, oi.
        """
        tickers = []
        for contract in contracts:
            ticker = self.ib.reqMktData(contract, "100", False, False)
            tickers.append((contract, ticker))

        await asyncio.sleep(3)  # Allow greeks to populate

        results = {}
        for contract, ticker in tickers:
            self.ib.cancelMktData(contract)
            greeks = ticker.modelGreeks or ticker.lastGreeks
            results[contract.strike] = {
                "delta": greeks.delta if greeks else None,
                "gamma": greeks.gamma if greeks else None,
                "theta": greeks.theta if greeks else None,
                "vega": greeks.vega if greeks else None,
                "bid": ticker.bid if ticker.bid != -1 else None,
                "ask": ticker.ask if ticker.ask != -1 else None,
                "open_interest": (
                    ticker.callOpenInterest
                    if contract.right == "C"
                    else ticker.putOpenInterest
                ),
            }

        return results

    async def get_historical_iv(
        self,
        contract: Contract,
        duration: str = "1 Y",
    ) -> pd.Series:
        """Fetch historical implied volatility as a daily time series."""
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow="OPTION_IMPLIED_VOLATILITY",
            useRTH=True,
            formatDate=1,
        )
        if not bars:
            return pd.Series(dtype=float)
        df = util.df(bars)
        df["date"] = pd.to_datetime(df["date"])
        return pd.Series(df["close"].values, index=df["date"])

    async def get_account_value(self) -> float:
        """Get net liquidation value of the account."""
        account_values = self.ib.accountValues()
        for av in account_values:
            if av.tag == "NetLiquidation" and av.currency == "USD":
                return float(av.value)
        raise ValueError("Could not retrieve account net liquidation value")
