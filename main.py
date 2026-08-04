#!/usr/bin/env python3
"""Point d'entree du scanner Binance Futures.

Exemples:
    python main.py --once                      # un seul passage, puis sortie
    python main.py --tf 5m --min-score 3       # boucle continue, filtre serre
    python main.py --symbols BTCUSDT,ETHUSDT   # liste imposee
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from bot.config import Config, load_dotenv
from bot.core.ranking import format_table
from bot.core.scanner import Scanner
from bot.exchange.binance_futures import BinanceError, BinanceFuturesClient
from bot.execution.base import OrderRouter
from bot.execution.paper import PaperRouter
from bot.notify.base import Notifier
from bot.notify.console import ConsoleNotifier
from bot.notify.telegram import TelegramNotifier

log = logging.getLogger("scanner")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scanner de signaux Binance USD-M Futures")
    parser.add_argument("--once", action="store_true", help="un seul cycle puis sortie")
    parser.add_argument("--tf", "--interval", dest="interval", help="unite de temps (ex: 5m, 1h)")
    parser.add_argument("--symbols", help="liste de symboles separes par des virgules")
    parser.add_argument("--min-score", type=int, help="score minimal d'un signal")
    parser.add_argument("--top", type=int, help="limiter aux N symboles les plus liquides")
    parser.add_argument(
        "--rank",
        nargs="?",
        type=int,
        const=10,
        help="afficher le classement des N paires les plus interessantes (defaut 10)",
    )
    parser.add_argument("--every", type=int, help="secondes entre deux cycles")
    parser.add_argument("--no-telegram", action="store_true", help="desactiver Telegram")
    parser.add_argument("--verbose", "-v", action="store_true", help="logs de debug")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    load_dotenv()
    cfg = Config.from_env()
    if args.interval:
        cfg.interval = args.interval
    if args.symbols:
        cfg.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.min_score is not None:
        cfg.min_score = args.min_score
    if args.top is not None:
        cfg.max_symbols = args.top
    if args.every is not None:
        cfg.scan_interval_seconds = args.every
    if args.no_telegram:
        cfg.telegram_bot_token = ""
    return cfg


def build_notifiers(cfg: Config) -> list[Notifier]:
    notifiers: list[Notifier] = [ConsoleNotifier()]
    if cfg.telegram_enabled:
        notifiers.append(TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id))
        log.info("Alertes Telegram activees.")
    else:
        log.info("Telegram inactif (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents).")
    return notifiers


async def build_router(cfg: Config, scanner: Scanner) -> OrderRouter | None:
    """Aucun routeur si le trading est desactive; papier tant que DRY_RUN est vrai."""
    if not cfg.trading_enabled:
        return None
    await scanner.universe()  # remplit scanner.filters
    if cfg.dry_run:
        equity = cfg.account_equity_usdt or 1000.0
        log.info("Execution en mode PAPIER (capital simule: %.2f USDT).", equity)
        return PaperRouter(scanner.filters, equity, cfg.risk_per_trade_pct, cfg.leverage)

    from bot.execution.live import LiveRouter  # importe seulement si vraiment necessaire

    log.warning("EXECUTION REELLE ACTIVE - des ordres seront passes avec de l'argent reel.")
    return LiveRouter(cfg, scanner.client, scanner.filters)


async def run(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    problems = cfg.validate()
    if problems:
        for problem in problems:
            log.error("Configuration: %s", problem)
        return 2

    notifiers = build_notifiers(cfg)
    async with BinanceFuturesClient(cfg) as client:
        scanner = Scanner(cfg, client, notifiers)
        try:
            await client.ping()
        except BinanceError as exc:
            log.error("Binance Futures injoignable: %s", exc)
            return 1

        scanner.rank_display = args.rank or 0
        scanner.router = await build_router(cfg, scanner)
        try:
            if args.once:
                result = await scanner.scan_once()
                log.info(
                    "%d symboles analyses en %.1fs - %d signal(aux)",
                    result.scanned,
                    result.duration,
                    len(result.signals),
                )
                if args.rank:
                    print(f"\n{format_table(result.ranking, args.rank)}\n")
                await scanner.dispatch(result.signals)
                if not result.signals:
                    print("Aucun signal sur ce cycle.")
            else:
                await scanner.run_forever()
        finally:
            for notifier in notifiers:
                await notifier.close()
            if scanner.router is not None:
                await scanner.router.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx journalise chaque requete en INFO: illisible quand on scanne 300 symboles.
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nArret demande, a bientot.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
