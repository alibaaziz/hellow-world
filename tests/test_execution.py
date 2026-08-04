import asyncio

import pytest

from bot.execution.base import (
    InsufficientSize,
    SymbolFilters,
    build_plan,
    quantity_precision,
)
from bot.execution.paper import PaperRouter
from bot.models import Side, Signal

FILTERS = SymbolFilters(
    symbol="BTCUSDT", tick_size=0.1, step_size=0.001, min_qty=0.001, min_notional=5.0
)


def signal(side: Side = Side.LONG, price: float = 50_000.0, stop: float = 49_000.0) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        side=side,
        interval="15m",
        price=price,
        score=3,
        reasons=["test"],
        stop_loss=stop,
        take_profit=price + 2 * (price - stop) if side is Side.LONG else price - 2 * (stop - price),
        atr=500.0,
    )


def test_filtres_lus_depuis_exchange_info():
    entry = {
        "symbol": "ETHUSDT",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "20"},
        ],
    }
    filters = SymbolFilters.from_exchange_info(entry)
    assert filters.tick_size == 0.01
    assert filters.step_size == 0.001
    assert filters.min_notional == 20.0


def test_arrondis_sans_bruit_binaire():
    filters = SymbolFilters("X", tick_size=0.01, step_size=0.1, min_qty=0.1, min_notional=0)
    assert filters.round_price(1.239) == pytest.approx(1.23)
    assert filters.round_qty(3.29) == pytest.approx(3.2)
    # L'arrondi va toujours vers le bas: jamais de depassement de taille.
    assert filters.round_qty(0.99) == pytest.approx(0.9)


def test_dimensionnement_respecte_le_risque():
    """1% de 10 000 USDT = 100 USDT risques sur une distance de 1 000 USDT -> 0.1 BTC."""
    plan = build_plan(signal(), equity_usdt=10_000, risk_pct=1.0, leverage=3, filters=FILTERS)
    assert plan.quantity == pytest.approx(0.1)
    assert plan.risk_usdt == pytest.approx(100.0)
    assert plan.notional == pytest.approx(5_000.0)
    assert plan.entry_side == "BUY"
    assert plan.exit_side == "SELL"


def test_dimensionnement_short():
    plan = build_plan(
        signal(Side.SHORT, price=50_000, stop=51_000),
        equity_usdt=10_000,
        risk_pct=1.0,
        leverage=5,
        filters=FILTERS,
    )
    assert plan.entry_side == "SELL"
    assert plan.exit_side == "BUY"
    assert plan.risk_usdt == pytest.approx(100.0)


def test_risque_plus_faible_donne_une_position_plus_petite():
    gros = build_plan(signal(), 10_000, 2.0, 3, FILTERS)
    petit = build_plan(signal(), 10_000, 0.5, 3, FILTERS)
    assert petit.quantity < gros.quantity


def test_capital_insuffisant_refuse_l_ordre():
    with pytest.raises(InsufficientSize):
        build_plan(signal(), equity_usdt=1.0, risk_pct=1.0, leverage=1, filters=FILTERS)


def test_marge_requise_superieure_au_capital_refusee():
    """Levier 1 et risque eleve: le notionnel depasse le capital disponible."""
    with pytest.raises(InsufficientSize):
        build_plan(
            signal(price=50_000, stop=49_900),  # stop tres serre -> position enorme
            equity_usdt=1_000,
            risk_pct=5.0,
            leverage=1,
            filters=FILTERS,
        )


def test_notionnel_minimum_respecte():
    filters = SymbolFilters("X", tick_size=0.1, step_size=0.001, min_qty=0.001, min_notional=100.0)
    with pytest.raises(InsufficientSize):
        build_plan(signal(), equity_usdt=100, risk_pct=0.1, leverage=3, filters=filters)


def test_signal_sans_stop_est_rejete():
    sig = signal()
    sig.stop_loss = None
    with pytest.raises(ValueError):
        build_plan(sig, 10_000, 1.0, 3, FILTERS)


def test_capital_nul_est_rejete():
    with pytest.raises(InsufficientSize):
        build_plan(signal(), equity_usdt=0.0, risk_pct=1.0, leverage=3, filters=FILTERS)


def test_precision_quantite():
    assert quantity_precision(0.001) == 3
    assert quantity_precision(1.0) == 0
    assert quantity_precision(0.1) == 1


def test_routeur_papier_enregistre_le_plan():
    router = PaperRouter({"BTCUSDT": FILTERS}, equity_usdt=10_000, risk_pct=1.0, leverage=3)
    plan = asyncio.run(router.submit(signal()))
    assert plan is not None
    assert router.plans == [plan]
    assert "BUY" in plan.describe()


def test_routeur_papier_ignore_un_symbole_inconnu():
    router = PaperRouter({}, equity_usdt=10_000, risk_pct=1.0, leverage=3)
    assert asyncio.run(router.submit(signal())) is None


def test_routeur_papier_ignore_un_capital_insuffisant():
    router = PaperRouter({"BTCUSDT": FILTERS}, equity_usdt=1.0, risk_pct=1.0, leverage=3)
    assert asyncio.run(router.submit(signal())) is None
    assert router.plans == []
