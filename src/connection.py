"""IB Gateway connection management with retry/backoff."""

import asyncio
import logging

from ib_async import IB

from src.config_loader import IBConfig

logger = logging.getLogger(__name__)


class IBConnection:
    """Manages connection to IB Gateway with automatic reconnection."""

    def __init__(self, config: IBConfig):
        self.config = config
        self.ib = IB()
        self._max_retries = 10
        self._base_delay = 5

    async def connect(self) -> IB:
        """Connect to IB Gateway with exponential backoff retry."""
        for attempt in range(1, self._max_retries + 1):
            try:
                await self.ib.connectAsync(
                    host=self.config.host,
                    port=self.config.port,
                    clientId=self.config.client_id,
                    timeout=self.config.timeout,
                )
                logger.info(
                    "Connected to IB Gateway at %s:%s (client %s)",
                    self.config.host,
                    self.config.port,
                    self.config.client_id,
                )
                self.ib.disconnectedEvent += self._on_disconnect
                return self.ib
            except Exception as e:
                delay = min(self._base_delay * (2 ** (attempt - 1)), 120)
                logger.warning(
                    "Connection attempt %d/%d failed: %s. Retrying in %ds...",
                    attempt,
                    self._max_retries,
                    e,
                    delay,
                )
                if attempt == self._max_retries:
                    raise ConnectionError(
                        f"Failed to connect after {self._max_retries} attempts"
                    ) from e
                await asyncio.sleep(delay)

    def _on_disconnect(self):
        """Handle unexpected disconnection by scheduling reconnect."""
        logger.warning("Disconnected from IB Gateway, scheduling reconnect...")
        asyncio.ensure_future(self._reconnect())

    async def _reconnect(self):
        """Attempt to reconnect after disconnection."""
        await asyncio.sleep(self._base_delay)
        try:
            await self.connect()
        except ConnectionError:
            logger.error("Reconnection failed after all retries")

    async def disconnect(self):
        """Gracefully disconnect from IB Gateway."""
        if self.ib.isConnected():
            self.ib.disconnectedEvent -= self._on_disconnect
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway")

    @property
    def is_connected(self) -> bool:
        return self.ib.isConnected()
