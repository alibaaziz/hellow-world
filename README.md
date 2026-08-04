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

La strategie a ete rejouee sur de vraies donnees OHLCV (EURUSD horaire,
5 000 bougies ; GOOG journalier, 2 148 bougies), avec la config par defaut :

| Jeu de donnees | Signaux | Reussite | Brut | Net (frais 0.08 %) |
|---|---|---|---|---|
| EURUSD 1h | 716 | 36.6 % | +0.066 R/trade | **-0.368 R/trade** |
| GOOG 1j | 296 | 36.5 % | +0.088 R/trade | +0.064 R/trade |

Le taux de reussite depasse a peine le seuil d'equilibre du ratio 1:2 (33.3 %),
donc l'avantage brut est mince. Sur EURUSD les stops ATR font 0.20 % du prix :
les frais coutent alors 0.43 R par trade et retournent completement le resultat.
Sur GOOG les stops font 3.90 %, les frais deviennent negligeables.

**Conclusion pratique** : cette strategie n'est pas exploitable sur des stops
serres. Elargir le stop (`ATR_STOP_MULTIPLIER`) ou monter en unite de temps
reduit la part des frais. A verifier sur vos propres donnees Binance avant
d'envisager le moindre ordre reel.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

88 tests, sans acces reseau : le RSI est verifie contre une table de reference
publiee, le scanner tourne sur un faux client, les regles de dimensionnement
sont testees jusqu'aux cas de refus et le simulateur de trades est verifie sur
des bougies construites a la main.

## Avertissement

Le trading de futures avec levier peut entrainer la perte totale du capital.
Ce bot est un outil d'aide a la decision fourni sans garantie ; les signaux ne
sont pas des conseils d'investissement. Testez longuement en mode papier avant
d'envisager le moindre ordre reel.

---

*Ce depot etait a l'origine mon premier programme "hello world" sur GitHub.*
