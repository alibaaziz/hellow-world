"""Alertes sur la sortie standard, avec couleurs si le terminal les supporte."""

from __future__ import annotations

import os
import sys

from ..models import Side, Signal
from .base import Notifier, format_signal

GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, color: bool | None = None) -> None:
        if color is None:
            color = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
        self.color = color

    async def send(self, signal: Signal) -> None:
        body = format_signal(signal)
        if self.color:
            tint = GREEN if signal.side is Side.LONG else RED
            body = f"{tint}{BOLD}{body}{RESET}"
        print(f"\n{'=' * 60}\n{body}\n{'=' * 60}", flush=True)
