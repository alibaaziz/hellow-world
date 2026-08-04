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
python main.py --rank 20                    # classement des 20 paires les plus actives
python main.py -v                           # logs detailles (requetes HTTP)
```

Les donnees de marche viennent d'endpoints **publics** : aucune cle API n'est
necessaire pour scanner.

## Ce que le bot detecte

Trois regles peuvent **declencher** un signal, dont deux actives par defaut :

| Regle | LONG | SHORT | Active |
|---|---|---|---|
| Croisement EMA | EMA9 passe au-dessus de EMA21 | EMA9 passe sous EMA21 | oui |
| Cassure Bollinger | cloture au-dessus de la bande haute | cloture sous la bande basse | oui |
| Retournement RSI | RSI sort de la survente (< 30) | RSI sort du surachat (> 70) | non |

Une quatrieme regle **confirme** sans jamais declencher seule : la position du
prix par rapport a l'EMA50 (filtre de tendance).

Le **sens** d'un signal vient uniquement des declencheurs ; en cas d'egalite le
bot s'abstient. Le filtre de tendance ajuste ensuite le **score** : +1 s'il va
dans le meme sens, -1 sinon. Il ne vote jamais comme un camp a part entiere,
sinon il annulerait tout declencheur a contre-tendance. Seuls les scores
`>= MIN_SCORE` (2 par defaut) sont alertes.

### Pourquoi le retournement RSI est desactive

La regle etait silencieusement morte : elle n'a produit que 7 signaux sur 20 000
bougies BTC horaires. La cause n'etait pas son seuil — le RSI franchit 30 a la
hausse 297 fois — mais l'agregation. Un rebond de survente se produit **par
construction** en tendance baissiere : le declencheur votait LONG, le filtre de
tendance SHORT, egalite, signal jete. Sur 297 franchissements, 4 seulement
(1 %) se produisent au-dessus de l'EMA50.

Corrige (le filtre de tendance ne s'applique plus aux setups de retour a la
moyenne), la regle produit 653 signaux. Mais ces trades **perdent 0.137 R en
moyenne** quand les trades de tendance en gagnent 0.246 — resultat confirme sur
un second jeu de donnees. Le code et le reglage sont conserves, le comportement
par defaut non : `RSI_REVERSAL_ENABLED=true` pour la reactiver.

Points importants :

- seules les bougies **fermees** sont analysees ; la bougie en cours est ecartee
  pour eviter les signaux qui disparaissent ;
- chaque signal porte un stop base sur l'ATR (`ATR_STOP_MULTIPLIER`) et un
  objectif derive de `RISK_REWARD` ;
- `COOLDOWN_MINUTES` empeche de realerter sur le meme symbole/sens.

## Classement des paires

Le score d'un signal est un entier de 1 a 4, et il vaut 2 dans **93 %** des cas
(mesure sur 20 000 bougies BTC) : il ne classe donc quasiment rien. Si vingt
paires declenchent en meme temps, dix-neuf sont a egalite.

`--rank` ajoute un classement **continu** de 0 a 100, calcule en comparant les
paires entre elles :

```bash
python main.py --once --rank        # top 10
python main.py --rank 20            # boucle continue, top 20 a chaque cycle
```

```
PAIRE        SCORE     VOL   VARIAT    ATR       OI  FUNDING  SETUP
--------------------------------------------------------------------------
PUMPUSDT      98.8  x14.49  +17.65%  x1.91   +72.0%  +0.250%  LONG
CALMEUSDT     41.9  x 1.00   +2.14%  x1.05    +0.8%  +0.001%  -
BTCUSDT       28.1  x 1.00   +2.05%  x1.03    +0.8%  +0.001%  -
```

Sept composantes, ponderees par `Config.ranking_weights` :

| Composante | Poids | Mesure |
|---|---|---|
| `volume` | 0.30 | volume de la derniere bougie / moyenne des 20 precedentes |
| `momentum` | 0.20 | variation absolue du prix sur la fenetre |
| `open_interest` | 0.15 | variation d'Open Interest sur `OI_LOOKBACK` periodes |
| `volatilite` | 0.15 | ATR courant / ATR moyen |
| `funding` | 0.10 | taux de funding absolu, en points de base |
| `extreme` | 0.05 | proximite d'un extreme du range recent |
| `setup` | 0.05 | une regle technique vient de se declencher |

Open Interest et funding sont classes sur leur **valeur absolue** : un
debouclage massif de positions est aussi remarquable qu'un afflux d'argent
frais, et un funding tres negatif signale autant qu'un tres positif.

Chaque composante est convertie en **rang centile sur l'univers scanne**, pas
comparee a un seuil absolu. C'est ce qui rend le score comparable d'une paire a
l'autre : un volume a 8 fois sa moyenne ne veut pas dire la meme chose sur
BTCUSDT que sur un altcoin, alors que « premiere paire de l'univers en anomalie
de volume » a le meme sens partout.

### Le cout des donnees Futures

Ces deux mesures ne coutent pas du tout la meme chose, d'ou des defauts
differents. Compte des requetes mesure sur un univers de 5 paires :

| Configuration | Requetes par cycle |
|---|---|
| sans funding ni OI | 8 |
| `FUNDING_ENABLED=true` | 9 |
| `+ OPEN_INTEREST_ENABLED=true` | 14 |

`/fapi/v1/premiumIndex` renvoie l'univers entier en **une** requete : le funding
est donc actif par defaut, son cout ne depend pas du nombre de paires.
`/futures/data/openInterestHist` est en revanche **par symbole** : sur 300
paires cela ajoute 300 requetes a chaque cycle, avec un vrai risque de blocage
temporaire de l'IP. Il est desactive par defaut, et le bot previent au demarrage
si vous l'activez sur plus de 50 paires. A utiliser avec `--top`.

Si une mesure est indisponible (desactivee, panne d'API, symbole sans
historique), sa composante est **retiree de la ponderation** et les poids se
renormalisent sur celles qui restent — le score n'est pas dilue. Une paire
isolee sans donnee recoit le rang median : ne pas savoir ne doit ni l'avantager
ni la penaliser.

A noter : le classement n'est pas soumis au `COOLDOWN_MINUTES`. Les alertes ne
se repetent pas, mais le classement doit refleter l'etat du marche a chaque
cycle.

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
  core/ranking.py            classement continu des paires (rangs centiles)
  core/scanner.py            univers, boucle de scan, cooldown, dispatch
  notify/                    console, Telegram
  execution/                 dimensionnement, routeur papier, routeur reel
main.py                      CLI
```

Ajouter une regle : ecrire une fonction `(Snapshot, Config) -> (Side, str) | None`
dans `core/signals.py`, l'envelopper dans un `Rule(...)` et la retourner depuis
`active_rules()` — ou l'ajouter a `CONFIRM_RULES` s'il s'agit d'un filtre.

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

136 tests, sans acces reseau : le RSI est verifie contre une table de reference
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
