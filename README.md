# Scanner Binance Futures

Bot de scan qui surveille les contrats perpetuels USD-M de Binance, detecte des
setups techniques et envoie une alerte (console + Telegram). L'execution
d'ordres est prevue et deja cablee, mais desactivee par defaut.

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env      # puis ajustez les valeurs
```

Python 3.11+ requis. La seule dependance est `httpx` : les indicateurs sont
ecrits en Python pur (ni pandas, ni numpy).

## Utilisation

```bash
python main.py                              # boucle continue, 15m, toutes les 60s
python main.py --once                       # un seul passage puis sortie
python main.py --tf 5m --min-score 3        # unite de temps 5m, filtre serre
python main.py --symbols BTCUSDT,ETHUSDT    # liste imposee
python main.py --top 50                     # les 50 symboles les plus liquides
python main.py -v                           # logs detailles (requetes HTTP)
```

Les donnees de marche viennent d'endpoints **publics** : aucune cle API n'est
necessaire pour scanner.

## Ce que le bot detecte

Trois regles peuvent **declencher** un signal :

| Regle | LONG | SHORT |
|---|---|---|
| Croisement EMA | EMA9 passe au-dessus de EMA21 | EMA9 passe sous EMA21 |
| Retournement RSI | RSI sort de la survente (< 30) | RSI sort du surachat (> 70) |
| Cassure Bollinger | cloture au-dessus de la bande haute | cloture sous la bande basse |

Une quatrieme regle **confirme** sans jamais declencher seule : la position du
prix par rapport a l'EMA50 (filtre de tendance).

Le **score** d'un signal est le nombre de regles qui vont dans le meme sens.
Seuls les scores `>= MIN_SCORE` (2 par defaut) sont alertes, et il faut toujours
au moins un vrai declencheur. Si LONG et SHORT sont a egalite, le bot s'abstient.

Points importants :

- seules les bougies **fermees** sont analysees ; la bougie en cours est ecartee
  pour eviter les signaux qui disparaissent ;
- chaque signal porte un stop base sur l'ATR (`ATR_STOP_MULTIPLIER`) et un
  objectif derive de `RISK_REWARD` ;
- `COOLDOWN_MINUTES` empeche de realerter sur le meme symbole/sens.

## Alertes

La console est toujours active. Pour ajouter Telegram, renseignez dans `.env` :

```
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321
```

Le token s'obtient aupres de [@BotFather](https://t.me/BotFather) ; pour le
`chat_id`, ecrivez un message a votre bot puis ouvrez
`https://api.telegram.org/bot<TOKEN>/getUpdates`.
Une alerte Telegram en echec est journalisee mais n'interrompt jamais le scan.

## Execution des ordres

Elle est desactivee par defaut (`TRADING_ENABLED=false`). Trois etats :

| `TRADING_ENABLED` | `DRY_RUN` | Comportement |
|---|---|---|
| `false` | — | alertes uniquement (defaut) |
| `true` | `true` | mode papier : la taille de position est calculee et journalisee |
| `true` | `false` | **ordres reels** : cles API requises |

Le dimensionnement risque `RISK_PER_TRADE_PCT` % du capital entre l'entree et le
stop, puis arrondit vers le bas selon les filtres du symbole (`stepSize`,
`tickSize`, `minNotional`). Un ordre est refuse si la marge requise depasse le
capital ou si les minimums ne sont pas atteints.

En mode reel, chaque entree MARKET est suivie d'un `STOP_MARKET` et d'un
`TAKE_PROFIT_MARKET` en `closePosition`. Si le stop ne peut pas etre place, une
erreur explicite est journalisee : **la position est alors non protegee et doit
etre verifiee a la main.**

> Avant tout passage en reel, testez sur le testnet en pointant
> `BINANCE_FUTURES_BASE_URL=https://testnet.binancefuture.com`.

## Configuration

Toutes les options passent par `.env` ou par variables d'environnement — voir
`.env.example`, chaque entree est commentee. La configuration est validee au
demarrage (historique suffisant, coherence EMA rapide/lente, cles presentes en
mode reel) et le bot refuse de demarrer si quelque chose cloche.

## Architecture

```
bot/
  config.py                  configuration + validation
  models.py                  Candle, Series, Signal, Side
  exchange/binance_futures.py client async, retry + backoff, endpoints signes
  core/indicators.py         SMA, EMA, RSI, Bollinger, ATR (Python pur)
  core/signals.py            regles de detection -> Signal
  core/scanner.py            univers, boucle de scan, cooldown, dispatch
  notify/                    console, Telegram
  execution/                 dimensionnement, routeur papier, routeur reel
main.py                      CLI
```

Ajouter une regle : ecrire une fonction `(Snapshot, Config) -> (Side, str) | None`
dans `core/signals.py` et l'ajouter a `TRIGGER_RULES` ou `CONFIRM_RULES`.

## Backtest

`tools/backtest.py` rejoue un historique bougie par bougie en appelant la **meme**
fonction `evaluate()` que le scanner en direct, puis simule chaque trade sur les
bougies suivantes : stop ou objectif touche en premier.

