# Momentum Detection Engine

> **Wat de engine doet:** uitzonderlijke **actuele** activiteit meten en
> uitleggen.
> **Wat hij niet doet:** koersen voorspellen, koopadvies geven of een
> rendement beloven. Een MOMENTUM-signaal zegt alleen dat er nu meetbaar veel
> meer gebeurt dan in het venster ervoor. Elk signaal draagt die disclaimer.

Code: `src/momentum/` (indicators, flow, metrics, thresholds, engine). Tests: `tests/unit/momentum/`.

## 1. Opgeslagen historie

| Wat | Tabel | Bron |
|---|---|---|
| Prijs, market cap/FDV, liquiditeit, volume, buys/sells (rollende 5m/1u/24u-totalen) | `market_snapshots` | DexScreener, elke 15 s tot 5 min (tier) |
| Holders (+ top-10) | `holder_snapshots` | Solana RPC, elke 60 s tot 30 min |
| Losse trades (wallet, kant, USD) | `trades` | ❌ trade stream nog niet gekoppeld (fase 3) |
| Engine-output bij een alert | `momentum_signals` | de engine zelf |

## 2. Waar de flow-metrics vandaan komen

Volume, buys, sells, transacties en unieke wallets per venster komen per
venster uit **één** bron. Zo wordt nooit een trade-telling met een
provider-telling vergeleken.

| Prioriteit | Bron | Wanneer | Wat je krijgt |
|---|---|---|---|
| 1 | `trades` | De trade-feed dekt het huidige én het vorige venster | Alles, inclusief unieke kopers en verkopers, uitsluiting van extreme trades en wash-trading-metingen |
| 2 | `provider_h24_delta` | Het token is jonger dan 24 u, met snapshots rond beide venstergrenzen | Volume, buys, sells en transacties **exact**: het verschil van de rollende 24u-totalen. Bij zo'n jong token is nog niets uit het 24u-venster gevallen. De werkelijke meetspanne wordt naar de vensterlengte geschaald en in de output vermeld. |
| 3 | `provider_rolling` | Alleen 5m en 1u | Het rollende venster van de provider zelf |

Zonder trade-data zijn unieke kopers/verkopers en wash-trading **niet gemeten**.
De engine zegt dat in `warnings`, verlaagt de `confidence` en blokkeert
standaard niet. Wil je wel blokkeren, zet dan `MOMENTUM_REQUIRE_TRADE_DATA=true`.

## 3. Vensters en indicatoren

**Vensters:** `1m`, `5m`, `15m`, `30m`, `1h`. Per venster wordt het huidige
venster `(nu − W, nu]` vergeleken met het vorige, niet-overlappende venster
`(nu − 2W, nu − W]`. Voor acceleratie komt daar het venster daarvoor bij.

**Snapshottolerantie:** een snapshot geldt als "op de grens" binnen
max(15 s, 20% van W). Bij holders is dat max(45 s, 30% van W). Ligt er geen
snapshot binnen die tolerantie, dan is de waarde **onbekend (null), nooit 0**.

| Indicator | Definitie |
|---|---|
| Volumeverandering | `(V_nu − V_vorig) / max(V_vorig, $500) × 100` |
| Transactieverandering | idem met transacties, ondergrens 5 |
| Kopersgroei | idem met unieke kopers (alleen met trade-data), ondergrens 3 |
| Holdergroei | idem met holders, ondergrens 10 |
| Market-cap-verandering | idem. Market cap wordt nooit met FDV vergeleken; is de basis anders, dan is het resultaat onbekend. |
| Liquiditeitsverandering | idem, ondergrens $1 000 |
| Buy/sell-ratio | `buys / max(sells, 1)`; plus `buyShare = buys / (buys + sells)` |
| Volume-/transactie-acceleratie | `((nu − vorig) − (vorig − daarvoor)) / max(vorig, ondergrens) × 100`, in procentpunten. Positief = de groei versnelt. |

De ondergrenzen voorkomen onzinpercentages zoals "$3 → $4 000 = +133 233%".

## 4. Regels en score

Regels worden gescoord op het **primaire venster** (`MOMENTUM_PRIMARY_WINDOW`,
standaard 5m). Alle vensters staan wel in `metrics`.

| Regel | Waarde | Drempel (env) | Volle sterkte | Gewicht |
|---|---|---|---|---|
| `volume_spike` | volumeverandering % (na uitsluiting van extreme trades) | `VOLUME_SPIKE_THRESHOLD` = 300 | 2× drempel | 20 |
| `tx_spike` | transactieverandering % | `TX_SPIKE_THRESHOLD` = 200 | 2× | 15 |
| `buyer_growth` | kopersgroei % | `BUYER_GROWTH_THRESHOLD` = 100 | 2× | 15 |
| `holder_growth` | holdergroei % | `HOLDER_GROWTH_THRESHOLD` = 20 | 2× | 10 |
| `liquidity_growth` | liquiditeitsverandering % | `LIQUIDITY_GROWTH_THRESHOLD` = 20 | 2× | 10 |
| `market_cap_change` | market-cap-verandering % | `MARKET_CAP_CHANGE_THRESHOLD` = 20 | 2× | 10 |
| `buy_pressure` | buyShare | `BUY_SHARE_THRESHOLD` = 0,6 | 0,8 | 10 |
| `volume_acceleration` | pp | `VOLUME_ACCELERATION_THRESHOLD` = 100 | 2× | 5 |
| `tx_acceleration` | pp | `TX_ACCELERATION_THRESHOLD` = 100 | 2× | 5 |

