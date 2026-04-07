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
        self._reconnecting = False
        self._upstream_ready = True  # False when IB Gateway loses IBKR upstream

    async def connect(self) -> IB:
        """Connect to IB Gateway with exponential backoff retry."""
        # Ensure any stale connection state is cleared before connecting
        if self.ib.isConnected():
            self.ib.disconnect()
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
                self.ib.errorEvent += self._on_error
                self._upstream_ready = True
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

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract):
        """Track IB Gateway upstream connectivity via error codes."""
        if errorCode == 1100:
            # Gateway lost connection to IBKR servers
            self._upstream_ready = False
            logger.warning("IB Gateway lost upstream connectivity (error 1100)")
        elif errorCode in (1101, 1102):
            # 1101: connectivity restored, data lost
            # 1102: connectivity restored, data maintained
            self._upstream_ready = True
            logger.info("IB Gateway upstream connectivity restored (error %d)", errorCode)

    def _on_disconnect(self):
        """Handle unexpected disconnection by scheduling reconnect."""
        if self._reconnecting:
            logger.debug("Reconnect already in progress, skipping duplicate")
            return
        logger.warning("Disconnected from IB Gateway, scheduling reconnect...")
        asyncio.ensure_future(self._reconnect())

    async def _reconnect(self):
        """Attempt to reconnect after disconnection."""
        if self._reconnecting:
            return
        self._reconnecting = True
        try:
            # Wait for the gateway to fully release the old client session
            logger.info("Waiting 10s for gateway to release old session...")
            await asyncio.sleep(10)
            # Remove handler before reconnect to avoid re-entry during connect attempts
            self.ib.disconnectedEvent -= self._on_disconnect
            await self.connect()
        except ConnectionError:
            logger.error("Reconnection failed after all retries")
        finally:
            self._reconnecting = False

    async def disconnect(self):
        """Gracefully disconnect from IB Gateway."""
        if self.ib.isConnected():
            self.ib.disconnectedEvent -= self._on_disconnect
            self.ib.errorEvent -= self._on_error
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway")

    @property
    def is_connected(self) -> bool:
        """True only when both the local socket and IBKR upstream are live."""
        return self.ib.isConnected() and self._upstream_ready