```bash
python tools/backtest.py donnees.csv --min-score 3
python tools/backtest.py klines.json --max-bars 50 --fee-pct 0.08
python tools/backtest.py export.csv --reverse   # fichier du plus recent au plus ancien
```

Il accepte un CSV avec des colonnes `Open/High/Low/Close/Volume` ou un export
JSON brut de `/fapi/v1/klines`. Deux choix volontairement pessimistes :

- si une bougie touche le stop **et** l'objectif, le trade est compte perdant :
  on ignore l'ordre reel des touches, et surestimer ses gains est la pire erreur
  d'un backtest ;
- `--fee-pct` (0.08 % par defaut, soit taker Binance des deux cotes) est converti
  en multiples de R **trade par trade**, car un stop serre encaisse
  proportionnellement beaucoup plus de frais qu'un stop large.

C'est cette derniere ligne qui compte : un resultat brut positif peut devenir
franchement negatif une fois les frais integres. Voir la section suivante.

## Ce que le backtest a montre

La strategie a ete rejouee sur de vraies bougies horaires de Bitcoin
(20 111 bougies Coinbase et 17 583 bougies Bitfinex, 2017-2019), config par
defaut, frais 0.08 % aller-retour :

| Jeu de donnees | Signaux | Reussite | Brut | Net |
|---|---|---|---|---|
| BTC 1h (Coinbase) | 2 767 | 40.5 % | +0.183 R/trade | +0.108 R/trade |
| BTC 1h (Bitfinex) | 2 410 | 40.9 % | +0.196 R/trade | +0.120 R/trade |
| EURUSD 1h | 716 | 36.6 % | +0.066 R/trade | **-0.368 R/trade** |

Le seuil d'equilibre d'un ratio 1:2 est 33.3 %. Ce qui separe le crypto du
forex ici n'est pas la qualite des signaux mais **la largeur du stop** : l'ATR
de BTC donne des stops a 1.64 % du prix, ceux d'EURUSD a 0.20 %. A frais egaux,
les seconds encaissent 0.43 R par trade contre 0.075 R pour les premiers.

Balayage de parametres sur BTC 1h, meme jeu de 20 111 bougies :

| Score min | ATR x | Ratio | Signaux | Reussite | Stop moyen | Net R/trade |
|---|---|---|---|---|---|---|
| 2 | 1.5 | 1:2 | 2 767 | 40.5 % | 1.64 % | +0.108 |
| 2 | 1.5 | 1:3 | 2 767 | 35.5 % | 1.64 % | +0.211 |
| 2 | 3.0 | 1:2 | 2 767 | 46.6 % | 3.28 % | +0.174 |
| **2** | **3.0** | **1:3** | **2 767** | **44.7 %** | **3.28 %** | **+0.248** |
| 2 | 5.0 | 1:2 | 2 767 | 48.2 % | 5.46 % | +0.140 |
| 2 | 5.0 | 1:3 | 2 767 | 47.7 % | 5.46 % | +0.182 |
| 3 | 3.0 | 1:3 | 201 | 39.3 % | 3.12 % | +0.060 |

Trois enseignements :

- **la surface est large, pas un pic** : les six configs testees a `MIN_SCORE=2`
  sont toutes positives, ce qui rend le resultat moins suspect qu'un optimum
  isole ;
- **monter `MIN_SCORE` a 3 degrade tout** (+0.060 contre +0.248) en divisant le
  nombre de trades par 14 : exiger trois regles concordantes elimine surtout des
  bons signaux ;
- le funding d'un perpetuel est negligeable a cette echelle : la detention
  moyenne est de 27 h, soit environ 0.034 % de funding, c'est-a-dire 0.010 R.
  Le net de la meilleure config passe de +0.248 a +0.238 R.

**Ces chiffres ne sont pas ceux de Binance Futures.** Il s'agit de BTC-USD au
comptant sur Coinbase et Bitfinex, sur une seule periode (2017-2019, bulle puis
marche baissier). Le slippage n'est pas modelise au-dela des frais, alors qu'un
`STOP_MARKET` crypto glisse reellement. Rejouez vos propres donnees avant toute
conclusion :

```bash
curl -s "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=1500" > btc.json
python tools/backtest.py btc.json --symbol BTCUSDT --interval 1h
```

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

92 tests, sans acces reseau : le RSI est verifie contre une table de reference
publiee, le scanner tourne sur un faux client, les regles de dimensionnement
sont testees jusqu'aux cas de refus et le simulateur de trades est verifie sur
des bougies construites a la main. Un test verifie aussi que le chemin rapide
du backtest (indicateurs precalcules une fois) donne des resultats rigoureusement
identiques au recalcul sur chaque fenetre.

## Avertissement

Le trading de futures avec levier peut entrainer la perte totale du capital.
Ce bot est un outil d'aide a la decision fourni sans garantie ; les signaux ne
sont pas des conseils d'investissement. Testez longuement en mode papier avant
d'envisager le moindre ordre reel.

---

*Ce depot etait a l'origine mon premier programme "hello world" sur GitHub.*
