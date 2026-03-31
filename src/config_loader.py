"""Configuration loading: YAML with ${ENV_VAR} substitution + Pydantic validation."""

import os
import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class IBConfig(BaseModel):
    host: str = "ib-gateway"
    trading_mode: str = "paper"
    client_id: int = 1
    timeout: int = 30

    @property
    def port(self) -> int:
        # gnzsnz/ib-gateway uses socat to relay: paper=4004, live=4003
        return 4004 if self.trading_mode == "paper" else 4003


class StrategyConfig(BaseModel):
    watchlist: list[str] = Field(default_factory=lambda: ["SPY", "QQQ", "IWM"])
    dte_min: int = 14
    dte_max: int = 45
    delta_long_min: float = 0.30
    delta_long_max: float = 0.50
    delta_short_min: float = 0.15
    delta_short_max: float = 0.30
    spread_width: float = 5.0
    min_open_interest: int = 200
    max_bid_ask_spread_pct: float = 0.10
    scan_interval_minutes: int = 5
    exit_check_interval_minutes: int = 1
    iv_rank_high_threshold: float = 0.50   # IV rank above this → high-IV strategies (iron condor, butterfly)
    iv_rank_low_threshold: float = 0.25    # IV rank below this → low-IV strategies (calendar)
    calendar_dte_near: int = 14            # Target DTE for the short leg of a calendar spread
    calendar_dte_far: int = 45             # Target DTE for the long leg of a calendar spread
    butterfly_wing_width: float = 5.0     # Distance (in $) from body to each wing in butterfly spreads


class IndicatorsConfig(BaseModel):
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    sma_fast: int = 20
    sma_slow: int = 50
    bb_period: int = 20
    bb_std: float = 2.0


class RiskConfig(BaseModel):
    max_risk_per_trade_pct: float = 0.02
    max_concurrent_positions: int = 10
    max_positions_per_symbol: int = 3
    daily_loss_limit_pct: float = 0.03
    monthly_loss_limit_pct: float = 0.08
    profit_target_pct: float = 0.50
    dte_exit_threshold: int = 21
    stop_loss_pct: float = 1.0


class ScheduleConfig(BaseModel):
    market_open: str = "09:30"
    market_close: str = "16:00"
    timezone: str = "US/Eastern"
    entry_start_offset_minutes: int = 15
    entry_stop_offset_minutes: int = 30


class LoggingConfig(BaseModel):
    level: str = "INFO"
    trade_journal: str = "/app/data/trades.csv"


class AppConfig(BaseModel):
    ib: IBConfig = Field(default_factory=IBConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    indicators: IndicatorsConfig = Field(default_factory=IndicatorsConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


def _substitute_env_vars(text: str) -> str:
    """Replace ${ENV_VAR} placeholders with environment variable values."""
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is None:
            raise ValueError(f"Environment variable '{var_name}' is not set")
        return value
    return _ENV_PATTERN.sub(replacer, text)


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load and validate configuration from YAML file.

    Looks for config.yaml in: provided path, ./config.yaml, /app/config.yaml.
    Substitutes ${ENV_VAR} placeholders before parsing.
    """
    if path:
        config_path = Path(path)
    else:
        candidates = [Path("config.yaml"), Path("/app/config.yaml")]
        config_path = next((p for p in candidates if p.exists()), None)
        if config_path is None:
            raise FileNotFoundError("No config.yaml found")

    raw = config_path.read_text()
    substituted = _substitute_env_vars(raw)
    data = yaml.safe_load(substituted) or {}
    return AppConfig(**data)
