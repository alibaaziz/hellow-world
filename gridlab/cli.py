"""Command line interface: fetch, backtest, optimize, risk, demo."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from . import data as data_mod
from .engine import GridConfig, backtest
from .grid import breakeven_step_pct, grid_step_pct, suggest_grids
from .optimize import format_table, robustness, sweep
from .report import write_report
from .risk import (
    funding_breakeven_rate,
    grid_max_leverage,
    liquidation_distance_pct,
    liquidation_price,
    max_safe_leverage,
)


def _floats(text: str) -> List[float]:
    return [float(x) for x in text.replace(" ", "").split(",") if x]


def _ints(text: str) -> List[int]:
    return [int(x) for x in text.replace(" ", "").split(",") if x]


def _add_grid_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lower", type=float, help="borne basse de la grille")
    parser.add_argument("--upper", type=float, help="borne haute de la grille")
    parser.add_argument("--auto-range", type=float, metavar="FRACTION",
                        help="deduire les bornes des donnees (ex: 0.9 = 90%% des cloture)")
    parser.add_argument("--grids", type=int, default=30, help="nombre de grilles")
    parser.add_argument("--mode", choices=["arithmetic", "geometric"], default="arithmetic")
    parser.add_argument("--direction", choices=["long", "short", "neutral"], default="long")
    parser.add_argument("--investment", type=float, default=1000.0, help="marge en USDT")
    parser.add_argument("--leverage", type=float, default=3.0)
    parser.add_argument("--maker-fee", type=float, default=0.0002)
    parser.add_argument("--taker-fee", type=float, default=0.0004)
    parser.add_argument("--qty-mode", choices=["equal_quote", "equal_base"],
                        default="equal_quote")
    parser.add_argument("--mmr", type=float, default=0.004,
                        help="taux de marge de maintenance")
    parser.add_argument("--funding-rate", type=float, default=0.0001,
                        help="taux de funding 8h utilise si aucun historique n'est fourni")
    parser.add_argument("--close-on-exit", action="store_true",
                        help="fermer la position si le prix sort du range")


def _config_from_args(args: argparse.Namespace, bars: Sequence[Sequence[float]]) -> GridConfig:
    lower, upper = args.lower, args.upper
    if args.auto_range:
        lower, upper = data_mod.auto_range(bars, args.auto_range)
    if lower is None or upper is None:
        raise SystemExit("Precisez --lower et --upper, ou utilisez --auto-range 0.9")
    return GridConfig(
        lower=lower, upper=upper, n_grids=args.grids, mode=args.mode,
        direction=args.direction, investment=args.investment, leverage=args.leverage,
        maker_fee=args.maker_fee, taker_fee=args.taker_fee, qty_mode=args.qty_mode,
        mmr=args.mmr, funding_rate=args.funding_rate, close_on_exit=args.close_on_exit,
    )


def _load_bars(args: argparse.Namespace) -> List[data_mod.Bar]:
    if getattr(args, "synthetic", False):
        return data_mod.synthetic_bars(n=args.synthetic, seed=getattr(args, "seed", 7))
    return data_mod.load_csv(args.data)


# ------------------------------------------------------------------ commands

def cmd_fetch(args: argparse.Namespace) -> int:
    print(f"Telechargement {args.symbol} {args.interval}, {args.days} jours...")
    bars = data_mod.fetch_klines(args.symbol, args.interval, args.days)
    out = args.out or f"data/{args.symbol.upper()}_{args.interval}.csv"
    data_mod.save_csv(out, bars)
    print(f"  {len(bars)} bougies -> {out}")

    if args.funding:
        rates = data_mod.fetch_funding(args.symbol, args.days)
        funding_out = out.rsplit(".", 1)[0] + "_funding.csv"
        data_mod.save_funding_csv(funding_out, rates)
        total = sum(r for _, r in rates)
        print(f"  {len(rates)} taux de funding -> {funding_out}")
        print(f"  funding cumule sur la periode: {total * 100:+.3f}% du notionnel")

    stats = data_mod.price_stats(bars)
    print(f"\n  min {stats['min']:.2f} | p05 {stats['p05']:.2f} | median "
          f"{stats['median']:.2f} | p95 {stats['p95']:.2f} | max {stats['max']:.2f}")
    print(f"  ATR(14) {stats['atr_pct']:.2f}% | volatilite par bougie "
          f"{stats['vol_per_bar_pct']:.2f}%")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    bars = _load_bars(args)
    config = _config_from_args(args, bars)
    funding = data_mod.load_funding_csv(args.funding_file) if args.funding_file else None

    result = backtest(bars, config, funding)
    print(result.summary())

    min_step = breakeven_step_pct(config.maker_fee)
    step = result.metrics["step_pct"]
    print(f"\nPas de grille {step:.3f}% vs seuil de rentabilite {min_step:.3f}% "
          f"({step / min_step:.1f}x)")
    if step < min_step * 2:
        print("  ATTENTION: pas trop serre, les frais mangent la majorite des cycles.")

    max_lev = grid_max_leverage(config.lower, config.upper, config.direction, mmr=config.mmr)
    print(f"Levier max pour ne pas liquider dans le range: x{max_lev:.1f} "
          f"(actuel x{config.leverage:g})")
    if config.leverage > max_lev:
        print("  ATTENTION: ce levier peut liquider a l'interieur meme de votre grille.")

    if args.report:
        path = write_report(result, args.report, args.title)
        print(f"\nRapport HTML: {path}")
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    bars = _load_bars(args)
    base = GridConfig(
        lower=1.0, upper=2.0, n_grids=10, mode=args.mode, direction=args.direction,
        investment=args.investment, maker_fee=args.maker_fee, taker_fee=args.taker_fee,
        qty_mode=args.qty_mode, mmr=args.mmr, funding_rate=args.funding_rate,
        close_on_exit=args.close_on_exit,
    )
    funding = data_mod.load_funding_csv(args.funding_file) if args.funding_file else None

    points = sweep(bars, base, widths=_floats(args.widths), grid_counts=_ints(args.grid_counts),
                   leverages=_floats(args.leverages), funding=funding)
    print(format_table(points, args.top))

    stats = robustness(points)
    print(f"\n{stats['combinations']} combinaisons | "
          f"{stats['profitable_pct']:.0f}% profitables | "
          f"{stats['liquidation_rate_pct']:.0f}% liquidees | "
          f"retour median {stats['median_return_pct']:+.2f}%")
    print("Un jeu de parametres optimal sur l'historique ne l'est pas sur le futur.\n"
          "Regardez la robustesse: combien de configurations voisines survivent.")

    if args.report and points:
        best = points[0]
        config = GridConfig(
            lower=best.lower, upper=best.upper, n_grids=best.n_grids, mode=args.mode,
            direction=args.direction, investment=args.investment, leverage=best.leverage,
            maker_fee=args.maker_fee, taker_fee=args.taker_fee, qty_mode=args.qty_mode,
            mmr=args.mmr, funding_rate=args.funding_rate, close_on_exit=args.close_on_exit,
        )
        path = write_report(backtest(bars, config, funding), args.report,
                            "GridLab - meilleure configuration")
        print(f"\nRapport HTML de la meilleure config: {path}")
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    if args.lower and args.upper:
        print(f"Grille {args.lower:.2f} - {args.upper:.2f} ({args.direction})")
        for buffer_pct in (0.0, 20.0, 50.0):
            lev = grid_max_leverage(args.lower, args.upper, args.direction,
                                    buffer_pct, args.mmr)
            label = "sans marge" if buffer_pct == 0 else f"marge {buffer_pct:.0f}%"
            print(f"  levier max ({label:>12}) : x{lev:.2f}")
        step = grid_step_pct(args.lower, args.upper, args.grids)
        print(f"\n  {args.grids} grilles -> pas {step:.3f}% "
              f"(seuil frais {breakeven_step_pct(args.maker_fee):.3f}%)")
        print(f"  grilles conseillees (3x les frais) : "
              f"{suggest_grids(args.lower, args.upper, args.maker_fee)}")
        rate = funding_breakeven_rate(step, args.maker_fee, args.cycles_per_day)
        print(f"  funding neutralisant le gain a {args.cycles_per_day:g} cycles/jour : "
              f"{rate * 100:.4f}% / 8h")

    if args.entry and args.qty:
        side = "short" if args.direction == "short" else "long"
        liq = liquidation_price(args.entry, args.qty, args.balance, side, args.mmr)
        dist = liquidation_distance_pct(args.entry, args.qty, args.balance, side, args.mmr)
        notional = args.entry * args.qty
        print(f"\nPosition {side} {args.qty:g} @ {args.entry:.2f} "
              f"(notionnel {notional:.2f}, marge {args.balance:.2f}, "
              f"levier effectif x{notional / args.balance:.2f})")
        if liq is not None and liq > 0:
            print(f"  liquidation : {liq:.2f}  ({dist:.2f}% du prix d'entree)")
        else:
            print("  liquidation : hors de portee, la marge couvre une chute a zero")

    if args.stop:
        entry = args.entry or ((args.lower + args.upper) / 2 if args.lower else None)
        if entry:
            lev = max_safe_leverage(entry, args.stop, args.direction, args.buffer, args.mmr)
            print(f"\nLevier max pour survivre a {args.stop:.2f} depuis {entry:.2f} "
                  f"(marge {args.buffer:.0f}%) : x{lev:.2f}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    print("Demo hors ligne sur donnees synthetiques (aucun appel reseau).\n")
    bars = data_mod.synthetic_bars(n=args.bars, seed=args.seed)
    lower, upper = data_mod.auto_range(bars, 0.9)
    config = GridConfig(lower=lower, upper=upper, n_grids=40, direction="long",
                        investment=1000.0, leverage=3.0, close_on_exit=False)
    result = backtest(bars, config)
    print(result.summary())
    path = write_report(result, args.report, "GridLab - demo")
    print(f"\nRapport HTML: {path}")
    print(
        "\nATTENTION: ces bougies sont generees par un processus a retour a la\n"
        "moyenne. C'est exactement le marche qu'une grille adore, et ce chiffre\n"
        "n'a aucune valeur predictive. Utilisez 'fetch' puis 'backtest' sur de\n"
        "vraies donnees avant de conclure quoi que ce soit."
    )
    return 0


# --------------------------------------------------------------------- entry

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridlab",
        description="Laboratoire de backtest pour bots grid sur Binance Futures.",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    fetch = subs.add_parser("fetch", help="telecharger les bougies et le funding")
    fetch.add_argument("--symbol", required=True, help="ex: BTCUSDT")
    fetch.add_argument("--interval", default="1h", choices=sorted(data_mod.INTERVAL_MS))
    fetch.add_argument("--days", type=int, default=90)
    fetch.add_argument("--out", help="fichier CSV de sortie")
    fetch.add_argument("--funding", action="store_true",
                       help="telecharger aussi l'historique de funding")
    fetch.set_defaults(func=cmd_fetch)

    bt = subs.add_parser("backtest", help="simuler une grille")
    bt.add_argument("--data", help="CSV de bougies")
    bt.add_argument("--synthetic", type=int, metavar="N",
                    help="utiliser N bougies synthetiques au lieu d'un CSV")
    bt.add_argument("--seed", type=int, default=7)
    bt.add_argument("--funding-file", help="CSV de funding")
    bt.add_argument("--report", help="ecrire un rapport HTML")
    bt.add_argument("--title", default="GridLab")
    _add_grid_args(bt)
    bt.set_defaults(func=cmd_backtest)

    opt = subs.add_parser("optimize", help="balayer les parametres")
    opt.add_argument("--data")
    opt.add_argument("--synthetic", type=int, metavar="N")
    opt.add_argument("--seed", type=int, default=7)
    opt.add_argument("--funding-file")
    opt.add_argument("--widths", default="0.05,0.10,0.15,0.25",
                     help="demi-largeurs de range, en fraction du prix")
    opt.add_argument("--grid-counts", default="10,20,40,80")
    opt.add_argument("--leverages", default="1,2,3,5")
    opt.add_argument("--top", type=int, default=20)
    opt.add_argument("--report")
    _add_grid_args(opt)
    opt.set_defaults(func=cmd_optimize)

    risk = subs.add_parser("risk", help="liquidation, levier max, seuils de frais")
    risk.add_argument("--lower", type=float)
    risk.add_argument("--upper", type=float)
    risk.add_argument("--grids", type=int, default=30)
    risk.add_argument("--entry", type=float)
    risk.add_argument("--qty", type=float)
    risk.add_argument("--balance", type=float, default=1000.0)
    risk.add_argument("--stop", type=float, help="prix a survivre")
    risk.add_argument("--buffer", type=float, default=20.0, help="marge de securite en %%")
    risk.add_argument("--direction", choices=["long", "short"], default="long")
    risk.add_argument("--mmr", type=float, default=0.004)
    risk.add_argument("--maker-fee", type=float, default=0.0002)
    risk.add_argument("--cycles-per-day", type=float, default=3.0)
    risk.set_defaults(func=cmd_risk)

    demo = subs.add_parser("demo", help="exemple complet hors ligne")
    demo.add_argument("--bars", type=int, default=2000)
    demo.add_argument("--seed", type=int, default=7)
    demo.add_argument("--report", default="reports/demo.html")
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except data_mod.DataError as exc:
        print(f"Erreur de donnees: {exc}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError) as exc:
        print(f"Erreur: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
