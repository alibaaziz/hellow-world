"""Configuration du bot, lue depuis les variables d'environnement / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Charge un fichier .env minimaliste sans ecraser l'environnement existant."""
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_str(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env_str(key, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # --- Marche scanne ---
    base_url: str = "https://fapi.binance.com"
    quote_asset: str = "USDT"
    interval: str = "15m"
    candles: int = 200
    symbols: list[str] = field(default_factory=list)  # vide = tout l'univers USDT perp
    max_symbols: int = 0  # 0 = pas de limite
    min_quote_volume_24h: float = 20_000_000.0  # filtre de liquidite (USDT sur 24h)

    # --- Boucle de scan ---
    scan_interval_seconds: int = 60
    concurrency: int = 8
    request_timeout: float = 15.0
    universe_refresh_minutes: int = 30

    # --- Indicateurs ---
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14

    # --- Filtrage des signaux ---
    min_score: int = 2
    cooldown_minutes: int = 60
    atr_stop_multiplier: float = 1.5
    risk_reward: float = 2.0

    # --- Alertes ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Execution (desactivee par defaut) ---
    trading_enabled: bool = False
    dry_run: bool = True
    binance_api_key: str = ""
    binance_api_secret: str = ""
    leverage: int = 3
    risk_per_trade_pct: float = 1.0  # % du capital risque entre l'entree et le stop
    account_equity_usdt: float = 0.0  # 0 = lire le solde reel via l'API

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @classmethod
    def from_env(cls) -> "Config":
        raw_symbols = _env_str("SYMBOLS", "")
        symbols = [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
        return cls(
            base_url=_env_str("BINANCE_FUTURES_BASE_URL", cls.base_url),
            quote_asset=_env_str("QUOTE_ASSET", cls.quote_asset).upper(),
            interval=_env_str("INTERVAL", cls.interval),
            candles=_env_int("CANDLES", cls.candles),
            symbols=symbols,
            max_symbols=_env_int("MAX_SYMBOLS", cls.max_symbols),
            min_quote_volume_24h=_env_float("MIN_QUOTE_VOLUME_24H", cls.min_quote_volume_24h),
            scan_interval_seconds=_env_int("SCAN_INTERVAL_SECONDS", cls.scan_interval_seconds),
            concurrency=_env_int("CONCURRENCY", cls.concurrency),
            request_timeout=_env_float("REQUEST_TIMEOUT", cls.request_timeout),
            universe_refresh_minutes=_env_int(
                "UNIVERSE_REFRESH_MINUTES", cls.universe_refresh_minutes
            ),
            rsi_period=_env_int("RSI_PERIOD", cls.rsi_period),
            rsi_oversold=_env_float("RSI_OVERSOLD", cls.rsi_oversold),
            rsi_overbought=_env_float("RSI_OVERBOUGHT", cls.rsi_overbought),
            ema_fast=_env_int("EMA_FAST", cls.ema_fast),
            ema_slow=_env_int("EMA_SLOW", cls.ema_slow),
            ema_trend=_env_int("EMA_TREND", cls.ema_trend),
            bb_period=_env_int("BB_PERIOD", cls.bb_period),
            bb_std=_env_float("BB_STD", cls.bb_std),
            atr_period=_env_int("ATR_PERIOD", cls.atr_period),
            min_score=_env_int("MIN_SCORE", cls.min_score),
            cooldown_minutes=_env_int("COOLDOWN_MINUTES", cls.cooldown_minutes),
            atr_stop_multiplier=_env_float("ATR_STOP_MULTIPLIER", cls.atr_stop_multiplier),
            risk_reward=_env_float("RISK_REWARD", cls.risk_reward),
            telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_env_str("TELEGRAM_CHAT_ID", ""),
            trading_enabled=_env_bool("TRADING_ENABLED", cls.trading_enabled),
            dry_run=_env_bool("DRY_RUN", cls.dry_run),
            binance_api_key=_env_str("BINANCE_API_KEY", ""),
            binance_api_secret=_env_str("BINANCE_API_SECRET", ""),
            leverage=_env_int("LEVERAGE", cls.leverage),
            risk_per_trade_pct=_env_float("RISK_PER_TRADE_PCT", cls.risk_per_trade_pct),
            account_equity_usdt=_env_float("ACCOUNT_EQUITY_USDT", cls.account_equity_usdt),
        )

    def validate(self) -> list[str]:
        """Retourne la liste des problemes de configuration (vide si tout va bien)."""
        problems: list[str] = []
        if self.candles < self.min_required_candles:
            problems.append(
                f"CANDLES={self.candles} est trop bas: il en faut au moins "
                f"{self.min_required_candles} pour les indicateurs configures."
            )
        if self.ema_fast >= self.ema_slow:
            problems.append("EMA_FAST doit etre strictement inferieur a EMA_SLOW.")
        if self.concurrency < 1:
            problems.append("CONCURRENCY doit etre >= 1.")
        if self.scan_interval_seconds < 5:
            problems.append("SCAN_INTERVAL_SECONDS < 5 risque de declencher un ban IP (429).")
        if self.trading_enabled and not self.dry_run:
            if not (self.binance_api_key and self.binance_api_secret):
                problems.append(
                    "Trading reel active mais BINANCE_API_KEY / BINANCE_API_SECRET manquent."
                )
            if self.risk_per_trade_pct <= 0 or self.risk_per_trade_pct > 5:
                problems.append("RISK_PER_TRADE_PCT doit etre dans ]0, 5] en trading reel.")
        return problems

    @property
    def min_required_candles(self) -> int:
        """Nombre minimal de bougies fermees pour calculer tous les indicateurs."""
        return max(self.ema_trend, self.bb_period, self.rsi_period + 1, self.atr_period + 1) + 2
