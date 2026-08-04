"""Alertes Telegram via l'API Bot."""

from __future__ import annotations

import logging

import httpx

from ..models import Side, Signal
from .base import Notifier, format_signal

log = logging.getLogger(__name__)


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0) -> None:
        if not (bot_token and chat_id):
            raise ValueError("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID sont requis.")
        self.chat_id = chat_id
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{bot_token}", timeout=timeout
        )

    async def send(self, signal: Signal) -> None:
        emoji = "\U0001f7e2" if signal.side is Side.LONG else "\U0001f534"
        text = f"{emoji} <b>Signal Binance Futures</b>\n<pre>{format_signal(signal)}</pre>"
        await self.send_text(text)

    async def send_text(self, text: str) -> None:
        """Envoie un message brut (HTML). N'echoue jamais bruyamment."""
        try:
            response = await self._client.post(
                "/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if response.status_code != 200:
                log.error("Telegram a repondu %d: %s", response.status_code, response.text[:200])
        except httpx.HTTPError as exc:
            log.error("Envoi Telegram impossible: %s", exc)

    async def close(self) -> None:
        await self._client.aclose()
