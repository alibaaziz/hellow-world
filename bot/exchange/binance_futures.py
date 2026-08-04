"""Client asynchrone pour l'API Binance USD-M Futures (fapi).

Les endpoints de marche sont publics (aucune cle requise). Les endpoints signes
(solde, ordres) ne sont utilises que par la couche d'execution.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import Config
from ..models import Candle, Series

log = logging.getLogger(__name__)

RETRY_STATUS = {418, 429, 500, 502, 503, 504}
MAX_RETRIES = 4


class BinanceError(RuntimeError):
    """Erreur renvoyee par l'API Binance."""


class BinanceFuturesClient:
    def __init__(self, cfg: Config, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=cfg.base_url,
            timeout=cfg.request_timeout,
            headers={"User-Agent": "binance-futures-scanner/1.0"},
        )
        self._semaphore = asyncio.Semaphore(cfg.concurrency)

    async def __aenter__(self) -> "BinanceFuturesClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ HTTP

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
    ) -> Any:
        params = dict(params or {})
        headers: dict[str, str] = {}

        if signed:
            if not (self.cfg.binance_api_key and self.cfg.binance_api_secret):
                raise BinanceError("Endpoint signe appele sans cle API configuree.")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            query = urlencode(params)
            params["signature"] = hmac.new(
                self.cfg.binance_api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            headers["X-MBX-APIKEY"] = self.cfg.binance_api_key

        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self._semaphore:
                    response = await self._client.request(
                        method, path, params=params, headers=headers
                    )
            except httpx.HTTPError as exc:  # reseau instable
                last_error = exc
                log.warning("%s %s: erreur reseau (%s), tentative %d", method, path, exc, attempt)
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code in RETRY_STATUS:
                    last_error = BinanceError(f"HTTP {response.status_code}: {response.text[:200]}")
                    retry_after = float(response.headers.get("Retry-After", 0) or 0)
                    log.warning(
                        "%s %s: HTTP %d, nouvelle tentative dans %.1fs",
                        method,
                        path,
                        response.status_code,
                        max(delay, retry_after),
                    )
                    delay = max(delay, retry_after)
                else:
                    raise BinanceError(f"HTTP {response.status_code}: {response.text[:300]}")

            if attempt < MAX_RETRIES:
                await asyncio.sleep(delay)
                delay *= 2
        raise BinanceError(f"{method} {path} a echoue apres {MAX_RETRIES} tentatives") from last_error

    # ---------------------------------------------------------- Marche public

    async def ping(self) -> None:
        await self._request("GET", "/fapi/v1/ping")

    async def exchange_info(self) -> dict[str, Any]:
        return await self._request("GET", "/fapi/v1/exchangeInfo")

    async def perpetual_symbols(self) -> list[dict[str, Any]]:
        """Contrats perpetuels en cours de negociation pour l'actif de cotation."""
        info = await self.exchange_info()
        return [
            s
            for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s.get("quoteAsset") == self.cfg.quote_asset
        ]

    async def ticker_24h(self) -> dict[str, dict[str, Any]]:
        """Statistiques 24h indexees par symbole."""
        rows = await self._request("GET", "/fapi/v1/ticker/24hr")
        return {row["symbol"]: row for row in rows}

    async def klines(self, symbol: str, interval: str, limit: int) -> Series:
        """Bougies FERMEES uniquement: la derniere bougie en cours est ecartee."""
        rows = await self._request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": min(limit + 1, 1500)},
        )
        candles = [Candle.from_binance(row) for row in rows]
        now_ms = int(time.time() * 1000)
        if candles and candles[-1].close_time >= now_ms:
            candles.pop()
        return Series(symbol=symbol, interval=interval, candles=candles[-limit:])

    # -------------------------------------------------------- Endpoints signes

    async def account_balance_usdt(self) -> float:
        rows = await self._request("GET", "/fapi/v2/balance", signed=True)
        for row in rows:
            if row.get("asset") == self.cfg.quote_asset:
                return float(row.get("balance", 0.0))
        return 0.0

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return await self._request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True
        )

    async def new_order(self, **params: Any) -> dict[str, Any]:
        return await self._request("POST", "/fapi/v1/order", params, signed=True)