```
sterkte = clamp((waarde − drempel) / (volle sterkte − drempel), 0, 1)
punten  = getriggerd ? gewicht × (0,5 + 0,5 × sterkte) : 0
score   = clamp(Σ punten − Σ aftrek, 0, 100)
```

Een getriggerde regel is dus tussen de helft en het geheel van zijn gewicht
waard. De output bevat per regel de waarde, drempel, volle sterkte, gewicht,
sterkte en punten, zodat je de score met de hand kunt narekenen.

**Aftrek** (zichtbaar in `penalties` en `warnings`):
- liquiditeit daalt minstens `LIQUIDITY_DROP_THRESHOLD`% in het venster: −20;
- market cap daalt minstens `MARKET_CAP_CHANGE_THRESHOLD`%: −10.

**Confidence** gaat over datadekking, niet over waarschijnlijkheid:
`(gewicht van regels met data / totaal gewicht) × (0,7 + 0,3 × geëvalueerde filters / alle filters)`.

**Signaaltype:**
- `NO_SIGNAL`: score < `MOMENTUM_MIN_SCORE` (60), of minder dan `MOMENTUM_MIN_TRIGGERED_RULES` (3) regels getriggerd, of confidence < `MOMENTUM_MIN_CONFIDENCE` (0,7).
- `FILTERED`: het zou een signaal zijn, maar een blokkerend filter faalt.
- `MOMENTUM`: alles in orde.

## 5. Filters tegen false positives

| Filter | Env | Zonder data |
|---|---|---|
| Liquiditeitsvloer | `MIN_LIQUIDITY` = 20 000 | blokkeert |
| Minimum holders | `MIN_HOLDERS` = 50 | blokkeert |
| Minimum absoluut volume in het venster | `MIN_WINDOW_VOLUME_USD` = 5 000 | blokkeert |
| Minimum transacties in het venster | `MIN_WINDOW_TRANSACTIONS` = 30 | blokkeert |
| Minimum unieke kopers | `MIN_UNIQUE_BUYERS` = 15 | niet gemeten; blokkeert alleen met `MOMENTUM_REQUIRE_TRADE_DATA=true` |
| Wash trading: top-3-wallets > `WASH_MAX_TOP_WALLETS_SHARE` (0,5) van het volume, óf heen-en-terug-volume > `WASH_MAX_ROUND_TRIP_SHARE` (0,3), óf meer dan `WASH_MAX_TX_PER_WALLET` (5) transacties per wallet | zie links | idem |

**Extreme losse trades** tellen niet mee voor het volume. Een trade geldt als
extreem als hij groter is dan `SINGLE_TRADE_MAX_SHARE` (0,4) van het
venstervolume **en** groter dan `SINGLE_TRADE_MEDIAN_MULTIPLE` (10) × de
mediane trade. Het ruwe volume en de uitgesloten trades staan in `metrics` en
`warnings`. Eén walvis kan zo geen volume-spike veroorzaken, en twee gelijke
trades worden niet ten onrechte weggefilterd.

## 6. Drempels per chain en tokentype

Volgorde, waarbij de laatste laag wint: ingebouwde standaard → environment
variables → `overrides["<chain>"]` → `overrides["<chain>:<tokenType>"]`.
Het tokentype is de DEX van de grootste pool, bijvoorbeeld `raydium` of
`pumpswap`.

```bash
MOMENTUM_THRESHOLD_OVERRIDES='{"solana":{"minLiquidityUsd":30000},"solana:pumpswap":{"volumeSpikePct":500,"weights":{"holder_growth":20}}}'
```

- Overrides worden bij het starten gevalideerd: onbekende velden, ongeldige waarden en inconsistente combinaties stoppen de bot met een duidelijke melding.
- Gewichten worden per regel samengevoegd.
- Welke lagen zijn toegepast, staat in de output (`profile`).

## 7. Output (voorbeeld)

Dit voorbeeld is door de engine berekend op het **synthetische**
testscenario uit `tests/unit/momentum/engine.test.ts`. Het zijn geen echte
marktdata.

```
Momentum Score: 66 (MOMENTUM, confidence 1)
Reasons:
* volume +428% (5m: $52.8k vs $10.0k)
* transactions +267% (5m: 165 vs 45)
* holder growth +37% (5m: 100 → 137)
* unique buyers +111% (5m: 74 vs 35)
* buy share 73% (5m: 120 buys / 45 sells)
* liquidity +24% (5m: $50.0k → $62.0k)
* volume acceleration +408 pp (5m)
* transaction acceleration +233 pp (5m)
Meting van uitzonderlijke actuele activiteit. Geen voorspelling, geen koopadvies en geen gegarandeerd rendement.
```

Opbouw: 14,3 (volume) + 10 (tx) + 9,3 (holders) + 8,4 (kopers) + 8,2 (koopdruk) + 6 (liquiditeit) + 5 + 5 (acceleraties) = **66,2**.
De market cap steeg hier +15%. Dat is onder de drempel van 20% en telt dus niet mee.

Het JSON-object heeft de velden:
- `token`, `timestamp`, `signalType`;
- `metrics` (per venster);
- `triggeredRules`, `rules`, `filters`, `penalties`;
- `score`, `confidence`, `reasons`, `warnings`, `disclaimer`;
- `profile`, `engineVersion`, `primaryWindow`.
