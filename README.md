# GridLab

Laboratoire de backtest et d'analyse de risque pour les **bots grid sur Binance
USDⓈ-M Futures**.

La plupart des simulateurs de grille comptent les cycles gagnants et s'arrêtent
là. GridLab compte aussi ce qui les annule :

```
PnL net = PnL réalisé − frais − funding + position ouverte
```

Cette dernière ligne est celle qui transforme un « +8% ce mois-ci » en drawdown
quand le prix quitte le range. Le rapport la met au même niveau que les gains.

Exemple réel de sortie — une grille qui a l'air très rentable :

```
Cycles fermes    : 7700  (profit brut des cycles +476.13 USDT)
PnL realise      : +466.42 USDT
Frais            : -259.68 USDT      <- 55% du brut
Funding          : -40.16 USDT
Position ouverte : -107.94 USDT      <- le sac accumulé
----------------------------------------------
PnL net          : +58.64 USDT  (+5.86%)
```

Sans dépendances : uniquement la bibliothèque standard Python (3.9+). Aucune
clé API — seuls les endpoints publics sont utilisés, rien ne peut toucher à
votre compte.

## Installation

```bash
git clone https://github.com/alibaaziz/hellow-world.git
cd hellow-world
python3 -m unittest discover -s tests    # 41 tests, ~0.1 s
```

## Démarrage rapide

```bash
# 1. Démo hors ligne, aucun réseau requis
python3 -m gridlab demo

# 2. Télécharger de vraies données (bougies + funding réalisé)
python3 -m gridlab fetch --symbol BTCUSDT --interval 1h --days 180 --funding

# 3. Backtester une grille
python3 -m gridlab backtest \
    --data data/BTCUSDT_1h.csv \
    --funding-file data/BTCUSDT_1h_funding.csv \
    --lower 55000 --upper 70000 --grids 40 \
    --direction long --investment 1000 --leverage 3 \
    --report reports/btc.html

# 4. Balayer les paramètres
python3 -m gridlab optimize --data data/BTCUSDT_1h.csv \
    --widths 0.05,0.10,0.20 --grid-counts 20,40,80 --leverages 1,2,3,5

# 5. Calculs de risque, sans backtest
python3 -m gridlab risk --lower 55000 --upper 70000 --grids 40 \
    --entry 62000 --qty 0.05 --balance 800
```

## Les commandes

### `fetch` — données de marché

Télécharge les bougies futures et, avec `--funding`, l'historique **réalisé**
des taux de funding. Affiche aussi les percentiles de prix et l'ATR, utiles
pour choisir un range.

### `backtest` — simuler une grille

Reconstruit le carnet d'ordres barre par barre et calcule le PnL décomposé.
En plus du résultat, il vérifie deux choses que les interfaces de bots
n'affichent pas :

- le **pas de grille comparé au seuil de rentabilité des frais** — un pas trop
  serré fait travailler le bot pour Binance ;
- le **levier maximal qui évite la liquidation à l'intérieur même du range**,
  comparé à celui que vous avez choisi.

Options utiles : `--auto-range 0.9` déduit les bornes des données (90% des
clôtures), `--close-on-exit` ferme la position à la sortie du range,
`--direction long|short|neutral`, `--mode arithmetic|geometric`.

### `optimize` — balayage de paramètres

Croise largeur de range × nombre de grilles × levier, puis classe les
résultats. Le score pénalise le drawdown et place **toute configuration
liquidée en dessous de n'importe quelle configuration survivante** : une
liquidation n'est pas un mauvais rendement, c'est la fin du compte.

Le chiffre à regarder n'est pas la meilleure ligne, c'est le taux de
liquidation et le rendement médian du balayage — combien de réglages *voisins*
du vôtre survivent.

### `risk` — calculs sans simulation

Prix de liquidation, levier maximal pour survivre à un prix donné, pas de
grille conseillé selon vos frais, et le taux de funding qui annulerait
exactement vos gains de grille.

## Ce que le modèle fait, et ne fait pas

**Modélisé :**

- Remplissages sur ordres limites au taux **maker**, position initiale et
  sorties forcées au taux **taker**.
- Funding aux règlements réels (00:00 / 08:00 / 16:00 UTC), à partir de
  l'historique téléchargé ou d'un taux constant.
- Liquidation en marge isolée, mode one-way, selon la formule de marge de
  maintenance de Binance.
- Grilles long / short / neutre, arithmétiques ou géométriques.
- Position d'amorçage : une grille long doit acheter au marché l'inventaire que
  chaque vente au-dessus du prix de départ va écouler.

**Non modélisé — et ça compte :**

- **Slippage et rejets d'ordres.** Sur un mouvement violent, tous les niveaux
  ne se remplissent pas au prix affiché.
- **Chemin intra-bougie.** Le moteur suppose ouverture → bas → haut → clôture
  sur une bougie haussière, et l'inverse sur une baissière. Les bougies qui
  balaient plusieurs fois dans les deux sens produisent en réalité **plus** de
  cycles que simulé — utilisez des bougies courtes (1m, 5m) pour réduire cette
  approximation.
- **Paliers de marge de maintenance.** Un `mmr` unique est utilisé
  (0,4% par défaut, palier 1 BTCUSDT). Sur gros notionnel, réglez `--mmr`.
- **Frais dégressifs, BNB, remises VIP.**

Un backtest optimiste par construction reste optimiste. Traitez les résultats
comme une borne haute, pas comme une prévision.

## En bibliothèque

```python
from gridlab import GridConfig, backtest, write_report
from gridlab.data import load_csv

bars = load_csv("data/BTCUSDT_1h.csv")
config = GridConfig(lower=55_000, upper=70_000, n_grids=40,
                    direction="long", investment=1000, leverage=3)

result = backtest(bars, config)
print(result.summary())
print(result.metrics["cycle_profit_gross"], result.metrics["unrealized_pnl"])
write_report(result, "reports/btc.html")
```

## Structure

| Fichier | Rôle |
|---|---|
| `gridlab/grid.py` | Géométrie des niveaux, seuil de rentabilité des frais |
| `gridlab/engine.py` | Moteur de simulation, comptabilité de position, funding, liquidation |
| `gridlab/risk.py` | Marge, prix de liquidation, levier maximal |
| `gridlab/data.py` | Téléchargement Binance, cache CSV, générateur synthétique |
| `gridlab/optimize.py` | Balayage de paramètres et robustesse |
| `gridlab/report.py` | Rapport HTML autonome (SVG écrit à la main) |
| `gridlab/cli.py` | Interface en ligne de commande |
| `tests/` | 41 tests unitaires |

## Avertissement

Outil d'analyse sur données historiques. Ce n'est pas un conseil en
investissement, et rien ici ne prédit un prix. Le trading de futures avec
levier peut faire perdre plus que le capital engagé.
