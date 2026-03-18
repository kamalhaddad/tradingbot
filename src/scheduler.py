"""Market-hours-aware job scheduling."""

import logging
from datetime import datetime, time

import pytz

from src.config_loader import ScheduleConfig

logger = logging.getLogger(__name__)


class Scheduler:
    """Determines if market is open and manages scan/exit check timing."""

    def __init__(self, config: ScheduleConfig):
        self.config = config
        self.tz = pytz.timezone(config.timezone)
        self._parse_times()

    def _parse_times(self):
        """Parse market open/close times from config strings."""
        oh, om = map(int, self.config.market_open.split(":"))
        ch, cm = map(int, self.config.market_close.split(":"))
        self.market_open = time(oh, om)
        self.market_close = time(ch, cm)

    def is_market_open(self) -> bool:
        """Check if current time is within market hours (weekday + time range)."""
        now = datetime.now(self.tz)
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        current_time = now.time()
        return self.market_open <= current_time <= self.market_close

    def can_enter_trades(self) -> bool:
        """Check if current time is within the entry window.

        Entry window starts after market_open + offset and ends before
        market_close - offset.
        """
        if not self.is_market_open():
            return False

        now = datetime.now(self.tz)
        current_minutes = now.hour * 60 + now.minute

        open_minutes = self.market_open.hour * 60 + self.market_open.minute
        close_minutes = self.market_close.hour * 60 + self.market_close.minute

        entry_start = open_minutes + self.config.entry_start_offset_minutes
        entry_stop = close_minutes - self.config.entry_stop_offset_minutes

        return entry_start <= current_minutes <= entry_stop

    def seconds_until_market_open(self) -> float:
        """Calculate seconds until next market open. Returns 0 if market is open."""
        if self.is_market_open():
            return 0.0

        now = datetime.now(self.tz)
        # Find next weekday
        target = now.replace(
            hour=self.market_open.hour,
            minute=self.market_open.minute,
            second=0,
            microsecond=0,
        )
        if target <= now:
            target = target.replace(day=target.day + 1)
        while target.weekday() >= 5:
            target = target.replace(day=target.day + 1)

        return (target - now).total_seconds()
